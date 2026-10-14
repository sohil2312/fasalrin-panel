#!/usr/bin/env python3
"""
Fasalrin - approve 3% PRI ADDITIONAL claims with the Branch Head login.

  Dashboard -> 'Claim Applications' tab -> 'pending for Approval' View Details
  -> FY 2025-2026 / PRI / SUBMITTED / ALL / Branch -> PROCEED
  row -> REVIEW -> read the preview -> checks -> APPROVE -> CONFIRM -> "approved successfully" -> OK

CHECKS before APPROVE (any failure -> BACK, never approved, MISMATCH)
  * the claim no is one our PRI job filed (branches/<SOL>/pri/<SOL>_pri_claims.csv, CLAIMED) and that row
    is still CLAIMED;  preview account == row account == ours;  claim no == ours
  * start date == what we entered (the Excel Disb. Date, or the nearest date the calendar allowed) and within
    3 days of the Excel Disb. Date;  end date == what we entered and == Excel Repay. Date or the nearest allowed
  * Applicable PRI == what we filed == lower of Excel 3% PRI and Maximum Allowed Claim, > 0
  * PRI Submission Type == COMPLETE;  Eligible For PRI == Yes;  Loan Application FY == 2024-2025;  SUBMITTED
Claims not filed by our PRI job are walked past.

RECORDS: branches/<SOL>/pri/<SOL>_pri_approvals.csv (append-only, fsync'd).

USAGE
  python fasalrin_pri_verify.py branches/3106/pri/3106_pri.csv
  NO_PROMPT=1 ... --yes [--limit N]
"""

from __future__ import annotations

import argparse
import csv
import os
import re
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

import fasalrin_regular as f
import fasalrin_claim as cl
import fasalrin_claim_verify as cv
import fasalrin_pri as pri
from fasalrin_regular import BTN, StuckError, wait_for, shot, money_to_float

PROFILE_DIR = ".pw_profile_head"      # Branch Head profile
CLAIM_FY, LOAN_FY = "2025-2026", "2024-2025"
HEADER = ["Time", f.COL_ACCT, "Claim No", "Result", f.COL_DETAIL]
MAX_ROW_FAILS = 2
RECORDS_CSV = APPROVALS_CSV = ""


