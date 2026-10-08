#!/usr/bin/env python3
"""
fasalrin_regular.py
-------------------
Automates the IS "regular" (new-FY) Loan Application entry on
https://fasalrin.gov.in for the accounts in 3119_IS_REG.csv, using Playwright.
Recorded from a live walkthrough on 2026-09-08.

FLOW (per CSV row, exactly what the branch user does by hand)
  1. side nav -> Loan Application -> popup "Fetch record by Aadhaar Number"
     Financial Year = 2025-2026, Aadhaar = CSV "Aadhaar No.", FETCH RECORD
  2. popup answers:
       "Beneficiary details exist in the system" -> Account numbers dropdown:
            pick the CSV "Account No."  (not in the list -> ACCT_NOT_IN_DROPDOWN, BACK TO DASHBOARD)
            -> OK
       "Kindly click OK and enter farmer-details" -> NOT_IN_SYSTEM (OK, re-open popup)
  3. Applicant Details : Application Type = Normal -> UPDATE & CONTINUE
  4. Account Details   : UPDATE & CONTINUE
  5. Financial Details : KCC loan sanctioned on = CSV "Disb. Date" (calendar click)
                         drawing limit          = CSV "DP"
                         sanction eligibility   = raised to DP if it is below DP, else untouched
                         -> SAVE & CONTINUE
  6. Activity          : Loan Sanctioned (INR) = DP (same as drawing limit) -> SAVE & CONTINUE
  7. Term Loan         : must show Term Loan For Current FY = 0.00 -> PREVIEW
  8. Preview page      : verify account / Aadhaar / date / amounts -> SUBMIT -> CONFIRM
                         -> "Loan application <no> submitted successfully" -> OK
  9. write status + app no to the CSV, next row.

SAFETY
  * DRY_RUN = True: does everything up to the preview page, verifies it, then presses
    BACK and never submits.  DRY_RUN = False really submits.
  * PAUSE_BEFORE_SUBMIT = True: shows the preview values and waits for ENTER.
  * SUBMIT_LIMIT stops the run after that many REAL submissions (0 = no limit: runs
    until the CSV is done or you press Ctrl+C).
  * Screen stuck (no response for STUCK_TIMEOUT_S seconds) -> page reload, back to dashboard, row retried
    (STUCK_RETRIES times); stuck after CONFIRM -> CHECK_PORTAL, never retried blindly.
  * Re-running resumes: rows whose Status is in DONE_STATES are skipped, untouched rows are done next
    (in CSV order), earlier ERROR:* / SKIPPED rows are retried at the end. CHECK_PORTAL:* rows are
    never retried (status is saved as CHECK_PORTAL just before CONFIRM, so even a crash can't re-submit).
    To force one to run again after checking the portal, set its Status to RETRY.
  * Entries made outside the script (by hand / other users): upload the portal's Approved /
    Pending-for-approval report in the control panel (Portal reports card) and press Match; matched rows
    become ALREADY_ON_PORTAL in the work list + progress file and are never entered. The script itself
    never downloads reports. The preview page is checked too: an application already
    Submitted/Approved/Pending is never submitted again.
  * --start-row N: only rows N and later this session (row 1 = header).
  * Every entry is also appended to <input>_progress.csv (fsync'd, before the CSV is saved). At start
    that file is read first and fills in any status the CSV is missing, so progress survives a crash,
    a locked CSV or the CSV being replaced with an older copy.
  * CSV is written after EVERY row (a .bak is kept), so a crash never loses progress.
  * Any unexpected page -> screenshot in ./screenshots + Status ERROR:*, move on.

You log in yourself (mobile + password + captcha). The script waits.
"""

from __future__ import annotations

import argparse
import collections
import csv
import io
import os
import re
import time
from datetime import date
from pathlib import Path

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

# ----------------------------------------------------------------------------
# CONFIG  — edit these
# ----------------------------------------------------------------------------
INPUT_CSV   = "3119_IS_REG.csv"      # default; override on the command line: python fasalrin_regular.py <file.csv>
OUTPUT_CSV  = INPUT_CSV              # same file = update in place (a .bak is kept)
PROGRESS_CSV = ""                    # append-only log of every entry: <input>_progress.csv (set at start)
START_ROW   = 0                      # --start-row N: only rows N.. this session (0 = all)
RETRY_REVERIFY = False               # --retry-reverify: AADHAAR_REVERIFY rows not retried before go once more
ONLY_REVERIFY = False                # --only-reverify: this run does only those rows (new rows wait)
REVERIFY_RETRY_DETAIL = "Aadhaar reverify: retried once"
REVERIFY_RETRIED = "(retried once)"  # added to Detail when the retry hits AADHAAR_REVERIFY again: never again
STOP_FLAG   = "stop.flag"            # file created by the control panel -> stop between rows
REPORTS_DIR = Path("reports")        # portal reports (farmer PII: local only, gitignored)
REPORT_TIMEOUT_S = 120               # wait this long for a fresh report, then use the newest saved one
REPORT_REFRESH_H = 2                 # "yes" -> download again every this many hours during the run
# portal "Application Status" filter values that mean "already submitted" -> never enter again.
# REJECTED / REVIEW REQUIRED are deliberately not here: those accounts are entered again.
PORTAL_REPORTS = {"approved": "APPROVED", "pending": "PENDING FOR APPROVAL(SUBMITTED)"}
FIN_YEAR    = "2025-2026"
DRY_RUN     = False                  # <<< True = fill + verify preview, never submit.  False = really submit.
PAUSE_BEFORE_SUBMIT = False          # <<< True = show preview values + wait for ENTER before each submit
SUBMIT_LIMIT = 0                     # <<< stop after this many REAL submissions; 0 = no limit (run until Ctrl+C / CSV done)
MAX_ITER    = 0                      # safety: max rows walked in one run (incl. skips); 0 = no limit
STUCK_TIMEOUT_S = 60                 # <<< seconds a screen may take to respond before it counts as stuck
STUCK_RETRIES = 2                    # screen stuck (a wait timed out) -> reload + dashboard + retry the row this many times
HEADLESS    = False
PROFILE_DIR = ".pw_profile"
SLOWMO_MS   = 30
NO_PROMPT   = os.environ.get("NO_PROMPT") == "1"   # 1 = never wait for ENTER; poll for login instead
SKIP_DP_ZERO = True                  # rows with DP 0 / blank -> DP_ZERO, never entered

BASE = "https://fasalrin.gov.in"
SHOTS = Path("screenshots"); SHOTS.mkdir(exist_ok=True)

COL_SOL    = "Sol ID"                # in work lists built from the master (branches.py)
COL_ACCT   = "Account No."
COL_AADH   = "Aadhaar No."
COL_DISB   = "Disb. Date"            # dd-mm-yyyy
COL_DP     = "DP"
COL_STATUS = "Status"
COL_DL     = "Drawing Limit (entered)"
COL_APPNO  = "Loan App No"
COL_DETAIL = "Detail"

