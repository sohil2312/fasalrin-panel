#!/usr/bin/env python3
"""
Fasalrin - file 3% PRI ADDITIONAL claims (2024-25 loans) with the Branch User login.

Steps as in the bank's flowchart "Flowchart for 3% PRI Additional claim FY 2025-26 entry", with the rules
agreed on the user's two sample entries:

  IS/PRI Claim Application: FY 2024-2025 / PRI / PENDING -> PROCEED
  per work-list row (account + Disb. Date): "Search Here" + magnifier -> ADD
    ADD each row of the account in turn (one row too); use the one whose Sanction/Rollover Date (form's
    activity table) is within 3 days of the Excel Disb. Date; BACK unsaved on the others; none -> NO_MATCH
    no row       -> NOT_FOUND
    Eligible for PRI = No -> NOT_ELIGIBLE (nothing saved)
  form: PRI Submission Type = COMPLETE
        start = Excel Disb. Date (the nearest date the calendar allows if it is greyed out)
        end   = Excel Repay. Date (the nearest date the calendar allows if it is greyed out)
        Max Withdrawal = Excel MAX(Dis_Amount); the form's Loan Sanctioned Amount when that is 0 or more than it
        Applicable PRI = lower of Excel 3% PRI and the portal's Maximum Allowed Claim (2 decimals)
        declaration ticked
  -> SAVE & CONTINUE -> OK -> SUBMIT -> CONFIRM -> "Claim application No. X has been submitted" -> OK

Claims filed outside the script: upload the portal's PRI claim report in the control panel (Portal
reports card) and press Match -> ALREADY_ON_PORTAL in the records file; the script never downloads reports.

Conventions as the other claim jobs: append-only fsync'd records <stem>_claims.csv (one row = account +
Disb. Date) read first at start, CLAIM_CHECK_PORTAL:draft / :submitting before SAVE / CONFIRM (never re-filed
blindly), login branch check by SOL, stop.flag between rows, --limit.

USAGE
  python fasalrin_pri.py branches/3106/pri/3106_pri.csv
  NO_PROMPT=1 ... --yes [--limit N]
"""

from __future__ import annotations

import argparse
import csv
import os
import re
import time
from datetime import date, timedelta
from pathlib import Path

from playwright.sync_api import sync_playwright

import fasalrin_regular as f
import fasalrin_claim as cl
from fasalrin_regular import BTN, wait_for, shot, money_to_float, modal_text

PROFILE_DIR = ".pw_profile_additional"   # Branch User profile (same login as IS additional)
LIST_FY = "2024-2025"                    # PRI PENDING list = the 2024-25 loans (PRI regular: 2025-2026)
SCHEME = "PRI additional"                 # configure("regular") switches to PRI regular
USE_MAXDIS = True                         # Max Withdrawal from Excel MAX(Dis_Amount) (PRI regular: portal's Loan Sanctioned)


def configure(scheme: str):
    """'additional' (default) or 'regular': PRI regular files FY 2025-2026 PRI claims on 2025-26 loans
    (the loan itself is entered through IS regular), Max Withdrawal = the portal's Loan Sanctioned Amount."""
    global LIST_FY, SCHEME, USE_MAXDIS
    if scheme == "regular":
        LIST_FY, SCHEME, USE_MAXDIS = "2025-2026", "PRI regular", False
    else:
        LIST_FY, SCHEME, USE_MAXDIS = "2024-2025", "PRI additional", True
CLAIM_FY = "2025-2026"                   # the claim itself (approval list, claim report)
SUBMISSION = "COMPLETE"
SLACK_DAYS = 3                           # row choice: Sanction/Rollover Date within 3 days of Disb. Date
COL_DISB, COL_REPAY, COL_MAXDIS, COL_PRI = "Disb. Date", "Repay. Date", "MAX(Dis_Amount)", "3% PRI"
HEADER = ["Time", f.COL_ACCT, COL_DISB, "Claim No", "Sanction Date", "Start Date", "End Date", "Max Withdrawal",
          "Max Allowed Claim", COL_PRI, "PRI Claimed", "Result", f.COL_DETAIL]
