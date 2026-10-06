#!/usr/bin/env python3
"""
Fasalrin — file IS (interest subvention) claims with the Branch User login.

FLOW (recorded from a live walkthrough on 2026-10-03)
  IS/PRI Claim Application (/claim-application-list): FY / Claim Type IS / Claim Status PENDING -> PROCEED
  search the account -> row (Status Pending) -> ADD -> /claim-against-loan-app:
    IS Submission Type               = PARTIAL first (it unlocks the date fields)
    First Loan Disbursal/Interest Cycle Start Date = the master's Disb. Date (first active date; a
                                       date the portal already prefilled is kept)
    Interest Cycle end/Rollover Date = 31-03-2026 (the last date)
    Max Withdrawal Amount            = the form's own Loan Sanctioned Amount (activity table)
    -> portal calculates "Maximum Allowed Claim"
    Applicable IS                    = lower of Maximum Allowed Claim and the master's Int Sub Amt
    IS Submission Type               = PARTIAL
    declaration ticked -> SAVE & CONTINUE -> "Claim application saved successfully" -> OK
  /claim-application-preview -> SUBMIT -> CONFIRM -> "Claim application No. <no> has been submitted successfully" -> OK
  (search: "Search Here" + magnifier button; IS Submission Type must be chosen first: it unlocks the dates)

Only applications the portal lists as Pending for an IS claim (= approved loans) are claimed, and only
accounts of the branch work list (Int Sub Amt comes from the master). Int Sub Amt 0/blank -> hand work.

RECORDS: branches/<SOL>/<SOL>_claims.csv (append-only, fsync'd, read first at start). The work list CSV
is not written, so this can run while loan entry runs. CLAIM_CHECK_PORTAL is saved just before SUBMIT:
a crash after that never files the claim twice (check it on the portal by hand).

USAGE
  python fasalrin_claim.py branches/3115/3115.csv          # asks for "yes" before filing
  NO_PROMPT=1 ... --yes                                     # unattended
"""

from __future__ import annotations

import argparse
import csv
import os
import re
import time
from datetime import date

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

import fasalrin_regular as f
from fasalrin_regular import BTN, StuckError, wait_for, modal_text, shot, money_to_float

PROFILE_DIR = ".pw_profile_claim"    # own browser profile: can run next to loan entry / approval
REPAY_DATE = date(2026, 3, 31)
SUBMISSION_TYPE = "PARTIAL"
COL_INTSUB = "Int Sub Amt"
CLAIMS_HEADER = ["Time", f.COL_ACCT, f.COL_APPNO, "Loan Sanctioned", "Max Allowed Claim", COL_INTSUB,
                 "IS Claimed", "Result", f.COL_DETAIL]
DONE = {"CLAIMED", "NO_INT_SUB_AMT", "CLAIM_ZERO_ALLOWED"}      # never tried again
CLAIMS_CSV = ""