DONE_STATES = {"COMPLETED", "ALREADY_ON_PORTAL", "OTHER_BRANCH", "NOT_IN_SYSTEM", "ACCT_NOT_IN_DROPDOWN", "NO_ACCT_DROPDOWN", "SCHEME_MISMATCH", "DRY_OK",
               "NO_AADHAAR", "DP_ZERO", "NO_ACTIVITY", "AADHAAR_REVERIFY", "APPLICANT_INCOMPLETE"}
# CONFIRM was clicked but the result is unknown -> never re-entered automatically (could double-submit).
# Check these on the portal by hand, then set Status to COMPLETED (with app no) or clear it to retry.
HOLD_PREFIX = "CHECK_PORTAL"


RETRY_STATUS = "RETRY"               # set by hand in the CSV to force a row to run again (ignores the progress file)


def is_done(status: str) -> bool:
    """Skipped on the next run (finished, needs hand work, or held for a portal check)."""
    s = status.strip().upper()
    return s in DONE_STATES or s.startswith(HOLD_PREFIX)


# "finished" = the application is on the portal (submitted for approval or approved). Every other
# skipped state (NOT_IN_SYSTEM, AADHAAR_REVERIFY, NO_AADHAAR, DP_ZERO, ...) needs work by hand.
FINISHED_STATES = {"COMPLETED", "ALREADY_ON_PORTAL"}


def bucket(status: str) -> str:
    """finished | hand (skipped, needs work by hand) | check (CHECK_PORTAL) | todo (next run does it)."""
    s = status.strip().upper()
    if s in FINISHED_STATES:
        return "finished"
    if s.startswith(HOLD_PREFIX):
        return "check"
    return "hand" if is_done(s) else "todo"

MONTHS = ["January", "February", "March", "April", "May", "June",
          "July", "August", "September", "October", "November", "December"]

BTN = lambda name: re.compile(rf"^\s*{name}\s*$", re.I)


# ----------------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------------
def parse_dmy(s: str) -> date:
    d, m, y = (int(x) for x in re.split(r"[-/]", s.strip()))
    return date(y, m, d)


def money_to_float(s: str) -> float:
    return float(re.sub(r"[^\d.]", "", s or "0") or 0)


def load_rows():
    raw = open(INPUT_CSV, "rb").read().decode("utf-8-sig")
    rows = list(csv.reader(io.StringIO(raw, newline="")))
    header = rows[0]
    for col in (COL_STATUS, COL_DL, COL_APPNO, COL_DETAIL):
        if col not in header:
            header.append(col)
    for r in rows[1:]:
        while len(r) < len(header):
            r.append("")
    idx = {c: header.index(c) for c in header}
    return header, rows, idx


def save_rows(header, rows):
    tmp = OUTPUT_CSV + ".tmp"
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows[1:])
    if os.path.exists(OUTPUT_CSV) and not os.path.exists(OUTPUT_CSV + ".bak"):
        try:
            os.replace(OUTPUT_CSV, OUTPUT_CSV + ".bak")
        except OSError:
            pass
    # the CSV is the resume record: never give up on writing it (e.g. it is open in Excel -> locked)
    warned = False
    while True:
        try:
            os.replace(tmp, OUTPUT_CSV)
            if warned:
                print("  [csv] saved", flush=True)
            return
        except PermissionError:
            if not warned:
                print(f"\n  !! cannot write {OUTPUT_CSV} (open in Excel?) — close it; retrying every 5 s", flush=True)
                warned = True
            time.sleep(5)


PROGRESS_HEADER = ["Time", COL_ACCT, COL_STATUS, COL_DL, COL_APPNO, COL_DETAIL]