def log(acct, claim_no, result, detail):
    new = not os.path.exists(APPROVALS_CSV)
    with open(APPROVALS_CSV, "a", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        if new:
            w.writerow(HEADER)
        w.writerow([time.strftime("%Y-%m-%d %H:%M:%S"), acct, claim_no, result, detail])
        fh.flush()
        os.fsync(fh.fileno())


def our_claims(rows, idx) -> dict:
    """claim no -> what our PRI job entered + the Excel row, for rows that are still CLAIMED."""
    xl = {pri.key(r[idx[f.COL_ACCT]], r[idx[pri.COL_DISB]]): r for r in rows[1:]}
    out, last = {}, {}
    if os.path.exists(RECORDS_CSV):
        with open(RECORDS_CSV, newline="", encoding="utf-8-sig") as fh:
            for e in csv.DictReader(fh):
                k = pri.key(e[f.COL_ACCT], e.get(pri.COL_DISB, ""))
                last[k] = e["Result"].strip()
                if e["Result"].strip() == "CLAIMED" and e.get("Claim No", "").strip():
                    out[e["Claim No"].strip()] = (k, e)
    ours = {}
    for no, (k, e) in out.items():
        r = xl.get(k)
        if last.get(k) != "CLAIMED" or r is None:
            continue
        ours[no] = {"acct": e[f.COL_ACCT].strip(), "start": pri.dmy(e.get("Start Date", "")),
                    "end": pri.dmy(e.get("End Date", "")), "claimed": money_to_float(e.get("PRI Claimed", "")),
                    "disb": pri.dmy(r[idx[pri.COL_DISB]]), "repay": pri.dmy(r[idx[pri.COL_REPAY]]),
                    "pri": money_to_float(r[idx[pri.COL_PRI]])}
    return ours


T = f.StepTimer()                     # seconds per step of each approval -> the log and the panel


def read_preview(page) -> dict:
    for _ in range(200):                     # up to ~10 s, every 50 ms
        d = page.evaluate("""() => {
            const out = {};
            for (const l of document.querySelectorAll('p.dataPreviewLabel')) {
                let v = l.nextElementSibling;
                while (v && !/darkText/.test(v.className)) v = v.nextElementSibling;
                if (v) out[l.innerText.trim()] = v.innerText.trim();
            }
            const st = [...document.querySelectorAll('span.fw-bold')].map(e => e.innerText.trim()).find(t => /Claim Status/i.test(t));
            if (st) out['_status'] = st;
            return out;
        }""")
        if d.get("Claim No.") and d.get("Applicable PRI"):
            return d
        page.wait_for_timeout(50)
    raise RuntimeError("PRI claim preview never populated")


def check_claim(d: dict, row: dict, mine: dict) -> list[str]:
    probs = []
    if d.get("Account No.", "").strip() != row["acct"]:
        probs.append(f"preview account {d.get('Account No.')!r} != row {row['acct']}")
    if d.get("Claim No.", "").strip() != row["claim"]:
        probs.append(f"preview claim {d.get('Claim No.')!r} != row {row['claim']}")
    if mine.get("any"):                       # a claim in the list not filed by our job: the portal's own limits
        portal, max_allowed = money_to_float(d.get("Applicable PRI")), money_to_float(d.get("Maximum Allowed Claim"))
        if portal <= 0:
            probs.append("applicable PRI is 0")
        if max_allowed > 0 and portal > max_allowed + 0.005:
            probs.append(f"applicable PRI {portal:.2f} > Maximum Allowed Claim {max_allowed:.2f}")
        if not re.search(r"SUBMITTED", d.get("_status", ""), re.I):
            probs.append(f"status {d.get('_status')!r}")
        return probs
    if mine["acct"] != row["acct"]:
        probs.append(f"our claim {row['claim']} was for account {mine['acct']}")
    portal, max_allowed = money_to_float(d.get("Applicable PRI")), money_to_float(d.get("Maximum Allowed Claim"))
    if abs(portal - mine["claimed"]) > 0.005:
        probs.append(f"applicable PRI {portal:.2f} != ours {mine['claimed']:.2f}")
    want = round(min(mine["pri"], max_allowed), 2) if max_allowed > 0 else mine["pri"]
    if abs(portal - want) > 0.005:
        probs.append(f"applicable PRI {portal:.2f} != lower of 3% PRI {mine['pri']:.2f} / max {max_allowed:.2f}")
    if portal <= 0:
        probs.append("applicable PRI is 0")
    sd = pri.dmy(d.get("First Loan Disbursal/Interest Cycle Start Date", ""))
    ed = pri.dmy(d.get("Interest Cycle end/Rollover Date", ""))
    if not (sd and ed):
        probs.append("could not read start / end dates")
    else:
        if sd != mine["start"]:
            probs.append(f"start {sd:%d/%m/%Y} != what we entered {mine['start']}")
        if mine["disb"] and abs((sd - mine["disb"]).days) > pri.SLACK_DAYS:
            probs.append(f"start {sd:%d/%m/%Y} far from Disb. Date {mine['disb']:%d/%m/%Y}")
        if ed != mine["end"]:
            probs.append(f"end {ed:%d/%m/%Y} != what we entered {mine['end']}")
    if d.get("PRI Submission Type", "").strip().upper() != pri.SUBMISSION:
        probs.append(f"submission type {d.get('PRI Submission Type')!r}")
    if not re.match(r"\s*yes", d.get("Eligible For PRI", ""), re.I):
        probs.append(f"Eligible For PRI = {d.get('Eligible For PRI')!r}")
    if d.get("Loan Application FY", "").strip() != LOAN_FY:
        probs.append(f"loan FY {d.get('Loan Application FY')!r} != {LOAN_FY!r}")
    if not re.search(r"SUBMITTED", d.get("_status", ""), re.I):
        probs.append(f"status {d.get('_status')!r}")
    return probs


# ----------------------------------------------------------------------------
# several tabs at once (same login): while one tab waits for the portal, the others work
# ----------------------------------------------------------------------------
# extra tabs: off by default - the portal keeps the login PER TAB (extra tabs open logged out, 14-10-2026).
# For speed run the same approval on several dashboards (ports) instead: each reserves a claim before opening it.
TABS = max(1, int(os.environ.get("FASALRIN_TABS") or 1))


def reserve(claim: str) -> bool:
    """Reserve a claim for this job before opening it (a small file next to the records, made atomically):
    False when another dashboard's job has it. A reservation older than 3 minutes is stale (a stopped job; one approval takes seconds)."""
    d = os.path.join(os.path.dirname(APPROVALS_CSV) or ".", ".taken")
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, re.sub(r"\W", "", claim) or "x")
    for _ in range(2):
        try:
            fd = os.open(p, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, f"{os.getpid()} {time.time():.0f}".encode())
            os.close(fd)
            return True
        except FileExistsError:
            try:
                if time.time() - os.path.getmtime(p) > 3 * 60:
                    os.remove(p)                 # stale: take it over
                    continue
            except OSError:
                pass
            return False
    return False


