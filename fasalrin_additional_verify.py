#!/usr/bin/env python3
"""
Fasalrin — approve IS ADDITIONAL (rollover) claims with the Branch Head login.

Ported from the proven D:\\fasalrin\\15 additional\\fasalrin-is-additional\\verify.py:
  Dashboard -> 'Claim Applications' tab -> 'pending for Approval' View Details
  -> FY 2025-2026 / IS / SUBMITTED / ALL / Branch -> PROCEED
  row -> REVIEW -> read the preview -> checks -> APPROVE -> CONFIRM -> "approved successfully" -> OK

CHECKS before APPROVE (any failure -> BACK, never approved, MISMATCH)
  * the claim no is one our IS additional job filed (branches/<SOL>/additional/<SOL>_additional_claims.csv, CLAIMED)
  * preview account == row account == ours;  claim no == ours
  * Applicable IS == what we filed == min(work list Int Sub Amt, Maximum Allowed Claim) (to the paisa), > 0
  * First Loan Disbursal/Interest Cycle Start Date == work list Disb. Date (up to 3 days apart)
  * Rollover Date == work list Repay. Date (or start + 1 year - 1 day if earlier)   (bank flowchart for 1.5% IS Additional FY 2025-26)
  * Is Rollover Claim == Yes;  Loan Application FY == 2024-2025;  status SUBMITTED
Claims not filed by our IS additional job (IS regular ones included) are walked past.

RECORDS: branches/<SOL>/additional/<SOL>_additional_approvals.csv (append-only, fsync'd).

USAGE
  python fasalrin_additional_verify.py branches/3115/additional/3115_additional.csv
  NO_PROMPT=1 ... --yes [--limit N]
"""

from __future__ import annotations

import argparse
import csv
import os
import re
import time

from playwright.sync_api import sync_playwright

import fasalrin_regular as f
import fasalrin_claim_verify as cv
import fasalrin_additional as ad
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


def our_claims() -> dict:
    """claim no -> {acct, is} for every IS additional claim our job filed whose account is still CLAIMED
    (a later WRONG_CLAIM / RETRY row for the account takes the claim out)."""
    out, last = {}, {}
    if os.path.exists(RECORDS_CSV):
        with open(RECORDS_CSV, newline="", encoding="utf-8-sig") as fh:
            for e in csv.DictReader(fh):
                acct = e[f.COL_ACCT].strip()
                last[acct] = e["Result"].strip()
                if e["Result"].strip() == "CLAIMED" and e.get("Claim No", "").strip():
                    out[e["Claim No"].strip()] = {"acct": acct, "is": money_to_float(e["IS Claimed"])}
    return {c: m for c, m in out.items() if last.get(m["acct"]) == "CLAIMED"}


def work_rows(rows, idx) -> dict:
    """account -> {disb, repay, intsub} from the IS additional work list (the Excel)."""
    out = {}
    for r in rows[1:]:
        acct = r[idx[f.COL_ACCT]].strip()
        if acct:
            out[acct] = {"disb": cv._date_in(r[idx["Disb. Date"]]), "repay": cv._date_in(r[idx["Repay. Date"]]),
                         "intsub": money_to_float(r[idx["Int Sub Amt"]])}
    return out


def check_claim(d: dict, row: dict, mine: dict, xl: dict | None) -> list[str]:
    probs = []
    if d.get("Account No.", "").strip() != row["acct"]:
        probs.append(f"preview account {d.get('Account No.')!r} != row {row['acct']}")
    if d.get("Claim No.", "").strip() != row["claim"]:
        probs.append(f"preview claim {d.get('Claim No.')!r} != row {row['claim']}")
    if mine["acct"] != row["acct"]:
        probs.append(f"our claim {row['claim']} was for account {mine['acct']}")
    portal_is, max_allowed = money_to_float(d.get("Applicable IS")), money_to_float(d.get("Maximum Allowed Claim"))
    if abs(portal_is - mine["is"]) > 0.005:
        probs.append(f"applicable IS {portal_is:.2f} != ours {mine['is']:.2f}")
    if portal_is <= 0:
        probs.append("applicable IS is 0")
    if max_allowed > 0 and portal_is > max_allowed + 0.005:
        probs.append(f"IS {portal_is:.2f} > max allowed {max_allowed:.2f}")
    fd = cv._date_in(d.get("First Loan Disbursal/Interest Cycle Start Date", ""))
    rd = cv._date_in(d.get("Interest Cycle end/Rollover Date", ""))
    if not xl:
        probs.append(f"account {row['acct']} is not in the work list")
    elif not (fd and rd):
        probs.append("could not read disbursal/rollover dates")
    else:
        if not xl["disb"] or abs((fd - xl["disb"]).days) > 3:
            probs.append(f"start {fd:%d/%m/%Y} != Disb. Date {xl['disb']:%d/%m/%Y}" if xl["disb"] else "no Disb. Date in work list")
        want_rd = min(xl["repay"], ad.year_end(fd)) if xl["repay"] else None
        if rd != want_rd:
            probs.append(f"rollover {rd:%d/%m/%Y} != Repay. Date {xl['repay']:%d/%m/%Y} (capped at start + 1y - 1d)"
                         if xl["repay"] else "no Repay. Date in work list")
        want = min(xl["intsub"], max_allowed) if max_allowed > 0 else xl["intsub"]
        if abs(portal_is - want) > 0.005:
            probs.append(f"applicable IS {portal_is:.2f} != lower of Int Sub Amt {xl['intsub']:.2f} / max {max_allowed:.2f}")
    if not re.match(r"\s*yes", d.get("Is Rollover Claim", ""), re.I):
        probs.append(f"Is Rollover Claim = {d.get('Is Rollover Claim')!r}")
    if d.get("Loan Application FY", "").strip() != LOAN_FY:
        probs.append(f"loan FY {d.get('Loan Application FY')!r} != {LOAN_FY!r}")
    if not re.search(r"SUBMITTED", d.get("_status", ""), re.I):
        probs.append(f"status {d.get('_status')!r}")
    return probs


