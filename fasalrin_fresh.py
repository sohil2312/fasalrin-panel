#!/usr/bin/env python3
"""
IS regular FRESH applications: farmers the portal does not know yet (NOT_IN_SYSTEM), entered from scratch with
the bank's "not in system" Excel (Aadhaar, DOB, name as per Aadhaar, gender, relative, mobile, address, village,
survey / khata / land area). Branch User login. Steps as the user showed on two samples (KCC CC004, KCC AH CC043):

  Loan Application -> FY 2025-2026 + Aadhaar -> FETCH RECORD -> "enter farmer-details" -> OK (blank form)
  1 Applicant : Application Type Normal, Name (Aadhaar) -> VERIFY, passbook name = Aadhaar name, DOB, gender,
                mobile (MO NO; blank / not 10 digits -> 9876543210), ST / OWNER / SMALL, SON OF (M) / WIFE OF (F),
                relative name, primary activity (CC004 Agri Crops, CC043 Animal Husbandry),
                residence Kadana + village, street address, pincode -> SAVE & CONTINUE
  2 Account   : account no twice, SINGLE -> SAVE & CONTINUE
  3 Financial : sanction date = Disb. Date, eligibility = DL = DP -> SAVE & CONTINUE
  4 Activity  : CC004 Agri Crops: sanctioned = DP, Castor (RF), survey, khata, land area, IRRIGATED, KHARIF
                CC043 Animal Husbandry: sanctioned = DP, DAIRY, COW, 4 units
                Find Location: Kadana + village -> PROCEED -> SAVE & CONTINUE
  5 Summary   : term loan 0 -> PREVIEW -> checks -> SUBMIT -> CONFIRM -> application no.

Anything that does not fit -> the row is hand work (reason recorded, never submitted) and the run goes on.

    .venv\\Scripts\\python fasalrin_fresh.py "not in sytem.xlsx"            # builds branches/<SOL>/fresh/<SOL>_fresh.csv
    .venv\\Scripts\\python fasalrin_fresh.py branches/3106/fresh/3106_fresh.csv [--limit N] [--villages]
"""

from __future__ import annotations

import argparse
import collections
import csv
import os
import re
import sys
import time
from datetime import date, datetime
from pathlib import Path

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

import fasalrin_regular as f
from fasalrin_regular import BTN, wait_for, modal_text, pane, wait_pane, fill_amount, pick_calendar_date, StuckError

FIN_YEAR = f.FIN_YEAR
SUBDISTRICT = "Kadana"
DEFAULT_MOBILE = "9876543210"        # user rule: MO NO blank / not 10 digits
DEFAULT_PIN = "389240"
MIN_AGE, MAX_AGE = 18, 100           # a DOB outside this age is a data error (e.g. 01-01-0975): skipped
BRANCH_PIN = {"LUNAWADA": "389230"}  # user rule: every entry of this branch uses this pincode
CASTE, FARMER_CAT, FARMER_TYPE, APP_TYPE = "ST", "OWNER", "SMALL", "Normal"
CROP = "Castor (Rehri, Rendi, Arandi) - RF"
LAND_TYPE, SEASON = "IRRIGATED", "KHARIF"
AH_CATEGORY, AH_ANIMAL, AH_UNITS = "DAIRY", "COW", "4"
ACTIVITY = {"CC004": "Agri Crops", "CC043": "Animal Husbandry"}

OUT_COLS = ["Status", "Loan App No", "Detail"]
DONE = {"COMPLETED", "ALREADY_ON_PORTAL", "EXISTS_ON_PORTAL", "AADHAAR_MISMATCH", "VERIFY_FAILED", "VILLAGE_NOT_FOUND", "NO_LAND",
        "BAD_DATA", "APPLICANT_INCOMPLETE", "SCHEME_UNKNOWN"}
HAND = DONE - {"COMPLETED", "ALREADY_ON_PORTAL"}
HOLD = "CHECK_PORTAL"
PROGRESS_HEADER = ["Time", "Account No.", "Status", "Loan App No", "Detail"]
LIMIT = 0


def is_done(st: str) -> bool:
    st = (st or "").strip().upper()
    return st in DONE or st.startswith(HOLD)


# ----------------------------------------------------------------------------
# Excel -> work list
# ----------------------------------------------------------------------------
def _txt(v) -> str:
    if v is None:
        return ""
    if isinstance(v, datetime):
        return v.strftime("%d-%m-%Y")
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    return re.sub(r"\s+", " ", str(v)).strip()