def preview_once(page) -> dict:
    """The preview's label/value pairs right now (empty if not shown yet) - one look, no waiting."""
    try:
        return page.evaluate("""() => {
            const out = {};
            for (const l of document.querySelectorAll('p.dataPreviewLabel')) {
                let v = l.nextElementSibling;
                while (v && !/darkText/.test(v.className)) v = v.nextElementSibling;
                if (v) out[l.innerText.trim()] = v.innerText.trim();
            }
            const st = [...document.querySelectorAll('span.fw-bold')].map(e => e.innerText.trim()).find(t => /Claim Status/i.test(t));
            if (st) out['_status'] = st;
            return out;
        }""")
    except Exception:
        return {}


def safe_rows(page):
    """The list rows, or None while the page is loading / navigating (then the tab just looks again)."""
    try:
        return cv.list_rows(page)
    except Exception:
        return None


def _click(page, selector, text):
    page.locator(selector, has_text=BTN(text)).first.click()


def open_tabs(ctx, page, n):
    """The first tab + up to n-1 more in the same login, each on the Submitted PRI list. A tab that is not
    logged in is closed (the portal then keeps logins per tab): the job goes on with the tabs it has."""
    tabs = [page]
    for i in range(1, n):
        try:
            t = cv.guard_page(ctx.new_page())
            t.goto(f.BASE, wait_until="domcontentloaded")
            try:
                wait_for(t, lambda: t.locator('ul#menu, input[type="password"]').count() > 0, 20, "page")
            except StuckError:
                pass
            if f.is_logged_out(t):
                print(f"[tabs] tab {i + 1} is not logged in (the portal keeps logins per tab): going on with "
                      f"{len(tabs)} tab(s)", flush=True)
                t.close()
                break
            f.dismiss_ok_dialogs(t)
            f.no_scroll(t)
            open_list(t)
            tabs.append(t)
        except Exception as e:
            print(f"[tabs] could not open tab {i + 1} ({type(e).__name__}): going on with {len(tabs)} tab(s)", flush=True)
            break
    print(f"[tabs] approving with {len(tabs)} tab(s) at once", flush=True)
    return tabs