def log_progress(acct, status, dl, app_no, detail):
    """Append one entry to the progress file and force it to disk (survives crashes / a locked CSV)."""
    new = not os.path.exists(PROGRESS_CSV)
    with open(PROGRESS_CSV, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if new:
            w.writerow(PROGRESS_HEADER)
        w.writerow([time.strftime("%Y-%m-%d %H:%M:%S"), acct, status, dl, app_no, detail])
        f.flush()
        os.fsync(f.fileno())


def load_progress() -> dict:
    """account -> latest (status, dl, app_no, detail) from the progress file."""
    last = {}
    if not os.path.exists(PROGRESS_CSV):
        return last
    with open(PROGRESS_CSV, newline="", encoding="utf-8-sig") as f:
        for e in csv.DictReader(f):
            acct = (e.get(COL_ACCT) or "").strip()
            if acct and (e.get(COL_STATUS) or "").strip():
                last[acct] = (e[COL_STATUS].strip(), e.get(COL_DL) or "", e.get(COL_APPNO) or "", e.get(COL_DETAIL) or "")
    return last


def shot(page, name):
    try:
        page.screenshot(path=str(SHOTS / f"{name}_{int(time.time())}.png"), full_page=True)
    except Exception:
        pass


def body_text(page) -> str:
    try:
        return page.evaluate("() => document.body.innerText")
    except Exception:
        return ""


def modal_text(page) -> str:
    try:
        return page.evaluate("""() => [...document.querySelectorAll('.modal-content')]
            .filter(m => m.offsetParent !== null)
            .map(m => m.innerText.replace(/\\s+/g,' ')).join(' | ')""")
    except Exception:
        return ""


def is_logged_out(page) -> bool:
    try:
        return page.evaluate("""() => {
            const pw = document.querySelector('input[type="password"]');
            const login = !!pw && pw.offsetParent !== null;
            const menu  = !!document.querySelector('ul#menu')
                       || !!document.querySelector('a[href="/loan-application-form"]')
                       || [...document.querySelectorAll('a,button,span')]
                            .some(e => /^\s*Logout\s*$/i.test(e.textContent || ''));
            return login || !menu;
        }""")
    except Exception:
        return False


def require_login(page, reason="Session logged out"):
    print(f"\n  !! {reason}. In the browser: LOG IN (mobile + password + captcha).", flush=True)
    while True:
        if NO_PROMPT:
            for _ in range(20):                 # ~10 s between "still waiting" lines
                page.wait_for_timeout(500)
                if not is_logged_out(page):
                    page.wait_for_timeout(1500)
                    print("  [login] session is back — continuing", flush=True)
                    return
            print("  ... still waiting for login", flush=True)
            continue
        input("  >> press ENTER once you are logged in (any page): ")
        page.wait_for_timeout(500)
        if not is_logged_out(page):
            return


class StuckError(RuntimeError):
    """The screen did not reach the expected state in time."""


def wait_for(page, fn, seconds, what):
    """Poll fn() every 150 ms until truthy; raise StuckError after `seconds`."""
    for _ in range(int(seconds * 1000 / 150)):
        try:
            v = fn()
        except Exception:
            v = None
        if v:
            return v
        page.wait_for_timeout(150)
    raise StuckError(f"timeout waiting for {what}")


def click_side_nav(page, href):
    """SPA routerLink click (never page.goto — a hard load logs the session out)."""
    a = page.locator(f'ul#menu a[href="{href}"]')
    if a.count() == 0:
        a = page.locator(f'a[href="{href}"]')
    a.first.evaluate("el => el.click()")


def dismiss_ok_dialogs(page):
    """Click OK / CANCEL on any visible generic modal (not the fetch popup)."""
    for _ in range(3):
        b = page.locator('.modal-content:visible button', has_text=BTN("OK"))
        if b.count() == 0:
            b = page.locator('.modal-content:visible button', has_text=BTN("CANCEL"))
        if b.count() == 0:
            return
        try:
            b.first.click()
            page.wait_for_timeout(400)
        except Exception:
            return


# ----------------------------------------------------------------------------
# popup: Fetch record by Aadhaar
# ----------------------------------------------------------------------------
def popup_visible(page) -> bool:
    # must be the Aadhaar box INSIDE the fetch modal - the blank new-farmer form has one too
    return page.locator('.modal-content:visible input[name="aadharNumber"]').count() > 0 \
        and page.locator('.modal-content:visible select[name="financialYear"]').count() > 0


def open_fetch_popup(page):
    """Get the 'Fetch record by Aadhaar Number' popup on screen."""
    if popup_visible(page):
        return
    dismiss_ok_dialogs(page)
    if popup_visible(page):
        return
    # side nav: Dashboard first (so the Loan Application route re-mounts), then Loan Application
    if "/loan-application-form" in page.url:
        click_side_nav(page, "/dashboard")
        page.wait_for_timeout(800)
    click_side_nav(page, "/loan-application-form")
    try:
        wait_for(page, lambda: popup_visible(page), STUCK_TIMEOUT_S, "fetch popup")
    except RuntimeError:
        if is_logged_out(page):
            require_login(page)
            return open_fetch_popup(page)
        raise


def fetch_record(page, aadhaar: str) -> str:
    """Fill FY + Aadhaar, click FETCH RECORD, return 'exists' | 'not_in_system' | other text."""
    fy = page.locator('.modal-content:visible select[name="financialYear"]').first
    for _ in range(5):
        fy.select_option(FIN_YEAR)
        page.wait_for_timeout(200)
        if fy.evaluate("s => s.value") == FIN_YEAR:
            break
    box = page.locator('.modal-content:visible input[name="aadharNumber"]').first
    box.click()
    box.fill("")
    box.fill(aadhaar)
    box.dispatch_event("input"); box.dispatch_event("change")
    page.locator('.modal-content:visible button', has_text=BTN("FETCH RECORD")).first.click()

    def outcome():
        t = modal_text(page)
        if re.search(r"Beneficiary details exist", t, re.I):
            return "exists"
        if re.search(r"Kindly click OK and enter farmer-details", t, re.I):
            return "not_in_system"
        if t and "Fetch record by Aadhaar" not in t:
            return "other:" + t[:160]
        return None
    return wait_for(page, outcome, STUCK_TIMEOUT_S, "FETCH RECORD response")


def choose_account(page, acct: str):
    """True = CSV account picked, False = not offered, None = popup has no account dropdown at all."""
    sel = page.locator('.modal-content:visible select[name="accountNumbers"]').first
    try:
        sel.wait_for(timeout=8000)
    except PWTimeout:
        # "Beneficiary details exist" with only BACK TO DASHBOARD / OK and no account list
        if page.locator('.modal-content:visible button', has_text=BTN("BACK TO DASHBOARD")).count():
            return None
        raise
    opts = sel.evaluate("s => [...s.options].map(o => o.value.trim())")
    if acct not in opts:
        print(f"\n    dropdown accounts: {opts}")
        return False
    sel.select_option(acct)
    page.wait_for_timeout(200)
    sel.dispatch_event("change")
    return True


# ----------------------------------------------------------------------------
# portal reports: applications already submitted (by this script, by hand, by other users)
# ----------------------------------------------------------------------------
def read_report(path) -> dict:
    """zip of xlsx part(s) -> {account: (application id, application status)}."""
    import zipfile
    import openpyxl
    found = {}
    with zipfile.ZipFile(path) as z:
        for name in z.namelist():
            if not name.lower().endswith(".xlsx"):
                continue
            wb = openpyxl.load_workbook(io.BytesIO(z.read(name)), read_only=True)
            it = wb.active.iter_rows(values_only=True)
            head = [str(c or "").strip() for c in next(it)]
            a, i, s = head.index("Account Number"), head.index("Application ID"), head.index("Application Status")
            for row in it:
                acct = str(row[a] or "").strip()
                if acct:
                    found[acct] = (str(row[i] or "").strip(), str(row[s] or "").strip())
    return found


def branch_of_list(rows, idx) -> str:
    """Branch Name of a work list built from the master (empty for plain CSVs)."""
    if "Branch Name" not in idx:
        return ""
    names = collections.Counter(r[idx["Branch Name"]].strip() for r in rows[1:] if r[idx["Branch Name"]].strip())
    return names.most_common(1)[0][0] if names else ""


def same_branch(portal: str, listed: str) -> bool:
    """Portal 'KIDIA' vs master 'Kidia': letters only, case-insensitive, one may be a prefix of the other.
    Spellings that differ only in vowels are the same branch too (portal 'SHEHRA' = master 'Shahera')."""
    # w = v in transliterated Gujarati names (portal 'LUNAVADA' = master 'Lunawada')
    a, b = (re.sub(r"[^a-z]", "", s.lower()).replace("w", "v") for s in (portal, listed))
    if not (a and b):
        return False
    ca, cb = (re.sub(r"[aeiouy]", "", s) for s in (a, b))
    return a.startswith(b) or b.startswith(a) or (len(ca) >= 2 and ca == cb)


def logged_in_role(page) -> str:
    """'Branch User' / 'Branch Head' / ... from the header 'User Category : <role>' ('' if not shown)."""
    try:
        t = wait_for(page, lambda: re.search(r"User Category\s*:\s*([A-Za-z ]+?)\s*(?:\n|$)", body_text(page)), 15,
                     "user category in the header")
        return t.group(1).strip()
    except Exception:
        return ""


def require_role(page, want: str) -> bool:
    """True when the portal session is the `want` role (e.g. 'Branch User'); prints why not otherwise."""
    got = logged_in_role(page)
    if got and got.lower() != want.lower():
        print(f"\n  !! WRONG LOGIN ROLE: the portal is logged in as '{got}', this job needs the {want} login."
              f"\n     Nothing done. Log out, log in with the {want} login and start again.", flush=True)
        return False
    print(f"[login] role check: portal '{got or '?'}' = {want}", flush=True)
    return True


def logged_in_branch(page) -> str:
    """Branch the portal session belongs to, from the dashboard header 'Welcome <bank> (KIDIA, Branch)'."""
    try:
        click_side_nav(page, "/dashboard")
        t = wait_for(page, lambda: re.search(r"\(([^(),]+),\s*Branch\)", body_text(page)), 15, "dashboard branch name")
        return t.group(1).strip()
    except Exception:
        return ""





# ----------------------------------------------------------------------------
# form tabs
# ----------------------------------------------------------------------------
def active_pane(page) -> str:
    try:
        return page.evaluate("() => (document.querySelector('.tab-pane.active') || {}).id || ''")
    except Exception:
        return ""


def validation_msgs(page) -> str:
    """Red 'Please select/enter ...' messages visible in the active tab pane."""
    try:
        return page.evaluate("""() => [...document.querySelectorAll('.tab-pane.active *')]
            .filter(e => e.children.length === 0 && e.offsetParent !== null
                      && /^\\s*Please (select|enter|provide|verify)/i.test(e.textContent || ''))
            .map(e => e.textContent.trim()).join('; ')""")
    except Exception:
        return ""


def wait_pane(page, n: int, seconds=STUCK_TIMEOUT_S, retry_btn=None):
    """Wait for tab pane n. Portal 'Warning: ... Please verify' modals are acknowledged with OK
    (then retry_btn, e.g. SAVE & CONTINUE, is clicked again if the pane still hasn't changed)."""
    state = {"warned": 0}
    def ready():
        if active_pane(page) == f"formTabs-tabpane-{n}":
            return "ok"
        mt = modal_text(page)
        # "Primary Activity is 'Animal Husbandry' Please enter an activity under 'Animal Husbandry' to proceed."
        sm = re.search(r"Primary Activity is .{0,80}?Please enter an activity under [^.]{0,80}", mt or "", re.I) \
            or re.search(r"Primary Activity is .{0,80}?Please enter an activity under [^.]{0,80}", body_text(page), re.I)
        if sm:
            return "scheme:" + re.sub(r"\s+", " ", sm.group(0)).strip()
        if mt and re.search(r"warning|please verify", mt, re.I) and state["warned"] < 3:
            state["warned"] += 1
            print(f"\n    portal warning acknowledged: {mt[:110]}", flush=True)
            page.locator('.modal-content:visible button', has_text=BTN("OK")).first.click()
            page.wait_for_timeout(1500)
            if active_pane(page) != f"formTabs-tabpane-{n}" and retry_btn is not None:
                retry_btn.click()
                page.wait_for_timeout(500)
            return None
        v = validation_msgs(page)
        if v:
            return "validation:" + v
        return None
    r = wait_for(page, ready, seconds, f"tab pane {n}")
    if r != "ok":
        raise RuntimeError(f"portal {r}")
    page.wait_for_timeout(300)


def pane(page, n: int):
    return page.locator(f"#formTabs-tabpane-{n}")


def name_orders(name: str) -> list[str]:
    """Name orders tried for Aadhaar (re)verify (user rule), surname = last word of the name given:
    as given -> surname first middle -> surname first -> first surname. Duplicates dropped."""
    w = (name or "").split()
    out = [" ".join(w)]
    if len(w) >= 2:
        first, sur, mid = w[0], w[-1], w[1:-1]
        out += [" ".join([sur, first, *mid]), f"{sur} {first}", f"{first} {sur}"]
    return list(dict.fromkeys(x for x in out if x))


def reverify_aadhaar(page) -> str | None:
    """Applicant tab shows REVERIFY (Aadhaar must be re-verified): REVERIFY -> 'Verify Aadhaar Number' dialog ->
    VERIFY -> verified when REVERIFY and VERIFY are gone (as the user showed on a sample account). When the
    portal says the name does not match, the dialog's Name (As per Aadhaar) is tried in the other orders
    (surname first middle, surname first, first surname). None = nothing to do / verified; otherwise why not."""
    rev = page.locator("button:visible", has_text=BTN("REVERIFY"))
    if not rev.count():
        return None
    print("\n    Aadhaar REVERIFY shown -> REVERIFY + VERIFY", flush=True)
    rev.first.click()
    ver = page.locator("button:visible", has_text=BTN("VERIFY"))
    try:
        wait_for(page, lambda: ver.count(), 15, "VERIFY in the reverify dialog")
    except StuckError:
        return "REVERIFY clicked but no VERIFY button appeared"
    dlg = page.locator(".modal-content:visible").last
    name_in = dlg.locator('input[name="beneficiaryName"]')
    if not name_in.count():                       # the dialog's first box is Name (As per Aadhaar)
        name_in = dlg.locator("input:not([type=hidden])")
    base = name_in.first.input_value().strip() if name_in.count() else ""
    verified = lambda: not page.locator("button:visible", has_text=BTN("REVERIFY")).count() \
        and not page.locator("button:visible", has_text=BTN("VERIFY")).count()
    tried, msg = [], ""
    for cand in name_orders(base) or [""]:
        if cand and cand != base:
            try:
                name_in.first.fill(cand)
                name_in.first.dispatch_event("input"); name_in.first.dispatch_event("change")
            except Exception:
                break                             # name box not editable: no other order can be tried
        tried.append(cand or "(as shown)")
        if not ver.count():
            break
        ver.last.click()

        def answer():
            if verified():
                return "ok"
            t = modal_text(page) or ""
            return "mismatch" if re.search(r"not\s+match|mismatch", t, re.I) else None
        try:
            got = wait_for(page, answer, 30, "Aadhaar to be verified")
        except StuckError:
            got = "timeout"
        if got == "ok":
            if cand and cand != base:
                print(f"    Aadhaar reverified with the name as {cand!r}", flush=True)
            page.wait_for_timeout(500)
            return None
        msg = re.sub(r"\s+", " ", modal_text(page) or "")[:120]
        # a separate alert (OK) on top of the dialog: close it, keep the dialog for the next order
        ok = page.locator(".modal-content:visible button", has_text=BTN("OK"))
        if ok.count():
            ok.last.click()
            page.wait_for_timeout(400)
        if got != "mismatch":
            break                                 # not a name problem: other orders will not help
    for b in ("CLOSE", "OK"):
        c = page.locator("button:visible", has_text=BTN(b))
        if c.count():
            c.first.click()
            page.wait_for_timeout(500)
            break
    return (f"REVERIFY + VERIFY did not verify (tried {', '.join(tried)})"
            + (f": {msg}" if msg else ""))[:240]


def fill_amount(page, locator, value):
    locator.click()
    locator.fill("")
    locator.fill(str(value))
    locator.dispatch_event("input")
    locator.dispatch_event("change")
    locator.press("Tab")
    page.wait_for_timeout(200)


def pick_calendar_date(page, date_input, target: date):
    """Open the rmdp picker attached to `date_input` and click `target`."""
    date_input.click()
    page.wait_for_selector(".rmdp-header-values", timeout=8000)
    tgt = (target.year, target.month)
    for _ in range(30):
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
    ok = page.evaluate(
        """(day) => {
            for (const d of document.querySelectorAll('.rmdp-day')) {
              const sp = d.querySelector('span');
              if (!sp || sp.textContent.trim() !== String(day)) continue;
              if (d.classList.contains('rmdp-day-hidden')) continue;
              if (d.classList.contains('rmdp-disabled')) return 'disabled';
              sp.click();
              return 'ok';
            }
            return 'notfound';
        }""", target.day)
    if ok != "ok":
        raise RuntimeError(f"date {target:%d/%m/%Y} not selectable ({ok})")
    want = f"{target:%d/%m/%Y}"
    wait_for(page, lambda: date_input.input_value().strip() == want, 5, f"date {want} to register")


PRIMARY_ACTIVITY_DEFAULT = "Agri Crops"   # picked when the portal leaves Primary Activity blank


def ensure_primary_activity(page):
    """Applicant tab: if 'Primary Activity' dropdown is unselected, choose PRIMARY_ACTIVITY_DEFAULT."""
    handle = page.evaluate_handle("""() => {
        const lab = [...document.querySelectorAll('#formTabs-tabpane-1 label')]
            .find(l => /^\\s*Primary Activity/i.test(l.textContent || ''));
        if (!lab) return null;
        const grp = lab.closest('.form-group') || lab.parentElement;
        return grp ? grp.querySelector('select') : null;
    }""")
    el = handle.as_element()
    if el is None:
        return
    cur = el.evaluate("s => (s.options[s.selectedIndex] || {}).text || ''").strip()
    if cur and cur.lower() != "select":
        return
    el.select_option(label=PRIMARY_ACTIVITY_DEFAULT)
    el.dispatch_event("input"); el.dispatch_event("change")
    page.wait_for_timeout(300)
    got = el.evaluate("s => (s.options[s.selectedIndex] || {}).text || ''").strip()
    if got != PRIMARY_ACTIVITY_DEFAULT:
        raise RuntimeError(f"could not set Primary Activity (now {got!r})")
    print(f"\n    primary activity was blank -> {PRIMARY_ACTIVITY_DEFAULT}", flush=True)


# ----------------------------------------------------------------------------
# process one CSV row -> (status, drawing_limit_entered, app_no, detail)
# ----------------------------------------------------------------------------
def process_row(page, acct: str, aadhaar: str, disb: date, dp: int, stage: dict, mark_submitting=None):
    stage["s"] = "start"
    open_fetch_popup(page)
    res = fetch_record(page, aadhaar)
    stage["s"] = "fetched"

    if res == "not_in_system":
        page.locator('.modal-content:visible button', has_text=BTN("OK")).first.click()
        page.wait_for_timeout(800)
        click_side_nav(page, "/dashboard")          # OK opens a blank new-farmer form -> leave it
        page.wait_for_timeout(800)
        return "NOT_IN_SYSTEM", "", "", "aadhaar not in portal (no record found)"
    if res != "exists":
        shot(page, f"fetch_{acct}")
        raise RuntimeError(f"unexpected FETCH response: {res}")

    picked = choose_account(page, acct)
    if picked is None:
        shot(page, f"noacctlist_{acct}")
        page.locator('.modal-content:visible button', has_text=BTN("BACK TO DASHBOARD")).first.click()
        page.wait_for_timeout(800)
        return "NO_ACCT_DROPDOWN", "", "", "farmer exists but popup has no account list: do by hand"
    if not picked:
        page.locator('.modal-content:visible button', has_text=BTN("BACK TO DASHBOARD")).first.click()
        page.wait_for_timeout(800)
        return "ACCT_NOT_IN_DROPDOWN", "", "", "csv account not offered for this aadhaar"
    page.locator('.modal-content:visible button', has_text=BTN("OK")).first.click()

    # ---- 1. Applicant Details ----
    wait_pane(page, 1)
    page.wait_for_timeout(800)
    why = reverify_aadhaar(page)                  # Aadhaar REVERIFY: the extra step, then the normal flow
    if why:
        shot(page, f"reverify_{acct}")
        click_side_nav(page, "/dashboard")
        page.wait_for_timeout(800)
        return "AADHAAR_REVERIFY", "", "", f"{why}: do by hand"
    at = page.locator('select[name="applicationType"]').first
    at.wait_for()
    for _ in range(5):
        at.select_option(label="Normal")
        page.wait_for_timeout(200)
        if at.evaluate("s => (s.options[s.selectedIndex]||{}).text.trim()") == "Normal":
            break
    at.dispatch_event("change")
    ensure_primary_activity(page)
    pane(page, 1).locator("button", has_text=BTN("UPDATE & CONTINUE")).first.click()
    stage["s"] = "applicant"

    # ---- 2. Account Details ----
    try:
        wait_pane(page, 2)
    except RuntimeError as e:
        if re.search(r"verify Aadhaar", str(e), re.I):
            shot(page, f"reverify_{acct}")
            click_side_nav(page, "/dashboard")
            page.wait_for_timeout(800)
            return "AADHAAR_REVERIFY", "", "", "portal asks REVERIFY Aadhaar on applicant tab: do by hand"
        if str(e).startswith("portal validation:"):
            # applicant tab missing data the branch must key in (state/district/village/...)
            shot(page, f"applicant_{acct}")
            click_side_nav(page, "/dashboard")
            page.wait_for_timeout(800)
            return "APPLICANT_INCOMPLETE", "", "", "applicant tab: " + str(e)[len("portal validation:"):][:120]
        raise
    pane(page, 2).locator("button", has_text=BTN("UPDATE & CONTINUE")).first.click()
    stage["s"] = "account"

    # ---- 3. Financial Details ----
    wait_pane(page, 3)
    fin = page.locator("#finance")
    date_in = fin.locator("input.rmdp-input").first
    elig_in = fin.locator('input[name="loanSanctionAmount"]').first
    dl_in   = fin.locator('input[name="drawingLimit"]').first
    date_in.wait_for()
    elig_in.wait_for()
    dl_in.wait_for()
    # existing farmers: portal prefills eligibility/DL within ~2 s. New farmers: fields stay
    # blank (eligibility 0 -> raised to DP below). Give prefill a short chance, then go on.
    try:
        wait_for(page, lambda: elig_in.input_value().strip() != "" or dl_in.input_value().strip() != "",
                 3, "financial fields to prefill")
    except RuntimeError:
        print("\n    financial fields blank (new farmer) -> eligibility & DL = DP", flush=True)

    pick_calendar_date(page, date_in, disb)
    elig = money_to_float(elig_in.input_value())
    elig_new = elig
    if elig < dp:                              # rule: eligibility below DP -> make both = DP
        elig_new = dp
        fill_amount(page, elig_in, dp)
    fill_amount(page, dl_in, dp)
    # make sure the values stuck (Angular sometimes re-renders)
    wait_for(page, lambda: money_to_float(dl_in.input_value()) == dp and
                           money_to_float(elig_in.input_value()) == elig_new,
             5, "financial amounts to stick")
    fin_save = fin.locator("button", has_text=BTN("SAVE & CONTINUE")).first
    fin_save.click()
    stage["s"] = "financial_saved"

    # ---- 4. Activity ----
    wait_pane(page, 4, retry_btn=fin_save)
    act = page.locator("#activity")
    ls_in = act.locator('input[name="loanSanctionedAmount"]').first
    try:
        ls_in.wait_for(timeout=4000)
    except PWTimeout:
        # new farmer: no activity added. Adding one needs land location / crop / survey no /
        # khata / land area -> manual job. Leave the draft, skip the row with a remark.
        shot(page, f"noactivity_{acct}")
        click_side_nav(page, "/dashboard")
        page.wait_for_timeout(800)
        return "NO_ACTIVITY", "", "", "activity tab empty: add activity (land/crop/survey/khata) by hand"
    act_date = act.locator("input.rmdp-input").first
    act_date.wait_for()
    page.wait_for_timeout(500)
    if act_date.input_value().strip() != f"{disb:%d/%m/%Y}":
        try:
            wait_for(page, lambda: act_date.input_value().strip() == f"{disb:%d/%m/%Y}", 4,
                     "activity sanction date = disbursal date")
        except RuntimeError:
            pick_calendar_date(page, act_date, disb)     # not auto-filled (new farmer) -> pick it
    fill_amount(page, ls_in, dp)
    wait_for(page, lambda: money_to_float(ls_in.input_value()) == dp, 5, "activity amount to stick")
    act_save = act.locator("button", has_text=BTN("SAVE & CONTINUE")).first
    act_save.click()
    stage["s"] = "activity_saved"

    # ---- 5. Term Loan ----
    try:
        wait_pane(page, 5, retry_btn=act_save)
    except RuntimeError as e:
        if str(e).startswith("portal scheme:"):
            # activity does not match the primary activity (e.g. Animal Husbandry) -> by hand
            shot(page, f"scheme_{acct}")
            dismiss_ok_dialogs(page)
            click_side_nav(page, "/dashboard")
            page.wait_for_timeout(800)
            return "SCHEME_MISMATCH", "", "", str(e)[len("portal scheme:"):][:150]
        raise
    t5 = pane(page, 5)
    def term_text():
        t = t5.inner_text()
        return t if "Term Loan For Current FY" in t else None
    txt5 = wait_for(page, term_text, STUCK_TIMEOUT_S, "term loan summary")
    m = re.search(r"Term Loan For Current FY.*?₹\s*([\d,]+\.\d+)", txt5.replace("\n", " "), re.S)
    term = money_to_float(m.group(1)) if m else -1
    if term != 0:
        shot(page, f"termloan_{acct}")
        raise RuntimeError(f"Term Loan For Current FY is {term}, expected 0 (DL != sanctioned?)")
    t5.locator("button", has_text=BTN("PREVIEW")).first.evaluate("el => el.click()")
    stage["s"] = "preview"

    # ---- 6. Preview page: verify everything ----
    wait_for(page, lambda: "/loan-application-preview" in page.url and
                           "Application Status" in body_text(page), STUCK_TIMEOUT_S, "preview page")
    page.wait_for_timeout(400)
    pv = body_text(page).replace("\n", " ")
    pv = re.sub(r"\s+", " ", pv)
    m = re.search(r"Application Status\s*(Submitted|Approved|Pending[\w ()]*)", pv, re.I)
    if m:   # someone submitted it after the report was taken -> never submit again
        shot(page, f"onportal_{acct}")
        page.get_by_role("button", name=BTN("BACK")).first.click()
        page.wait_for_timeout(800)
        return "ALREADY_ON_PORTAL", "", "", f"preview shows status {m.group(1).strip()}: not re-submitted"
    problems = []
    def has(pattern, what):
        if not re.search(pattern, pv):
            problems.append(what)
    has(rf"Account Number\s*{acct}\b", "account number")
    has(rf"Aadhaar No\.\s*XXXX-XXXX-{aadhaar[-4:]}", "aadhaar last-4")
    has(rf"KCC loan sanctioned / KCC renewed on\s*{disb:%d/%m/%Y}", "sanction date")
    def has_amount(label, expected, what):
        # portal prints Indian grouping (₹1,50,000.00) -> compare numerically
        m = re.search(label + r"\s*₹\s*([\d,]+(?:\.\d+)?)", pv)
        if not m or money_to_float(m.group(1)) != float(expected):
            problems.append(f"{what} (got {m.group(1) if m else 'none'}, want {expected})")
    has_amount(r"KCC drawing limit for current FY", dp, "drawing limit")
    has_amount(r"KCC Loan Sanction eligiblity as per SOF", int(elig_new), "eligibility")
    has_amount(r"Loan Sanctioned \(INR\)", dp, "activity loan sanctioned")
    has(rf"Sanction/Rollover Date\s*{disb:%d/%m/%Y}", "activity date")
    detail = f"date={disb:%d/%m/%Y} DL={dp} elig={int(elig)}->{int(elig_new)}"
    if problems:
        shot(page, f"preview_{acct}")
        raise RuntimeError(f"preview mismatch: {', '.join(problems)}")

    if DRY_RUN:
        shot(page, f"dryrun_{acct}")
        page.get_by_role("button", name=BTN("BACK")).first.click()
        page.wait_for_timeout(800)
        return "DRY_OK", str(dp), "", detail

    if PAUSE_BEFORE_SUBMIT:
        ans = input(f"\r    submit {acct}?  {detail}  [ENTER=yes / s=skip / q=quit]: ").strip().lower()
        if ans == "q":
            raise KeyboardInterrupt("user quit")
        if ans == "s":
            page.get_by_role("button", name=BTN("BACK")).first.click()
            page.wait_for_timeout(800)
            return "SKIPPED", str(dp), "", detail

    # ---- 7. SUBMIT -> CONFIRM -> OK ----
    page.get_by_role("button", name=BTN("SUBMIT")).first.click()
    wait_for(page, lambda: re.search(r"sure you want to submit", modal_text(page), re.I), STUCK_TIMEOUT_S, "confirm dialog")
    if mark_submitting:
        mark_submitting()        # CSV says CHECK_PORTAL before CONFIRM: a hard kill now can't cause a re-submit
    page.locator('.modal-content:visible button', has_text=BTN("CONFIRM")).first.click()
    stage["s"] = "submitting"
    def submitted_text():
        t = modal_text(page)
        return t if re.search(r"submitted successfully", t, re.I) else None
    txt = wait_for(page, submitted_text, STUCK_TIMEOUT_S, "submitted-successfully dialog")
    m = re.search(r"Loan application\s*([0-9]+)\s*submitted", txt, re.I)
    app_no = m.group(1) if m else ""
    page.locator('.modal-content:visible button', has_text=BTN("OK")).first.click()
    stage["s"] = "done"
    page.wait_for_timeout(600)
    return "COMPLETED", str(dp), app_no, detail


def _recover(page):
    """Best effort: dismiss dialogs and get back to a state where the popup can be opened."""
    try:
        dismiss_ok_dialogs(page)
    except Exception:
        pass
    try:
        if "/loan-application-preview" in page.url:
            page.get_by_role("button", name=BTN("BACK")).first.click()
            page.wait_for_timeout(800)
    except Exception:
        pass
    try:
        click_side_nav(page, "/dashboard")
        page.wait_for_timeout(1000)
    except Exception:
        pass


def reload_to_dashboard(page):
    """Screen stuck: hard-reload the page and start again from the dashboard.
    A hard load can drop the session -> wait for the user to log in again."""
    try:
        page.reload(wait_until="domcontentloaded", timeout=60000)
    except Exception:
        pass
    try:                                        # let the SPA mount: side menu or login form
        wait_for(page, lambda: page.locator('ul#menu, input[type="password"]').count() > 0,
                 20, "page after reload")
    except RuntimeError:
        pass
    if is_logged_out(page):
        require_login(page, "Logged out after reload")
    try:
        dismiss_ok_dialogs(page)
        click_side_nav(page, "/dashboard")
        page.wait_for_timeout(1000)
    except Exception:
        pass


# ----------------------------------------------------------------------------
# main
# ----------------------------------------------------------------------------
def main():
    header, rows, idx = load_rows()
    a_i, ad_i, d_i, dp_i = idx[COL_ACCT], idx[COL_AADH], idx[COL_DISB], idx[COL_DP]
    s_i, dl_i, no_i, de_i = idx[COL_STATUS], idx[COL_DL], idx[COL_APPNO], idx[COL_DETAIL]

    # progress file first: restore entries the CSV missed (crash, locked file, CSV replaced by an old copy)
    if not os.path.exists(PROGRESS_CSV):         # first run with a progress file: seed it from the CSV
        for r in rows[1:]:
            if r[a_i].strip() and r[s_i].strip():
                log_progress(r[a_i].strip(), r[s_i], r[dl_i], r[no_i], r[de_i])
    progress = load_progress()
    restored = 0
    for r in rows[1:]:
        e = progress.get(r[a_i].strip())
        cur = r[s_i].strip()
        if e and not is_done(cur) and cur.upper() != RETRY_STATUS and cur != e[0]:
            r[s_i], r[dl_i], r[no_i], r[de_i] = e
            restored += 1
    print(f"[progress] {PROGRESS_CSV}: {len(progress)} accounts logged"
          + (f", {restored} rows restored into the CSV" if restored else ", CSV already up to date"))
    if restored:
        save_rows(header, rows)

    def finish(r):
        """Row result -> progress file (first, fsync'd), then the CSV."""
        if r[s_i].strip():
            log_progress(r[a_i].strip(), r[s_i], r[dl_i], r[no_i], r[de_i])
        save_rows(header, rows)

    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            PROFILE_DIR, headless=HEADLESS, slow_mo=SLOWMO_MS,
            viewport=None, args=["--start-maximized"],
        )
        ctx.set_default_timeout(STUCK_TIMEOUT_S * 1000)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(BASE, wait_until="domcontentloaded")

        print("\n" + "=" * 70)
        print("  1. LOG IN in the browser window (mobile + password + captcha).")
        print("  2. Come back here and press ENTER. Don't touch the browser after that.")
        print(f"     DRY_RUN={DRY_RUN}  PAUSE_BEFORE_SUBMIT={PAUSE_BEFORE_SUBMIT}  SUBMIT_LIMIT={SUBMIT_LIMIT}")
        print("=" * 70)
        if NO_PROMPT:
            print("  (NO_PROMPT=1: waiting for the login to appear, no ENTER needed)", flush=True)
            page.wait_for_timeout(2000)
        else:
            input("  >> press ENTER once logged in: ")
        if is_logged_out(page):
            require_login(page, "Not logged in yet")

        # branch work list: the login must be that branch ("Welcome ... (KIDIA, Branch)" on the dashboard)
        want = branch_of_list(rows, idx)
        if want:
            got = logged_in_branch(page)
            if got and not same_branch(got, want):
                print(f"\n  !! WRONG LOGIN: the portal is logged in as branch '{got}', this work list is '{want}'."
                      f"\n     Nothing entered. Log in with the {want} Branch User login and start again.")
                if not NO_PROMPT:
                    input("  >> press ENTER to close the browser: ")
                ctx.close()
                return
            print(f"[login] branch check: portal '{got or '?'}' = work list '{want}'")

        # rows[i] is CSV/Excel row i+1 (row 1 = header); --start-row N leaves rows 2..N-1 alone
        first = max(START_ROW - 1, 1)

        # --retry-reverify: every AADHAAR_REVERIFY row not retried before is tried once more this run
        reverify_retry = set()
        if RETRY_REVERIFY:
            for r in rows[first:]:
                if r[s_i].strip().upper() == "AADHAAR_REVERIFY" and REVERIFY_RETRIED not in r[de_i]:
                    r[s_i], r[de_i] = RETRY_STATUS, REVERIFY_RETRY_DETAIL
                    log_progress(r[a_i].strip(), r[s_i], r[dl_i], r[no_i], r[de_i])
                    reverify_retry.add(r[a_i].strip())
            if reverify_retry:
                save_rows(header, rows)
            print(f"[resume] {len(reverify_retry)} Aadhaar-reverify rows retried once this run")
        # rows an earlier --retry-reverify run already put back (still RETRY): the same one retry
        for r in rows[first:]:
            if r[s_i].strip().upper() == RETRY_STATUS and r[de_i].strip() == REVERIFY_RETRY_DETAIL:
                reverify_retry.add(r[a_i].strip())

        # resume: skip finished rows; untouched rows first (in CSV order), earlier failures retried last
        data = [r for r in rows[first:] if r[a_i].strip()]
        if ONLY_REVERIFY:                          # just the Aadhaar-reverify retries; everything else waits
            data = [r for r in data if r[a_i].strip() in reverify_retry and not is_done(r[s_i])]
            print(f"[resume] only Aadhaar-reverify rows this run: {len(data)}")
        if START_ROW > 2:
            print(f"[resume] starting at row {START_ROW} (rows 2..{START_ROW - 1} not touched this session)")
        fresh = [r for r in data if not r[s_i].strip()]
        retry = [r for r in data if r[s_i].strip() and not is_done(r[s_i])]
        held = [r for r in data if r[s_i].strip().upper().startswith(HOLD_PREFIX)]
        pending = fresh + retry
        total = len(pending)
        b = collections.Counter(bucket(r[s_i]) for r in data)
        print(f"[resume] {len(data)} rows in CSV: {b['finished']} finished (submitted/approved), "
              f"{b['hand']} need hand work (skipped), {b['check']} CHECK_PORTAL, "
              f"{len(fresh)} new, {len(retry)} earlier failures to retry (at the end)")
        if fresh:
            print(f"[resume] continuing at CSV row {rows.index(fresh[0]) + 1} (account {fresh[0][a_i].strip()})")
        if held:
            print(f"[resume] {len(held)} rows CHECK_PORTAL -> NOT retried, verify by hand: "
                  + ", ".join(r[a_i].strip() for r in held[:10]) + (" ..." if len(held) > 10 else ""))
        print(f"[start] {total} rows pending\n")

        submitted = walked = 0
        cnt = {"COMPLETED": 0, "ALREADY_ON_PORTAL": 0, "DRY_OK": 0, "NOT_IN_SYSTEM": 0, "ACCT_NOT_IN_DROPDOWN": 0, "NO_ACCT_DROPDOWN": 0, "SCHEME_MISMATCH": 0,
               "NO_AADHAAR": 0, "DP_ZERO": 0, "NO_ACTIVITY": 0, "AADHAAR_REVERIFY": 0, "APPLICANT_INCOMPLETE": 0, "SKIPPED": 0, "ERROR": 0}
        def tally():
            return "  ".join(f"{k.lower()} {v}" for k, v in cnt.items() if v)

        stop = False
        for r in pending:
            if os.path.exists(STOP_FLAG):              # control panel: "Stop after current row"
                print("\n[stop] stop requested — stopping between rows")
                break
            if stop or (SUBMIT_LIMIT and submitted >= SUBMIT_LIMIT) or (MAX_ITER and walked >= MAX_ITER):
                if SUBMIT_LIMIT and submitted >= SUBMIT_LIMIT:
                    print(f"\n[stop] reached SUBMIT_LIMIT={SUBMIT_LIMIT}")
                break
            walked += 1
            acct = r[a_i].strip()
            aadhaar = re.sub(r"\D", "", r[ad_i])
            dp = int(money_to_float(r[dp_i]))
            try:
                disb = parse_dmy(r[d_i])
            except Exception:
                disb = None

            # ---- pre-checks that need no browser ----
            sol = r[idx[COL_SOL]].strip() if COL_SOL in idx else ""
            if sol and not acct.startswith(sol):        # master work list: never another branch's account
                r[s_i], r[de_i] = "OTHER_BRANCH", f"account does not start with SOL {sol}"
                cnt["ERROR"] += 1
                print(f"[{walked:>4}/{total}] {acct}  OTHER_BRANCH")
                finish(r); continue
            if len(aadhaar) != 12:
                r[s_i], r[de_i] = "NO_AADHAAR", f"aadhaar={r[ad_i].strip()!r}"
                cnt["NO_AADHAAR"] += 1
                print(f"[{walked:>4}/{total}] {acct}  NO_AADHAAR")
                finish(r); continue
            if dp <= 0 and SKIP_DP_ZERO:
                r[s_i], r[de_i] = "DP_ZERO", "DP is 0"
                cnt["DP_ZERO"] += 1
                print(f"[{walked:>4}/{total}] {acct}  DP_ZERO")
                finish(r); continue
            if disb is None:
                r[s_i], r[de_i] = "ERROR:BAD_DATE", f"disb={r[d_i]!r}"
                cnt["ERROR"] += 1
                print(f"[{walked:>4}/{total}] {acct}  ERROR bad disb date {r[d_i]!r}")
                finish(r); continue

            def mark_submitting(r=r):
                r[s_i], r[de_i] = f"{HOLD_PREFIX}:submitting", "CONFIRM clicked, result not recorded yet"
                finish(r)

            attempts = stuck_tries = 0
            while True:
                attempts += 1
                stage = {"s": "start"}
                print(f"\r[{walked:>4}/{total}] {acct}  working...        ", end="", flush=True)
                try:
                    status, dl, app_no, detail = process_row(page, acct, aadhaar, disb, dp, stage, mark_submitting)
                    if status == "AADHAAR_REVERIFY" and acct in reverify_retry:
                        detail = f"{detail} {REVERIFY_RETRIED}"      # the one retry failed too: hand work for good
                    r[s_i], r[dl_i], r[no_i], r[de_i] = status, dl, app_no, detail
                    if status == "COMPLETED":
                        submitted += 1
                    cnt[status if status in cnt else "ERROR"] += 1
                    print(f"\r[{walked:>4}/{total}] {acct}  {status:<21} {detail:<44} "
                          f"app={app_no or '-':<20} | {tally()}")
                    break
                except KeyboardInterrupt:
                    print("\n[quit] stopping at user request")
                    if stage["s"] == "submitting":
                        r[s_i], r[de_i] = "CHECK_PORTAL:submitting", "interrupted after CONFIRM"
                    stop = True
                    break
                except Exception as e:
                    import traceback
                    with open("errors.log", "a") as ef:
                        ef.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} acct={acct} stage={stage['s']} url={page.url}\n")
                        ef.write(traceback.format_exc())
                    try:
                        page.wait_for_timeout(800)
                    except Exception:
                        pass
                    print(f"\n    [exc] stage={stage['s']} url={page.url} {type(e).__name__}: {str(e).splitlines()[0][:160]}", flush=True)
                    if is_logged_out(page):
                        shot(page, f"logout_{acct}")
                        if stage["s"] in ("submitting", "done"):
                            r[s_i], r[de_i] = f"CHECK_PORTAL:{stage['s']}", str(e)[:80]
                            cnt["ERROR"] += 1
                            print(f"\r[{walked:>4}/{total}] {acct}  CHECK_PORTAL  logged out after "
                                  f"{stage['s']} — verify on portal by hand")
                            require_login(page)
                            break
                        if attempts <= 3:
                            print(f"\r[{walked:>4}/{total}] {acct}  logged out during {stage['s']} — retry after login")
                            require_login(page)
                            continue
                    stuck = isinstance(e, (StuckError, PWTimeout))
                    if stuck and stage["s"] in ("submitting", "done"):
                        # CONFIRM was already clicked: never re-enter the row blindly
                        shot(page, f"stuck_{acct}")
                        r[s_i], r[de_i] = f"CHECK_PORTAL:{stage['s']}", str(e)[:80]
                        cnt["ERROR"] += 1
                        print(f"\r[{walked:>4}/{total}] {acct}  CHECK_PORTAL  stuck after "
                              f"{stage['s']} — verify on portal by hand")
                        reload_to_dashboard(page)
                        break
                    if stuck and stuck_tries < STUCK_RETRIES:
                        stuck_tries += 1
                        shot(page, f"stuck_{acct}")
                        print(f"\r[{walked:>4}/{total}] {acct}  stuck at {stage['s']} — reload, "
                              f"retry from dashboard ({stuck_tries}/{STUCK_RETRIES})", flush=True)
                        reload_to_dashboard(page)
                        continue
                    kind = "timeout" if stuck else type(e).__name__
                    shot(page, f"error_{acct}")
                    msg = str(e).splitlines()[0][:90]
                    if stage["s"] in ("submitting", "done"):
                        r[s_i], r[de_i] = f"{HOLD_PREFIX}:{stage['s']}", msg   # never ERROR: would be retried
                    else:
                        r[s_i], r[de_i] = f"ERROR:{kind}", msg
                    cnt["ERROR"] += 1
                    print(f"\r[{walked:>4}/{total}] {acct}  ERROR:{kind:<12} {msg:<44} | {tally()}")
                    if stuck:
                        reload_to_dashboard(page)
                    else:
                        _recover(page)
                    break
            finish(r)

        print(f"\n[done] {submitted} submitted, {walked} walked.  {tally()}.  CSV: {OUTPUT_CSV}")
        if not NO_PROMPT:
            input("  >> press ENTER to close the browser: ")
        ctx.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Enter Fasalrin IS Regular loan applications from a CSV.")
    ap.add_argument("csv", nargs="?", default=INPUT_CSV,
                    help=f"input CSV, updated in place (default: {INPUT_CSV})")
    ap.add_argument("--retry-reverify", action="store_true",
                    help="try every AADHAAR_REVERIFY row once more (rows already retried once are left)")
    ap.add_argument("--only-reverify", action="store_true",
                    help="with --retry-reverify: this run does only those rows")
    ap.add_argument("--start-row", type=int, default=0, metavar="N",
                    help="only work on CSV rows N and later (row 1 = header); finished rows are still skipped")
    args = ap.parse_args()
    if not os.path.isfile(args.csv):
        ap.error(f"CSV not found: {args.csv}")
    if args.start_row and args.start_row < 2:
        ap.error("--start-row must be 2 or more (row 1 is the header)")
    INPUT_CSV = OUTPUT_CSV = args.csv
    START_ROW = args.start_row
    RETRY_REVERIFY = args.retry_reverify or args.only_reverify
    ONLY_REVERIFY = args.only_reverify
    PROGRESS_CSV = str(Path(args.csv).with_name(Path(args.csv).stem + "_progress.csv"))
    REPORTS_DIR = Path(args.csv).resolve().parent / "reports"     # per branch: branches/<SOL>/reports
    try:
        main()
    except KeyboardInterrupt:
        print("\n[quit] interrupted — CSV is saved up to the last finished row; rerun to resume")
