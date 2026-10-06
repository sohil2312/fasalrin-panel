#!/usr/bin/env python3
"""
Fasalrin — file IS ADDITIONAL (rollover) claims with the Branch User login.

IS additional = the rollover IS claim for 2024-25 loans, claimed in FY 2025-26. Steps as in the bank's
flowchart "Flowchart for 1.5% IS Additional claim FY 2025-26 entry" (portal clicks from the proven
D:\\fasalrin\\15 additional\\fasalrin-is-additional\\fasalrin_rollover.py):

  IS/PRI Claim Application: FY 2024-2025 / IS / ROLLOVER / ALL -> PROCEED
  per account: "Search Here" + magnifier -> REVIEW the account's rows, highest Applicable IS first, until the
  IS Claim Details show
      First Loan Disbursal/Interest Cycle Start Date == work list Disb. Date (up to 3 days apart)  and
      Interest Cycle end/Rollover Date == 31/03/2025
    (no row of the account in the list -> NOT_FOUND; rows but none matching -> NO_MATCH, hand work)
    -> CONTINUE ROLLOVER -> "no longer available" -> CLOSED   |   "proceed ... 2025-2026?" -> YES
    -> form: Rollover Date = work list Repay. Date, or portal start + 1 year - 1 day if that is earlier
             Max Withdrawal = Loan Sanctioned Amount (Total row)
             Applicable IS  = work list Int Sub Amt, or Maximum Allowed Claim if that is lower
             declaration ticked
    -> SAVE & CONTINUE -> OK -> SUBMIT -> CONFIRM -> "Claim application No. X has been submitted" -> OK

Around it, the same conventions as the IS regular scripts: branch work list
branches/<SOL>/additional/<SOL>_additional.csv (built by branches.py from the IS additional master),
append-only records branches/<SOL>/additional/<SOL>_additional_claims.csv (fsync'd, read first at start;
the work CSV is not written while running), CLAIM_CHECK_PORTAL:draft / :submitting saved after SAVE and
before CONFIRM (never re-filed blindly), branch login check, stop.flag between accounts.

USAGE
  python fasalrin_additional.py branches/3115/additional/3115_additional.csv     # asks "yes" before filing
  NO_PROMPT=1 ... --yes [--limit N]
"""

from __future__ import annotations

import argparse
import csv
import os
import re
import time
from datetime import date
from pathlib import Path

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

import fasalrin_regular as f
from fasalrin_regular import shot, money_to_float

PROFILE_DIR = ".pw_profile_additional"   # own browser profile: can run next to the other jobs
FIN_YEAR = "2024-2025"                   # rollover list = the 2024-25 loans
COL_INTSUB = "Int Sub Amt"
HEADER = ["Time", f.COL_ACCT, "Claim No", "First Disbursal", "Rollover Date", "Loan Sanctioned",
          "Max Allowed Claim", COL_INTSUB, "IS Claimed", "Result", f.COL_DETAIL]
COL_DISB, COL_REPAY = "Disb. Date", "Repay. Date"
CYCLE_END = date(2025, 3, 31)            # the 2024-25 cycle being rolled over ends on 31/03/2025
DISB_SLACK_DAYS = 3                      # portal start date may differ from the Excel Disb. Date by up to 3 days
# finished or hand work: never re-filed by the job (a re-upload with Fixed = Y appends RETRY)
DONE = {"CLAIMED", "ALREADY_ON_PORTAL", "NOT_FOUND", "CLOSED", "NO_MATCH", "WRONG_CLAIM"}
RECORDS_CSV = ""
MONTHS = f.MONTHS


