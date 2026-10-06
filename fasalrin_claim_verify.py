#!/usr/bin/env python3
"""
Fasalrin — approve SUBMITTED IS claims with the Branch Head login.

FLOW (recorded from a live walkthrough on 2026-10-03)
  Dashboard -> "Claim Applications" tab -> View Details -> /claim-application-list
  filters: FY / Claim Type IS / Claim Status SUBMITTED / Submission Type ALL / Branch -> PROCEED
  row -> open (REVIEW/VIEW button) -> /claim-application-preview -> APPROVE (-> CONFIRM if asked)
  -> "Claim application No. <no> has been approved successfully." -> OK -> back to the list

Claims are matched by CLAIM NUMBER against branches/<SOL>/<SOL>_claims.csv (CLAIMED there). IS additional
(rollover) claims sit in the same Submitted list, often for the same account. Before APPROVE the preview
is read (as in the proven rollover verify.py) and must show: same account + claim no, Is Rollover Claim
not Yes, Applicable IS = what we filed and <= Maximum Allowed Claim, end date 31/03/2026, start date =
Disb. Date, status SUBMITTED. Any failure -> BACK, MISMATCH, never approved. Not ours -> NOT_OURS.

RECORDS: branches/<SOL>/<SOL>_claim_approvals.csv (append-only, fsync'd). Approved claims leave the
Submitted list, so a rerun continues with what is left.

USAGE
  python fasalrin_claim_verify.py branches/3115/3115.csv     # asks for "yes" before approving
  NO_PROMPT=1 ... --yes
"""

from __future__ import annotations

import argparse
import csv
import os
import re
import time

from playwright.sync_api import sync_playwright

from datetime import date

import fasalrin_regular as f
from fasalrin_regular import BTN, StuckError, wait_for, modal_text, shot, money_to_float

REPAY_DATE = date(2026, 3, 31)        # IS regular: interest cycle end on every claim

PROFILE_DIR = ".pw_profile_head"      # Branch Head profile (shared with the loan approval script)
HEADER = ["Time", f.COL_ACCT, f.COL_APPNO, "Claim No", "Result", f.COL_DETAIL]
MAX_ROW_FAILS = 2
CLAIMS_CSV = APPROVALS_CSV = ""