def parallel_approve(ctx, page, ours, limit, cnt, taken):
    """Approve the Submitted list with several tabs, one step per tab in turn (a step = a click or a look at the
    screen, never a wait). Returns 'done', or 'fallback' (logged out / tabs failing: go on with one tab)."""
    tabs = [{"p": t, "state": "IDLE", "n": i + 1, "empty": 0} for i, t in enumerate(open_tabs(ctx, page, TABS))]
    if len(tabs) < 2:
        return "fallback"
    streak = 0
    while True:
        stopping = os.path.exists(f.STOP_FLAG) or (limit and cnt["approved"] + sum(1 for t in tabs if t["state"] not in ("IDLE", "DONE")) >= limit)
        busy = [t for t in tabs if t["state"] != "DONE"]
        if not busy:
            return "done"
        if stopping and all(t["state"] in ("IDLE", "DONE") for t in tabs):
            if os.path.exists(f.STOP_FLAG):
                print("\n[stop] stop requested - stopping between claims")
            return "done"
        for t in busy:
            pg, now = t["p"], time.time()
            try:
                st = t["state"]
                if st == "IDLE":
                    if stopping:
                        continue
                    shown = safe_rows(pg)
                    if shown is None:                    # still loading: look again next turn
                        continue
                    rows = [r for r in shown if r["status"].lower() == "submitted" and r["claim"]
                            and r["claim"] not in taken]
                    if not rows:
                        t["empty"] += 1
                        if t["empty"] > 2:               # this tab sees nothing more to take
                            t["state"] = "DONE"
                            continue
                        if any(r["claim"] in taken for r in shown) and cv.goto_next_page(pg):
                            continue                     # the page is all taken: the next page
                        _click(pg, "button:visible", "PROCEED")
                        t.update(state="WAIT_LIST", until=now + 30)
                        continue
                    row = rows[0]
                    taken.add(row["claim"])
                    if not reserve(row["claim"]):        # another dashboard has it
                        continue
                    t.update(row=row, mine=ours.get(row["claim"]) or {"acct": row["acct"], "any": True},
                             t0=now, empty=0, confirmed=False)
                    pg.locator("table tbody tr", has_text=row["claim"]).first.locator(
                        "button", has_text=BTN("REVIEW")).first.click()
                    t.update(state="WAIT_PREVIEW", until=now + f.LIMITS.limit("pri preview"))
                elif st == "WAIT_PREVIEW":
                    d = preview_once(pg) if "/claim-application-preview" in pg.url else {}
                    if d.get("Claim No.") and d.get("Applicable PRI") and pg.locator(
                            "button:visible", has_text=BTN("APPROVE")).count():
                        f.LIMITS.obs["pri preview"].append(now - t["t0"])
                        row, probs = t["row"], check_claim(d, t["row"], t["mine"])
                        if probs:
                            shot(pg, f"pri_mismatch_{row['acct']}")
                            cnt["mismatched"] += 1
                            log(row["acct"], row["claim"], "MISMATCH", "; ".join(probs))
                            print(f"[tab{t['n']}] {row['claim']}  acct {row['acct']}  MISMATCH  {'; '.join(probs)[:120]}", flush=True)
                            _click(pg, "button:visible", "BACK")
                            t.update(state="WAIT_BACK", until=now + 15)
                        else:
                            t["d"] = d
                            _click(pg, "button:visible", "APPROVE")
                            t.update(state="WAIT_APPROVE", until=now + f.STUCK_TIMEOUT_S)
                    elif now > t["until"]:
                        raise StuckError("claim preview did not open")
                elif st == "WAIT_APPROVE":
                    txt = cv.modal_text(pg)
                    if re.search(r"approved successfully", txt, re.I):
                        row = t["row"]
                        m = re.search(r"Claim application No\.?\s*(\d+)", txt, re.I)
                        cnt["approved"] += 1
                        streak = 0
                        log(row["acct"], row["claim"], "APPROVED",
                            f"claim {m.group(1) if m else row['claim']} PRI {t['d'].get('Applicable PRI')}")
                        print(f"[{cnt['approved'] + cnt['errors']:>4}] {row['claim']}  acct {row['acct']}  APPROVED   "
                              f"| approved {cnt['approved']}  mismatch {cnt['mismatched']}  errors {cnt['errors']}"
                              f"   (tab {t['n']}, {now - t['t0']:.1f} s)", flush=True)
                        _click(pg, ".modal-content:visible button", "OK")
                        t.update(state="AFTER_OK", until=now + 5)
                    elif not t["confirmed"] and pg.locator(".modal-content:visible button", has_text=BTN("CONFIRM")).count():
                        _click(pg, ".modal-content:visible button", "CONFIRM")
                        t["confirmed"] = True
                    elif now > t["until"]:
                        raise StuckError("no 'approved successfully' after APPROVE")
                elif st == "AFTER_OK":
                    if not cv.modal_text(pg).strip() or now > t["until"]:
                        if "/claim-application-preview" in pg.url and pg.locator("button:visible", has_text=BTN("BACK")).count():
                            _click(pg, "button:visible", "BACK")
                            t.update(state="WAIT_BACK", until=now + 15)
                        elif "/claim-application-list" in pg.url and pg.locator("button:visible", has_text=BTN("PROCEED")).count():
                            _click(pg, "button:visible", "PROCEED")
                            t.update(state="WAIT_LIST", until=now + 30)
                        else:
                            open_list(pg)
                            t["state"] = "IDLE"
                elif st == "WAIT_BACK":
                    if "/claim-application-list" in pg.url and pg.locator("button:visible", has_text=BTN("PROCEED")).count():
                        _click(pg, "button:visible", "PROCEED")
                        t.update(state="WAIT_LIST", until=now + 30)
                    elif now > t["until"]:
                        open_list(pg)
                        t["state"] = "IDLE"
                elif st == "WAIT_LIST":
                    if safe_rows(pg) or "No record(s) found" in f.body_text(pg) or now > t["until"]:
                        t["state"] = "IDLE"
            except KeyboardInterrupt:
                raise
            except Exception as e:
                import traceback
                with open("errors.log", "a") as ef:
                    ef.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} PRI-VERIFY tab {t['n']} state={t['state']}\n")
                    ef.write(traceback.format_exc())
                cnt["errors"] += 1
                streak += 1
                msg = (str(e).splitlines() or [type(e).__name__])[0][:100]
                row = t.get("row") or {}
                print(f"\n    [exc] tab {t['n']} {t['state']} {type(e).__name__}: {msg}", flush=True)
                if t["state"] in ("WAIT_APPROVE", "AFTER_OK") and row:      # APPROVE clicked: never again blindly
                    log(row.get("acct", ""), row.get("claim", ""), "CHECK_PORTAL", f"error after APPROVE: {msg}")
                try:
                    if pg.is_closed() or f.is_logged_out(pg):
                        print("[tabs] a tab is logged out / closed: going on with one tab", flush=True)
                        return "fallback"
                    open_list(pg)
                    t["state"] = "IDLE"
                except Exception:
                    t["state"] = "DONE"
                if streak >= cv.MAX_ERROR_STREAK:
                    print(f"\n[tabs] {streak} errors in a row: going on with one tab", flush=True)
                    return "fallback"
        page.wait_for_timeout(40)


