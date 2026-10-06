#!/usr/bin/env python3
"""
Fasalrin — approve SUBMITTED loan applications with the Branch Head login.

FLOW (recorded from a live walkthrough on 2026-10-02)
  Dashboard -> "View Details" -> /loan-application-list
  filters: FY / Application Status = SUBMITTED / Branch -> PROCEED
  table row (Status = Submitted) -> REVIEW -> preview page -> APPROVE
  -> "Are you sure you want to approve this loan application?" -> CONFIRM
  -> "Loan application <no> approved successfully." -> OK -> back to the list

Approves EVERY application the list shows as Submitted for the branch (also ones entered by hand),
without comparing it to the CSV. Approving cannot be undone on the portal.

RECORDS (the files the entry script maintains)
  * <input>_approvals.csv : append-only, one line per approval / failure (fsync'd before the CSV).
  * <input>.csv           : columns "Approval" + "Approved On" on the row with that account
                            (Status stays COMPLETED). At start the approvals file is read first and
                            fills in anything the CSV is missing.
  Resume is natural: approved applications are no longer listed as Submitted.

USAGE
  python fasalrin_verify.py 310615REG3009.csv            # asks for "yes" before approving
  python fasalrin_verify.py 310615REG3009.csv --limit 5  # approve at most 5 this run
  NO_PROMPT=1 ... --yes                                  # unattended (no question)
You log in yourself with the BRANCH HEAD login (mobile + password + captcha).
"""

from __future__ import annotations

import argparse
import csv
import os
import re
import time

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

import fasalrin_regular as f
from fasalrin_regular import BTN, StuckError, wait_for, modal_text, shot

PROFILE_DIR = ".pw_profile_head"     # own browser profile: can run next to the entry script
COL_APPROVAL, COL_APPROVED_ON = "Approval", "Approved On"
APPROVALS_HEADER = ["Time", f.COL_ACCT, f.COL_APPNO, COL_APPROVAL, f.COL_DETAIL]
MAX_ROW_FAILS = 2                    # an application that fails this often is skipped for this run
APPROVALS_CSV = ""                   # set at start: <input>_approvals.csv