DONE = {"CLAIMED", "ALREADY_ON_PORTAL", "NOT_FOUND", "NO_MATCH", "NOT_ELIGIBLE"}
RECORDS_CSV = ""


# ----------------------------------------------------------------------------
# records: one row per loan (account + Disb. Date)
# ----------------------------------------------------------------------------
def key(acct, disb) -> str:
    return f"{str(acct).strip()}|{str(disb).strip()}"


def log(acct, disb, result, detail, **v):
    new = not os.path.exists(RECORDS_CSV)
    rec = {"Time": time.strftime("%Y-%m-%d %H:%M:%S"), f.COL_ACCT: acct, COL_DISB: disb, "Result": result,
           f.COL_DETAIL: detail, **v}
    with open(RECORDS_CSV, "a", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        if new:
            w.writerow(HEADER)
        w.writerow([rec.get(c, "") for c in HEADER])
        fh.flush()
        os.fsync(fh.fileno())


def load_records() -> dict:
    out = {}
    if os.path.exists(RECORDS_CSV):
        with open(RECORDS_CSV, newline="", encoding="utf-8-sig") as fh:
            for e in csv.DictReader(fh):
                out[key(e[f.COL_ACCT], e.get(COL_DISB, ""))] = e["Result"].strip()
    return out


def dmy(text):
    m = re.search(r"(\d{2})[/-](\d{2})[/-](\d{4})", text or "")
    return date(int(m.group(3)), int(m.group(2)), int(m.group(1))) if m else None


def year_end(start: date) -> date:
    try:
        return date(start.year + 1, start.month, start.day) - timedelta(days=1)
    except ValueError:
        return date(start.year + 1, start.month, start.day - 1) - timedelta(days=1)


# ----------------------------------------------------------------------------
# login branch check by SOL (the PRI master spells some names differently, e.g. KIDIYA vs portal KIDIA)
# ----------------------------------------------------------------------------
def branch_ok(page, sol: str) -> tuple[bool, str, set]:
    import branches
    names = branches.branch_names(sol)
    got = f.logged_in_branch(page)
    if not got or not names:
        return True, got, names
    return any(f.same_branch(got, n) for n in names), got, names


# ----------------------------------------------------------------------------
# portal steps
# ----------------------------------------------------------------------------
def open_list(page):
    """IS/PRI Claim Application -> 2024-2025 / PRI / PENDING -> PROCEED."""
    f.click_side_nav(page, "/dashboard")
    page.wait_for_timeout(800)
    f.click_side_nav(page, "/claim-application-list")
    wait_for(page, lambda: "/claim-application-list" in page.url, f.STUCK_TIMEOUT_S, "claim list page")
    cl._select(page, "financialYear", LIST_FY)
    cl._select(page, "claimType", "PRI")
    cl._select(page, "claimStatus", "PENDING")
    page.locator("button:visible", has_text=BTN("PROCEED")).first.click()
    wait_for(page, lambda: ("Total Count" in f.body_text(page) and cl._table_state(page) in ("rows", "empty"))
             or "No record(s) found" in f.body_text(page), 90, "PRI list to load")
    page.wait_for_timeout(300)


def ensure_list(page):
    if "/claim-application-list" not in page.url or not page.locator('input[placeholder="Search Here"]').count():
        open_list(page)


def add_buttons(page, acct):
    return page.locator("table tbody tr", has_text=acct).locator("button, a", has_text=BTN("ADD"))


def form_info(page) -> dict:
    """Sanction/Rollover Date (activity table) and the Eligible for PRI choice on the claim form."""
    return page.evaluate(r"""() => {
        const act = [...document.querySelectorAll('table')].find(t => /Sanction\/Rollover Date/i.test(t.innerText));
        let sanction = '';
        if (act) {
            const head = [...act.querySelectorAll('th')].map(th => th.innerText.trim());
            const i = head.findIndex(h => /Sanction\/Rollover Date/i.test(h));
            const r = [...act.querySelectorAll('tbody tr')].find(tr => !/total/i.test(tr.innerText));
            if (r && r.children[i]) sanction = r.children[i].innerText.trim();
        }
        const el = document.querySelector('select[name="eligibleForPRI"]');
        return {sanction, eligible: el ? (el.options[el.selectedIndex] || {}).text.trim() : ''};
    }""")


def back_to_list(page, acct):
    page.locator("button:visible", has_text=BTN("BACK")).first.click()
    try:
        wait_for(page, lambda: "/claim-application-list" in page.url, 20, "back on the list")
    except f.StuckError:
        pass
    ensure_list(page)
    cl.find_rows(page, acct)


def open_form(page, acct, k):
    page.wait_for_timeout(150)
    add_buttons(page, acct).nth(k).click()
    wait_for(page, lambda: "/claim-against-loan-app" in page.url and
             page.locator('input[name="maxWithdrawalAmount"]').count(), f.STUCK_TIMEOUT_S, "PRI claim form")
    wait_for(page, lambda: form_info(page)["sanction"] or None, 15, "Sanction/Rollover Date on the form")
    return form_info(page)


def is_open(inp):
    return inp.evaluate("""el => { const r = el.getBoundingClientRect();
        const top = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
        return !el.disabled && !!top && (top === el || el.contains(top) || top.contains(el)); }""")


def pick_nearest(page, inp, target: date) -> date:
    """Pick `target` in the rmdp calendar; if that day is greyed out, the nearest day the calendar allows
    (same month first, then up to 2 months either side). Returns the date picked."""
    try:
        f.pick_calendar_date(page, inp, target)
        return target
    except RuntimeError as e:
        if "not selectable" not in str(e):
            raise
    # calendar is still open on target's month: collect the enabled days of this and neighbouring months
    best = None
    for shift in (0, -1, 1, -2, 2):
        hdr = page.locator(".rmdp-header-values").first.inner_text()
        m = re.search(r"([A-Za-z]+)\D+(\d{4})", hdr)
        cur = date(int(m.group(2)), f.MONTHS.index(m.group(1)) + 1, 1)
        want_month = (target.replace(day=1) + timedelta(days=32 * shift)).replace(day=1) if shift else target.replace(day=1)
        while cur != want_month:
            page.locator(f".rmdp-arrow-container{'.rmdp-right' if cur < want_month else '.rmdp-left'}").first.click()
            page.wait_for_timeout(120)
            hdr = page.locator(".rmdp-header-values").first.inner_text()
            m = re.search(r"([A-Za-z]+)\D+(\d{4})", hdr)
            cur = date(int(m.group(2)), f.MONTHS.index(m.group(1)) + 1, 1)
        days = page.evaluate("""() => [...document.querySelectorAll('.rmdp-day')]
            .filter(d => !d.classList.contains('rmdp-day-hidden') && !d.classList.contains('rmdp-disabled'))
            .map(d => parseInt(d.innerText.trim(), 10)).filter(n => n > 0)""")
        for d in days:
            c = cur.replace(day=d)
            if best is None or abs((c - target).days) < abs((best - target).days):
                best = c
        if best and shift == 0:
            break
        if best:
            break
    page.keyboard.press("Escape")
    page.wait_for_timeout(200)
    if not best:
        raise RuntimeError(f"no selectable date near {target:%d/%m/%Y}")
    f.pick_calendar_date(page, inp, best)
    return best


def process_row(page, acct, disb, repay, maxdis, pri, stage, mark):
    """-> (result, detail, values dict)"""
    stage["s"] = "search"
    ensure_list(page)
    cl.find_rows(page, acct)
    n = add_buttons(page, acct).count()
    if n == 0:
        return "NOT_FOUND", f"not in the portal's PRI PENDING list (FY {LIST_FY})", {}
    seen, info = [], None
    for k in range(n):
        if k >= add_buttons(page, acct).count():
            break
        info = open_form(page, acct, k)
        sd = dmy(info["sanction"])
        if sd and abs((sd - disb).days) <= SLACK_DAYS:      # one row or several: always checked
            break
        seen.append(info["sanction"])
        back_to_list(page, acct)
        info = None
    if info is None:
        return "NO_MATCH", (f"{n} ADD row(s), none with Sanction/Rollover Date within {SLACK_DAYS} days of "
                            f"{disb:%d/%m/%Y}: {', '.join(seen)}"), {}
    vals = {"Sanction Date": info["sanction"]}
    if info["eligible"].lower() != "yes":
        back_to_list(page, acct)
        return "NOT_ELIGIBLE", f"Eligible for PRI = {info['eligible'] or '?'} on the portal", vals
    stage["s"] = "form"
    page.wait_for_timeout(500)
    cl._select(page, "priSubmissionType", SUBMISSION)
    dates = page.locator("input.rmdp-input")
    wait_for(page, lambda: dates.count() >= 2, 15, "claim date fields")
    start_in, end_in = dates.nth(0), dates.nth(1)
    page.wait_for_timeout(800)
    if dmy(start_in.input_value()) != disb:
        wait_for(page, lambda: is_open(start_in), 15, "start date field to unlock")
        start = pick_nearest(page, start_in, disb)
    else:
        start = disb
    page.wait_for_timeout(400)
    wait_for(page, lambda: is_open(end_in), 15, "end date field to unlock")
    end = dmy(end_in.input_value())
    if end != repay:
        end = pick_nearest(page, end_in, repay)
    page.wait_for_timeout(400)
    vals.update({"Start Date": f"{start:%d/%m/%Y}", "End Date": f"{end:%d/%m/%Y}"})

    sanctioned = wait_for(page, lambda: cl._table_amounts(page)[0] or None, 10, "Loan Sanctioned Amount on the form")
    # portal rule: Max Withdrawal <= sum of Loan Sanctioned in the activity table -> above it (or 0) use the portal's
    mw = maxdis if 0 < maxdis <= sanctioned else sanctioned
    wd = page.locator('input[name="maxWithdrawalAmount"]').first
    wait_for(page, lambda: wd.is_enabled(), 15, "Max Withdrawal Amount to unlock")
    f.fill_amount(page, wd, cl.fmt_amount(mw))
    max_allowed = wait_for(page, lambda: cl._table_amounts(page)[1] or None, 15, "Maximum Allowed Claim")
    claim = round(min(pri, max_allowed), 2)
    vals.update({"Max Withdrawal": cl.fmt_amount(mw), "Max Allowed Claim": f"{max_allowed:.2f}", "PRI Claimed": f"{claim:.2f}"})
    if claim <= 0:
        raise RuntimeError(f"Applicable PRI would be {claim} (3% PRI {pri}, max allowed {max_allowed}) - refusing")
    pin = page.locator('input[name="applicablePRIAmount"]').first
    wait_for(page, lambda: pin.is_enabled(), 15, "Applicable PRI to unlock")
    f.fill_amount(page, pin, f"{claim:.2f}")
    sub = page.locator('select[name="priSubmissionType"]').first
    if sub.evaluate("s => (s.options[s.selectedIndex]||{}).text.trim()") != SUBMISSION:
        raise RuntimeError("PRI Submission Type changed after filling the amounts")
    decl = page.locator('input[name="declarationText"]').first
    if not decl.is_checked():
        decl.check()
    wait_for(page, lambda: money_to_float(pin.input_value()) == claim and money_to_float(wd.input_value()) == mw,
             5, "claim amounts to stick")
    if dmy(start_in.input_value()) != start or dmy(end_in.input_value()) != end:
        raise RuntimeError("dates changed after filling the amounts")
    stage["s"] = "filled"

    # ---- SAVE & CONTINUE -> "saved successfully" -> OK   (from here a DRAFT exists on the portal)
    page.locator("button:visible", has_text=BTN("SAVE & CONTINUE")).first.click()
    wait_for(page, lambda: re.search(r"saved successfully", modal_text(page), re.I), f.STUCK_TIMEOUT_S, "claim saved")
    stage["s"] = "draft"
    mark("draft", vals)
    page.locator(".modal-content:visible button", has_text=BTN("OK")).first.click()
    wait_for(page, lambda: "/claim-application-preview" in page.url and
             page.locator("button:visible", has_text=BTN("SUBMIT")).count(), f.STUCK_TIMEOUT_S, "claim preview")
    page.wait_for_timeout(500)
    if acct not in re.sub(r"\s+", " ", f.body_text(page)):
        raise RuntimeError(f"claim preview does not show account {acct}")
    # ---- SUBMIT -> CONFIRM -> "Claim application No. <no> has been submitted successfully" -> OK
    page.locator("button:visible", has_text=BTN("SUBMIT")).first.click()
    confirm = page.locator(".modal-content:visible button", has_text=BTN("CONFIRM"))
    wait_for(page, lambda: confirm.count(), f.STUCK_TIMEOUT_S, "submit confirm dialog")
    stage["s"] = "submitting"
    mark("submitting", vals)
    confirm.first.click()
    txt = wait_for(page, lambda: (lambda t: t if re.search(r"submitted successfully", t, re.I) else None)(modal_text(page)),
                   f.STUCK_TIMEOUT_S, "claim submitted dialog")
    m = re.search(r"Claim application No\.?\s*([0-9]+)", txt, re.I)
    page.locator(".modal-content:visible button", has_text=BTN("OK")).first.click()
    page.wait_for_timeout(800)
    stage["s"] = "done"
    vals["Claim No"] = m.group(1) if m else ""
    return "CLAIMED", (f"claim {vals['Claim No']}  start {vals['Start Date']} end {vals['End Date']} "
                       f"MW {vals['Max Withdrawal']} max {max_allowed:.2f} PRI {claim:.2f}"), vals


def _recover(page):
    try:
        for name in ("OK", "CANCEL", "NO"):
            b = page.locator(".modal-content:visible button", has_text=BTN(name))
            if b.count():
                b.first.click()
                page.wait_for_timeout(500)
                break
    except Exception:
        pass
    try:
        if "/claim-application-list" not in page.url:
            back = page.locator("button:visible", has_text=BTN("BACK"))
            if back.count():
                back.first.click()
                page.wait_for_timeout(1500)
    except Exception:
        pass
    try:
        open_list(page)
    except Exception:
        pass


# ----------------------------------------------------------------------------
# PRI claim report reader (the panel's Match uses it)
# ----------------------------------------------------------------------------
def read_pri_report(path, loan_fy: str | None = None) -> dict:
    """zip / xlsx -> {account: [(claim no, loan disbursal date, user)]} for approved PRI claims whose
    'Loan application FY' is loan_fy (default LIST_FY: 2024-2025 = PRI additional, 2025-2026 = PRI regular)."""
    loan_fy = loan_fy or LIST_FY
    import io
    import zipfile
    import openpyxl
    path = Path(path)
    if path.suffix.lower() == ".xlsx":
        parts = [path.read_bytes()]
    else:
        with zipfile.ZipFile(path) as z:
            parts = [z.read(n) for n in z.namelist() if n.lower().endswith(".xlsx")]
    found = {}
    for data in parts:
        wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True)
        it = wb.active.iter_rows(values_only=True)
        head = [str(c or "").strip() for c in next(it)]
        col = {k: head.index(k) for k in ("Claim Number", "Claim Type", "Account Number", "Loan application FY",
                                          "Application Status", "User Name")}
        idisb = head.index("Loan Disbursal Date") if "Loan Disbursal Date" in head else None
        for row in it:
            acct = row[col["Account Number"]]
            acct = str(int(acct)) if isinstance(acct, float) else str(acct or "").strip()
            if (acct and str(row[col["Claim Type"]] or "").strip().upper() == "PRI"
                    and str(row[col["Loan application FY"]] or "").strip() == loan_fy
                    and str(row[col["Application Status"]] or "").strip().lower() == "approved"):
                d = row[idisb] if idisb is not None else None
                d = d.date() if hasattr(d, "date") else dmy(str(d or ""))
                found.setdefault(acct, []).append((str(row[col["Claim Number"]] or "").strip(), d,
                                                   str(row[col["User Name"]] or "").strip()))
        wb.close()
    return found