def open_list(page):
    """Dashboard -> Claim Applications tab -> 'pending for Approval' View Details -> 2025-2026 / PRI /
    SUBMITTED / ALL / Branch -> PROCEED (the route the user showed)."""
    f.click_side_nav(page, "/dashboard")
    wait_for(page, lambda: "/dashboard" in page.url, 15, "dashboard")
    tab = page.locator(".nav-link", has_text=re.compile(r"^\s*Claim Applications\s*$", re.I))
    wait_for(page, lambda: tab.count(), 15, "Claim Applications tab")
    tab.first.click()
    btn = page.locator(".dashboard-box", has_text=re.compile("pending for Approval", re.I)) \
              .locator("button", has_text=re.compile("View Details", re.I))
    wait_for(page, lambda: btn.count(), f.STUCK_TIMEOUT_S, "'pending for Approval' View Details")
    btn.first.click()
    wait_for(page, lambda: "/claim-application-list" in page.url, f.STUCK_TIMEOUT_S, "claim list page")
    cl._select(page, "financialYear", CLAIM_FY)
    cl._select(page, "claimType", "PRI")
    cl._select(page, "claimStatus", "SUBMITTED")
    if page.locator('select[name="priSubmissionTypeID"]').count():
        cl._select(page, "priSubmissionTypeID", "ALL")
    if page.locator('select[name="branchOrPacs"]').count():
        cl._select(page, "branchOrPacs", "Branch")
    page.locator("button:visible", has_text=BTN("PROCEED")).first.click()
    wait_rows(page, 90)


def wait_rows(page, seconds) -> bool:
    """Wait for the list's ROWS (not just the table frame): True when rows show, False on 'No record(s) found'.
    Reading the list the moment the frame appeared made the job think it was empty and stop early."""
    state = wait_for(page, lambda: "rows" if cv.list_rows(page) else
                     ("none" if "No record(s) found" in f.body_text(page) else None), seconds, "claim table")
    if state == "none":                          # the message can linger a moment: look once more
        page.wait_for_timeout(1500)
        return bool(cv.list_rows(page))
    return True


# ---- in-browser waits: the browser says the moment a screen is ready (no Python polling round trips).
# The trace showed the portal approves in ~0.06 s: the seconds are the screens, not the server. ----
JS_PREVIEW_READY = r"""() => {
    if (!location.pathname.includes('claim-application-preview')) return false;
    const btn = [...document.querySelectorAll('button')].some(b => b.offsetParent !== null && /^\s*APPROVE\s*$/i.test(b.innerText));
    if (!btn) return false;
    const val = re => { for (const l of document.querySelectorAll('p.dataPreviewLabel')) {
        if (!re.test(l.innerText.trim())) continue;
        let v = l.nextElementSibling; while (v && !/darkText/.test(v.className)) v = v.nextElementSibling;
        return v && v.innerText.trim(); } return ''; };
    return !!(val(/^Claim No\.?$/i) && val(/^Applicable PRI$/i));
}"""
JS_MODAL_STEP = r"""() => {
    const ms = [...document.querySelectorAll('.modal-content')].filter(m => m.offsetParent !== null);
    const t = ms.map(m => m.innerText).join(' | ');
    if (/approved successfully/i.test(t)) return 'done';
    const c = ms.flatMap(m => [...m.querySelectorAll('button')]).find(b => /^\s*CONFIRM\s*$/i.test(b.innerText));
    if (c) { c.click(); return 'confirm'; }
    return false;
}"""
JS_APPROVED = r"""() => [...document.querySelectorAll('.modal-content')].filter(m => m.offsetParent !== null)
    .some(m => /approved successfully/i.test(m.innerText))"""