def log(acct, app, claim_no, result, detail):
    new = not os.path.exists(APPROVALS_CSV)
    with open(APPROVALS_CSV, "a", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        if new:
            w.writerow(HEADER)
        w.writerow([time.strftime("%Y-%m-%d %H:%M:%S"), acct, app, claim_no, result, detail])
        fh.flush()
        os.fsync(fh.fileno())


def our_claims() -> dict:
    """claim no -> {acct, is} for every claim our claim script filed (CLAIMED). Keyed by CLAIM NUMBER:
    IS regular and IS additional (rollover) claims share the same Submitted list, often for the same
    account, so the account alone is not enough."""
    out = {}
    if os.path.exists(CLAIMS_CSV):
        with open(CLAIMS_CSV, newline="", encoding="utf-8-sig") as fh:
            for e in csv.DictReader(fh):
                if e["Result"].strip() != "CLAIMED":
                    continue
                m = re.search(r"claim no (\d+)", e.get(f.COL_DETAIL) or "")
                if m:
                    out[m.group(1)] = {"acct": e[f.COL_ACCT].strip(), "is": money_to_float(e.get("IS Claimed"))}
    return out


def read_preview(page) -> dict:
    """Label/value pairs on /claim-application-preview (as in the proven rollover verify.py)."""
    for _ in range(100):                       # up to ~10 s for the values to render
        d = page.evaluate("""() => {
            const out = {};
            for (const l of document.querySelectorAll('p.dataPreviewLabel')) {
                let v = l.nextElementSibling;
                while (v && !/darkText/.test(v.className)) v = v.nextElementSibling;
                if (v) out[l.innerText.trim()] = v.innerText.trim();
            }
            const st = [...document.querySelectorAll('span.fw-bold')].map(e => e.innerText.trim())
                .find(t => /Claim Status/i.test(t));
            if (st) out['_status'] = st;
            return out;
        }""")
        if d.get("Claim No.") and d.get("Applicable IS"):
            return d
        page.wait_for_timeout(100)
    raise RuntimeError("claim preview never populated")


def _date_in(text: str):
    m = re.search(r"(\d{2})[/-](\d{2})[/-](\d{4})", text or "")
    return date(int(m.group(3)), int(m.group(2)), int(m.group(1))) if m else None


def check_claim(d: dict, row: dict, mine: dict, disb: str) -> list[str]:
    """Problems that block approval (empty = safe). IS regular rules."""
    probs = []
    if d.get("Account No.", "").strip() != row["acct"]:
        probs.append(f"preview account {d.get('Account No.')!r} != row {row['acct']}")
    if d.get("Claim No.", "").strip() != row["claim"]:
        probs.append(f"preview claim {d.get('Claim No.')!r} != row {row['claim']}")
    if mine["acct"] != row["acct"]:
        probs.append(f"our claim {row['claim']} was for account {mine['acct']}")
    if re.match(r"\s*yes", d.get("Is Rollover Claim", ""), re.I):
        probs.append("Is Rollover Claim = Yes (IS additional, not regular)")
    portal_is, max_allowed = money_to_float(d.get("Applicable IS")), money_to_float(d.get("Maximum Allowed Claim"))
    if abs(portal_is - mine["is"]) > 0.005:
        probs.append(f"Applicable IS {portal_is:.2f} != ours {mine['is']:.2f}")
    if portal_is <= 0:
        probs.append("Applicable IS is 0")
    if max_allowed > 0 and portal_is > max_allowed + 0.005:
        probs.append(f"IS {portal_is:.2f} > Maximum Allowed Claim {max_allowed:.2f}")
    end = _date_in(d.get("Interest Cycle end/Rollover Date", ""))
    if end != REPAY_DATE:
        probs.append(f"end date {d.get('Interest Cycle end/Rollover Date')!r} != {REPAY_DATE:%d/%m/%Y}")
    start = _date_in(d.get("First Loan Disbursal/Interest Cycle Start Date", ""))
    if disb and start and start != _date_in(disb):
        probs.append(f"start date {start:%d/%m/%Y} != Disb. Date {disb}")
    if not re.search(r"SUBMITTED", d.get("_status", ""), re.I):
        probs.append(f"status {d.get('_status')!r}")
    return probs


def click_back(page):
    page.locator("button:visible", has_text=BTN("BACK")).first.click()
    wait_for(page, lambda: "/claim-application-list" in page.url and page.locator("table.issCustomTable").count(),
             f.STUCK_TIMEOUT_S, "back on the claim list")
    page.wait_for_timeout(300)


def goto_next_page(page) -> bool:
    """Next numbered page tab of the list (as in verify.py). False if there is none."""
    sel = page.locator("ul.pagination-container li.pagination-item.selected")
    try:
        cur = int(sel.first.inner_text().strip()) if sel.count() else 1
    except ValueError:
        cur = 1
    tabs = page.locator("ul.pagination-container li.pagination-item:not(.pagination-icon)")
    for i in range(tabs.count()):
        try:
            n = int(tabs.nth(i).inner_text().strip())
        except ValueError:
            continue
        if n == cur + 1:
            tabs.nth(i).click()
            page.wait_for_timeout(1500)
            page.locator("table.issCustomTable").first.wait_for(timeout=20000)
            return True
    return False


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
    return page.evaluate(r"""() => {
        const t = [...document.querySelectorAll('table')].find(t => /Claim No/i.test(t.innerText));
        if (!t) return [];
        const head = [...t.querySelectorAll('thead th')].map(th => th.innerText.trim());
        const col = re => head.findIndex(h => re.test(h));
        const ia = col(/^Account No/i), ip = col(/Loan Application No/i), ic = col(/^Claim No/i),
              is = col(/^Status/i), im = col(/Applicable IS/i);
        return [...t.querySelectorAll('tbody tr')].map(tr => {
            const c = [...tr.children].map(td => td.innerText.trim());
            return {acct: c[ia] || '', app: c[ip] || '', claim: c[ic] || '', status: c[is] || '', amount: c[im] || ''};
        }).filter(r => /^\d+$/.test(r.acct));
    }""")


def open_list(page):
    """Dashboard -> Claim Applications tab -> View Details -> filters -> PROCEED."""
    f.click_side_nav(page, "/dashboard")
    page.wait_for_timeout(1000)
    if "/claim-application-list" not in page.url:
        # Dashboard -> 'Claim Applications' tab -> card 'pending for Approval' -> View Details (confirmed route)
        tab = page.locator(".nav-link", has_text=re.compile(r"^\s*Claim Applications\s*$", re.I))
        wait_for(page, lambda: tab.count(), 15, "Claim Applications tab")
        tab.first.click()
        page.wait_for_timeout(800)
        btn = page.locator(".dashboard-box", has_text=re.compile("pending for Approval", re.I)) \
                  .locator("button", has_text=re.compile("View Details", re.I))
        wait_for(page, lambda: btn.count(), f.STUCK_TIMEOUT_S, "'pending for Approval' View Details")
        btn.first.click()
        wait_for(page, lambda: "/claim-application-list" in page.url, f.STUCK_TIMEOUT_S, "claim list page")
    _select(page, "financialYear", f.FIN_YEAR)
    _select(page, "claimType", "IS")
    _select(page, "claimStatus", "SUBMITTED")
    if page.locator('select[name="priSubmissionTypeID"]').count():
        _select(page, "priSubmissionTypeID", "ALL")
    if page.locator('select[name="branchOrPacs"]').count():
        _select(page, "branchOrPacs", "Branch")
    page.locator("button:visible", has_text=BTN("PROCEED")).first.click()
    page.wait_for_timeout(1000)
    wait_for(page, lambda: (re.search(r"Total Count", f.body_text(page)) and page.locator("table.issCustomTable").count())
             or re.search(r"No record\(s\) found", f.body_text(page)), 90, "claim table")
    page.wait_for_timeout(300)


def review_and_approve(page, row, mine, disb, stage):
    """REVIEW -> read preview -> checks -> APPROVE (or BACK). Returns (result, detail)."""
    tr = page.locator("table tbody tr", has_text=row["claim"]).first
    tr.locator("button", has_text=BTN("REVIEW")).first.click()
    wait_for(page, lambda: "/claim-application-preview" in page.url and
             page.locator("button:visible", has_text=BTN("APPROVE")).count(), f.STUCK_TIMEOUT_S, "claim preview")
    d = read_preview(page)
    probs = check_claim(d, row, mine, disb)
    if probs:
        shot(page, f"claim_mismatch_{row['acct']}")
        click_back(page)
        return "MISMATCH", "; ".join(probs)
    stage["s"] = "approving"
    no = approve_one(page, row)
    return "APPROVED", f"claim {no} IS {d.get('Applicable IS')}"


def approve_one(page, row) -> str:
    page.locator("button:visible", has_text=BTN("APPROVE")).first.click()
    # APPROVE -> CONFIRM -> "Claim application No. <no> has been approved successfully." -> OK (confirmed route)
    confirm = page.locator(".modal-content:visible button", has_text=BTN("CONFIRM"))

    def after():
        t = modal_text(page)
        if re.search(r"approved successfully", t, re.I):
            return t
        if confirm.count():
            confirm.first.click()
        return None
    txt = wait_for(page, after, f.STUCK_TIMEOUT_S, "claim approved dialog")
    m = re.search(r"Claim application No\.?\s*(\d+)", txt, re.I)
    page.locator(".modal-content:visible button", has_text=BTN("OK")).first.click()
    page.wait_for_timeout(800)
    return m.group(1) if m else row["claim"]


def main(limit: int, assume_yes: bool):
    f.INPUT_CSV = f.OUTPUT_CSV
    header, rows, idx = f.load_rows()
    ours = our_claims()
    disb_of = {r[idx[f.COL_ACCT]].strip(): r[idx[f.COL_DISB]].strip() for r in rows[1:]} if f.COL_DISB in idx else {}
    print(f"[claims] {CLAIMS_CSV}: {len(ours)} claims filed by the script (only these, after checks, get approved)")

    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(PROFILE_DIR, headless=f.HEADLESS, slow_mo=f.SLOWMO_MS,
                                                   viewport=None, args=["--start-maximized"])
        ctx.set_default_timeout(f.STUCK_TIMEOUT_S * 1000)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(f.BASE, wait_until="domcontentloaded")
        print("\n" + "=" * 70)
        print("  LOG IN with the BRANCH HEAD login (mobile + password + captcha).")
        print("=" * 70)
        if f.NO_PROMPT:
            page.wait_for_timeout(2000)
        else:
            input("  >> press ENTER once logged in: ")
        if f.is_logged_out(page):
            f.require_login(page, "Not logged in yet")
        try:
            f.dismiss_ok_dialogs(page)
        except Exception:
            pass
        want = f.branch_of_list(rows, idx)
        if want:
            got = f.logged_in_branch(page)
            if got and not f.same_branch(got, want):
                print(f"\n  !! WRONG LOGIN: the portal is logged in as branch '{got}', this work list is '{want}'."
                      f"\n     Nothing approved. Log in with the {want} Branch Head login and start again.")
                ctx.close()
                return
            print(f"[login] branch check: portal '{got or '?'}' = work list '{want}'")

        open_list(page)
        first = [r for r in list_rows(page) if r["status"].lower() == "submitted"]
        print(f"[list] {len(first)} IS claims Submitted on the first page")
        if first and not assume_yes:
            print("\n  Submitted IS claims that the claim script filed will be APPROVED. This cannot be undone.")
            while (a := input("  >> type yes to approve, no to stop: ").strip().lower()) not in ("yes", "no"):
                pass
            if a == "no":
                ctx.close()
                return

        approved = errors = skipped = mismatched = 0
        fails: dict[str, int] = {}
        seen: set[str] = set()                         # claim numbers walked past this run (not ours / mismatch)
        while not (limit and approved >= limit):
            if os.path.exists(f.STOP_FLAG):
                print("\n[stop] stop requested — stopping between claims")
                break
            cand = [r for r in list_rows(page) if r["status"].lower() == "submitted" and r["claim"]
                    and r["claim"] not in seen and fails.get(r["claim"], 0) < MAX_ROW_FAILS]
            if not cand:
                if goto_next_page(page):                 # this page is all walked past -> next page
                    continue
                open_list(page)
                cand = [r for r in list_rows(page) if r["status"].lower() == "submitted" and r["claim"]
                        and r["claim"] not in seen and fails.get(r["claim"], 0) < MAX_ROW_FAILS]
                if not cand:
                    break
            row = cand[0]
            mine = ours.get(row["claim"])
            if not mine:                                 # not filed by our IS regular claim script
                seen.add(row["claim"])
                skipped += 1
                log(row["acct"], row["app"], row["claim"], "NOT_OURS",
                    "not in our IS regular claims file (filed by hand, or IS additional): not approved")
                print(f"[----] {row['claim']}  acct {row['acct']}  NOT_OURS (not filed by the IS regular claim job)")
                continue
            stage = {"s": "review"}
            print(f"\r[{approved + errors + 1:>4}] {row['claim']}  acct {row['acct']}  checking...   ", end="", flush=True)
            try:
                result, detail = review_and_approve(page, row, mine, disb_of.get(row["acct"], ""), stage)
                log(row["acct"], row["app"], row["claim"], result, detail)
                if result == "APPROVED":
                    approved += 1
                    print(f"\r[{approved + errors:>4}] {row['claim']}  acct {row['acct']}  APPROVED   | approved {approved}"
                          f"  mismatch {mismatched}  errors {errors}", flush=True)
                    if "/claim-application-list" not in page.url or not list_rows(page):
                        open_list(page)
                else:
                    seen.add(row["claim"])
                    mismatched += 1
                    print(f"\r[{approved + errors:>4}] {row['claim']}  acct {row['acct']}  MISMATCH   {detail[:120]}", flush=True)
            except KeyboardInterrupt:
                raise
            except Exception as e:
                import traceback
                with open("errors.log", "a") as ef:
                    ef.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} CLAIM-VERIFY claim={row['claim']} stage={stage['s']} url={page.url}\n")
                    ef.write(traceback.format_exc())
                errors += 1
                msg = str(e).splitlines()[0][:100]
                shot(page, f"claimverify_error_{row['acct']}")
                print(f"\n    [exc] stage={stage['s']} {type(e).__name__}: {msg}", flush=True)
                if stage["s"] == "approving":            # APPROVE clicked: never again blindly
                    seen.add(row["claim"])
                    log(row["acct"], row["app"], row["claim"], "CHECK_PORTAL", f"error after APPROVE: {msg}")
                else:
                    fails[row["claim"]] = fails.get(row["claim"], 0) + 1
                    if fails[row["claim"]] >= MAX_ROW_FAILS:
                        log(row["acct"], row["app"], row["claim"], "ERROR", msg)
                if f.is_logged_out(page):
                    f.require_login(page)
                f.reload_to_dashboard(page)
                open_list(page)

        print(f"\n[done] {approved} claims approved, {mismatched} mismatch (not approved), {skipped} not ours, "
              f"{errors} errors.  Log: {APPROVALS_CSV}")
        if not f.NO_PROMPT:
            input("  >> press ENTER to close the browser: ")
        ctx.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Approve SUBMITTED Fasalrin IS claims (Branch Head login).")
    ap.add_argument("csv", help="branch work list, e.g. branches/3115/3115.csv")
    ap.add_argument("--limit", type=int, default=0, help="approve at most N this run (0 = all)")
    ap.add_argument("--yes", action="store_true", help="don't ask before approving (needed with NO_PROMPT=1)")
    args = ap.parse_args()
    if not os.path.isfile(args.csv):
        ap.error(f"CSV not found: {args.csv}")
    if f.NO_PROMPT and not args.yes:
        ap.error("NO_PROMPT=1 cannot ask: pass --yes")
    f.INPUT_CSV = f.OUTPUT_CSV = args.csv
    base = os.path.splitext(args.csv)[0]
    CLAIMS_CSV, APPROVALS_CSV = base + "_claims.csv", base + "_claim_approvals.csv"
    try:
        main(args.limit, args.yes)
    except KeyboardInterrupt:
        print("\n[quit] interrupted — approvals so far are recorded; rerun to continue")
