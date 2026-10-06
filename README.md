# Fasalrin IS Regular — Loan Application automation

Enters new-FY (2025-2026) KCC Loan Applications on https://fasalrin.gov.in for every
row in `3119_IS_REG.csv`. Recorded from a live walkthrough on 2026-09-08.

## Run
```bash
cd "/Users/mufasa/Documents/Projects/fasalrin/is regular"
NO_PROMPT=1 PYTHONUNBUFFERED=1 nohup .venv/bin/python fasalrin_regular.py 3119_IS_REG.csv > run.log 2>&1 &
```
CSV path is the first argument (default `3119_IS_REG.csv`); the file is updated in place and a `<name>.bak` is kept.
Progress: `tr '\r' '\n' < run.log | grep -E "^\["`
1. Chromium opens on the portal. **Log in yourself** with the **Branch User** login (not Branch Head:
   that login has no entry form).
2. Press **ENTER** in the terminal. Don't touch the browser after that.
3. Script opens side nav → Loan Application → popup and walks the CSV.

Options: `--start-row N` (only CSV rows N and later this session; row 1 = header). Needs `openpyxl` in the venv.

Rows marked `AADHAAR_REVERIFY` before the REVERIFY step existed: tick **Retry Aadhaar-reverify rows once**
(and **Only those rows this run**) on the Enter applications job; a row that still fails gets "(retried once)".
Other hand-work reasons can be reset to `RETRY` in the CSV totals card (IS regular only).

## Entries made outside the script (by hand, other users)
The portal does not block a second application / claim for the same farmer, so the portal's own reports are
matched in the control panel (**Portal reports** card, every tab). The scripts never download reports.
1. Download that branch's report from the portal yourself:
   - IS regular: Reports → Loan Application → `BRANCH_WISE` / FY / `APPROVED` and `PENDING FOR APPROVAL(SUBMITTED)`
   - IS additional / PRI additional: Reports → Claim Application → `BRANCH_WISE` / FY 2025-2026 / `IS` or `PRI` / `APPROVED`
2. Pick the branch's work list, **Upload report** (.zip / .xlsx). It is saved in that list's `reports/` folder;
   a report of the wrong type, or with another SOL's accounts, is refused (reports are per branch).
3. Pick one of the saved reports and press **Match**: rows it shows as already on the portal (and not already
   done by us) → `ALREADY_ON_PORTAL` (IS regular: work list + progress file; claims: the records file).
- `REJECTED` / `REVIEW REQUIRED` applications are not in these reports → those accounts are entered again.
- Last guard: a preview page showing Submitted / Approved / Pending is never submitted (→ `ALREADY_ON_PORTAL`).

## What it does per row
| Step | Action |
|---|---|
| Popup | FY `2025-2026`, Aadhaar from CSV, FETCH RECORD |
| "Beneficiary details exist" | pick CSV `Account No.` in dropdown → OK |
| "Kindly click OK and enter farmer-details" | → `NOT_IN_SYSTEM`, next row |
| Applicant Details | REVERIFY shown → REVERIFY → VERIFY (Aadhaar re-verified; not verified → `AADHAAR_REVERIFY`), then Application Type = Normal → UPDATE & CONTINUE |
| Account Details | UPDATE & CONTINUE |
| Financial Details | sanctioned-on date = CSV `Disb. Date`; drawing limit = CSV `DP`; eligibility raised to DP **only if below DP** → SAVE & CONTINUE |
| Activity | Loan Sanctioned = DP → SAVE & CONTINUE |
| Term Loan | asserts Term Loan For Current FY = ₹0.00 → PREVIEW |
| Preview | verifies account, Aadhaar last-4, date, DL, eligibility, activity amount → SUBMIT → CONFIRM → OK, captures app no |

## Knobs (top of `fasalrin_regular.py`)
| Setting | Meaning | Now |
|---|---|---|
| `DRY_RUN` | `True` = fill + verify preview, press BACK, never submit | `False` |
| `PAUSE_BEFORE_SUBMIT` | wait for ENTER before each submit (`s`=skip, `q`=quit) | `False` |
| `SUBMIT_LIMIT` | stop after this many real submissions; `0` = no limit (runs until the CSV is done or Ctrl+C) | `0` |
| `MAX_ITER` | max rows walked in one run incl. skips; `0` = no limit | `0` |
| `STUCK_TIMEOUT_S` | seconds a screen may take to respond before it counts as stuck | `60` |
| `STUCK_RETRIES` | screen stuck → reload + dashboard + retry the row this many times | `2` |
| `SKIP_DP_ZERO` | rows with DP 0 → `DP_ZERO`, never entered | `True` |