# ----------------------------------------------------------------------------
# records
# ----------------------------------------------------------------------------
def log_claim(acct, app_no, sanctioned, max_allowed, intsub, claimed, result, detail):
    new = not os.path.exists(CLAIMS_CSV)
    with open(CLAIMS_CSV, "a", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        if new:
            w.writerow(CLAIMS_HEADER)
        w.writerow([time.strftime("%Y-%m-%d %H:%M:%S"), acct, app_no, sanctioned, max_allowed, intsub,
                    claimed, result, detail])
        fh.flush()
        os.fsync(fh.fileno())


def load_claims() -> dict:
    """account -> latest Result."""
    out = {}
    if os.path.exists(CLAIMS_CSV):
        with open(CLAIMS_CSV, newline="", encoding="utf-8-sig") as fh:
            for e in csv.DictReader(fh):
                out[e[f.COL_ACCT].strip()] = e["Result"].strip()
    return out


def work_list():
    """[(account, app no, Int Sub Amt as float, Disb. Date)] for the branch work list's finished applications."""
    f.INPUT_CSV = f.OUTPUT_CSV
    header, rows, idx = f.load_rows()
    out = []
    for r in rows[1:]:
        acct = r[idx[f.COL_ACCT]].strip()
        if acct and f.bucket(r[idx[f.COL_STATUS]]) == "finished":
            out.append((acct, r[idx[f.COL_APPNO]].strip(),
                        money_to_float(r[idx[COL_INTSUB]]) if COL_INTSUB in idx else 0.0,
                        r[idx[f.COL_DISB]].strip() if f.COL_DISB in idx else ""))
    return header, rows, idx, out


# ----------------------------------------------------------------------------
# portal
# ----------------------------------------------------------------------------
def _select(page, name, label):
    sel = page.locator(f'select[name="{name}"]').first
    sel.wait_for()
    for _ in range(5):
        sel.select_option(label=label)
        page.wait_for_timeout(300)
        if sel.evaluate("s => (s.options[s.selectedIndex]||{}).text.trim()") == label:
            return
    raise StuckError(f"could not select {label!r} in {name}")


def open_list(page):
    """IS/PRI Claim Application -> FY / IS / PENDING -> PROCEED."""
    f.click_side_nav(page, "/dashboard")
    page.wait_for_timeout(800)
    f.click_side_nav(page, "/claim-application-list")
    wait_for(page, lambda: "/claim-application-list" in page.url, f.STUCK_TIMEOUT_S, "claim list page")
    _select(page, "financialYear", f.FIN_YEAR)
    _select(page, "claimType", "IS")
    _select(page, "claimStatus", "PENDING")
    page.locator("button:visible", has_text=BTN("PROCEED")).first.click()
    # loaded = "Total Count" + the list table (as in the proven rollover script), or no records at all
    wait_for(page, lambda: (re.search(r"Total Count", f.body_text(page)) and _table_state(page) in ("rows", "empty"))
             or re.search(r"No record\(s\) found", f.body_text(page)), 90, "claim list to load")
    page.wait_for_timeout(300)


def _table_state(page) -> str:
    return page.evaluate(r"""() => {
        const t = [...document.querySelectorAll('table')].find(t => /Loan Application No/i.test(t.innerText));
        if (!t) return 'none';
        if (/No record/i.test(t.innerText)) return 'empty';
        return t.querySelectorAll('tbody tr').length ? 'rows' : 'none';
    }""")


def find_rows(page, acct) -> list[dict]:
    """Search the loaded list for the account; visible rows [{acct, app, status}]."""
    # proven in the rollover script: type into "Search Here", click the magnifier, then wait until the
    # table really shows THIS search (a row with the account, or "No record(s) found") - never a stale table
    box = page.locator('input[placeholder="Search Here"]').first
    box.fill("")
    box.fill(acct)
    mag = page.locator("button.btn-secondary:has(i.fa-search)")
    if mag.count():
        mag.first.click()
    else:
        box.press("Enter")
    wait_for(page, lambda: page.evaluate(r"""(a) => {
        if (document.body.innerText.includes('No record(s) found')) return true;
        const t = [...document.querySelectorAll('table')].find(t => /Loan Application No/i.test(t.innerText));
        return !!t && [...t.querySelectorAll('tbody tr')].some(r => [...r.children].some(td => td.innerText.trim() === a));
    }""", acct), 12, "search results for this account")
    page.wait_for_timeout(150)
    return page.evaluate(r"""() => {
        const t = [...document.querySelectorAll('table')].find(t => /Loan Application No/i.test(t.innerText));
        if (!t) return [];
        const head = [...t.querySelectorAll('thead th')].map(th => th.innerText.trim());
        const col = re => head.findIndex(h => re.test(h));
        const ia = col(/^Account No/i), ip = col(/Loan Application No/i), is = col(/^Status/i);
        return [...t.querySelectorAll('tbody tr')].map(tr => {
            const c = [...tr.children].map(td => td.innerText.trim());
            return {acct: c[ia] || '', app: c[ip] || '', status: c[is] || ''};
        }).filter(r => /^\d+$/.test(r.acct));
    }""")


def _table_amounts(page):
    """(loan sanctioned from the activity table, Maximum Allowed Claim) on the claim form."""
    return page.evaluate(r"""() => {
        const num = s => parseFloat(String(s || '').replace(/[^\d.]/g, '')) || 0;
        const tables = [...document.querySelectorAll('table')];
        const act = tables.find(t => /Loan Sanctioned Amount/i.test(t.innerText) && /Activities/i.test(t.innerText));
        let sanctioned = 0;
        if (act) {
            const head = [...act.querySelectorAll('thead th, tr:first-child th')].map(th => th.innerText.trim());
            const i = head.findIndex(h => /Loan Sanctioned Amount/i.test(h));
            const rows = [...act.querySelectorAll('tbody tr')].map(tr => [...tr.children].map(td => td.innerText.trim()));
            const total = rows.find(r => /total/i.test(r[0] || ''));
            sanctioned = total ? num(total[i]) : rows.reduce((s, r) => s + (r.length > i ? num(r[i]) : 0), 0);
        }
        const cl = tables.find(t => /Maximum Allowed Claim/i.test(t.innerText));
        let maxAllowed = 0;
        if (cl) {
            const head = [...cl.querySelectorAll('thead th, tr:first-child th')].map(th => th.innerText.trim());
            const i = head.findIndex(h => /Maximum Allowed Claim/i.test(h));
            const r = cl.querySelector('tbody tr');
            if (r && r.children[i]) maxAllowed = num(r.children[i].innerText);
        }
        return [sanctioned, maxAllowed];
    }""")


def fmt_amount(v: float) -> str:
    return str(int(v)) if float(v).is_integer() else f"{v:.2f}"


def fmt_is(v: float) -> str:
    return f"{v:.2f}"                    # Applicable IS keeps 2 decimals, as the portal shows it


def file_claim(page, row, intsub, disb, mark_submitting):
    """ADD -> fill -> SAVE & CONTINUE -> SUBMIT. Returns (sanctioned, max_allowed, claimed)."""
    tr = page.locator("table tbody tr", has_text=row["acct"]).first
    tr.locator("button", has_text=BTN("ADD")).first.click()
    wait_for(page, lambda: "/claim-against-loan-app" in page.url and
             page.locator('input[name="maxWithdrawalAmount"]').count(), f.STUCK_TIMEOUT_S, "claim form")
    page.wait_for_timeout(800)

    # the date fields stay greyed out (covered) until IS Submission Type is chosen -> choose it first
    _select(page, "priSubmissionType", SUBMISSION_TYPE)
    dates = page.locator("input.rmdp-input")
    wait_for(page, lambda: dates.count() >= 2, 15, "claim date fields")
    start_in, end_in = dates.nth(0), dates.nth(1)

    def is_open(inp):
        return inp.evaluate("""el => { const r = el.getBoundingClientRect();
            const top = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
            return !el.disabled && !!top && (top === el || el.contains(top) || top.contains(el)); }""")

    # start = disbursal date (the first active date of that calendar); a date the portal prefilled wins
    page.wait_for_timeout(800)
    if not re.fullmatch(r"\d{2}/\d{2}/\d{4}", start_in.input_value().strip()):
        if not disb:
            raise RuntimeError("no Disb. Date in the work list to pick as interest cycle start")
        wait_for(page, lambda: is_open(start_in), 15, "start date field to unlock (after IS Submission Type)")
        try:
            f.pick_calendar_date(page, start_in, f.parse_dmy(disb))
        except RuntimeError as e:
            raise RuntimeError(f"start date {disb} (Disb. Date) not selectable on the portal: {e}")
    page.wait_for_timeout(500)

    # end = repayment date 31-03-2026 (the last date)
    wait_for(page, lambda: is_open(end_in), 15, "end date field to unlock")
    if end_in.input_value().strip() != f"{REPAY_DATE:%d/%m/%Y}":
        f.pick_calendar_date(page, end_in, REPAY_DATE)
    page.wait_for_timeout(500)

    # the activity table renders a beat after the inputs: poll, never read once
    sanctioned = wait_for(page, lambda: _table_amounts(page)[0] or None, 10, "Loan Sanctioned Amount on the form")
    wd = page.locator('input[name="maxWithdrawalAmount"]').first
    wait_for(page, lambda: wd.is_enabled(), 15, "Max Withdrawal Amount to unlock")
    f.fill_amount(page, wd, fmt_amount(sanctioned))
    max_allowed = wait_for(page, lambda: _table_amounts(page)[1] or None, 15, "Maximum Allowed Claim")

    claim = min(max_allowed, intsub)
    if claim <= 0:
        return sanctioned, max_allowed, 0.0, ""
    isin = page.locator('input[name="applicableISAmount"]').first
    wait_for(page, lambda: isin.is_enabled(), 15, "Applicable IS to unlock")
    f.fill_amount(page, isin, fmt_is(claim))
    sub = page.locator('select[name="priSubmissionType"]').first
    if sub.evaluate("s => (s.options[s.selectedIndex]||{}).text.trim()") != SUBMISSION_TYPE:
        raise RuntimeError("IS Submission Type changed after filling the amounts")
    decl = page.locator('input[name="declarationText"]').first
    if not decl.is_checked():
        decl.check()
    wait_for(page, lambda: money_to_float(isin.input_value()) == round(claim, 2)
             and money_to_float(wd.input_value()) == sanctioned, 5, "claim amounts to stick")

    # ---- SAVE & CONTINUE -> "saved successfully" -> OK   (from here a DRAFT exists on the portal)
    page.locator("button:visible", has_text=BTN("SAVE & CONTINUE")).first.click()
    wait_for(page, lambda: re.search(r"saved successfully", modal_text(page), re.I), f.STUCK_TIMEOUT_S, "claim saved")
    mark_submitting("draft", sanctioned, max_allowed, claim)
    page.locator(".modal-content:visible button", has_text=BTN("OK")).first.click()
    wait_for(page, lambda: "/claim-application-preview" in page.url and
             page.locator("button:visible", has_text=BTN("SUBMIT")).count(), f.STUCK_TIMEOUT_S, "claim preview")
    page.wait_for_timeout(500)
    body = re.sub(r"\s+", " ", f.body_text(page))
    if row["acct"] not in body:
        raise RuntimeError(f"claim preview does not show account {row['acct']}")

    # ---- SUBMIT -> CONFIRM -> "Claim application No. <no> has been submitted successfully" -> OK
    page.locator("button:visible", has_text=BTN("SUBMIT")).first.click()
    confirm = page.locator(".modal-content:visible button", has_text=BTN("CONFIRM"))
    wait_for(page, lambda: confirm.count(), f.STUCK_TIMEOUT_S, "submit confirm dialog")
    mark_submitting("submitting", sanctioned, max_allowed, claim)
    confirm.first.click()
    txt = wait_for(page, lambda: (lambda t: t if re.search(r"submitted successfully", t, re.I) else None)(modal_text(page)),
                   f.STUCK_TIMEOUT_S, "claim submitted dialog")
    m = re.search(r"Claim application No\.?\s*([0-9]+)", txt, re.I)
    page.locator(".modal-content:visible button", has_text=BTN("OK")).first.click()
    page.wait_for_timeout(800)
    return sanctioned, max_allowed, claim, (m.group(1) if m else "")


# ----------------------------------------------------------------------------
# main
# ----------------------------------------------------------------------------
def main(limit: int, assume_yes: bool):
    header, rows, idx, finished = work_list()
    done = load_claims()
    todo = [(a, app, ia, dd) for a, app, ia, dd in finished if done.get(a) not in DONE and not
            (done.get(a) or "").startswith("CLAIM_CHECK_PORTAL")]
    held = [a for a, r in done.items() if r.startswith("CLAIM_CHECK_PORTAL")]
    print(f"[claims] {CLAIMS_CSV}: {len(done)} accounts logged; work list has {len(finished)} finished "
          f"applications -> {len(todo)} to claim (only those the portal lists as Pending are filed)")
    if held:
        print(f"[claims] {len(held)} CLAIM_CHECK_PORTAL -> NOT retried, check on the portal: {', '.join(held[:10])}")

    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(PROFILE_DIR, headless=f.HEADLESS, slow_mo=f.SLOWMO_MS,
                                                   viewport=None, args=["--start-maximized"])
        ctx.set_default_timeout(f.STUCK_TIMEOUT_S * 1000)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(f.BASE, wait_until="domcontentloaded")
        print("\n" + "=" * 70)
        print("  LOG IN with the BRANCH USER login (mobile + password + captcha).")
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
                      f"\n     Nothing claimed. Log in with the {want} Branch User login and start again.")
                ctx.close()
                return
            print(f"[login] branch check: portal '{got or '?'}' = work list '{want}'")

        if todo and not assume_yes:
            print(f"\n  IS claims will be FILED for the applications above (repayment {REPAY_DATE:%d-%m-%Y}, "
                  f"IS = lower of Maximum Allowed Claim and {COL_INTSUB}, type {SUBMISSION_TYPE}).")
            while (a := input("  >> type yes to file, no to stop: ").strip().lower()) not in ("yes", "no"):
                pass
            if a == "no":
                ctx.close()
                return

        open_list(page)
        claimed = waiting = errors = walked = 0
        cnt = {}
        for acct, app, intsub, disb in todo:
            if os.path.exists(f.STOP_FLAG):
                print("\n[stop] stop requested — stopping between claims")
                break
            if limit and claimed >= limit:
                break
            walked += 1
            tag = f"[{walked:>4}/{len(todo)}] {app or acct}  acct {acct}"
            if intsub <= 0:
                log_claim(acct, app, "", "", "", "", "NO_INT_SUB_AMT", "Int Sub Amt is 0 or blank in the master")
                cnt["NO_INT_SUB_AMT"] = cnt.get("NO_INT_SUB_AMT", 0) + 1
                print(f"{tag}  NO_INT_SUB_AMT")
                continue
            stage = {"s": "search"}
            for attempt in range(1, f.STUCK_RETRIES + 2):
                try:
                    if not page.locator('input[type="search"]:visible').count() or _table_state(page) == "none":
                        open_list(page)
                    hits = [r for r in find_rows(page, acct) if r["acct"] == acct and r["status"].lower() == "pending"]
                    if not hits:
                        waiting += 1                     # not approved yet / claimed already: nothing to do now
                        print(f"{tag}  not pending for claim (loan not approved yet, or already claimed)")
                        break
                    print(f"\r{tag}  claiming...   ", end="", flush=True)
                    stage["s"] = "form"

                    def mark(where, s, m, c, acct=acct, app=hits[0]["app"] or app):
                        # draft = saved on the portal; submitting = CONFIRM about to be clicked.
                        # Either way a rerun must not file this claim blindly again.
                        stage["s"] = where
                        log_claim(acct, app, s, m, intsub, fmt_is(c), f"CLAIM_CHECK_PORTAL:{where}",
                                  "claim saved as draft" if where == "draft" else "CONFIRM clicked, result not recorded yet")
                    s, m, c, claim_no = file_claim(page, hits[0], intsub, disb, mark)
                    if c <= 0:
                        log_claim(acct, hits[0]["app"], s, m, intsub, 0, "CLAIM_ZERO_ALLOWED",
                                  "portal Maximum Allowed Claim is 0")
                        cnt["CLAIM_ZERO_ALLOWED"] = cnt.get("CLAIM_ZERO_ALLOWED", 0) + 1
                        print(f"\r{tag}  CLAIM_ZERO_ALLOWED")
                        f.click_side_nav(page, "/dashboard")
                        open_list(page)
                        break
                    log_claim(acct, hits[0]["app"], s, m, intsub, fmt_is(c), "CLAIMED",
                              f"claim no {claim_no or '?'}; repay {REPAY_DATE:%d-%m-%Y} withdrawal {fmt_amount(s)} "
                              f"max {m} IS {fmt_is(c)}")
                    claimed += 1
                    print(f"\r{tag}  CLAIMED  claim {claim_no or '?'}  IS {fmt_is(c)} (max {m}, int sub {fmt_amount(intsub)})"
                          f"   | claimed {claimed}  errors {errors}", flush=True)
                    break
                except KeyboardInterrupt:
                    raise
                except Exception as e:
                    import traceback
                    with open("errors.log", "a") as ef:
                        ef.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} CLAIM acct={acct} stage={stage['s']} url={page.url}\n")
                        ef.write(traceback.format_exc())
                    msg = str(e).splitlines()[0][:100]
                    shot(page, f"claim_error_{acct}")
                    print(f"\n    [exc] stage={stage['s']} {type(e).__name__}: {msg}", flush=True)
                    if stage["s"] in ("draft", "submitting"):   # saved / confirmed: never again blindly
                        errors += 1
                        print(f"{tag}  CLAIM_CHECK_PORTAL — check this claim on the portal by hand")
                        f.reload_to_dashboard(page)
                        open_list(page)
                        break
                    if f.is_logged_out(page):
                        f.require_login(page)
                    f.reload_to_dashboard(page)
                    open_list(page)
                    if attempt > f.STUCK_RETRIES:
                        errors += 1
                        log_claim(acct, app, "", "", intsub, "", f"CLAIM_ERROR:{type(e).__name__}", msg)
                        print(f"{tag}  CLAIM_ERROR  {msg}")
        print(f"\n[done] {claimed} claimed, {waiting} not pending yet, {errors} errors, {cnt or ''}.  Log: {CLAIMS_CSV}")
        if not f.NO_PROMPT:
            input("  >> press ENTER to close the browser: ")
        ctx.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="File Fasalrin IS claims (Branch User login).")
    ap.add_argument("csv", help="branch work list, e.g. branches/3115/3115.csv")
    ap.add_argument("--limit", type=int, default=0, help="file at most N claims this run (0 = all)")
    ap.add_argument("--yes", action="store_true", help="don't ask before filing (needed with NO_PROMPT=1)")
    args = ap.parse_args()
    if not os.path.isfile(args.csv):
        ap.error(f"CSV not found: {args.csv}")
    if f.NO_PROMPT and not args.yes:
        ap.error("NO_PROMPT=1 cannot ask: pass --yes")
    f.INPUT_CSV = f.OUTPUT_CSV = args.csv
    CLAIMS_CSV = os.path.splitext(args.csv)[0] + "_claims.csv"
    try:
        main(args.limit, args.yes)
    except KeyboardInterrupt:
        print("\n[quit] interrupted — claims so far are recorded; rerun to continue")