# ----------------------------------------------------------------------------
# records
# ----------------------------------------------------------------------------
def log(acct, claim_no, first_disb, roll, sanctioned, max_allowed, intsub, claimed, result, detail):
    new = not os.path.exists(RECORDS_CSV)
    with open(RECORDS_CSV, "a", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        if new:
            w.writerow(HEADER)
        w.writerow([time.strftime("%Y-%m-%d %H:%M:%S"), acct, claim_no, first_disb, roll, sanctioned,
                    max_allowed, intsub, claimed, result, detail])
        fh.flush()
        os.fsync(fh.fileno())


def load_records() -> dict:
    out = {}
    if os.path.exists(RECORDS_CSV):
        with open(RECORDS_CSV, newline="", encoding="utf-8-sig") as fh:
            for e in csv.DictReader(fh):
                out[e[f.COL_ACCT].strip()] = e["Result"].strip()
    return out


# ----------------------------------------------------------------------------
# portal steps — ported unchanged from the proven fasalrin_rollover.py
# ----------------------------------------------------------------------------
def date_in(text: str):
    """'dd/mm/yyyy' or 'dd-mm-yyyy' anywhere in text -> date (None if none)."""
    m = re.search(r"(\d{2})[/-](\d{2})[/-](\d{4})", text or "")
    return date(int(m.group(3)), int(m.group(2)), int(m.group(1))) if m else None


SELECT_PLAN = [                          # dropdowns found by an option they contain (formcontrolname is unreliable)
    ("2024-2025", "Financial Year",  FIN_YEAR),
    ("PRI",       "Claim type",      "IS"),
    ("ROLLOVER",  "Claim Status",    "ROLLOVER"),
    ("COMPLETE",  "Submission Type", "ALL"),
]


def find_select(page, key_option):
    return page.locator("select", has=page.locator(
        "option", has_text=re.compile(rf"^\s*{re.escape(key_option)}\s*$"))).first


def list_page_ready(page) -> bool:
    return find_select(page, "ROLLOVER").count() > 0 and find_select(page, "2024-2025").count() > 0


def selected_label(locator) -> str:
    return locator.evaluate("s => (s.options[s.selectedIndex] || {}).text || ''").strip()


def set_select(page, key_option, human_name, label):
    sel = find_select(page, key_option)
    for _ in range(60):
        if sel.count() > 0:
            break
        page.wait_for_timeout(250)
    if sel.count() == 0:
        raise RuntimeError(f"{human_name} dropdown not found (no option {key_option!r})")
    cur = ""
    for _ in range(6):                   # the Angular form re-initialises defaults: select, re-read, retry
        sel.select_option(label=label)
        page.wait_for_timeout(400)
        cur = selected_label(sel)
        if cur == label:
            return
        page.wait_for_timeout(800)
    raise RuntimeError(f"{human_name} stuck at {cur!r}, wanted {label!r}")


def goto_list(page):
    def wait_ready(seconds):
        for _ in range(int(seconds * 4)):
            if list_page_ready(page):
                return True
            page.wait_for_timeout(250)
        return list_page_ready(page)
    if wait_ready(2):
        return
    f.click_side_nav(page, "/dashboard")
    page.wait_for_timeout(800)
    f.click_side_nav(page, "/claim-application-list")       # routerLink soft-nav, never page.goto
    if not wait_ready(15):
        raise RuntimeError("claim list dropdowns did not appear")


def do_warmup(page):
    goto_list(page)
    page.wait_for_timeout(800)
    for key, human, label in SELECT_PLAN:
        set_select(page, key, human, label)
    page.get_by_role("button", name="PROCEED").click()
    page.wait_for_selector('input[placeholder="Search Here"]', timeout=90000)
    page.wait_for_selector("text=Total Count", timeout=90000)
    page.wait_for_selector("table.issCustomTable", timeout=90000)
    page.wait_for_timeout(200)


def ensure_on_list(page):
    try:
        page.wait_for_selector('input[placeholder="Search Here"]', timeout=15000)
        return
    except PWTimeout:
        do_warmup(page)


def search_account(page, acct):
    box = page.locator('input[placeholder="Search Here"]')
    box.fill("")
    box.fill(acct)
    page.locator('button.btn-secondary:has(i.fa-search)').first.click()
    for _ in range(40):                  # up to ~12 s, until the table reflects THIS search
        state = page.evaluate("""(acct) => {
            if (document.body.innerText.includes('No record(s) found')) return 'none';
            const rows=[...document.querySelectorAll('table.issCustomTable tbody tr')];
            for (const r of rows){
              const c=[...r.querySelectorAll('td')].map(x=>x.textContent.trim());
              if (c[3] === acct) return 'found';
            }
            return 'pending';
        }""", acct)
        if state != "pending":
            page.wait_for_timeout(150)
            return
        page.wait_for_timeout(150)
    raise RuntimeError("search results did not update for this account")


def year_end(start: date) -> date:
    """start + 1 year - 1 day: the last rollover date the portal's calendar allows."""
    try:
        one_year = date(start.year + 1, start.month, start.day)
    except ValueError:                   # 29 Feb -> non-leap next year
        one_year = date(start.year + 1, start.month, start.day - 1)
    return date.fromordinal(one_year.toordinal() - 1)


def review_buttons(page, acct) -> list:
    """REVIEW buttons of every row with Account No. == acct (an account can have several claim rows),
    highest Applicable IS Amt. first."""
    if page.get_by_text("No record(s) found").count() > 0:
        return []
    rows = page.locator("table.issCustomTable tbody tr")
    out = []
    for i in range(rows.count()):
        row = rows.nth(i)
        if row.locator('button:has-text("REVIEW")').count() == 0:
            continue
        try:
            cells = row.locator("td")
            if cells.nth(3).inner_text().strip() == acct:
                out.append((money_to_float(cells.nth(8).inner_text()), i, row.locator('button:has-text("REVIEW")').first))
        except Exception:
            continue
    out.sort(key=lambda t: (-t[0], t[1]))
    return [b for _, _, b in out]


def read_review_cycle(page):
    """IS Claim Details (after REVIEW): (First Loan Disbursal/Interest Cycle Start Date, Interest Cycle
    end/Rollover Date) as dates."""
    for _ in range(100):                 # up to ~10 s for the values to render
        body = page.inner_text("body")
        s = re.search(r"Interest Cycle Start Date\s*(\d{2}/\d{2}/\d{4})", body)
        e = re.search(r"Interest Cycle end/Rollover Date\s*(\d{2}/\d{2}/\d{4})", body)
        if s and e:
            return date_in(s.group(1)), date_in(e.group(1))
        page.wait_for_timeout(100)
    raise RuntimeError("IS Claim Details never showed the interest cycle dates")


def back_to_list(page, acct):
    back = page.get_by_role("button", name=re.compile(r"^\s*BACK\s*$", re.I)).first
    back.wait_for(timeout=10000)
    back.click()
    page.wait_for_selector('input[placeholder="Search Here"]', timeout=20000)
    search_account(page, acct)


def read_first_disbursal(page) -> str:
    loc = page.locator("input.rmdp-input").first
    for _ in range(100):
        v = loc.input_value().strip()
        if re.fullmatch(r"\d{2}/\d{2}/\d{4}", v):
            return v
        page.wait_for_timeout(100)
    raise RuntimeError("First-Disbursal date never populated on the form")


def read_loan_sanctioned(page) -> float:
    return money_to_float(page.evaluate("""() => {
        for (const t of document.querySelectorAll('table')){
          const ths=[...t.querySelectorAll('th')].map(x=>x.textContent.trim());
          const ci=ths.findIndex(h=>/Loan Sanctioned Amount/i.test(h));
          if(ci>=0){
            const trs=[...t.querySelectorAll('tbody tr')];
            const tot=trs.find(r=>/Total/i.test(r.textContent)) || trs[0];
            const tds=[...tot.querySelectorAll('td')];
            if(tds[ci]) return tds[ci].textContent.trim();
          }
        }
        return "";
    }"""))


def read_max_allowed(page) -> float:
    return money_to_float(page.evaluate("""() => {
        for (const t of document.querySelectorAll('table')){
          const ths=[...t.querySelectorAll('th')].map(x=>x.textContent.trim());
          const ci=ths.findIndex(h=>/Maximum Allowed Claim/i.test(h));
          if(ci>=0){
            for(const r of t.querySelectorAll('tbody tr')){
              const c=[...r.querySelectorAll('td')];
              if(c[ci] && /₹/.test(c[ci].textContent)) return c[ci].textContent.trim();
            }
          }
        }
        return "";
    }"""))


def pick_rollover_date(page, target: date):
    date_input = page.locator("input.rmdp-input").nth(1)
    date_input.click()
    page.wait_for_selector(".rmdp-header-values", timeout=8000)
    tgt = (target.year, target.month)
    for _ in range(14):
        hdr = page.locator(".rmdp-header-values").first.inner_text()
        m = re.search(r"([A-Za-z]+)\D+(\d{4})", hdr)
        if not m:
            raise RuntimeError(f"unreadable calendar header: {hdr!r}")
        cur = (int(m.group(2)), MONTHS.index(m.group(1)) + 1)
        if cur == tgt:
            break
        arrow = ".rmdp-right" if cur < tgt else ".rmdp-left"
        page.locator(f".rmdp-arrow-container{arrow}").first.click()
        page.wait_for_timeout(120)
    else:
        raise RuntimeError(f"could not reach {target:%m/%Y} in calendar")
    ok = page.evaluate("""(day) => {
        for (const d of document.querySelectorAll('.rmdp-day')){
          const sp=d.querySelector('span');
          if(!sp || sp.textContent.trim()!==String(day)) continue;
          if(d.classList.contains('rmdp-day-hidden')) continue;
          if(d.classList.contains('rmdp-disabled')) return 'disabled';
          sp.click();
          return 'ok';
        }
        return 'notfound';
    }""", target.day)
    if ok != "ok":
        raise RuntimeError(f"date {target:%d/%m/%Y} not selectable ({ok})")
    for _ in range(20):
        if date_input.input_value().strip():
            return
        page.wait_for_timeout(100)
    raise RuntimeError(f"date {target:%d/%m/%Y} did not register")


def fill_amount(page, locator, value):
    locator.click()
    locator.fill(str(value))
    locator.dispatch_event("input")
    locator.dispatch_event("change")
    locator.blur()
    page.wait_for_timeout(150)


def process_account(page, acct, intsub, disb, repay, stage, mark):
    """-> (status, claim_no, first_disb, rollover, sanctioned, max_allowed, applicable_str, detail)
    disb / repay = the work list's Disb. Date / Repay. Date (dates)."""
    stage["s"] = "start"
    ensure_on_list(page)
    search_account(page, acct)
    stage["s"] = "search"
    n = len(review_buttons(page, acct))
    if n == 0:
        return "NOT_FOUND", "", "", "", "", "", "", "not in the portal's ROLLOVER list"
    seen = []
    for k in range(n):                   # REVIEW each row of the account until the cycle matches the Excel
        btns = review_buttons(page, acct)
        if k >= len(btns):
            break
        btns[k].click()
        cont = page.get_by_text("CONTINUE ROLLOVER", exact=True).last
        cont.wait_for(timeout=20000)
        start, end = read_review_cycle(page)
        if abs((start - disb).days) <= DISB_SLACK_DAYS and end == CYCLE_END:
            break
        seen.append(f"{start:%d/%m/%Y}-{end:%d/%m/%Y}")
        back_to_list(page, acct)
    else:
        return ("NO_MATCH", "", "", "", "", "", "",
                f"no rollover row with start {disb:%d/%m/%Y} (±{DISB_SLACK_DAYS} days) and end {CYCLE_END:%d/%m/%Y}; portal rows: {', '.join(seen)}")
    cont.click()
    page.wait_for_selector("text=/no longer available|proceed with the rollover/i", timeout=15000)
    body = page.inner_text("body")
    if re.search(r"no longer available", body, re.I):
        page.get_by_role("button", name=re.compile(r"^\s*OK\s*$", re.I)).first.click()
        back = page.get_by_role("button", name=re.compile(r"^\s*BACK\s*$", re.I)).first
        back.wait_for(timeout=10000)
        back.click()
        page.wait_for_selector('input[placeholder="Search Here"]', timeout=20000)
        return "CLOSED", "", "", "", "", "", "", "rollover no longer available (already rolled / closed)"
    if not re.search(r"proceed with the rollover", body, re.I):
        shot(page, f"add_unexpected_modal_{acct}")
        raise RuntimeError("unexpected modal after CONTINUE ROLLOVER")
    page.get_by_role("button", name=re.compile(r"^\s*YES\s*$", re.I)).first.click()
    stage["s"] = "form"

    page.wait_for_selector("input.rmdp-input", timeout=20000)
    first_disb = read_first_disbursal(page)
    if date_in(first_disb) != start:     # the form must be the cycle we reviewed
        _recover(page)
        return ("NO_MATCH", "", first_disb, "", "", "", "",
                f"form First Disbursal {first_disb} != reviewed start {start:%d/%m/%Y}")
    target = min(repay, year_end(start))  # Repay. Date, but never past what the portal's calendar allows
    sanctioned = 0.0
    for _ in range(60):
        sanctioned = read_loan_sanctioned(page)
        if sanctioned > 0:
            break
        page.wait_for_timeout(150)
    if sanctioned <= 0:
        raise RuntimeError("could not read Loan Sanctioned Amount")
    pick_rollover_date(page, target)

    mw = page.locator("input.inputextRightAlign.form-control").nth(0)
    for _ in range(60):
        if mw.is_enabled():
            break
        page.wait_for_timeout(100)
    fill_amount(page, mw, int(sanctioned))
    max_allowed = 0.0
    for _ in range(80):                  # shows 0.00 until recomputed: poll for the real value
        max_allowed = read_max_allowed(page)
        if max_allowed > 0:
            break
        page.wait_for_timeout(150)
    if max_allowed <= 0:
        raise RuntimeError("Maximum Allowed Claim stayed 0.00 after entering Max Withdrawal")
    # Applicable IS = the Excel's Int Sub Amt (flowchart); if that is more than the portal allows, the max allowed
    applicable = min(float(intsub), max_allowed)
    if applicable <= 0:
        raise RuntimeError(f"applicable IS would be {applicable} (Int Sub Amt={intsub}) — refusing")
    applicable_str = f"{applicable:.2f}"
    ai = page.locator("input.inputextRightAlign.form-control").nth(1)
    for _ in range(60):
        if ai.is_enabled():
            break
        page.wait_for_timeout(100)
    fill_amount(page, ai, applicable_str)
    page.locator("#declarationText").check()
    page.wait_for_timeout(100)
    stage["s"] = "filled"
    vals = (first_disb, f"{target:%d/%m/%Y}", int(sanctioned), f"{max_allowed:.2f}", applicable_str)

    page.get_by_text("SAVE & CONTINUE", exact=True).last.click()
    page.wait_for_selector("text=saved successfully", timeout=20000)
    stage["s"] = "draft"                 # a DRAFT now exists on the portal
    mark("draft", *vals)
    page.get_by_role("button", name=re.compile(r"^\s*OK\s*$", re.I)).first.click()
    submit_btn = page.get_by_role("button", name=re.compile(r"^\s*SUBMIT\s*$", re.I)).first
    submit_btn.wait_for(timeout=20000)
    submit_btn.click()
    confirm_btn = page.get_by_role("button", name=re.compile(r"^\s*CONFIRM\s*$", re.I)).first
    confirm_btn.wait_for(timeout=15000)
    stage["s"] = "submitting"
    mark("submitting", *vals)
    confirm_btn.click()
    page.wait_for_selector("text=submitted successfully", timeout=20000)
    m = re.search(r"Claim application No\.\s*([0-9]+)\s*has been submitted", page.inner_text("body"), re.I)
    claim_no = m.group(1) if m else ""
    page.get_by_role("button", name=re.compile(r"^\s*OK\s*$", re.I)).first.click()
    stage["s"] = "done"
    detail = f"rollover {target:%d/%m/%Y} MW {int(sanctioned)} max {max_allowed:.2f} IS {applicable_str}"
    return ("CLAIMED", claim_no, *vals, detail)


def _recover(page):
    try:
        for name in ("OK", "CANCEL", "NO"):
            b = page.get_by_role("button", name=re.compile(rf"^\s*{name}\s*$", re.I))
            if b.count() > 0:
                b.first.click()
                page.wait_for_timeout(500)
                break
    except Exception:
        pass
    try:
        back = page.get_by_role("button", name=re.compile(r"^\s*BACK\s*$", re.I))
        if back.count() > 0 and page.locator('input[placeholder="Search Here"]').count() == 0:
            back.first.click()
            page.wait_for_selector('input[placeholder="Search Here"]', timeout=15000)
    except Exception:
        pass
    if page.locator('input[placeholder="Search Here"]').count() == 0:
        try:
            do_warmup(page)
        except Exception:
            pass


# ----------------------------------------------------------------------------
# main
# ----------------------------------------------------------------------------
# ----------------------------------------------------------------------------
# portal claim report (uploaded in the panel, matched there -> ALREADY_ON_PORTAL in the records file)
# ----------------------------------------------------------------------------
def read_claim_report(path) -> dict:
    """zip of xlsx part(s) -> {account: (claim no, user)} for approved IS rollover claims on 2024-25 loans."""
    import io
    import zipfile
    import openpyxl
    found = {}
    path = Path(path)
    if path.suffix.lower() == ".xlsx":                   # a report saved by hand, already unzipped
        parts = [path.read_bytes()]
    else:
        with zipfile.ZipFile(path) as z:
            parts = [z.read(n) for n in z.namelist() if n.lower().endswith(".xlsx")]
    if True:
        for data in parts:
            wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True)
            it = wb.active.iter_rows(values_only=True)
            head = [str(c or "").strip() for c in next(it)]
            col = {k: head.index(k) for k in ("Claim Number", "Account Number", "Is Rollover", "Loan application FY",
                                              "Application Status", "User Name")}
            for row in it:
                acct = row[col["Account Number"]]
                acct = str(int(acct)) if isinstance(acct, float) else str(acct or "").strip()
                if (acct and str(row[col["Is Rollover"]] or "").strip().upper() == "YES"
                        and str(row[col["Loan application FY"]] or "").strip() == FIN_YEAR
                        and str(row[col["Application Status"]] or "").strip().lower() == "approved"):
                    found[acct] = (str(row[col["Claim Number"]] or "").strip(), str(row[col["User Name"]] or "").strip())
            wb.close()
    return found


