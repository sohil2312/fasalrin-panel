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


# ---- hang-proofing: shared by the approval scripts ----
MAX_ERROR_STREAK = 5                  # this many errors in a row -> stop cleanly (portal down / logged out for good)


def guard_page(page):
    """A browser pop-up (alert / confirm) never freezes the script: it is dismissed (= Cancel, nothing approved)."""
    try:
        page.on("dialog", lambda d: d.dismiss())
    except Exception:
        pass
    return page


def live_page(ctx, page):
    """The tab was closed or crashed -> open a new one on the portal (log in again if the session went)."""
    try:
        if not page.is_closed():
            return page
    except Exception:
        pass
    print("\n    [recover] browser tab was closed: opening a new one", flush=True)
    page = guard_page(ctx.new_page())
    page.goto(f.BASE, wait_until="domcontentloaded")
    if f.is_logged_out(page):
        f.require_login(page, "The browser tab was closed")
    f.no_scroll(page)
    return page


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


# ---- learning the portal's preview data (so a later version can read it without waiting for the screen) ----
LEARN_FILE = "logs/preview_api_learn.json"
LEARN_MAX = 3                         # previews to learn from per run
_learn = {"n": 0, "resp": []}


def learn_listen(page):
    """Keep the JSON answers the portal sends while a preview opens (only during the first previews)."""
    def on_resp(resp):
        if _learn["n"] >= LEARN_MAX:
            return
        try:
            if "json" in (resp.headers.get("content-type") or ""):
                _learn["resp"].append((resp.request.method, resp.url.split("?")[0], resp.json()))
        except Exception:
            pass
    try:
        page.on("response", on_resp)
    except Exception:
        pass


def learn_store(d: dict):
    """Which JSON fields hold the preview's values: saved as field PATHS only (no farmer data)."""
    if _learn["n"] >= LEARN_MAX:
        return
    _learn["n"] += 1
    import json
    want = {k: str(v).strip() for k, v in d.items() if v and k in (
        "Claim No.", "Account No.", "Applicable IS", "Maximum Allowed Claim", "Is Rollover Claim",
        "Interest Cycle end/Rollover Date", "First Loan Disbursal/Interest Cycle Start Date", "_status")}
    norm = lambda x: re.sub(r"[^0-9a-z.]", "", str(x).lower())
    found = []
    def walk(o, path, method, url):
        if isinstance(o, dict):
            for k, v in o.items():
                walk(v, f"{path}.{k}", method, url)
        elif isinstance(o, list):
            for i, v in enumerate(o[:5]):
                walk(v, f"{path}[{i}]", method, url)
        else:
            for label, val in want.items():
                if o is not None and norm(o) and (norm(o) == norm(val) or norm(o) == norm(money_to_float(val))):
                    found.append({"label": label, "method": method, "url": re.sub(r"\d{6,}", "<n>", url), "path": path})
    for method, url, body in _learn["resp"]:
        walk(body, "$", method, url)
    _learn["resp"] = []
    try:
        os.makedirs(os.path.dirname(LEARN_FILE), exist_ok=True)
        old = json.load(open(LEARN_FILE, encoding="utf-8")) if os.path.exists(LEARN_FILE) else []
        old.append({"time": time.strftime("%Y-%m-%d %H:%M:%S"), "labels": sorted(want), "found": found})
        json.dump(old[-10:], open(LEARN_FILE, "w", encoding="utf-8"), indent=1)
        print(f"    [learn] preview data: {len({x['label'] for x in found})} of {len(want)} values found in the "
              f"portal's answers -> {LEARN_FILE}", flush=True)
    except Exception:
        pass


T = f.StepTimer()


def decided() -> dict:
    """claim no -> latest result in the approvals file (APPROVED / MISMATCH / CHECK_PORTAL / ...)."""
    out = {}
    if os.path.exists(APPROVALS_CSV):
        with open(APPROVALS_CSV, newline="", encoding="utf-8-sig") as fh:
            for e in csv.DictReader(fh):
                if (e.get("Claim No") or "").strip():
                    out[e["Claim No"].strip()] = (e.get("Result") or "").strip()
    return out