# ----------------------------------------------------------------------------
# main
# ----------------------------------------------------------------------------
def main(limit: int, assume_yes: bool, only: str = ""):
    f.INPUT_CSV = f.OUTPUT_CSV
    header, rows, idx = f.load_rows()
    need = [f.COL_ACCT, COL_DISB, COL_REPAY, COL_PRI] + ([COL_MAXDIS] if USE_MAXDIS else [])
    if any(c not in idx for c in need):
        raise SystemExit(f"work list needs the columns {need}")
    sol = Path(f.INPUT_CSV).parent.parent.name
    recs = load_records()
    held = [k for k, r in recs.items() if r.startswith("CLAIM_CHECK_PORTAL")]
    todo, bad = [], []
    for r in rows[1:]:
        acct, disb_s = r[idx[f.COL_ACCT]].strip(), r[idx[COL_DISB]].strip()
        st = recs.get(key(acct, disb_s), "")
        if (not acct or (only and acct != only) or money_to_float(r[idx[COL_PRI]]) <= 0 or st in DONE
                or st.startswith("CLAIM_CHECK_PORTAL")):
            continue
        disb, repay = dmy(disb_s), dmy(r[idx[COL_REPAY]])
        if not disb or not repay:
            bad.append(acct)
            continue
        maxdis = money_to_float(r[idx[COL_MAXDIS]]) if USE_MAXDIS else 0.0   # 0 -> the portal's Loan Sanctioned
        todo.append((acct, disb_s, disb, repay, maxdis, money_to_float(r[idx[COL_PRI]])))
    print(f"[pri] {RECORDS_CSV}: {len(recs)} rows logged; {len(rows) - 1} in the work list -> {len(todo)} to do")
    if bad:
        print(f"[pri] {len(bad)} rows without a readable Disb. Date / Repay. Date -> skipped: {', '.join(bad[:10])}")
    if held:
        print(f"[pri] {len(held)} CLAIM_CHECK_PORTAL -> NOT retried, check on the portal: {', '.join(held[:10])}")

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
        if not f.require_role(page, "Branch User"):
            ctx.close()
            return
        ok, got, names = branch_ok(page, sol)
        if not ok:
            print(f"\n  !! WRONG LOGIN: the portal is logged in as branch '{got}', this work list is SOL {sol} "
                  f"({' / '.join(sorted(names))}).\n     Nothing claimed. Log in with the SOL {sol} Branch User login.")
            ctx.close()
            return
        print(f"[login] branch check: portal '{got or '?'}' = SOL {sol} ({' / '.join(sorted(names)) or '?'})")

        if todo and not assume_yes:
            print(f"\n  {SCHEME.upper()} claims will be FILED (COMPLETE, start = Disb. Date, end = Repay. Date, "
                  "Max Withdrawal = MAX(Dis_Amount) or Loan Sanctioned if lower/0, PRI = lower of 3% PRI and Maximum Allowed Claim). Cannot be undone.")
            while (a := input("  >> type yes to file, no to stop: ").strip().lower()) not in ("yes", "no"):
                pass
            if a == "no":
                ctx.close()
                return

        claimed = walked = errors = 0
        cnt = {}
        for acct, disb_s, disb, repay, maxdis, pri in todo:
            if os.path.exists(f.STOP_FLAG):
                print("\n[stop] stop requested - stopping between rows")
                break
            if limit and claimed >= limit:
                break
            walked += 1
            tag = f"[{walked:>4}/{len(todo)}] {acct}"
            for attempt in range(1, f.STUCK_RETRIES + 2):
                stage = {"s": "start"}

                def mark(where, vals, acct=acct, disb_s=disb_s, pri=pri):
                    log(acct, disb_s, f"CLAIM_CHECK_PORTAL:{where}",
                        "claim saved as draft" if where == "draft" else "CONFIRM clicked, result not recorded yet",
                        **vals, **{COL_PRI: pri})
                print(f"\r{tag}  working...   ", end="", flush=True)
                try:
                    st, detail, vals = process_row(page, acct, disb, repay, maxdis, pri, stage, mark)
                    log(acct, disb_s, st, detail, **vals, **{COL_PRI: pri})
                    cnt[st] = cnt.get(st, 0) + 1
                    claimed += st == "CLAIMED"
                    print(f"\r{tag}  {st}  {detail}   | claimed {claimed}  errors {errors}", flush=True)
                    break
                except KeyboardInterrupt:
                    raise
                except Exception as e:
                    import traceback
                    with open("errors.log", "a") as ef:
                        ef.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} PRI acct={acct} stage={stage['s']} url={page.url}\n")
                        ef.write(traceback.format_exc())
                    msg = str(e).splitlines()[0][:100]
                    shot(page, f"pri_error_{acct}")
                    print(f"\n    [exc] stage={stage['s']} {type(e).__name__}: {msg}", flush=True)
                    if stage["s"] in ("draft", "submitting"):     # saved / confirmed: never again blindly
                        errors += 1
                        print(f"{tag}  CLAIM_CHECK_PORTAL - check this claim on the portal by hand")
                        if f.is_logged_out(page):
                            f.require_login(page)
                        _recover(page)
                        break
                    if f.is_logged_out(page):
                        f.require_login(page)
                        open_list(page)
                        continue
                    _recover(page)
                    if attempt > f.STUCK_RETRIES:
                        errors += 1
                        log(acct, disb_s, f"CLAIM_ERROR:{type(e).__name__}", msg, **{COL_PRI: pri})
                        print(f"{tag}  CLAIM_ERROR  {msg}")
        print(f"\n[done] {claimed} claimed, {walked} walked, {errors} errors, {cnt}.  Log: {RECORDS_CSV}")
        if not f.NO_PROMPT:
            input("  >> press ENTER to close the browser: ")
        ctx.close()