def main(limit: int, assume_yes: bool, only: str = ""):
    f.INPUT_CSV = f.OUTPUT_CSV
    header, rows, idx = f.load_rows()
    a_i, is_i = idx[f.COL_ACCT], idx.get(COL_INTSUB)
    d_i, r_i = idx.get(COL_DISB), idx.get(COL_REPAY)
    if None in (is_i, d_i, r_i):
        raise SystemExit(f"work list needs the columns '{COL_INTSUB}', '{COL_DISB}', '{COL_REPAY}'")
    recs = load_records()
    held = [a for a, r in recs.items() if r.startswith("CLAIM_CHECK_PORTAL")]
    todo, bad = [], []
    for r in rows[1:]:
        acct = r[a_i].strip()
        if (not acct or (only and acct != only) or money_to_float(r[is_i]) <= 0 or recs.get(acct) in DONE
                or (recs.get(acct) or "").startswith("CLAIM_CHECK_PORTAL")):
            continue
        disb, repay = date_in(r[d_i]), date_in(r[r_i])
        if not disb or not repay:
            bad.append(acct)
            continue
        todo.append((acct, money_to_float(r[is_i]), disb, repay))
    print(f"[additional] {RECORDS_CSV}: {len(recs)} accounts logged; {len(rows) - 1} in the work list -> "
          f"{len(todo)} to do")
    if bad:
        print(f"[additional] {len(bad)} rows without a readable Disb. Date / Repay. Date -> skipped: {', '.join(bad[:10])}")
    if held:
        print(f"[additional] {len(held)} CLAIM_CHECK_PORTAL -> NOT retried, check on the portal: {', '.join(held[:10])}")

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
            print("\n  IS ADDITIONAL (rollover) claims will be FILED (row with start = Disb. Date and end 31/03/2025,"
                  " rollover date = Repay. Date, IS = Int Sub Amt or the max allowed if lower). Cannot be undone.")
            while (a := input("  >> type yes to file, no to stop: ").strip().lower()) not in ("yes", "no"):
                pass
            if a == "no":
                ctx.close()
                return

        do_warmup(page)
        claimed = walked = errors = 0
        cnt = {}
        for acct, intsub, disb, repay in todo:
            if os.path.exists(f.STOP_FLAG):
                print("\n[stop] stop requested — stopping between accounts")
                break
            if limit and claimed >= limit:
                break
            walked += 1
            tag = f"[{walked:>4}/{len(todo)}] {acct}"
            for attempt in range(1, f.STUCK_RETRIES + 2):
                stage = {"s": "start"}

                def mark(where, fd, rd, s, m, c, acct=acct):
                    log(acct, "", fd, rd, s, m, intsub, c, f"CLAIM_CHECK_PORTAL:{where}",
                        "claim saved as draft" if where == "draft" else "CONFIRM clicked, result not recorded yet")
                print(f"\r{tag}  working...   ", end="", flush=True)
                try:
                    st, no, fd, rd, s, m, c, detail = process_account(page, acct, intsub, disb, repay, stage, mark)
                    log(acct, no, fd, rd, s, m, intsub, c, st, detail)
                    cnt[st] = cnt.get(st, 0) + 1
                    claimed += st == "CLAIMED"
                    print(f"\r{tag}  {st}  {('claim ' + no + '  ') if no else ''}{detail}"
                          f"   | claimed {claimed}  errors {errors}", flush=True)
                    break
                except KeyboardInterrupt:
                    raise
                except Exception as e:
                    import traceback
                    with open("errors.log", "a") as ef:
                        ef.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} ADDITIONAL acct={acct} stage={stage['s']} url={page.url}\n")
                        ef.write(traceback.format_exc())
                    msg = str(e).splitlines()[0][:100]
                    shot(page, f"add_error_{acct}")
                    print(f"\n    [exc] stage={stage['s']} {type(e).__name__}: {msg}", flush=True)
                    if stage["s"] in ("draft", "submitting"):     # saved / confirmed: never again blindly
                        errors += 1
                        print(f"{tag}  CLAIM_CHECK_PORTAL — check this claim on the portal by hand")
                        if f.is_logged_out(page):
                            f.require_login(page)
                        _recover(page)
                        break
                    if f.is_logged_out(page):
                        f.require_login(page)
                        do_warmup(page)
                        continue
                    _recover(page)
                    if attempt > f.STUCK_RETRIES:
                        errors += 1
                        log(acct, "", "", "", "", "", intsub, "", f"CLAIM_ERROR:{type(e).__name__}", msg)
                        print(f"{tag}  CLAIM_ERROR  {msg}")
        print(f"\n[done] {claimed} claimed, {walked} walked, {errors} errors, {cnt}.  Log: {RECORDS_CSV}")
        if not f.NO_PROMPT:
            input("  >> press ENTER to close the browser: ")
        ctx.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="File Fasalrin IS additional (rollover) claims (Branch User login).")
    ap.add_argument("csv", help="IS additional work list, e.g. branches/3115/additional/3115_additional.csv")
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
        print("\n[quit] interrupted — claims so far are recorded; rerun to continue")