JS_CLICK_OK = r"""() => {
    const ms = [...document.querySelectorAll('.modal-content')].filter(m => m.offsetParent !== null);
    const b = ms.flatMap(m => [...m.querySelectorAll('button')]).find(b => /^\s*OK\s*$/i.test(b.innerText));
    if (b) b.click(); return !!b;
}"""
JS_NO_MODAL = "() => ![...document.querySelectorAll('.modal-content')].some(m => m.offsetParent !== null)"
JS_LIST_READY = r"""() => location.pathname.includes('claim-application-list')
    && [...document.querySelectorAll('table tbody tr')].some(tr => /^\d{6,}$/.test((tr.children[1] || {}).innerText || '')
       || [...tr.children].some(td => /^\d{12,}$/.test(td.innerText.trim())))"""


def fast_preview(page, row):
    """REVIEW -> the preview with its values (waited for inside the browser)."""
    tr = page.locator("table tbody tr", has_text=row["claim"]).first
    tr.locator("button", has_text=BTN("REVIEW")).first.evaluate("b => b.click()")
    page.wait_for_function(JS_PREVIEW_READY, timeout=int(f.LIMITS.limit("pri preview") * 1000))


def fast_approve(page, row) -> str:
    """APPROVE -> CONFIRM -> 'approved successfully' -> OK, each popup handled the moment it shows."""
    page.locator("button:visible", has_text=BTN("APPROVE")).first.evaluate("b => b.click()")
    # CONFIRM is clicked EXACTLY ONCE (inside the browser, the moment it shows); then only the success text
    st = page.wait_for_function(JS_MODAL_STEP, timeout=f.STUCK_TIMEOUT_S * 1000).json_value()
    if st != "done":
        page.wait_for_function(JS_APPROVED, timeout=f.STUCK_TIMEOUT_S * 1000)
    txt = cv.modal_text(page)
    m = re.search(r"Claim application No\.?\s*(\d+)", txt, re.I)
    page.evaluate(JS_CLICK_OK)
    try:
        page.wait_for_function(JS_NO_MODAL, timeout=5000)
    except Exception:
        pass
    return m.group(1) if m else row["claim"]


JS_WHERE = "() => (" + JS_LIST_READY + ")() ? 'list' : (location.pathname.includes('claim-application-preview') && (" + JS_NO_MODAL + ")() ? 'preview' : false)"


def fast_back_to_list(page):
    """After OK: the list (the portal reloads it by itself) or still the preview -> BACK at once. Waited for
    inside the browser; anything else -> BACK / PROCEED as before."""
    try:
        where = page.wait_for_function(JS_WHERE, timeout=4000).json_value()
    except Exception:
        where = None
    if where == "list":
        return
    if where == "preview":
        back = page.locator("button:visible", has_text=BTN("BACK"))
        if back.count():
            back.first.evaluate("b => b.click()")
            try:
                page.wait_for_function(JS_LIST_READY, timeout=5000)
                return
            except Exception:
                pass
    refresh_list(page)


def refresh_list(page):
    """After an approval: BACK to the list; when the list is there with rows that is enough (the approved claim
    is skipped as seen). Only an empty / missing list is reloaded (PROCEED), never the whole dashboard route."""
    if "/claim-application-preview" in page.url:
        back = page.locator("button:visible", has_text=BTN("BACK"))
        if back.count():
            back.first.click()
            try:
                wait_for(page, lambda: "/claim-application-list" in page.url and safe_rows(page), 5, "the claim list rows")
                return                           # list shown again: no reload needed
            except StuckError:
                pass
    proceed = page.locator("button:visible", has_text=BTN("PROCEED"))
    if "/claim-application-list" in page.url and proceed.count():
        proceed.first.click()
        try:
            wait_rows(page, 30)
            return
        except StuckError:
            pass
    open_list(page)                              # not on the list: the full route