def cli(scheme: str = "additional"):
    global RECORDS_CSV
    configure(scheme)
    example = "branches/3106/prireg/3106_prireg.csv" if scheme == "regular" else "branches/3106/pri/3106_pri.csv"
    ap = argparse.ArgumentParser(description=f"File Fasalrin 3% {SCHEME} claims (Branch User login).")
    ap.add_argument("csv", help=f"{SCHEME} work list, e.g. {example}")
    ap.add_argument("--limit", type=int, default=0, help="file at most N claims this run (0 = all)")
    ap.add_argument("--only", default="", help="file only this account (e.g. a retry)")
    ap.add_argument("--yes", action="store_true", help="don't ask before filing (needed with NO_PROMPT=1)")
    args = ap.parse_args()
    if not os.path.isfile(args.csv):
        ap.error(f"CSV not found: {args.csv}")
    if f.NO_PROMPT and not args.yes:
        ap.error("NO_PROMPT=1 cannot ask: pass --yes")
    f.INPUT_CSV = f.OUTPUT_CSV = args.csv
    RECORDS_CSV = os.path.splitext(args.csv)[0] + "_claims.csv"
    try:
        main(args.limit, args.yes, args.only.strip())
    except KeyboardInterrupt:
        print("\n[quit] interrupted - claims so far are recorded; rerun to continue")


if __name__ == "__main__":
    cli("additional")