Recommended: first run as-is (dry), check `Status` = `DRY_OK` and screenshots in `screenshots/dryrun_*`.
Then `DRY_RUN = False`, keep `PAUSE_BEFORE_SUBMIT = True` for a few, then set it `False`.
Stop any time with Ctrl+C — the CSV is saved after every row, rerun to resume.

## CSV columns written (after every row; `3119_IS_REG.csv.bak` kept)
- `Status`: `COMPLETED`, `ALREADY_ON_PORTAL` (submitted outside this script), `DRY_OK`,
  `NO_ACCT_DROPDOWN` (farmer exists but the popup has no account list — by hand), `NOT_IN_SYSTEM` (no record for Aadhaar), `ACCT_NOT_IN_DROPDOWN`,
  `NO_AADHAAR`, `DP_ZERO`, `NO_ACTIVITY` (new farmer, activity tab empty — needs land/crop/survey/khata by hand),
  `AADHAAR_REVERIFY` (portal asks REVERIFY on applicant tab — by hand), `APPLICANT_INCOMPLETE` (state/district/village etc. missing — by hand), `SKIPPED`, `ERROR:*`,
  `CHECK_PORTAL:*` (logged out after CONFIRM — check by hand)

## Manual follow-up list
Filter CSV `Status` = `NO_ACTIVITY`, `AADHAAR_REVERIFY`, `APPLICANT_INCOMPLETE`, `NOT_IN_SYSTEM`. Script never retries these.

## Handled automatically
- Primary Activity blank → "Agri Crops"
- Financial tab blank (new farmer) → eligibility & DL = DP
- Portal "Warning … exceeds ₹5,00,000 … please verify" modal → OK, save again
- Preview amounts in Indian grouping (`₹1,50,000.00`) compared numerically
- Full traceback of every failure in `errors.log`
- `Drawing Limit (entered)`, `Loan App No`, `Detail`

Re-running resumes: rows in a done state are skipped; untouched rows run next in CSV order;
`SKIPPED` / `ERROR:*` are retried at the end. `CHECK_PORTAL:*` is never retried automatically (it is
saved just before CONFIRM, so even a crash can't re-submit); after checking the portal set it to
`COMPLETED`, or to `RETRY` to run it again.
Every entry is also appended to `<csv name>_progress.csv` (forced to disk before the CSV is saved);
at start that file is read first and fills in any status the CSV is missing.

## Skipped before touching the browser
- Aadhaar not 12 digits (46 rows) → `NO_AADHAAR`
- DP 0 (83 rows) → `DP_ZERO`

## Control panel (all branches, from the master file)
```bash
.venv\Scripts\python portal.py
```
Opens http://localhost:8765 (this PC only; no farmer data leaves it).
To run it on a free Oracle Cloud machine in India instead (laptop can stay off), see
[deploy/oracle/ORACLE_SETUP.md](deploy/oracle/ORACLE_SETUP.md).
1. **Master list**: upload the bank's pendency `.xlsx` (all branches; saved in `master/`). Every SOL is listed.
2. **Build** a SOL → `branches/<SOL>/<SOL>.csv` = that SOL's master rows with Status **Pending** only (master Status
   kept as `MIS Status`), compared with that branch's own records in `branches/<SOL>/`: `<SOL>_progress.csv`,
   `reports/` (portal reports), `<SOL>_approvals.csv`. Rebuilding from a newer master keeps all progress.
3. **Start**: pick the work list, Enter (Branch User login) or Approve (Branch Head login), answer the question,
   log in in the Chromium window. Live rows/counts; **Stop after current row** (stop.flag) or Force stop.
Totals: **finished** = on the portal (submitted for approval or approved); **needs hand work** = skipped by the
script (not in system, Aadhaar reverify, no Aadhaar, DP 0, …; not retried); **to do** = next run does them.
Also `python branches.py <master.xlsx>` (list SOLs) / `python branches.py <master.xlsx> <SOL>` (build).

**Errors by hand → re-upload.** *Export error CSVs* writes `branches/<SOL>/hand_work/<SOL>_<REASON>.csv`, one per
reason (not in system, Aadhaar reverify, no Aadhaar, DP 0, …): all the row's data + `Reason`, `What to do`,
`Fixed (Y)` (zip download in the panel). Correct `Aadhaar No.` / `Disb. Date` / `DP` or fix the case on the portal,
put `Y` in `Fixed (Y)`, upload the file(s) with *Upload corrected CSVs*: those rows get the new values and Status
`RETRY` (entered on the next run); corrections are kept in `<SOL>_corrections.csv` and re-applied on every rebuild.
Rows Excel damaged (Aadhaar like `1.23457E+11`), wrong-branch accounts, bad dates / DP 0 are refused with the row
number. Keep Account / Aadhaar columns as Text in Excel.