def write_worklist(path: Path, recs: list[dict]):
    """Write the work list for one SOL, keeping Status / Loan App No / Detail already there (CSV + progress file)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    head = [c for c in recs[0] if c and c not in OUT_COLS]
    cols = head + OUT_COLS
    old = {}
    if path.exists():
        with open(path, newline="", encoding="utf-8-sig") as fh:
            old = {r.get("Account No.", ""): r for r in csv.DictReader(fh)}
    for acct, rec in progress_records(path).items():
        old.setdefault(acct, {}).update(rec)
    with open(path, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        for d in recs:
            o = old.get(d.get("Account No.", ""), {})
            w.writerow({**{c: d.get(c, "") for c in head}, **{c: o.get(c, "") or "" for c in OUT_COLS}})


def build_from_excel(xlsx: str) -> list[Path]:
    """One work list per SOL in branches/<SOL>/fresh/ (same as Build in the panel's IS fresh tab)."""
    import branches
    out = []
    for s in branches.sols_fresh(xlsx):
        r = branches.build_fresh(xlsx, s["sol"])
        print(f"[build] {r['csv']}: {r['master_rows']} rows ({r['cc004']} CC004, {r['cc043']} CC043), "
              f"{r['finished']} entered, {r['hand']} hand work, {r['to_do']} to do"
              + ("" if r["area_col"] else "  - no LAND AREA column: crop rows will be NO_LAND"))
        out.append(Path(r["csv"]))
    return out


def progress_path(csv_path) -> Path:
    p = Path(csv_path)
    return p.with_name(p.stem + "_progress.csv")


def progress_records(csv_path) -> dict:
    """Latest record per account from the append-only progress file."""
    p = progress_path(csv_path)
    out = {}
    if p.exists():
        with open(p, newline="", encoding="utf-8") as fh:
            for r in csv.DictReader(fh):
                out[r["Account No."]] = {"Status": r["Status"], "Loan App No": r["Loan App No"], "Detail": r["Detail"]}
    return out


def log_progress(csv_path, acct, status, app_no, detail):
    p = progress_path(csv_path)
    new = not p.exists()
    with open(p, "a", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        if new:
            w.writerow(PROGRESS_HEADER)
        w.writerow([time.strftime("%Y-%m-%d %H:%M:%S"), acct, status, app_no, detail])
        fh.flush()
        os.fsync(fh.fileno())


def save_csv(path, header, rows):
    tmp = Path(str(path) + ".tmp")
    with open(tmp, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=header)
        w.writeheader()
        w.writerows(rows)
    for _ in range(10):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:                     # open in Excel: the progress file still has everything
            time.sleep(1)
    print(f"\n    (could not update {path}: open in Excel? progress file is up to date)")


# ----------------------------------------------------------------------------
# portal helpers for the new-farmer form
# ----------------------------------------------------------------------------
def norm(s: str) -> str:
    s = (s or "").upper().replace("UTTAR", "NORTH").replace("DAKSHIN", "SOUTH")
    return re.sub(r"[^A-Z]", "", s)


def one_edit(a: str, b: str) -> bool:
    """a and b differ by at most one letter (added, dropped or changed): ZALASAG ~ ZALASANG."""
    if abs(len(a) - len(b)) > 1:
        return False
    if len(a) > len(b):
        a, b = b, a
    i = 0
    while i < len(a) and a[i] == b[i]:
        i += 1
    return a[i:] == b[i + 1:] or (len(a) == len(b) and a[i + 1:] == b[i + 1:])


def village_candidates(want: str) -> list[str]:
    """Names to try, in order (user rule): each comma / slash part; within a part the whole text, then the
    first word, first + second, third, third + fourth ('ZALASAG BACHKARIYA ZALASAG')."""
    parts = [x.strip() for x in re.split(r"[/,;]", want or "") if x.strip()] or [want or ""]
    out = []                                          # (name, from words of a longer text: strict matching only)
    for part in parts:
        w = part.split()
        out += [(part, False)] + [(x, True) for x in
                                  [*([w[0], " ".join(w[:2])] if len(w) > 1 else []),
                                   *([w[2]] if len(w) > 2 else []), *([" ".join(w[2:4])] if len(w) > 3 else [])]]
    seen, res = set(), []
    for x, strict in out:
        if x.strip() and x not in seen:
            seen.add(x)
            res.append((x, strict))
    return res


def match_village(want: str, options: list[str]) -> str | None:
    """Excel village -> one portal option: exact (letters only, UTTAR = North), then prefix, then same consonants,
    then same words, then consonant prefix, then one letter off. Candidates from village_candidates().
    Ambiguous or nothing -> None (hand work, no guess)."""
    opts = [o for o in options if o and o.strip().lower() != "select"]
    cons = lambda x: re.sub(r"[AEIOUY]", "", x)
    for part, strict in village_candidates(want):       # the first name the portal has (uniquely)
        w = norm(part)
        if not w:
            continue
        exact = lambda o: norm(o) == w
        prefix = lambda o: len(w) >= 4 and (norm(o).startswith(w) or w.startswith(norm(o)))
        base = lambda x: re.sub(r"(NORTH|SOUTH|EAST|WEST)$", "", norm(x))      # 'Karodia (North)' -> KARODIA
        near = lambda o: len(w) >= 5 and (one_edit(norm(o), w) or one_edit(base(o), base(part)))
        loose = (lambda o: cons(norm(o)) == cons(w),
                 lambda o: sorted(norm(x) for x in o.split()) == sorted(norm(x) for x in part.split()),
                 lambda o: len(cons(w)) >= 3 and cons(norm(o)).startswith(cons(w)))
        for test in ((exact, prefix, near) if strict else (exact, prefix, near, *loose)):
            hit = [o for o in opts if test(o)]
            if len(hit) == 1:
                return hit[0]
            if len(hit) > 1:
                break                               # ambiguous for this name: try the next name, never guess
    return None


def locked(loc) -> bool:
    """A field the portal fills itself and does not let us change (known farmer: disabled / read-only)."""
    try:
        return loc.evaluate("e => e.disabled || e.readOnly || e.getAttribute('aria-disabled') === 'true'")
    except Exception:
        return False


def select_label(scope, name: str, label: str, page, wait_options=8):
    sel = scope.locator(f'select[name="{name}"]').first
    sel.wait_for()
    if locked(sel):
        return                                       # portal-owned value: leave it
    wait_for(page, lambda: label in sel.evaluate("s => [...s.options].map(o => o.text.trim())"), wait_options,
             f"option {label!r} in {name}")
    picked = lambda: sel.evaluate("s => (s.options[s.selectedIndex] || {}).text.trim()") == label
    for _ in range(5):
        sel.select_option(label=label)
        sel.dispatch_event("change")
        for _ in range(10):                          # poll (<= 0.5 s) instead of a fixed pause
            if picked():
                return
            page.wait_for_timeout(50)
    raise RuntimeError(f"could not select {label!r} in {name}")


name_orders = f.name_orders          # same name orders as the IS regular reverify


def relative_name(raw: str, aadhaar_name: str) -> str:
    """Relative name for the portal: letters and spaces only. Blank, an Excel error (#N/A, #NAME?, ...) or a
    placeholder (NA, N/A, NIL, -, 0) -> the farmer's Aadhaar name (user rule)."""
    raw = (raw or "").strip()
    if not raw or raw.startswith("#") or re.fullmatch(r"(?i)\s*(n\s*/?\s*a|nil|null|none|-+|0)\s*", raw):
        return aadhaar_name
    clean = re.sub(r"\s+", " ", re.sub(r"[^A-Za-z ]", " ", raw)).strip()
    return clean or aadhaar_name


def form_values(scope, names) -> dict:
    """{name: shown value as text} for inputs / selects in `scope` (select = its selected text; missing = '')."""
    return scope.evaluate("""(el, names) => Object.fromEntries(names.map(n => {
        const e = el.querySelector(`[name="${n}"]`);
        if (!e) return [n, ''];
        return [n, e.tagName === 'SELECT' ? ((e.options[e.selectedIndex] || {}).text || '').trim() : (e.value || '').trim()];
    }))""", list(names))


def locked_names(scope, names) -> set:
    """The fields among `names` the portal has locked (disabled / read-only): its values, not ours."""
    return set(scope.evaluate("""(el, names) => names.filter(n => {
        const e = el.querySelector(`[name="${n}"]`);
        return !!e && (e.disabled || e.readOnly);
    })""", list(names)))


def check_form(page, scope, want: dict, what: str, fixers: dict | None = None):
    """Read back what the portal shows and compare with what was meant. A wrong value is set once more
    (fixers), then checked again; still wrong -> RuntimeError (the row is never saved on a guess)."""
    def same(a, b):
        a, b = re.sub(r"\s+", " ", str(a or "")).strip(), re.sub(r"\s+", " ", str(b or "")).strip()
        try:
            return float(a) == float(b)
        except ValueError:
            return a.upper() == b.upper()
    for attempt in range(2):
        got, fixed_by_portal = form_values(scope, want), locked_names(scope, want)
        bad = {k: (got.get(k), v) for k, v in want.items()
               if k not in fixed_by_portal and not same(got.get(k), v)}   # locked fields: the portal's
        if not bad:
            return
        if attempt == 0 and fixers:
            for k in bad:
                if k in fixers:
                    fixers[k]()
            continue
        raise RuntimeError(f"{what} check failed: " + "; ".join(f"{k}={g!r} (want {w!r})" for k, (g, w) in bad.items())[:200])


def options_of(scope, name: str) -> list[str]:
    return scope.locator(f'select[name="{name}"]').first.evaluate("s => [...s.options].map(o => o.text.trim())")


def fill_text(page, loc, value: str):
    loc.wait_for()
    if locked(loc):
        return                                       # portal-owned value: leave it
    loc.click()
    loc.fill("")
    loc.fill(value)
    loc.dispatch_event("input")
    loc.dispatch_event("change")


def pick_dob(page, inp, target: date):
    """DOB: type DD/MM/YYYY into the box (the Age box filling in = accepted); otherwise the rmdp calendar, using
    visible panels only: year header -> year panel (12 a page) -> month header -> month panel -> day."""
    want = f"{target:%d/%m/%Y}"
    age = pane(page, 1).locator('input[name="age"]').first
    accepted = lambda: inp.input_value().strip() == want and (age.count() == 0 or age.input_value().strip() not in ("", "0"))
    try:
        inp.click()
        inp.fill(want)
        inp.dispatch_event("input"); inp.dispatch_event("change")
        inp.press("Enter")
        for _ in range(15):                          # Age fills in when the portal took the date
            if accepted():
                break
            page.wait_for_timeout(100)
        if accepted():
            page.keyboard.press("Escape")
            return
    except Exception:
        pass

    if not page.locator(".rmdp-header-values:visible").count():
        inp.click()
    page.wait_for_selector(".rmdp-header-values >> visible=true", timeout=8000)
    vis = lambda sel: page.locator(sel).locator("visible=true")
    vis(".rmdp-header-values span").filter(has_text=re.compile(r"^\s*\d{4}\s*$")).first.click()     # year panel
    page.wait_for_timeout(300)
    for _ in range(40):
        years = [int(t) for t in vis(".rmdp-year-picker .rmdp-day span").all_inner_texts() if t.strip().isdigit()]
        if years and min(years) <= target.year <= max(years):
            break
        arrow = ".rmdp-left" if not years or target.year < min(years) else ".rmdp-right"
        vis(f".rmdp-arrow-container{arrow}").first.click()
        page.wait_for_timeout(150)
    else:
        raise RuntimeError(f"DOB year {target.year} not reachable")
    vis(".rmdp-year-picker .rmdp-day span").filter(has_text=re.compile(rf"^\s*{target.year}\s*$")).first.click()
    page.wait_for_timeout(300)
    mon = f.MONTHS[target.month - 1]
    if not vis(".rmdp-month-picker").count():
        vis(".rmdp-header-values span").filter(has_text=re.compile(r"[A-Za-z]")).first.click()        # month panel
        page.wait_for_timeout(300)
    if vis(".rmdp-month-picker").count():
        vis(".rmdp-month-picker .rmdp-day span").filter(has_text=re.compile(rf"^\s*{mon[:3]}", re.I)).first.click()
        page.wait_for_timeout(300)
    else:                                                     # no month panel: step with the arrows
        for _ in range(24):
            hdr = " ".join(vis(".rmdp-header-values span").all_inner_texts())
            if re.search(mon, hdr, re.I):
                break
            cur = next((n for n, m in enumerate(f.MONTHS) if re.search(m, hdr, re.I)), 0)
            vis(".rmdp-arrow-container.rmdp-right" if cur < target.month - 1 else ".rmdp-arrow-container.rmdp-left").first.click()
            page.wait_for_timeout(150)
    ok = page.evaluate("""(day) => {
        const vis = e => e.getClientRects().length > 0;
        for (const d of document.querySelectorAll('.rmdp-day-picker .rmdp-day')) {
          const sp = d.querySelector('span');
          if (!vis(d) || !sp || sp.textContent.trim() !== String(day) || d.classList.contains('rmdp-day-hidden')) continue;
          if (d.classList.contains('rmdp-disabled')) return 'disabled';
          sp.click(); return 'ok';
        }
        return 'notfound'; }""", target.day)
    if ok != "ok":
        raise RuntimeError(f"DOB {want} not selectable ({ok})")
    wait_for(page, accepted, 5, f"DOB {want} to register")
    page.keyboard.press("Escape")


def dob_input(page):
    """The DOB box of the applicant tab: the rmdp input next to the Age box."""
    p1 = pane(page, 1)
    loc = p1.locator("input.rmdp-input")
    if loc.count():
        return loc.first
    return p1.locator('input[name="age"]').locator("xpath=preceding::input[1]").first


def block_of(branch: str, options: list[str]) -> str | None:
    """The portal block (sub-district) for a branch name: Kadanagam -> Kadana, Lunawada -> Lunawada."""
    b = norm(branch)
    hits = [o for o in options if o and o.lower() != "select" and norm(o)
            and (b.startswith(norm(o)) or norm(o).startswith(b) or f.same_branch(o, branch))]
    return max(hits, key=lambda o: len(norm(o))) if hits else None


def pick_block(page, scope, name: str, branch: str) -> str:
    wait_for(page, lambda: len(options_of(scope, name)) > 1, 10, f"{name} options")
    blk = block_of(branch, options_of(scope, name))
    if not blk:
        raise RuntimeError(f"no block in the portal list for branch {branch!r}")
    select_label(scope, name, blk, page)
    return blk


def find_location(page, village: str, branch: str):
    """Activity tab: 'Find Location?' -> the branch's block + village -> PROCEED."""
    page.locator("a", has_text=re.compile(r"Find Location", re.I)).first.click()
    m = page.locator(".modal-content:visible")
    pick_block(page, m, "landSubDistrictID", branch)
    wait_for(page, lambda: len(options_of(m, "landVillageID")) > 1, 10, "land villages")
    v = match_village(village, options_of(m, "landVillageID"))
    if not v:
        raise VillageError(village)
    open_ = lambda: page.locator('.modal-content:visible select[name="landVillageID"]').count()
    for attempt in range(3):            # PROCEED sometimes does nothing the first time: re-pick the village, press again
        select_label(m, "landVillageID", v, page)
        page.wait_for_timeout(150)
        m.locator("button", has_text=BTN("PROCEED")).first.click()
        try:
            wait_for(page, lambda: not open_(), 6, "location popup to close")
            return
        except StuckError:
            continue
    raise StuckError("location popup did not close after PROCEED (3 tries)")


class VillageError(RuntimeError):
    pass


def to_dashboard(page):
    try:
        f.dismiss_ok_dialogs(page)
        f.click_side_nav(page, "/dashboard")
        page.wait_for_timeout(400)
    except Exception:
        pass


# ----------------------------------------------------------------------------
# one row
# ----------------------------------------------------------------------------
def process_row(page, d: dict, stage: dict, mark_submitting):
    acct = d["Account No."]
    aadhaar = re.sub(r"\D", "", d["Aadhaar No."])
    scheme = d["Scheme Code"].strip().upper()
    activity = ACTIVITY[scheme]
    disb = f.parse_dmy(d["Disb. Date"])
    dob = f.parse_dmy(d["DOB"])
    dp = int(f.money_to_float(d["DP"]))
    name = re.sub(r"[\s.]+$", "", d["NAME AS PER ADHAR"]).strip()
    village = d["VILLAGE"]
    mob = re.sub(r"\D", "", d.get("MO NO", ""))
    mobile = mob if len(mob) == 10 else DEFAULT_MOBILE
    female = d["GENDER"].strip().upper().startswith("F")
    addr = re.sub(r"\s+", " ", d["ADDRESS"]).strip(" ,")
    pin = BRANCH_PIN.get(d.get("Branch Name", "").strip().upper()) \
        or (re.findall(r"\b(\d{6})\b", addr) or [d.get("_pin") or DEFAULT_PIN])[-1]

    stage["s"] = "start"
    if d.get("_draft"):
        # a draft this script left on the portal (reset to retry): finish it with the IS regular flow, which opens
        # the farmer's application, walks the saved tabs and submits; an already submitted one is caught on the preview
        st, _dl, app, det = f.process_row(page, acct, aadhaar, disb, dp, stage, mark_submitting)
        if st in ("COMPLETED", "ALREADY_ON_PORTAL"):
            return st, app, f"draft finished · {det}"
        if st != "NOT_IN_SYSTEM":                    # NOT_IN_SYSTEM = the draft is gone: enter it fresh below
            return f"{HOLD}:DRAFT", "", f"draft could not be finished automatically ({st}: {det}) - finish it by hand"[:200]
        stage["s"] = "start"
    f.open_fetch_popup(page)
    res = f.fetch_record(page, aadhaar)
    stage["s"] = "fetched"
    known = res == "exists"
    if known:
        sel = page.locator('.modal-content:visible select[name="accountNumbers"]')
        offered = sel.first.evaluate("s => [...s.options].map(o => o.value.trim())") if sel.count() else []
        ok_btn = page.locator(".modal-content:visible button", has_text=BTN("OK"))
        if not ok_btn.count():
            page.locator('.modal-content:visible button', has_text=BTN("(?:BACK TO DASHBOARD|CLOSE)")).first.click()
            to_dashboard(page)
            return "EXISTS_ON_PORTAL", "", "Beneficiary details exist but the popup has no OK: check by hand"
        if acct in offered:                       # pick this account when the portal lists it
            sel.first.select_option(acct)
            sel.first.dispatch_event("change")
            page.wait_for_timeout(200)
        # "Beneficiary details exist in the system" -> OK, then the application is entered the fresh way
        print(f"\n    Beneficiary details exist -> OK (account {acct} "
              f"{'in' if acct in offered else 'not in'} its list)", flush=True)
        ok_btn.first.click()
        page.wait_for_timeout(500)
        more = page.locator('.modal-content:visible button', has_text=BTN("(?:OK|YES|CONFIRM|PROCEED)"))
        if more.count() and f.active_pane(page) != "formTabs-tabpane-1":
            more.first.click()                        # a confirmation, if the portal asks one
        try:
            wait_for(page, lambda: f.active_pane(page) == "formTabs-tabpane-1", 10, "application form after OK")
        except StuckError:                            # OK did not open the form (e.g. an account must be picked)
            f.shot(page, f"fresh_exists_{acct}")
            to_dashboard(page)
            return "EXISTS_ON_PORTAL", "", "Beneficiary details exist: OK did not open the form (account not in its list?)"
    elif res != "not_in_system":
        raise RuntimeError(f"unexpected FETCH response: {res}")
    else:
        page.locator('.modal-content:visible button', has_text=BTN("OK")).first.click()

    # ---- 1. Applicant ----
    p1 = pane(page, 1)
    wait_pane(page, 1)
    select_label(p1, "applicationType", APP_TYPE, page)
    # Aadhaar VERIFY: the Excel name first, then other orders of the same words (user rule) until one matches
    tried, verified = [], False
    if known:
        page.wait_for_timeout(800)
        portal_name = (form_values(p1, ["beneficiaryName"]).get("beneficiaryName") or "").strip()
        if not portal_name:
            portal_name = p1.locator('input[name="beneficiaryName"]').first.input_value().strip()
        if p1.locator("button:visible", has_text=BTN("REVERIFY")).count():
            why = f.reverify_aadhaar(page)
            if why:
                to_dashboard(page)
                return "VERIFY_FAILED", "", f"known farmer (Beneficiary details exist): {why}"[:200]
            verified = True
        elif not p1.locator("button:visible", has_text=BTN("VERIFY")).count():
            verified = True                           # already verified on the portal
        if verified:
            name = p1.locator('input[name="beneficiaryName"]').first.input_value().strip() or portal_name or name
            print(f"    known farmer: Aadhaar already verified as {name!r}", flush=True)
    for cand in ([] if verified else name_orders(name)):
        try:
            box = p1.locator('input[name="beneficiaryName"]').first
            fill_text(page, box, cand)
            vbtn = p1.locator("button:visible", has_text=BTN("VERIFY"))
            wait_for(page, lambda: vbtn.count(), 6, "VERIFY button")
        except Exception:
            break                                   # the portal does not offer VERIFY again: stop trying
        vbtn.first.click()
        stage["s"] = "verify"
        tried.append(cand)
        # verified = the VERIFY button goes away (green tick); a popup may or may not come first
        def verify_answer():
            t = modal_text(page)
            if t:
                return "popup", t
            if not p1.locator("button:visible", has_text=BTN("VERIFY")).count():
                return "verified", ""
            return None
        kind, txt = wait_for(page, verify_answer, 45, "Aadhaar VERIFY answer")
        if kind == "popup":
            print(f"\n    verify popup ({cand}): {re.sub(r'[0-9]{12}', 'XXXXXXXXXXXX', txt)[:120]}", flush=True)
            page.locator('.modal-content:visible button', has_text=BTN("(?:OK|CLOSE)")).first.click()
            page.wait_for_timeout(300)
        why = re.sub(r"\s+", " ", txt or "").replace(" OK", "").strip()[:120]
        if re.search(r"not\s+match|mismatch", why, re.I):
            continue                                # wrong order: try the next one
        if p1.locator("button:visible", has_text=BTN("VERIFY")).count():
            to_dashboard(page)
            return "VERIFY_FAILED", "", f"Aadhaar VERIFY with name {cand!r}: {why}"
        if cand != name:
            print(f"\n    Aadhaar verified with the name as {cand!r}", flush=True)
        name = cand                                 # the verified spelling is the Aadhaar name from here on
        verified = True
        break
    if not verified:
        to_dashboard(page)
        return "AADHAAR_MISMATCH", "", f"no name order matches Aadhaar (tried {', '.join(tried) or name})"[:200]
    stage["s"] = "applicant"                        # nothing saved until SAVE & CONTINUE: still safe to retry
    fill_text(page, p1.locator('input[name="beneficiaryPassbookName"]').first, name)
    if not locked(dob_input(page)):                 # known farmer: the portal's DOB may be locked
        pick_dob(page, dob_input(page), dob)
    select_label(p1, "gender", "FEMALE" if female else "MALE", page)
    if p1.locator('select[name="isMobileRequired"]:visible').count():
        select_label(p1, "isMobileRequired", "Yes", page)
    fill_text(page, p1.locator('input[name="mobile"]').first, mobile)
    select_label(p1, "casteCategory", CASTE, page)
    select_label(p1, "farmerCategory", FARMER_CAT, page)
    select_label(p1, "farmerType", FARMER_TYPE, page)
    select_label(p1, "relation", "WIFE OF" if female else "SON OF", page)
    fill_text(page, p1.locator('input[name="relativeName"]').first, relative_name(d.get("RELATIVE NAME", ""), name))
    select_label(p1, "primaryActivity", activity, page)
    block = pick_block(page, p1, "resSubDistrictId", d["Branch Name"])
    wait_for(page, lambda: len(options_of(p1, "resVillageId")) > 1, 10, "residence villages")
    v = match_village(village, options_of(p1, "resVillageId"))
    if not v:
        to_dashboard(page)
        return "VILLAGE_NOT_FOUND", "", f"village {village!r} not (or not uniquely) in the portal list for {block}"
    select_label(p1, "resVillageId", v, page)
    ad = p1.locator('input[name="resAddress"]').first
    maxlen = int(ad.get_attribute("maxlength") or 0)
    fill_text(page, ad, addr[:maxlen] if maxlen else addr)
    fill_text(page, p1.locator('input[name="resPincode"]').first, pin)
    rel_name = relative_name(d.get("RELATIVE NAME", ""), name)
    addr_in = addr[:maxlen] if maxlen else addr
    sl = lambda n, v: (lambda: select_label(p1, n, v, page))
    ft = lambda n, v: (lambda: fill_text(page, p1.locator(f'input[name="{n}"]').first, v))
    want1 = {"applicationType": APP_TYPE, "beneficiaryName": name, "beneficiaryPassbookName": name,
             "gender": "FEMALE" if female else "MALE", "mobile": mobile, "casteCategory": CASTE,
             "farmerCategory": FARMER_CAT, "farmerType": FARMER_TYPE, "relation": "WIFE OF" if female else "SON OF",
             "relativeName": rel_name, "primaryActivity": activity, "resSubDistrictId": block,
             "resVillageId": v, "resAddress": addr_in, "resPincode": pin}
    check_form(page, p1, want1, "applicant tab",
               {k: (sl(k, val) if k in ("applicationType", "gender", "casteCategory", "farmerCategory", "farmerType",
                                        "relation", "primaryActivity", "resVillageId") else ft(k, val))
                for k, val in want1.items() if k not in ("beneficiaryName", "resSubDistrictId")})
    got_aadhaar = re.sub(r"\D", "", form_values(p1, ["aadharNumber"]).get("aadharNumber") or "")
    if got_aadhaar and got_aadhaar[-4:] != aadhaar[-4:]:
        raise RuntimeError(f"applicant tab shows Aadhaar ending {got_aadhaar[-4:]}, want {aadhaar[-4:]}")
    dob_box = dob_input(page)
    if not locked(dob_box) and dob_box.input_value().strip() != f"{dob:%d/%m/%Y}":
        pick_dob(page, dob_box, dob)
        if dob_box.input_value().strip() != f"{dob:%d/%m/%Y}":
            raise RuntimeError(f"DOB shows {dob_box.input_value()!r}, want {dob:%d/%m/%Y}")
    save1 = p1.locator("button", has_text=BTN("(?:SAVE|UPDATE) & CONTINUE")).first
    save1.click()
    stage["s"] = "applicant_saved"

    # ---- 2. Account ----
    try:
        wait_pane(page, 2, retry_btn=save1)
    except RuntimeError as e:
        if str(e).startswith("portal validation:"):
            f.shot(page, f"fresh_applicant_{acct}")
            to_dashboard(page)
            return "APPLICANT_INCOMPLETE", "", "applicant tab: " + str(e)[len("portal validation:"):][:120]
        raise
    p2 = pane(page, 2)
    fill_text(page, p2.locator('input[name="accountNumber"]').first, acct)
    fill_text(page, p2.locator('input[name="confirmAccountNumber"]').first, acct)
    select_label(p2, "accountHolder", "SINGLE", page)
    check_form(page, p2, {"accountNumber": acct, "confirmAccountNumber": acct, "accountHolder": "SINGLE"}, "account tab",
               {"accountNumber": lambda: fill_text(page, p2.locator('input[name="accountNumber"]').first, acct),
                "confirmAccountNumber": lambda: fill_text(page, p2.locator('input[name="confirmAccountNumber"]').first, acct),
                "accountHolder": lambda: select_label(p2, "accountHolder", "SINGLE", page)})
    save2 = p2.locator("button", has_text=BTN("(?:SAVE|UPDATE) & CONTINUE")).first
    save2.click()
    stage["s"] = "account_saved"

    # ---- 3. Financial ----
    wait_pane(page, 3, retry_btn=save2)
    fin = page.locator("#finance")
    date_in = fin.locator("input.rmdp-input").first
    elig_in = fin.locator('input[name="loanSanctionAmount"]').first
    dl_in = fin.locator('input[name="drawingLimit"]').first
    pick_calendar_date(page, date_in, disb)
    fill_amount(page, elig_in, dp)
    fill_amount(page, dl_in, dp)
    wait_for(page, lambda: f.money_to_float(dl_in.input_value()) == dp and f.money_to_float(elig_in.input_value()) == dp,
             5, "financial amounts to stick")
    if date_in.input_value().strip() != f"{disb:%d/%m/%Y}":
        raise RuntimeError(f"financial tab date shows {date_in.input_value()!r}, want {disb:%d/%m/%Y}")
    save3 = fin.locator("button", has_text=BTN("SAVE & CONTINUE")).first
    save3.click()
    stage["s"] = "financial_saved"

    # ---- 4. Activity ----
    wait_pane(page, 4, retry_btn=save3)
    act = page.locator("#activity")
    act.locator("button", has_text=BTN(re.escape(activity))).first.click()
    ls_in = act.locator('input[name="loanSanctionedAmount"]').first
    ls_in.wait_for(timeout=8000)
    act_date = act.locator("input.rmdp-input").first
    if act_date.count() and act_date.input_value().strip() != f"{disb:%d/%m/%Y}":
        try:
            wait_for(page, lambda: act_date.input_value().strip() == f"{disb:%d/%m/%Y}", 3, "activity date")
        except StuckError:
            pick_calendar_date(page, act_date, disb)
    fill_amount(page, ls_in, dp)
    if scheme == "CC004":
        select_label(act, "cropCode", CROP, page)
        fill_text(page, act.locator('input[name="surveyNumber"]').first, d["SURVEY NO"])
        fill_text(page, act.locator('input[name="khataNumber"]').first, d["KHATA NO"])
        fill_text(page, act.locator('input[name="landArea"]').first, d["_area"])
        select_label(act, "landType", LAND_TYPE, page)
        select_label(act, "seasonCode", SEASON, page)
    else:
        select_label(act, "stockCount", AH_CATEGORY, page)
        select_label(act, "liveStockCode", AH_ANIMAL, page)
        fill_text(page, act.locator('input[name="unitCount"]').first, AH_UNITS)
    try:
        find_location(page, village, d["Branch Name"])
    except VillageError:
        to_dashboard(page)
        return "VILLAGE_NOT_FOUND", "", f"land village {village!r} not (or not uniquely) in the portal list"
    wait_for(page, lambda: f.money_to_float(ls_in.input_value()) == dp, 5, "activity amount to stick")
    if scheme == "CC004":
        want4 = {"cropCode": CROP, "surveyNumber": d["SURVEY NO"], "khataNumber": d["KHATA NO"], "landArea": d["_area"],
                 "landType": LAND_TYPE, "seasonCode": SEASON}
        fix4 = {"cropCode": lambda: select_label(act, "cropCode", CROP, page),
                "landType": lambda: select_label(act, "landType", LAND_TYPE, page),
                "seasonCode": lambda: select_label(act, "seasonCode", SEASON, page),
                **{k: (lambda k=k: fill_text(page, act.locator(f'input[name="{k}"]').first, want4[k]))
                   for k in ("surveyNumber", "khataNumber", "landArea")}}
    else:
        want4 = {"stockCount": AH_CATEGORY, "liveStockCode": AH_ANIMAL, "unitCount": AH_UNITS}
        fix4 = {"stockCount": lambda: select_label(act, "stockCount", AH_CATEGORY, page),
                "liveStockCode": lambda: select_label(act, "liveStockCode", AH_ANIMAL, page),
                "unitCount": lambda: fill_text(page, act.locator('input[name="unitCount"]').first, AH_UNITS)}
    check_form(page, act, want4, "activity tab", fix4)
    loc_txt = form_values(act, ["landLocation"]).get("landLocation") or ""
    if loc_txt and norm(v) not in norm(loc_txt):
        raise RuntimeError(f"land location shows {loc_txt!r}, want village {v!r}")
    if act_date.count() and act_date.input_value().strip() != f"{disb:%d/%m/%Y}":
        raise RuntimeError(f"activity date shows {act_date.input_value()!r}, want {disb:%d/%m/%Y}")
    save4 = act.locator("button", has_text=BTN("SAVE & CONTINUE")).first
    save4.click()
    stage["s"] = "activity_saved"

    # ---- 5. Summary -> preview ----
    wait_pane(page, 5, retry_btn=save4)
    t5 = pane(page, 5)
    txt5 = wait_for(page, lambda: (lambda t: t if "Term Loan For Current FY" in t else None)(t5.inner_text()),
                    f.STUCK_TIMEOUT_S, "term loan summary")
    m = re.search(r"Term Loan For Current FY.*?₹\s*([\d,]+\.\d+)", txt5.replace("\n", " "), re.S)
    if (f.money_to_float(m.group(1)) if m else -1) != 0:
        f.shot(page, f"fresh_termloan_{acct}")
        raise RuntimeError("Term Loan For Current FY is not 0")
    t5.locator("button", has_text=BTN("PREVIEW")).first.evaluate("el => el.click()")
    stage["s"] = "preview"

    wait_for(page, lambda: "/loan-application-preview" in page.url and "Application Status" in f.body_text(page),
             f.STUCK_TIMEOUT_S, "preview page")
    page.wait_for_timeout(200)
    pv = re.sub(r"\s+", " ", f.body_text(page))
    problems = []
    # the name is checked where the preview shows it (label "...As per Aadhaar")
    if re.search(r"As per Aadhaar", pv, re.I) and not re.search(re.escape(re.sub(r"\s+", " ", name)), pv, re.I):
        problems.append("farmer name")
    for pat, what in [(rf"Account Number\s*{acct}\b", "account"),
                      (rf"Aadhaar No\.\s*XXXX-XXXX-{aadhaar[-4:]}", "aadhaar last-4"),
                      (rf"KCC loan sanctioned / KCC renewed on\s*{disb:%d/%m/%Y}", "sanction date")]:
        if not re.search(pat, pv):
            problems.append(what)
    for label, what in [(r"KCC drawing limit for current FY", "drawing limit"),
                        (r"KCC Loan Sanction eligiblity as per SOF", "eligibility"),
                        (r"Loan Sanctioned \(INR\)", "activity amount")]:
        mm = re.search(label + r"\s*₹\s*([\d,]+(?:\.\d+)?)", pv)
        if not mm or f.money_to_float(mm.group(1)) != dp:
            problems.append(f"{what} (got {mm.group(1) if mm else 'none'}, want {dp})")
    if problems:
        f.shot(page, f"fresh_preview_{acct}")
        raise RuntimeError(f"preview mismatch: {', '.join(problems)}")
    if re.search(r"Application Status\s*(Submitted|Approved)", pv, re.I):
        page.get_by_role("button", name=BTN("BACK")).first.click()
        return "ALREADY_ON_PORTAL", "", "preview shows it is already submitted"

    # ---- SUBMIT -> CONFIRM -> OK ----
    page.get_by_role("button", name=BTN("SUBMIT")).first.click()
    wait_for(page, lambda: re.search(r"sure you want to submit", modal_text(page), re.I), f.STUCK_TIMEOUT_S, "confirm dialog")
    mark_submitting()
    page.locator('.modal-content:visible button', has_text=BTN("CONFIRM")).first.click()
    stage["s"] = "submitting"
    txt = wait_for(page, lambda: (lambda t: t if re.search(r"submitted successfully", t, re.I) else None)(modal_text(page)),
                   f.STUCK_TIMEOUT_S, "submitted-successfully dialog")
    m = re.search(r"Loan application\s*([0-9]+)\s*submitted", txt, re.I)
    page.locator('.modal-content:visible button', has_text=BTN("OK")).first.click()
    stage["s"] = "done"
    page.wait_for_timeout(300)
    kind = f"{activity}" + (f" {CROP} {d['_area']} {LAND_TYPE}" if scheme == "CC004" else f" {AH_CATEGORY} {AH_ANIMAL} x{AH_UNITS}")
    return "COMPLETED", m.group(1) if m else "", f"{v} · DP {dp} · {kind}"


# ----------------------------------------------------------------------------
# run
# ----------------------------------------------------------------------------
SAFE_STAGES = ("start", "fetched", "verify", "applicant")      # before the first SAVE: no draft on the portal


def area_column(header) -> str | None:
    """The land-area column: LAND / LAND AREA / AREA (any case, optional unit in brackets)."""
    return next((c for c in header if re.fullmatch(r"\s*(LAND\s*AREA|LAND|AREA)\s*(\(.*\))?\s*", c or "", re.I)), None)


def precheck(d: dict) -> tuple[str, str] | None:
    """Row problems found without the browser -> (status, detail)."""
    if len(re.sub(r"\D", "", d.get("Aadhaar No.", ""))) != 12:
        return "BAD_DATA", "Aadhaar not 12 digits"
    if d.get("Scheme Code", "").strip().upper() not in ACTIVITY:
        return "SCHEME_UNKNOWN", f"scheme {d.get('Scheme Code')!r}: only CC004 / CC043 are known"
    for col in ("Disb. Date", "DOB"):
        try:
            f.parse_dmy(d.get(col, ""))
        except Exception:
            return "BAD_DATA", f"{col} {d.get(col)!r} is not a date"
    dob = f.parse_dmy(d["DOB"])
    today = date.today()
    age = today.year - dob.year - ((today.month, today.day) < (dob.month, dob.day))
    if not MIN_AGE <= age <= MAX_AGE:                # e.g. 01-01-0975 or a date in the future
        return "BAD_DATA", f"DOB {d['DOB']!r} looks wrong (age {age}): fix it in the Excel"
    if f.money_to_float(d.get("DP") or "0") <= 0:
        return "BAD_DATA", "DP is 0"
    if not d.get("NAME AS PER ADHAR", "").strip() or not d.get("VILLAGE", "").strip():
        return "BAD_DATA", "name or village missing"
    if re.search(r"^\s*CHECK\s*$|^\s*#|#NAME|#N/A|ERROR", d["VILLAGE"], re.I):
        return "BAD_DATA", f"village is {d['VILLAGE']!r} in the Excel: fill the real village"
    if d["Scheme Code"].strip().upper() == "CC004":
        area = d.get("_area", "")
        if not d.get("SURVEY NO", "").strip() or not d.get("KHATA NO", "").strip() or not area:
            return "NO_LAND", "crop loan needs SURVEY NO, KHATA NO and land area"
        try:
            if float(area) <= 0:
                raise ValueError
        except ValueError:
            return "NO_LAND", f"land area {area!r} is not a number"
    return None


def run(csv_path: Path):
    with open(csv_path, newline="", encoding="utf-8-sig") as fh:
        rd = csv.DictReader(fh)
        header, rows = rd.fieldnames, list(rd)
    area_col = area_column(header)
    pins = collections.Counter(p for r in rows for p in re.findall(r"\b(\d{6})\b", r.get("ADDRESS", "")))
    for r in rows:
        r["_pin"] = pins.most_common(1)[0][0] if pins else DEFAULT_PIN        # this branch's usual pincode
        r["_area"] = re.sub(r"[^\d.]", "", r.get(area_col, "")) if area_col else ""
    for acct, rec in progress_records(csv_path).items():         # the progress file wins over the CSV
        for r in rows:
            if r["Account No."] == acct:
                r.update(rec)
    out_header = header
    for r in rows:                      # a draft reset to retry: continued on the portal, not started again
        r["_draft"] = not r.get("Status") and "was CHECK_PORTAL" in (r.get("Detail") or "")
    for r in rows:                      # Excel problems are re-checked every run: a fixed row goes back to the queue
        if r.get("Status") in ("NO_LAND", "BAD_DATA", "SCHEME_UNKNOWN") and not precheck(r):
            r["Status"], r["Detail"] = "", "data fixed in the Excel"

    def finish(r):
        log_progress(csv_path, r["Account No."], r["Status"], r["Loan App No"], r["Detail"])
        save_csv(csv_path, out_header, [{k: x.get(k, "") for k in out_header} for x in rows])

    todo = [r for r in rows if r.get("Account No.") and not is_done(r.get("Status", ""))]
    b = collections.Counter(("done" if r["Status"] in ("COMPLETED", "ALREADY_ON_PORTAL") else
                             "hand" if r["Status"] in HAND else "check" if r["Status"].startswith(HOLD) else "todo")
                            for r in rows)
    print(f"[resume] {len(rows)} rows: {b['done']} entered, {b['hand']} hand work, {b['check']} check on portal, "
          f"{len(todo)} to do" + ("" if area_col else "  (no LAND AREA column yet: crop rows become NO_LAND)"))

    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(f.PROFILE_DIR, headless=False, slow_mo=0,
                                                   viewport=None, args=["--start-maximized"])
        ctx.set_default_timeout(f.STUCK_TIMEOUT_S * 1000)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(f.BASE, wait_until="domcontentloaded")
        print("\n  LOG IN in the browser window with the Branch User login (mobile + password + captcha).", flush=True)
        page.wait_for_timeout(2000)
        if f.is_logged_out(page):
            f.require_login(page, "Not logged in yet")
        if not f.require_role(page, "Branch User"):
            ctx.close(); return
        want = collections.Counter(r.get("Branch Name", "") for r in rows).most_common(1)[0][0]
        got = f.logged_in_branch(page)
        if want and got and not f.same_branch(got, want):
            print(f"\n  !! WRONG LOGIN: the portal is logged in as branch '{got}', this work list is '{want}'. Nothing entered.")
            ctx.close(); return
        print(f"[login] branch check: portal '{got or '?'}' = work list '{want}'", flush=True)

        print(f"[start] {len(todo)} rows pending\n", flush=True)
        cnt = collections.Counter()
        done = 0
        for n, r in enumerate(todo, 1):
            if os.path.exists(f.STOP_FLAG):
                print("\n[stop] stop requested — stopping between rows"); break
            if LIMIT and done >= LIMIT:
                print(f"\n[stop] reached --limit {LIMIT}"); break
            acct = r["Account No."]
            pre = precheck(r)
            if pre:
                r["Status"], r["Loan App No"], r["Detail"] = pre[0], "", pre[1]
                cnt[pre[0]] += 1
                print(f"[{n:>4}/{len(todo)}] {acct}  {pre[0]:<21} {pre[1]}", flush=True)
                finish(r); continue

            def mark_submitting(r=r):
                r["Status"], r["Detail"] = f"{HOLD}:submitting", "CONFIRM clicked, result not recorded yet"
                finish(r)

            stage = {"s": "start"}
            tries = 0
            while True:
                tries += 1
                print(f"\r[{n:>4}/{len(todo)}] {acct}  working...        ", end="", flush=True)
                try:
                    st, app, det = process_row(page, r, stage, mark_submitting)
                    r["Status"], r["Loan App No"], r["Detail"] = st, app, det
                    done += 1
                    break
                except KeyboardInterrupt:
                    raise
                except Exception as e:
                    import traceback
                    with open("errors.log", "a", encoding="utf-8") as ef:
                        ef.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} FRESH acct={acct} stage={stage['s']} url={page.url}\n")
                        ef.write(traceback.format_exc())
                    msg = str(e).splitlines()[0][:120]
                    print(f"\n    [exc] stage={stage['s']} {type(e).__name__}: {msg}", flush=True)
                    if f.is_logged_out(page):
                        f.require_login(page)
                    if stage["s"] in ("submitting", "done"):
                        r["Status"], r["Detail"] = f"{HOLD}:{stage['s']}", msg      # never re-submit blindly
                        f.reload_to_dashboard(page)
                        break
                    if isinstance(e, (StuckError, PWTimeout)) and tries <= f.STUCK_RETRIES and stage["s"] in ("start", "fetched"):
                        f.reload_to_dashboard(page)
                        continue                                                  # nothing saved yet: retry
                    # a draft may exist after SAVE & CONTINUE: hand work, not a blind retry
                    r["Status"] = f"ERROR:{type(e).__name__.upper()}" if stage["s"] in SAFE_STAGES else f"{HOLD}:DRAFT"
                    r["Detail"] = f"{stage['s']}: {msg}"
                    f.shot(page, f"fresh_error_{acct}")
                    f.reload_to_dashboard(page)
                    break
            cnt[r["Status"].split(":")[0]] += 1
            print(f"\r[{n:>4}/{len(todo)}] {acct}  {r['Status']:<21} {r['Detail'][:70]:<70} app={r['Loan App No'] or '-'}"
                  f" | " + "  ".join(f"{k.lower()} {v}" for k, v in cnt.items()), flush=True)
            finish(r)
        print(f"\n[done] " + "  ".join(f"{k.lower()} {v}" for k, v in cnt.items()) + f".  CSV: {csv_path}")
        ctx.close()


def cli():
    global LIMIT
    ap = argparse.ArgumentParser(description="Enter IS regular FRESH applications (farmers not in the portal).")
    ap.add_argument("source", help="the bank's 'not in system' .xlsx (builds the work list) or a fresh work-list CSV")
    ap.add_argument("--limit", type=int, default=0, help="stop after N rows entered (0 = all)")
    ap.add_argument("--yes", action="store_true", help="no questions (panel)")
    a = ap.parse_args()
    LIMIT = a.limit
    if a.source.lower().endswith(".xlsx"):
        paths = build_from_excel(a.source)
        print("Work list(s) ready. Start the entry with:")
        for p in paths:
            print(f"   .venv\\Scripts\\python fasalrin_fresh.py {p}")
        return
    run(Path(a.source))


if __name__ == "__main__":
    os.environ.setdefault("NO_PROMPT", "1")
    f.NO_PROMPT = True
    try:
        cli()
    except KeyboardInterrupt:
        print("\n[quit] stopped; entries so far are in the progress file")