# ----------------------------------------------------------------------------
# approvals file + CSV columns
# ----------------------------------------------------------------------------
def log_approval(acct, app_no, result, detail):
    new = not os.path.exists(APPROVALS_CSV)
    with open(APPROVALS_CSV, "a", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        if new:
            w.writerow(APPROVALS_HEADER)
        w.writerow([time.strftime("%Y-%m-%d %H:%M:%S"), acct, app_no, result, detail])
        fh.flush()
        os.fsync(fh.fileno())


def load_approvals() -> dict:
    """account -> latest (time, app no, result)."""
    last = {}
    if os.path.exists(APPROVALS_CSV):
        with open(APPROVALS_CSV, newline="", encoding="utf-8-sig") as fh:
            for e in csv.DictReader(fh):
                if (e.get(f.COL_ACCT) or "").strip():
                    last[e[f.COL_ACCT].strip()] = (e["Time"], e.get(f.COL_APPNO, ""), e.get(COL_APPROVAL, ""))
    return last


class Sheet:
    """The input CSV (same file the entry script keeps) with the approval columns."""

    def __init__(self):
        self.header, self.rows, _ = f.load_rows()
        for col in (COL_APPROVAL, COL_APPROVED_ON):
            if col not in self.header:
                self.header.append(col)
        for r in self.rows[1:]:
            while len(r) < len(self.header):
                r.append("")
        ix = {c: i for i, c in enumerate(self.header)}
        self.a, self.no, self.ap, self.on = ix[f.COL_ACCT], ix[f.COL_APPNO], ix[COL_APPROVAL], ix[COL_APPROVED_ON]
        self.by_acct = {r[self.a].strip(): r for r in self.rows[1:] if r[self.a].strip()}

    def set(self, acct, app_no, result, when) -> bool:
        r = self.by_acct.get(acct)
        if r is None:
            return False
        r[self.ap], r[self.on] = result, when
        if app_no and not r[self.no].strip():
            r[self.no] = app_no
        return True

    def save(self):
        # a branch work list (branches/<SOL>/<SOL>.csv) belongs to the entry script, which may be running
        # at the same time: approvals there live only in <SOL>_approvals.csv (the panel reads that file)
        p = f.Path(f.OUTPUT_CSV)
        if p.stem == p.parent.name:
            return
        f.save_rows(self.header, self.rows)


def record(sheet, acct, app_no, result, detail):
    """approvals file first (fsync'd), then the CSV."""
    log_approval(acct, app_no, result, detail)
    if sheet.set(acct, app_no, result, time.strftime("%Y-%m-%d %H:%M")):
        sheet.save()


# ----------------------------------------------------------------------------
# portal: list page + one approval
# ----------------------------------------------------------------------------
def user_category(page) -> str:
    try:
        m = re.search(r"User Category\s*:\s*([^\n]+)", page.evaluate("() => document.body.innerText"))
        return m.group(1).strip() if m else ""
    except Exception:
        return ""


def _select(page, name, label):
    sel = page.locator(f'select[name="{name}"]').first
    sel.wait_for()
    for _ in range(5):
        sel.select_option(label=label)
        page.wait_for_timeout(300)
        if sel.evaluate("s => (s.options[s.selectedIndex]||{}).text.trim()") == label:
            return
    raise StuckError(f"could not select {label!r} in {name}")


def list_rows(page) -> list[dict]:
    """Rows of the application table: [{app, acct, status}] (empty list = no rows)."""
    return page.evaluate(r"""() => {
        const t = [...document.querySelectorAll('table')].find(t => /Loan Application No/i.test(t.innerText));
        if (!t) return [];
        const head = [...t.querySelectorAll('thead th')].map(th => th.innerText.trim());
        const col = re => head.findIndex(h => re.test(h));
        const ia = col(/Loan Application No/i), ic = col(/Account No/i), is = col(/^Status/i);
        return [...t.querySelectorAll('tbody tr')].map(tr => {
            const c = [...tr.children].map(td => td.innerText.trim());
            return {app: c[ia] || '', acct: c[ic] || '', status: c[is] || ''};
        }).filter(r => /^\d+$/.test(r.app));
    }""")


def open_list(page):
    """Dashboard -> View Details -> list page -> FY / SUBMITTED / Branch -> PROCEED."""
    f.click_side_nav(page, "/dashboard")
    page.wait_for_timeout(1000)
    if "/loan-application-list" not in page.url:
        btn = page.locator("button:visible", has_text=re.compile(r"View Details", re.I))
        wait_for(page, lambda: btn.count(), f.STUCK_TIMEOUT_S, "dashboard View Details")
        btn.first.click()
        wait_for(page, lambda: "/loan-application-list" in page.url, f.STUCK_TIMEOUT_S, "application list page")
    _select(page, "financialYear", f.FIN_YEAR)
    _select(page, "applicationStatus", "SUBMITTED")
    if page.locator('select[name="branchOrPacs"]').count():
        _select(page, "branchOrPacs", "Branch")
    page.locator("button:visible", has_text=BTN("PROCEED")).first.click()
    page.wait_for_timeout(1500)
    # table appears, or the page settles with none (nothing pending)
    try:
        wait_for(page, lambda: list_rows(page), 15, "application table")
    except StuckError:
        pass


def approve_one(page, row) -> str:
    """REVIEW -> APPROVE -> CONFIRM -> success. Returns the approved application number."""
    app, acct = row["app"], row["acct"]
    tr = page.locator("table tbody tr", has_text=app).first
    tr.locator("button", has_text=BTN("REVIEW")).first.click()
    wait_for(page, lambda: "/loan-application-preview" in page.url and
             page.locator("button:visible", has_text=BTN("APPROVE")).count(), f.STUCK_TIMEOUT_S, "preview page")
    page.wait_for_timeout(500)
    body = re.sub(r"\s+", " ", page.evaluate("() => document.body.innerText"))
    if acct and acct not in body:                    # make sure REVIEW opened the row we meant
        shot(page, f"verify_mismatch_{app}")
        raise RuntimeError(f"preview does not show account {acct}")
    page.locator("button:visible", has_text=BTN("APPROVE")).first.click()

    def after_approve():
        t = modal_text(page)
        if re.search(r"approved successfully", t, re.I):
            return "done"
        if re.search(r"sure you want to approve", t, re.I):
            return "confirm"
        return None
    if wait_for(page, after_approve, f.STUCK_TIMEOUT_S, "approve confirm dialog") == "confirm":
        page.locator(".modal-content:visible button", has_text=BTN("CONFIRM")).first.click()
    txt = wait_for(page, lambda: (lambda t: t if re.search(r"approved successfully", t, re.I) else None)(modal_text(page)),
                   f.STUCK_TIMEOUT_S, "approved-successfully dialog")
    m = re.search(r"Loan application\s*(\d+)\s*approved successfully", txt, re.I)
    got = m.group(1) if m else ""
    page.locator(".modal-content:visible button", has_text=BTN("OK")).first.click()
    page.wait_for_timeout(800)
    if got and got != app:
        raise RuntimeError(f"portal approved {got}, expected {app}")
    return got or app


# ----------------------------------------------------------------------------
# main
# ----------------------------------------------------------------------------
def main(limit: int, assume_yes: bool):
    sheet = Sheet()

    # approvals file first: fill in what the CSV is missing
    done = load_approvals()
    filled = sum(sheet.set(acct, e[1], e[2], e[0][:16]) for acct, e in done.items()
                 if acct in sheet.by_acct and sheet.by_acct[acct][sheet.ap] != e[2])
    if filled:
        sheet.save()
    print(f"[approvals] {APPROVALS_CSV}: {len(done)} logged"
          + (f", {filled} filled into the CSV" if filled else ", CSV up to date"))

    # approvals made outside this script (by hand): newest saved APPROVED report from the entry script
    files = sorted(f.REPORTS_DIR.glob(f"*_{f.FIN_YEAR}_approved.zip"))
    if files:
        seen = 0
        for acct, (app_no, status) in f.read_report(files[-1]).items():
            if status.lower() == "approved" and done.get(acct, ("", "", ""))[2] != "APPROVED":
                record(sheet, acct, app_no, "APPROVED", f"in portal approved report {files[-1].name}")
                seen += 1
        print(f"[approvals] {files[-1].name}: {seen} approvals made outside this script recorded")

    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(PROFILE_DIR, headless=f.HEADLESS, slow_mo=f.SLOWMO_MS,
                                                   viewport=None, args=["--start-maximized"])
        ctx.set_default_timeout(f.STUCK_TIMEOUT_S * 1000)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(f.BASE, wait_until="domcontentloaded")
        print("\n" + "=" * 70)
        print("  LOG IN with the BRANCH HEAD login (mobile + password + captcha).")
        print("  Then don't touch the browser.")
        print("=" * 70)
        if f.NO_PROMPT:
            page.wait_for_timeout(2000)
        else:
            input("  >> press ENTER once logged in: ")
        if f.is_logged_out(page):
            f.require_login(page, "Not logged in yet")
        try:
            f.dismiss_ok_dialogs(page)                   # "Dear Banker, Important Update" alert
        except Exception:
            pass
        cat = user_category(page)
        if cat and not re.search(r"Branch Head", cat, re.I):
            print(f"\n  !! logged in as '{cat}': approving needs the BRANCH HEAD login. Stopping.")
            ctx.close()
            return

        # branch work list: the Branch Head login must be that branch
        want = f.branch_of_list(sheet.rows, {c: i for i, c in enumerate(sheet.header)})
        if want:
            got = f.logged_in_branch(page)
            if got and not f.same_branch(got, want):
                print(f"\n  !! WRONG LOGIN: the portal is logged in as branch '{got}', this work list is '{want}'."
                      f"\n     Nothing approved. Log in with the {want} Branch Head login and start again.")
                ctx.close()
                return
            print(f"[login] branch check: portal '{got or '?'}' = work list '{want}'")

        open_list(page)
        first = list_rows(page)
        pending = [r for r in first if r["status"].lower() == "submitted"]
        print(f"[list] {len(pending)} applications Submitted on the first page"
              + (" (more pages follow as these get approved)" if len(first) >= 50 else ""))
        if not pending:
            print("[done] nothing to approve")
            ctx.close()
            return
        if not assume_yes:
            print("\n  Every application listed as Submitted for the branch will be APPROVED"
                  + (f" (at most {limit} this run)" if limit else "") + ". This cannot be undone.")
            while True:
                a = input("  >> type yes to approve, no to stop: ").strip().lower()
                if a in ("yes", "no"):
                    break
            if a == "no":
                ctx.close()
                return

        approved = errors = 0
        fails: dict[str, int] = {}
        while not (limit and approved >= limit):
            if os.path.exists(f.STOP_FLAG):            # control panel: "Stop after current row"
                print("\n[stop] stop requested — stopping between applications")
                break
            rows =[r for r in list_rows(page) if r["status"].lower() == "submitted"
                    and fails.get(r["app"], 0) < MAX_ROW_FAILS]
            if not rows:
                open_list(page)                          # refresh once; maybe more pages / still loading
                rows = [r for r in list_rows(page) if r["status"].lower() == "submitted"
                        and fails.get(r["app"], 0) < MAX_ROW_FAILS]
                if not rows:
                    break
            row = rows[0]
            print(f"\r[{approved + errors + 1:>4}] {row['app']}  acct {row['acct']}  approving...   ", end="", flush=True)
            try:
                got = approve_one(page, row)
                approved += 1
                record(sheet, row["acct"], got, "APPROVED", "approved by Branch Head")
                print(f"\r[{approved + errors:>4}] {row['app']}  acct {row['acct']}  APPROVED"
                      f"{'' if row['acct'] in sheet.by_acct else '  (account not in CSV)'}"
                      f"   | approved {approved}  errors {errors}", flush=True)
                if "/loan-application-list" not in page.url or not list_rows(page):
                    open_list(page)
            except KeyboardInterrupt:
                raise
            except Exception as e:
                import traceback
                with open("errors.log", "a") as ef:
                    ef.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} VERIFY app={row['app']} url={page.url}\n")
                    ef.write(traceback.format_exc())
                errors += 1
                fails[row["app"]] = fails.get(row["app"], 0) + 1
                msg = str(e).splitlines()[0][:100]
                shot(page, f"verify_error_{row['app']}")
                print(f"\n    [exc] {type(e).__name__}: {msg}", flush=True)
                if fails[row["app"]] >= MAX_ROW_FAILS:
                    record(sheet, row["acct"], row["app"], "ERROR", msg)
                if f.is_logged_out(page):
                    f.require_login(page)
                f.reload_to_dashboard(page)
                open_list(page)

        print(f"\n[done] {approved} approved, {errors} errors.  Log: {APPROVALS_CSV}  CSV: {f.OUTPUT_CSV}")
        if not f.NO_PROMPT:
            input("  >> press ENTER to close the browser: ")
        ctx.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Approve SUBMITTED Fasalrin loan applications (Branch Head login).")
    ap.add_argument("csv", help="the entry script's CSV (approval columns are added to it)")
    ap.add_argument("--limit", type=int, default=0, help="approve at most N this run (0 = all)")
    ap.add_argument("--yes", action="store_true", help="don't ask before approving (needed with NO_PROMPT=1)")
    args = ap.parse_args()
    if not os.path.isfile(args.csv):
        ap.error(f"CSV not found: {args.csv}")
    if f.NO_PROMPT and not args.yes:
        ap.error("NO_PROMPT=1 cannot ask: pass --yes")
    f.INPUT_CSV = f.OUTPUT_CSV = args.csv
    f.REPORTS_DIR = f.Path(args.csv).resolve().parent / "reports"   # per branch: branches/<SOL>/reports
    base = os.path.splitext(args.csv)[0]
    APPROVALS_CSV = base + "_approvals.csv"
    try:
        main(args.limit, args.yes)
    except KeyboardInterrupt:
        print("\n[quit] interrupted — approvals so far are recorded; rerun to continue")