def main(limit: int, assume_yes: bool):
    f.INPUT_CSV = f.OUTPUT_CSV
    header, rows, idx = f.load_rows()
    ours = our_claims(rows, idx)
    sol = Path(f.INPUT_CSV).parent.parent.name
    print(f"[pri] {RECORDS_CSV}: {len(ours)} PRI claims filed by our job (full checks); every other Submitted "
          f"PRI claim in the list is approved too, after the portal's own limits")
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(f.profile(PROFILE_DIR), headless=f.HEADLESS, slow_mo=f.SLOWMO_MS,
                                                   viewport=None, args=f.BROWSER_ARGS)
        ctx.set_default_timeout(f.STUCK_TIMEOUT_S * 1000)
        f.zoom_out(ctx)
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
        if not f.require_role(page, "Branch Head"):
            ctx.close()
            return
        ok, got, names = pri.branch_ok(page, sol)
        if not ok:
            print(f"\n  !! WRONG LOGIN: the portal is logged in as branch '{got}', this work list is SOL {sol} "
                  f"({' / '.join(sorted(names))}).\n     Nothing approved. Log in with the SOL {sol} Branch Head login.")
            ctx.close()
            return
        print(f"[login] branch check: portal '{got or '?'}' = SOL {sol} ({' / '.join(sorted(names)) or '?'})")
        f.no_scroll(page)   # logged in: no scrolling from here on

        open_list(page)
        if ours and not assume_yes:
            print("\n  Submitted PRI ADDITIONAL claims that our job filed will be APPROVED after the checks. Cannot be undone.")
            while (a := input("  >> type yes to approve, no to stop: ").strip().lower()) not in ("yes", "no"):
                pass
            if a == "no":
                ctx.close()
                return

        approved = errors = mismatched = skipped = 0
        fails: dict[str, int] = {}
        seen: set[str] = set()
        more = True                                  # the one-tab loop below: only when the tabs did not finish
        if TABS > 1:
            cnt = {"approved": 0, "errors": 0, "mismatched": 0}
            res = parallel_approve(ctx, page, ours, limit, cnt, seen)
            approved, errors, mismatched = cnt["approved"], cnt["errors"], cnt["mismatched"]
            for extra in ctx.pages[1:]:              # back to one tab
                try:
                    extra.close()
                except Exception:
                    pass
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            more = res != "done"
            if more:
                if f.is_logged_out(page):
                    f.require_login(page)
                open_list(page)
        while more and not (limit and approved >= limit):
            if os.path.exists(f.STOP_FLAG):
                print("\n[stop] stop requested - stopping between claims")
                break
            cand = [r for r in cv.list_rows(page) if r["status"].lower() == "submitted" and r["claim"]
                    and r["claim"] not in seen and fails.get(r["claim"], 0) < MAX_ROW_FAILS]
            if not cand:
                if cv.goto_next_page(page):
                    continue
                for attempt in range(2):         # two fresh looks before saying "no more"
                    open_list(page) if attempt else refresh_list(page)
                    cand = [r for r in cv.list_rows(page) if r["status"].lower() == "submitted" and r["claim"]
                            and r["claim"] not in seen and fails.get(r["claim"], 0) < MAX_ROW_FAILS]
                    if cand:
                        break
                if not cand:
                    break
            row = cand[0]
            if not reserve(row["claim"]):            # another dashboard's job has this claim
                seen.add(row["claim"])
                continue
            # every PRI claim in the Submitted list is approved (user, 14-10-2026): ours with the full checks,
            # others (filed by hand / another computer) with the portal's own limits
            mine = ours.get(row["claim"]) or {"acct": row["acct"], "any": True}
            if not mine:
                seen.add(row["claim"])
                skipped += 1
                continue
            stage = {"s": "review"}
            print(f"\r[{approved + errors + 1:>4}] {row['claim']}  acct {row['acct']}  checking...   ", end="", flush=True)
            T.reset()
            try:
                t_open = time.time()
                fast_preview(page, row)                  # waited for inside the browser
                f.LIMITS.obs["pri preview"].append(time.time() - t_open)
                T.mark("review")
                d = read_preview(page)
                T.mark("preview")
                probs = check_claim(d, row, mine)
                if probs:
                    shot(page, f"pri_mismatch_{row['acct']}")
                    cv.click_back(page)
                    seen.add(row["claim"])
                    mismatched += 1
                    log(row["acct"], row["claim"], "MISMATCH", "; ".join(probs))
                    print(f"\r[{approved + errors:>4}] {row['claim']}  acct {row['acct']}  MISMATCH  {'; '.join(probs)[:120]}", flush=True)
                    continue
                stage["s"] = "approving"
                no = fast_approve(page, row)
                T.mark("approve")
                stage["s"] = "approved"              # recorded below: a later error is not 'check on portal'
                approved += 1
                seen.add(row["claim"])
                log(row["acct"], row["claim"], "APPROVED", f"claim {no} PRI {d.get('Applicable PRI')}")
                print(f"\r[{approved + errors:>4}] {row['claim']}  acct {row['acct']}  APPROVED   | approved {approved}"
                      f"  mismatch {mismatched}  errors {errors}", flush=True)
                fast_back_to_list(page)              # the portal's own list reload, else BACK / PROCEED
                T.mark("list")
                print(T.line(), flush=True)
            except KeyboardInterrupt:
                raise
            except Exception as e:
                import traceback
                with open("errors.log", "a") as ef:
                    ef.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} PRI-VERIFY claim={row['claim']} stage={stage['s']} url={page.url}\n")
                    ef.write(traceback.format_exc())
                errors += 1
                msg = str(e).splitlines()[0][:100]
                shot(page, f"priverify_error_{row['acct']}")
                print(f"\n    [exc] stage={stage['s']} {type(e).__name__}: {msg}", flush=True)
                if stage["s"] == "approving":
                    seen.add(row["claim"])
                    log(row["acct"], row["claim"], "CHECK_PORTAL", f"error after APPROVE: {msg}")
                else:
                    fails[row["claim"]] = fails.get(row["claim"], 0) + 1
                    if fails[row["claim"]] >= MAX_ROW_FAILS:
                        log(row["acct"], row["claim"], "ERROR", msg)
                if f.is_logged_out(page):
                    f.require_login(page)
                f.reload_to_dashboard(page)
                open_list(page)
        if T.summary():
            print("\n" + T.summary())
        if limit and approved >= limit:            # say why the run ended
            print(f"\n[stop] 'At most {limit}' reached: {approved} approved this run. Empty At most = all.")
        elif not os.path.exists(f.STOP_FLAG):
            print("\n[list] no more Submitted claims of ours in the portal list")
        print(f"\n[done] {approved} approved, {mismatched} mismatch (not approved), {skipped} not ours, "
              f"{errors} errors.  Log: {APPROVALS_CSV}")
        if not f.NO_PROMPT:
            input("  >> press ENTER to close the browser: ")
        ctx.close()