def can_search(page) -> bool:
    return page.locator('input[placeholder="Search Here"]:visible').count() > 0


def search_list(page, acct: str) -> list[dict]:
    """Search the Submitted list for one account (as the claim filing script does): its rows, [] if none.
    Waits until the table shows THIS search (a row with the account, or 'No record(s) found')."""
    box = page.locator('input[placeholder="Search Here"]:visible').first
    box.fill("")
    box.fill(acct)
    mag = page.locator("button.btn-secondary:has(i.fa-search)")
    if mag.count():
        mag.first.click()
    else:
        box.press("Enter")
    shows = lambda: page.evaluate(r"""(a) => {
        const t = [...document.querySelectorAll('table')].find(t => /Claim No/i.test(t.innerText));
        if (t && [...t.querySelectorAll('tbody tr')].some(r => [...r.children].some(td => td.innerText.trim() === a)))
            return 'rows';
        return document.body.innerText.includes('No record(s) found') ? 'none' : '';
    }""", acct)
    state = wait_for(page, shows, 12, "search results for this account")
    if state == "none":                           # maybe the previous search's message: look once more
        page.wait_for_timeout(700)
        state = shows() or "none"
    return [r for r in list_rows(page) if r["acct"] == acct] if state == "rows" else []


def refresh_list(page):
    """Back on a fresh Submitted list: PROCEED again when already on it (no dashboard round trip)."""
    proceed = page.locator("button:visible", has_text=BTN("PROCEED"))
    if "/claim-application-list" in page.url and proceed.count():
        proceed.first.click()
        try:
            wait_for(page, lambda: list_rows(page) or re.search(r"No record\(s\) found", f.body_text(page)), 20,
                     "claim table")
            return
        except StuckError:
            pass
    open_list(page)


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
            before = [r["claim"] for r in list_rows(page)]
            tabs.nth(i).click()
            try:                                     # the page tab is selected and the rows changed (not a fixed 1.5 s)
                wait_for(page, lambda: sel.count() and sel.first.inner_text().strip() == str(n)
                         and [r["claim"] for r in list_rows(page)] != before, 20, f"list page {n}")
            except StuckError:
                page.locator("table.issCustomTable").first.wait_for(timeout=20000)
            return True
    return False


def _select(page, name, label):
    sel = page.locator(f'select[name="{name}"]').first
    sel.wait_for()
    picked = lambda: sel.evaluate("s => (s.options[s.selectedIndex]||{}).text.trim()") == label
    if picked():
        return
    for _ in range(5):
        sel.select_option(label=label)
        for _ in range(10):                          # poll (<= 0.5 s, usually at once) instead of a fixed 0.3 s
            if picked():
                return
            page.wait_for_timeout(50)
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
    wait_for(page, lambda: "/dashboard" in page.url, 15, "dashboard")
    if "/claim-application-list" not in page.url:
        # Dashboard -> 'Claim Applications' tab -> card 'pending for Approval' -> View Details (confirmed route)
        tab = page.locator(".nav-link", has_text=re.compile(r"^\s*Claim Applications\s*$", re.I))
        wait_for(page, lambda: tab.count(), 15, "Claim Applications tab")
        tab.first.click()
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
    wait_for(page, lambda: (re.search(r"Total Count", f.body_text(page)) and list_rows(page))
             or re.search(r"No record\(s\) found", f.body_text(page)), 90, "claim table")