## Approving (Branch Head login)
```bash
.venv\Scripts\python fasalrin_verify.py 310615REG3009.csv
```
Log in with the **Branch Head** login, press ENTER, type `yes`. The script opens Dashboard → View Details →
application list (FY / `SUBMITTED` / Branch → PROCEED) and approves every Submitted application:
REVIEW → APPROVE → CONFIRM → "approved successfully" → OK. It checks the preview shows the row's account and
that the approved number is the row's. Approving cannot be undone.
- Every approval → `<csv name>_approvals.csv` (append-only, read first at start) and the CSV columns
  `Approval` / `Approved On` (Status stays `COMPLETED`). Approvals found in the newest saved approved report
  (made by hand) are recorded too.
- An application failing twice → `ERROR` in `Approval`, skipped this run. Rerun continues with what is left.
- Own browser profile (`.pw_profile_head`), so it can run next to the entry script.

## Panel tabs: IS regular | IS additional
The panel has two tabs; each shows only its own master, branch lists, jobs and totals.

**IS regular** (2025-26 loans) — pendency master, `branches/<SOL>/<SOL>.csv`:
| Job | Login | Script | Records |
|---|---|---|---|
| Enter applications | Branch User | `fasalrin_regular.py` | `<SOL>_progress.csv` |
| Approve applications | Branch Head | `fasalrin_verify.py` | `<SOL>_approvals.csv` |
| File IS claims | Branch User | `fasalrin_claim.py` | `<SOL>_claims.csv` |
| Approve IS claims | Branch Head | `fasalrin_claim_verify.py` | `<SOL>_claim_approvals.csv` |

IS claim filing (approved loans only): IS/PRI Claim Application → FY / IS / PENDING → search account → ADD →
IS Submission Type **PARTIAL** first (unlocks the dates) → start = **Disb. Date** (a prefilled date wins) →
end **31-03-2026** → Max Withdrawal = form's Loan Sanctioned Amount → Applicable IS = lower of Maximum Allowed
Claim and `Int Sub Amt` → declaration → SAVE & CONTINUE → SUBMIT → CONFIRM. `Int Sub Amt` 0 → `NO_INT_SUB_AMT`.
Claim approval matches by **claim number** and checks the preview first (account, claim no, IS = ours,
IS ≤ Maximum Allowed, end 31/03/2026, start = Disb. Date, **Is Rollover Claim not Yes**, SUBMITTED); any
failure → `MISMATCH`, not approved. Claims not filed by the claim job → `NOT_OURS`.

**IS additional** (rollover claims for 2024-25 loans) — master `1.5% IS Additional FY 2025-26.xlsx` (Sheet1;
no Status column), `branches/<SOL>/additional/<SOL>_additional.csv` = that SOL's rows with `Int Sub Amt` > 0:
| Job | Login | Script | Records |
|---|---|---|---|
| File IS additional claims | Branch User | `fasalrin_additional.py` | `<SOL>_additional_claims.csv` |
| Approve IS additional claims | Branch Head | `fasalrin_additional_verify.py` | `<SOL>_additional_approvals.csv` |

Steps as in the bank's flowchart (1.5% IS Additional claim FY 2025-26): FY 2024-2025 / IS / ROLLOVER / ALL →
search → REVIEW the account's rows (highest Applicable IS first) until one shows start = `Disb. Date` (up to 3 days
apart) and end = 31/03/2025 (rows but none matching → `NO_MATCH`; no row → `NOT_FOUND`) → CONTINUE ROLLOVER → YES
(or "no longer available" → `CLOSED`) → Rollover Date = `Repay. Date`, or portal start + 1 year − 1 day if that
is earlier (the calendar allows nothing later) → Max Withdrawal = Loan Sanctioned → IS = lower of `Int Sub Amt`
and Maximum Allowed Claim → SAVE → SUBMIT → CONFIRM. Approval checks the same rules against the work list, plus
claim no and IS = ours, **Is Rollover Claim = Yes**, loan FY 2024-2025, SUBMITTED. A claim filed wrongly can be
marked `WRONG_CLAIM` in the records file: it is then never approved by the script.
Accounts with an approved rollover claim in a matched Claim Application report → `ALREADY_ON_PORTAL`.

