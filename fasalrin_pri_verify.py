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
from fasalrin_regular import BTN, wait_for, shot, money_to_float

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


def read_preview(page) -> dict:
    for _ in range(100):
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
        page.wait_for_timeout(100)
    raise RuntimeError("PRI claim preview never populated")


def check_claim(d: dict, row: dict, mine: dict) -> list[str]:
    probs = []
    if d.get("Account No.", "").strip() != row["acct"]:
        probs.append(f"preview account {d.get('Account No.')!r} != row {row['acct']}")
    if d.get("Claim No.", "").strip() != row["claim"]:
        probs.append(f"preview claim {d.get('Claim No.')!r} != row {row['claim']}")
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


def open_list(page):
    """Dashboard -> Claim Applications tab -> 'pending for Approval' View Details -> 2025-2026 / PRI /
    SUBMITTED / ALL / Branch -> PROCEED (the route the user showed)."""
    f.click_side_nav(page, "/dashboard")
    page.wait_for_timeout(1000)
    tab = page.locator(".nav-link", has_text=re.compile(r"^\s*Claim Applications\s*$", re.I))
    wait_for(page, lambda: tab.count(), 15, "Claim Applications tab")
    tab.first.click()
    page.wait_for_timeout(800)
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
    page.wait_for_timeout(1000)
    wait_for(page, lambda: ("Total Count" in f.body_text(page) and page.locator("table.issCustomTable").count())
             or "No record(s) found" in f.body_text(page), 90, "claim table")
    page.wait_for_timeout(300)


def main(limit: int, assume_yes: bool):
    f.INPUT_CSV = f.OUTPUT_CSV
    header, rows, idx = f.load_rows()
    ours = our_claims(rows, idx)
    sol = Path(f.INPUT_CSV).parent.parent.name
    print(f"[pri] {RECORDS_CSV}: {len(ours)} PRI claims filed by our job (only these, after checks, get approved)")
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
        while not (limit and approved >= limit):
            if os.path.exists(f.STOP_FLAG):
                print("\n[stop] stop requested - stopping between claims")
                break
            cand = [r for r in cv.list_rows(page) if r["status"].lower() == "submitted" and r["claim"]
                    and r["claim"] not in seen and fails.get(r["claim"], 0) < MAX_ROW_FAILS]
            if not cand:
                if cv.goto_next_page(page):
                    continue
                open_list(page)
                cand = [r for r in cv.list_rows(page) if r["status"].lower() == "submitted" and r["claim"]
                        and r["claim"] not in seen and fails.get(r["claim"], 0) < MAX_ROW_FAILS]
                if not cand:
                    break
            row = cand[0]
            mine = ours.get(row["claim"])
            if not mine:                         # filed by hand / another job: never approved here
                seen.add(row["claim"])
                skipped += 1
                continue
            stage = {"s": "review"}
            print(f"\r[{approved + errors + 1:>4}] {row['claim']}  acct {row['acct']}  checking...   ", end="", flush=True)
            try:
                tr = page.locator("table tbody tr", has_text=row["claim"]).first
                tr.locator("button", has_text=BTN("REVIEW")).first.click()
                wait_for(page, lambda: "/claim-application-preview" in page.url and
                         page.locator("button:visible", has_text=BTN("APPROVE")).count(), f.STUCK_TIMEOUT_S, "claim preview")
                d = read_preview(page)
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
                no = cv.approve_one(page, row)
                approved += 1
                seen.add(row["claim"])
                log(row["acct"], row["claim"], "APPROVED", f"claim {no} PRI {d.get('Applicable PRI')}")
                print(f"\r[{approved + errors:>4}] {row['claim']}  acct {row['acct']}  APPROVED   | approved {approved}"
                      f"  mismatch {mismatched}  errors {errors}", flush=True)
                if "/claim-application-list" not in page.url or not cv.list_rows(page):
                    open_list(page)
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