def cli(scheme: str = "additional"):
    """PRI regular: same approval list (FY 2025-2026 / PRI / SUBMITTED); its claims are on 2025-26 loans."""
    global RECORDS_CSV, APPROVALS_CSV, LOAN_FY
    pri.configure(scheme)
    LOAN_FY = pri.LIST_FY
    example = "branches/3106/prireg/3106_prireg.csv" if scheme == "regular" else "branches/3106/pri/3106_pri.csv"
    ap = argparse.ArgumentParser(description=f"Approve Fasalrin 3% {pri.SCHEME} claims (Branch Head login).")
    ap.add_argument("csv", help=f"{pri.SCHEME} work list, e.g. {example}")
    ap.add_argument("--limit", type=int, default=0, help="approve at most N this run (0 = all)")
    ap.add_argument("--yes", action="store_true", help="don't ask before approving (needed with NO_PROMPT=1)")
    args = ap.parse_args()
    if not os.path.isfile(args.csv):
        ap.error(f"CSV not found: {args.csv}")
    if f.NO_PROMPT and not args.yes:
        ap.error("NO_PROMPT=1 cannot ask: pass --yes")
    f.INPUT_CSV = f.OUTPUT_CSV = args.csv
    base = os.path.splitext(args.csv)[0]
    RECORDS_CSV, APPROVALS_CSV = base + "_claims.csv", base + "_approvals.csv"
    try:
        main(args.limit, args.yes)
    except KeyboardInterrupt:
        print("\n[quit] interrupted - approvals so far are recorded; rerun to continue")


if __name__ == "__main__":
    cli("additional")