**PRI additional** (3% PRI claims for 2024-25 loans) — master `3% PRI Additional FY 2025-26.xlsx` (Sheet1),
`branches/<SOL>/pri/<SOL>_pri.csv` = that SOL's rows with `3% PRI` > 0 (one row = account + Disb. Date):
| Job | Login | Script | Records |
|---|---|---|---|
| File PRI additional claims | Branch User | `fasalrin_pri.py` | `<SOL>_pri_claims.csv` |
| Approve PRI additional claims | Branch Head | `fasalrin_pri_verify.py` | `<SOL>_pri_approvals.csv` |

Filing (flowchart + the user's sample entries): FY 2024-2025 / PRI / PENDING → search → ADD the row whose
Sanction/Rollover Date is within 3 days of `Disb. Date` (one row is checked too; none → `NO_MATCH`; Eligible for
PRI = No → `NOT_ELIGIBLE`) → COMPLETE → start = `Disb. Date`, end = `Repay. Date` (nearest allowed date if greyed
out) → Max Withdrawal = `MAX(Dis_Amount)` (the portal's Loan Sanctioned when 0 or more than it) → PRI = lower of `3% PRI` and Maximum Allowed
Claim → SAVE → SUBMIT → CONFIRM. Loans in a matched PRI claim report → `ALREADY_ON_PORTAL`.
Approval: FY 2025-2026 / PRI / SUBMITTED / ALL / Branch, only claims our job filed, after checking claim no,
dates, PRI amount, COMPLETE, eligibility and loan FY 2024-2025. Login branch check by SOL (the PRI master spells
some branch names differently).

**PRI regular** (3% PRI claims for 2025-26 loans; claim only — the loan application is entered and approved
through IS regular first) — master `3% PRI Regular FY 2025-26.xlsx` (Sheet1, no Aadhaar), work lists
`branches/<SOL>/prireg/<SOL>_prireg.csv` (3% PRI > 0):
| Job | Login | Script | Records |
|---|---|---|---|
| File PRI regular claims | Branch User | `fasalrin_prireg.py` | `<SOL>_prireg_claims.csv` |
| Approve PRI regular claims | Branch Head | `fasalrin_prireg_verify.py` | `<SOL>_prireg_approvals.csv` |

Same steps and rules as PRI additional (the wrappers call `fasalrin_pri.py` / `fasalrin_pri_verify.py` with
`configure("regular")`), except: claim list FY **2025-2026** / PRI / PENDING; Max Withdrawal = the portal's Loan
Sanctioned Amount (flowchart); loan FY 2025-2026 in the approval checks and the claim report. Accounts not in the
PENDING list (loan not entered / approved yet) → `NOT_FOUND`, hand work. Every claim job checks the login role
(Branch User for filing, Branch Head for approval) before doing anything.

Every claim job: records are append-only and read first at start (reruns continue), `CLAIM_CHECK_PORTAL:draft`
/ `:submitting` saved after SAVE and before CONFIRM (never re-filed blindly — check on the portal), branch login
check, Stop after current row, optional "At most N" in the panel, own browser profile (`.pw_profile*`).
Error CSV export + re-upload work on both tabs (IS additional: `Fixed (Y)` → `RETRY` in its records).

## Excel version
`Fasal Rin IS Regular Upload Utility.xlsm` does the same row flow from Excel (ribbon tab **IS Regular Upload**),
driving Chrome through SeleniumBasic like the old `Fasal Rin Upload Utility` workbook. Its sheet `Instructions`
explains use. Sources and build are in `excel_utility/`:
```bash
python excel_utility/build_xlsm.py excel_utility/_build/unverified.xlsm
python excel_utility/finish_xlsm.py excel_utility/_build/unverified.xlsm "Fasal Rin IS Regular Upload Utility.xlsm"
python excel_utility/tests/test_flow.py "Fasal Rin IS Regular Upload Utility.xlsm"   # VBA flow vs mock portal
```
Use either the script or the workbook on a given CSV, not both: each keeps its own Status column.

## If it stalls
- Screen stuck (no response for `STUCK_TIMEOUT_S` = 60 s) → the page is reloaded, the script goes back to the
  dashboard and retries the same row, up to `STUCK_RETRIES` times, then `ERROR:timeout` and next row.
  If the reload drops the session it prints `!! Logged out after reload` and waits for you to log in.
  Stuck after CONFIRM → `CHECK_PORTAL` (never re-entered blindly).
- Unexpected page → `ERROR:*` + screenshot in `screenshots/`, then next row.
- Session dropped → prints `!! Session logged out`; log in again in the browser, press ENTER, it retries the same row (unless it was mid-submit → `CHECK_PORTAL`).
- Never type portal URLs into the address bar — a hard load logs you out.