def review_and_approve(page, row, mine, disb, stage):
    """REVIEW -> read preview -> checks -> APPROVE (or BACK). Returns (result, detail)."""
    T.mark("find")
    tr = page.locator("table tbody tr", has_text=row["claim"]).first
    tr.locator("button", has_text=BTN("REVIEW")).first.click()
    f.LIMITS.wait(page, "claim preview", lambda: "/claim-application-preview" in page.url and
                  page.locator("button:visible", has_text=BTN("APPROVE")).count(), "claim preview")
    T.mark("review")
    d = read_preview(page)
    T.mark("preview")
    learn_store(d)
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
    T.mark("approve")
    m = re.search(r"Claim application No\.?\s*(\d+)", txt, re.I)
    page.locator(".modal-content:visible button", has_text=BTN("OK")).first.click()
    try:                                         # dialog gone (instead of a fixed 0.8 s)
        wait_for(page, lambda: not modal_text(page).strip(), 5, "approved dialog to close")
    except StuckError:
        pass
    T.mark("close")
    return m.group(1) if m else row["claim"]


def main(limit: int, assume_yes: bool):
    f.INPUT_CSV = f.OUTPUT_CSV
    header, rows, idx = f.load_rows()
    ours = our_claims()
    disb_of = {r[idx[f.COL_ACCT]].strip(): r[idx[f.COL_DISB]].strip() for r in rows[1:]} if f.COL_DISB in idx else {}
    print(f"[claims] {CLAIMS_CSV}: {len(ours)} claims filed by the script (only these, after checks, get approved)")

    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(PROFILE_DIR, headless=f.HEADLESS, slow_mo=0,   # waits poll
                                                   viewport=None, args=f.BROWSER_ARGS)
        ctx.set_default_timeout(f.STUCK_TIMEOUT_S * 1000)
        f.zoom_out(ctx)
        page = guard_page(ctx.pages[0] if ctx.pages else ctx.new_page())
        learn_listen(page)
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
            f.no_scroll(page)   # logged in: no scrolling from here on

        f.lighten(ctx)                                # logged in: no pictures / trackers from here on
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
        streak = 0                                     # errors in a row
        dec = decided()
        targets = [(cn, m) for cn, m in ours.items() if dec.get(cn) not in ("APPROVED", "MISMATCH", "CHECK_PORTAL")]
        walk = bool(targets)                           # page through the whole list (slow) only without search
        if not targets:
            print(f"[search] none of our {len(ours)} filed claims is waiting for approval in this computer's records "
                  f"({CLAIMS_CSV}): nothing to approve. Claims filed on another computer need its records file.")
        if targets and can_search(page):
            walk = False
            pending = dict(targets)                    # claim no -> ours, still to approve
            total = len(pending)
            print(f"[search] {total} of our claims to approve: taken from the list page shown, "
                  f"or looked up by account when that page has none of ours (other claims are not opened)")
            n = 0
            while pending:
                if os.path.exists(f.STOP_FLAG):
                    print("\n[stop] stop requested — stopping between claims")
                    break
                if (limit and approved >= limit) or streak >= MAX_ERROR_STREAK:
                    break
                stage = {"s": "search"}
                cn = None
                T.reset()
                try:
                    page = live_page(ctx, page)
                    if "/claim-application-list" not in page.url or not can_search(page):
                        refresh_list(page)
                    on_page = [r for r in list_rows(page) if r["status"].lower() == "submitted" and r["claim"] in pending]
                    if on_page:                        # fastest: one of ours is right here
                        row = on_page[0]
                    else:                              # look the next one up by its account
                        cn = next(iter(pending))
                        acct = pending[cn]["acct"]
                        hits = [r for r in search_list(page, acct)
                                if r["claim"] == cn and r["status"].lower() == "submitted"]
                        if not hits:                   # approved by hand already / not submitted
                            n += 1
                            skipped += 1
                            pending.pop(cn)
                            print(f"[{n:>4}/{total}] {cn}  acct {acct}  NOT_SUBMITTED  not in the Submitted list now",
                                  flush=True)
                            continue
                        row = hits[0]
                    cn, mine = row["claim"], pending[row["claim"]]
                    tag = f"[{n + 1:>4}/{total}] {cn}  acct {row['acct']}"
                    print(f"\r{tag}  checking...   ", end="", flush=True)
                    result, detail = review_and_approve(page, row, mine, disb_of.get(row["acct"], ""), stage)
                    log(row["acct"], row["app"], cn, result, detail)
                    pending.pop(cn)
                    n += 1
                    streak = 0
                    if result == "APPROVED":
                        approved += 1
                        print(f"\r{tag}  APPROVED   | approved {approved}  mismatch {mismatched}  errors {errors}",
                              flush=True)
                        print(T.line(), flush=True)
                    else:
                        mismatched += 1
                        print(f"\r{tag}  MISMATCH   {detail[:120]}", flush=True)
                except KeyboardInterrupt:
                    raise
                except Exception as e:
                    import traceback
                    with open("errors.log", "a") as ef:
                        ef.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} CLAIM-VERIFY claim={cn} stage={stage['s']}\n")
                        ef.write(traceback.format_exc())
                    errors += 1
                    streak += 1
                    msg = str(e).splitlines()[0][:100] if str(e) else type(e).__name__
                    print(f"\n    [exc] stage={stage['s']} {type(e).__name__}: {msg}", flush=True)
                    try:
                        page = live_page(ctx, page)
                        shot(page, f"claimverify_error_{cn}")
                    except Exception:
                        pass
                    if cn in pending:
                        acct = pending[cn]["acct"]
                        if stage["s"] == "approving":    # APPROVE clicked: never again blindly
                            log(acct, "", cn, "CHECK_PORTAL", f"error after APPROVE: {msg}")
                            print(f"[{n + 1:>4}/{total}] {cn}  acct {acct}  CHECK_PORTAL  check it on the portal", flush=True)
                            pending.pop(cn)
                            n += 1
                        else:
                            fails[cn] = fails.get(cn, 0) + 1
                            if fails[cn] >= MAX_ROW_FAILS:
                                log(acct, "", cn, "ERROR", msg)
                                print(f"[{n + 1:>4}/{total}] {cn}  acct {acct}  ERROR  {msg}", flush=True)
                                pending.pop(cn)
                                n += 1
                    try:
                        if f.is_logged_out(page):
                            f.require_login(page)
                        f.reload_to_dashboard(page)
                        open_list(page)
                    except Exception:
                        pass
            if streak >= MAX_ERROR_STREAK:
                print(f"\n[stop] {streak} errors in a row - stopping (portal slow / down?). Rerun to continue.")
        while walk and not (limit and approved >= limit) and streak < MAX_ERROR_STREAK:
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
                streak = 0
                if result == "APPROVED":
                    approved += 1
                    print(f"\r[{approved + errors:>4}] {row['claim']}  acct {row['acct']}  APPROVED   | approved {approved}"
                          f"  mismatch {mismatched}  errors {errors}", flush=True)
                    if "/claim-application-list" not in page.url or not list_rows(page):
                        refresh_list(page)
                else:
                    seen.add(row["claim"])
                    mismatched += 1
                    print(f"\r[{approved + errors:>4}] {row['claim']}  acct {row['acct']}  MISMATCH   {detail[:120]}", flush=True)
            except KeyboardInterrupt:
                raise
            except Exception as e:
                import traceback
                with open("errors.log", "a") as ef:
                    ef.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} CLAIM-VERIFY claim={row['claim']} stage={stage['s']}\n")
                    ef.write(traceback.format_exc())
                errors += 1
                streak += 1
                page = live_page(ctx, page)
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

        if T.summary():
            print("\n" + T.summary())
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
    import fasalrin_claim
    base = fasalrin_claim.records_base(args.csv)          # IS regular work list or IS regular claim list
    CLAIMS_CSV, APPROVALS_CSV = base + "_claims.csv", base + "_claim_approvals.csv"
    try:
        main(args.limit, args.yes)
    except KeyboardInterrupt:
        print("\n[quit] interrupted — approvals so far are recorded; rerun to continue")