def main(limit: int, assume_yes: bool):
    f.INPUT_CSV = f.OUTPUT_CSV
    header, rows, idx = f.load_rows()
    ours = our_claims()
    xl = work_rows(rows, idx)
    print(f"[additional] {RECORDS_CSV}: {len(ours)} IS additional claims filed by our job (only these, after checks, get approved)")
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
        want = f.branch_of_list(rows, idx)
        if want:
            got = f.logged_in_branch(page)
            if got and not f.same_branch(got, want):
                print(f"\n  !! WRONG LOGIN: the portal is logged in as branch '{got}', this work list is '{want}'."
                      f"\n     Nothing approved. Log in with the {want} Branch Head login and start again.")
                ctx.close()
                return
            print(f"[login] branch check: portal '{got or '?'}' = work list '{want}'")

        cv.open_list(page)                       # same list as IS regular: 2025-2026 / IS / SUBMITTED / ALL / Branch
        if ours and not assume_yes:
            print("\n  Submitted IS ADDITIONAL claims that our job filed will be APPROVED after the checks. Cannot be undone.")
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
                print("\n[stop] stop requested — stopping between claims")
                break
            cand = [r for r in cv.list_rows(page) if r["status"].lower() == "submitted" and r["claim"]
                    and r["claim"] not in seen and fails.get(r["claim"], 0) < MAX_ROW_FAILS]
            if not cand:
                if cv.goto_next_page(page):
                    continue
                cv.open_list(page)
                cand = [r for r in cv.list_rows(page) if r["status"].lower() == "submitted" and r["claim"]
                        and r["claim"] not in seen and fails.get(r["claim"], 0) < MAX_ROW_FAILS]
                if not cand:
                    break
            row = cand[0]
            mine = ours.get(row["claim"])
            if not mine:                         # IS regular or filed by hand: never approved here
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
                d = cv.read_preview(page)
                probs = check_claim(d, row, mine, xl.get(row["acct"]))
                if probs:
                    shot(page, f"add_mismatch_{row['acct']}")
                    cv.click_back(page)
                    seen.add(row["claim"])
                    mismatched += 1
                    log(row["acct"], row["claim"], "MISMATCH", "; ".join(probs))
                    print(f"\r[{approved + errors:>4}] {row['claim']}  acct {row['acct']}  MISMATCH  {'; '.join(probs)[:120]}", flush=True)
                    continue
                stage["s"] = "approving"
                no = cv.approve_one(page, row)
                approved += 1
                log(row["acct"], row["claim"], "APPROVED", f"claim {no} IS {d.get('Applicable IS')}")
                print(f"\r[{approved + errors:>4}] {row['claim']}  acct {row['acct']}  APPROVED   | approved {approved}"
                      f"  mismatch {mismatched}  errors {errors}", flush=True)
                if "/claim-application-list" not in page.url or not cv.list_rows(page):
                    cv.open_list(page)
            except KeyboardInterrupt:
                raise
            except Exception as e:
                import traceback
                with open("errors.log", "a") as ef:
                    ef.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} ADDITIONAL-VERIFY claim={row['claim']} stage={stage['s']} url={page.url}\n")
                    ef.write(traceback.format_exc())
                errors += 1
                msg = str(e).splitlines()[0][:100]
                shot(page, f"addverify_error_{row['acct']}")
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
                cv.open_list(page)
        print(f"\n[done] {approved} approved, {mismatched} mismatch (not approved), {skipped} not ours, "
              f"{errors} errors.  Log: {APPROVALS_CSV}")
        if not f.NO_PROMPT:
            input("  >> press ENTER to close the browser: ")
        ctx.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Approve Fasalrin IS additional (rollover) claims (Branch Head login).")
    ap.add_argument("csv", help="IS additional work list, e.g. branches/3115/additional/3115_additional.csv")
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
        print("\n[quit] interrupted — approvals so far are recorded; rerun to continue")
