#!/usr/bin/env python3
"""
Branch work lists from the bank's master pendency file (all branches in one .xlsx).

    python branches.py "FASAL RIN PENDENCY 5.xlsx"            -> SOLs in the master with counts
    python branches.py "FASAL RIN PENDENCY 5.xlsx" 3106       -> build branches/3106/3106.csv

A work list = that SOL's rows whose master Status is Pending. The master's Status is the bank's
(often out of date), so it is kept as "MIS Status" and compared with the branch's own records,
all kept separately per branch in branches/<SOL>/:
    <SOL>.csv            work list (Status column = entry script's result)
    <SOL>_progress.csv   every entry the script made (account-keyed; survives a new master)
    <SOL>_approvals.csv  approvals (Branch Head script)
    reports/             portal Approved / Pending-for-approval reports downloaded with that login
Rebuilding from a newer master keeps all progress: statuses come back from the progress file, and
accounts found in the branch's saved portal reports are marked ALREADY_ON_PORTAL.
"""

from __future__ import annotations

import collections
import csv
import io
import os
import re
import sys
from datetime import date, datetime
from pathlib import Path

import fasalrin_regular as f

ROOT = Path(__file__).resolve().parent
BRANCHES = ROOT / "branches"
MASTER_COLS = {"sol": "Sol ID", "branch": "Branch Name", "status": "Status"}
WORK_STATUSES = {"pending"}                     # master Status values that go into a work list
MIS_STATUS = "MIS Status"
OURS = [f.COL_STATUS, f.COL_DL, f.COL_APPNO, f.COL_DETAIL]


def _cell(v) -> str:
    if v is None:
        return ""
    if isinstance(v, (datetime, date)):
        return v.strftime("%d-%m-%Y")            # entry script expects dd-mm-yyyy
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v).strip()


_cache: dict = {}


REGULAR_REQUIRED = [*MASTER_COLS.values(), f.COL_ACCT, f.COL_AADH, f.COL_DISB, f.COL_DP]
ADDITIONAL_REQUIRED = ["Sol ID", "Branch Name", f.COL_ACCT, "Int Sub Amt"]   # IS additional: no Status column


def read_master(path, required=None) -> list[dict]:
    """All rows of the master. The sheet used is the first whose header row (first row with >= 4 filled
    cells) has every `required` column, e.g. the IS additional file keeps its data in Sheet1 after a
    Sheet3 summary."""
    required = required or REGULAR_REQUIRED
    path = Path(path)
    key = (str(path), path.stat().st_mtime, tuple(required))
    if key in _cache:
        return _cache[key]
    import openpyxl
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    rows, best_missing = None, None
    try:                                         # read_only keeps the file open until close()
        for ws in wb.worksheets:
            it = ws.iter_rows(values_only=True)
            head = None
            for n, r in enumerate(it):
                if sum(1 for c in r if c not in (None, "")) >= 4:
                    head = [_cell(c) for c in r]
                    break
                if n > 20:
                    break
            missing = [c for c in required if c not in (head or [])]
            if missing:
                if best_missing is None or len(missing) < len(best_missing):
                    best_missing = missing
                continue
            rows = [{h: _cell(v) for h, v in zip(head, r) if h} for r in it if r and any(c not in (None, "") for c in r)]
            break
    finally:
        wb.close()
    if rows is None:
        raise ValueError("master is missing columns: " + ", ".join(best_missing or required))
    if len(_cache) > 3:                          # keep a few (regular + additional masters)
        _cache.clear()
    _cache[key] = rows
    return rows


def branch_dir(sol: str) -> Path:
    return BRANCHES / sol


def worklist_path(sol: str) -> Path:
    return branch_dir(sol) / f"{sol}.csv"


def sols(master) -> list[dict]:
    """[{sol, branch, rows, pending, built}] for every SOL in the master."""
    out = {}
    for r in read_master(master):
        s = out.setdefault(r["Sol ID"], {"sol": r["Sol ID"], "branch": r["Branch Name"], "rows": 0, "pending": 0})
        s["rows"] += 1
        s["pending"] += r["Status"].strip().lower() in WORK_STATUSES
    for s in out.values():
        s["built"] = worklist_path(s["sol"]).exists()
    return sorted(out.values(), key=lambda s: s["sol"])


def approvals(sol: str) -> dict:
    """account -> latest Approval result from branches/<SOL>/<SOL>_approvals.csv."""
    p = branch_dir(sol) / f"{sol}_approvals.csv"
    out = {}
    if p.exists():
        with open(p, newline="", encoding="utf-8-sig") as fh:
            for e in csv.DictReader(fh):
                if (e.get(f.COL_ACCT) or "").strip():
                    out[e[f.COL_ACCT].strip()] = (e.get("Approval") or "").strip()
    return out


def build(master, sol: str) -> dict:
    """Write branches/<SOL>/<SOL>.csv and compare it with the branch's progress + saved reports."""
    sol = str(sol).strip()
    rows = [r for r in read_master(master) if r["Sol ID"] == sol]
    if not rows:
        raise ValueError(f"SOL {sol} is not in the master")
    work = [r for r in rows if r["Status"].strip().lower() in WORK_STATUSES]
    d = branch_dir(sol)
    (d / "reports").mkdir(parents=True, exist_ok=True)
    path = worklist_path(sol)
    f.INPUT_CSV = f.OUTPUT_CSV = str(path)
    f.PROGRESS_CSV = str(d / f"{sol}_progress.csv")
    f.REPORTS_DIR = d / "reports"

    # an existing work list's results go into the progress file first, so a rebuild loses nothing
    if path.exists():
        old_h, old_rows, old_ix = f.load_rows()
        logged = f.load_progress()
        for r in old_rows[1:]:
            acct, st = r[old_ix[f.COL_ACCT]].strip(), r[old_ix[f.COL_STATUS]].strip()
            if acct and st and acct not in logged:
                f.log_progress(acct, st, r[old_ix[f.COL_DL]], r[old_ix[f.COL_APPNO]], r[old_ix[f.COL_DETAIL]])

    # every master column, in the master's order (its Status becomes MIS Status; ours come last)
    keep = [c for c in rows[0] if c != "Status" and c not in OURS and c != MIS_STATUS]
    header = keep + [MIS_STATUS] + OURS
    out_rows = [header]
    other = 0
    for r in work:
        if not r[f.COL_ACCT].startswith(sol):   # never another branch's account
            other += 1
            continue
        out_rows.append([r.get(c, "") for c in keep] + [r["Status"]] + [""] * len(OURS))
    ix = {c: i for i, c in enumerate(header)}

    # corrections the branch made by hand (uploaded reason CSVs) win over the master's values
    fixes = load_corrections(path)
    for r in out_rows[1:]:
        for c, v in fixes.get(r[ix[f.COL_ACCT]], {}).items():
            r[ix[c]] = v

    # compare 1: the branch's own progress file
    progress = f.load_progress()
    from_progress = 0
    for r in out_rows[1:]:
        e = progress.get(r[ix[f.COL_ACCT]])
        if e:
            r[ix[f.COL_STATUS]], r[ix[f.COL_DL]], r[ix[f.COL_APPNO]], r[ix[f.COL_DETAIL]] = e
            from_progress += 1

    # portal reports are not read here: upload one in the panel and press Match (match_report below);
    # what a Match marked is in the progress file, so a rebuild keeps it
    on_portal = sum(1 for r in out_rows[1:] if r[ix[f.COL_STATUS]] == "ALREADY_ON_PORTAL")

    f.save_rows(header, out_rows)
    b = collections.Counter(f.bucket(r[ix[f.COL_STATUS]]) for r in out_rows[1:])
    accts = {r[ix[f.COL_ACCT]] for r in out_rows[1:]}
    approved = sum(1 for a, e in approvals(sol).items() if a in accts and e == "APPROVED")
    return {"sol": sol, "branch": rows[0]["Branch Name"], "csv": str(path.relative_to(ROOT)),
            "master_rows": len(rows), "master_pending": len(work), "other_branch": other,
            "from_progress": from_progress, "on_portal": on_portal,
            "finished": b["finished"], "approved": approved, "hand": b["hand"], "check": b["check"],
            "to_do": b["todo"],
            "reports_from": "matched in the Portal reports card"}


# ----------------------------------------------------------------------------
# IS additional (rollover claims for 2024-25 loans): its own master and work lists
# ----------------------------------------------------------------------------
ADD_OURS = [f.COL_STATUS, "Applicable IS (entered)", "Claim App No", f.COL_DETAIL]
ADD_DONE = {"CLAIMED", "NOT_FOUND", "CLOSED"}         # never tried again (CLAIM_CHECK_PORTAL:* is held too)


def additional_dir(sol: str) -> Path:
    return branch_dir(sol) / "additional"


def additional_path(sol: str) -> Path:
    return additional_dir(sol) / f"{sol}_additional.csv"


def additional_records(csv_path) -> dict:
    """account -> latest entry of <stem>_claims.csv (written by fasalrin_additional.py)."""
    p = Path(csv_path)
    p = p.with_name(p.stem + "_claims.csv")
    out = {}
    if p.exists():
        with open(p, newline="", encoding="utf-8-sig") as fh:
            for e in csv.DictReader(fh):
                if (e.get(f.COL_ACCT) or "").strip():
                    out[e[f.COL_ACCT].strip()] = e
    return out


def _intsub(r) -> float:
    return f.money_to_float(r.get("Int Sub Amt", ""))


def sols_additional(master) -> list[dict]:
    """[{sol, branch, rows, pending (Int Sub Amt > 0), built}] for the IS additional master."""
    out = {}
    for r in read_master(master, ADDITIONAL_REQUIRED):
        s = out.setdefault(r["Sol ID"], {"sol": r["Sol ID"], "branch": r["Branch Name"], "rows": 0, "pending": 0})
        s["rows"] += 1
        s["pending"] += _intsub(r) > 0
    for s in out.values():
        s["built"] = additional_path(s["sol"]).exists()
    return sorted(out.values(), key=lambda s: s["sol"])


def build_additional(master, sol: str) -> dict:
    """branches/<SOL>/additional/<SOL>_additional.csv = that SOL's IS additional rows with Int Sub Amt > 0,
    compared with the branch's own IS additional records (so a rebuild keeps progress)."""
    sol = str(sol).strip()
    rows = [r for r in read_master(master, ADDITIONAL_REQUIRED) if r["Sol ID"] == sol]
    if not rows:
        raise ValueError(f"SOL {sol} is not in the IS additional master")
    work = [r for r in rows if _intsub(r) > 0 and r[f.COL_ACCT].startswith(sol)]
    other = sum(1 for r in rows if _intsub(r) > 0 and not r[f.COL_ACCT].startswith(sol))
    path = additional_path(sol)
    path.parent.mkdir(parents=True, exist_ok=True)
    keep = [c for c in rows[0] if c not in ADD_OURS]
    header = keep + ADD_OURS
    ix = {c: i for i, c in enumerate(header)}
    out_rows = [header] + [[r.get(c, "") for c in keep] + [""] * len(ADD_OURS) for r in work]

    recs = additional_records(path)
    for r in out_rows[1:]:
        e = recs.get(r[ix[f.COL_ACCT]])
        if e:
            r[ix[f.COL_STATUS]] = e.get("Result", "")
            r[ix["Applicable IS (entered)"]] = e.get("IS Claimed", "")
            r[ix["Claim App No"]] = e.get("Claim No", "")
            r[ix[f.COL_DETAIL]] = e.get(f.COL_DETAIL, "")
    f.OUTPUT_CSV = str(path)
    f.save_rows(header, out_rows)
    st = collections.Counter(additional_bucket(r[ix[f.COL_STATUS]]) for r in out_rows[1:])
    return {"sol": sol, "branch": rows[0]["Branch Name"], "csv": str(path.relative_to(ROOT)),
            "master_rows": len(rows), "with_int_sub": len(work), "zero_int_sub": len(rows) - len(work) - other,
            "other_branch": other, "claimed": st["claimed"], "closed_or_not_found": st["skipped"],
            "check": st["check"], "errors": st["error"], "to_do": st["todo"]}


def additional_bucket(status: str) -> str:
    s = status.strip().upper()
    if s == "CLAIMED":
        return "claimed"
    if s == "ALREADY_ON_PORTAL":         # approved claim found in the portal's claim report (filed by hand / others)
        return "on_portal"
    if s in ("NOT_FOUND", "CLOSED", "NO_MATCH", "NOT_ELIGIBLE"):
        return "skipped"
    if s.startswith("CLAIM_CHECK_PORTAL"):
        return "check"
    if s.startswith("CLAIM_ERROR") or s in ("MISMATCH", "WRONG_CLAIM"):
        return "error"
    return "todo"


def is_additional_list(csv_path) -> bool:
    p = Path(csv_path)
    return p.parent.name == "additional" and p.stem.endswith("_additional")


# ----------------------------------------------------------------------------
# PRI additional (3% PRI claims for 2024-25 loans): own master, branches/<SOL>/pri/<SOL>_pri.csv
# A row is one loan: account + Disb. Date (one account can have two rows), so records are keyed by both.
# ----------------------------------------------------------------------------
PRI_REQUIRED = ["Sol ID", "Branch Name", f.COL_ACCT, "Disb. Date", "Repay. Date", "MAX(Dis_Amount)", "3% PRI"]
# PRI regular (3% PRI claims on 2025-26 loans, claim only): own master with DP instead of MAX(Dis_Amount),
# work lists branches/<SOL>/prireg/<SOL>_prireg.csv, same records / reasons / approvals as PRI additional
PRIREG_REQUIRED = ["Sol ID", "Branch Name", f.COL_ACCT, "Disb. Date", "Repay. Date", "3% PRI"]
PRI_KINDS = {"pri": PRI_REQUIRED, "prireg": PRIREG_REQUIRED}
PRI_OURS = [f.COL_STATUS, "PRI Claimed", "Claim App No", f.COL_DETAIL]


def pri_dir(sol: str) -> Path:
    return branch_dir(sol) / "pri"


def pri_path(sol: str, kind: str = "pri") -> Path:
    return branch_dir(sol) / kind / f"{sol}_{kind}.csv"


def is_pri_list(csv_path) -> bool:
    """PRI additional or PRI regular work list (same records / reasons / approvals)."""
    p = Path(csv_path)
    return p.parent.name in PRI_KINDS and p.stem.endswith("_" + p.parent.name)


def is_prireg_list(csv_path) -> bool:
    p = Path(csv_path)
    return p.parent.name == "prireg" and p.stem.endswith("_prireg")


# ----------------------------------------------------------------------------
# IS fresh (farmers the portal does not know yet, entered from scratch): own Excel ("not in system"),
# work lists branches/<SOL>/fresh/<SOL>_fresh.csv, progress in <SOL>_fresh_progress.csv (fasalrin_fresh.py)
# ----------------------------------------------------------------------------
FRESH_REQUIRED = ["Sol ID", "Branch Name", f.COL_ACCT, "Scheme Code", f.COL_DISB, f.COL_DP, f.COL_AADH, "DOB",
                  "NAME AS PER ADHAR", "GENDER", "RELATIVE NAME", "ADDRESS", "VILLAGE"]


def is_fresh_list(csv_path) -> bool:
    p = Path(csv_path)
    return p.parent.name == "fresh" and p.stem.endswith("_fresh")


def fresh_path(sol: str) -> Path:
    return BRANCHES / str(sol).strip() / "fresh" / f"{str(sol).strip()}_fresh.csv"


def sols_fresh(master) -> list[dict]:
    """[{sol, branch, rows, pending (= rows: every row is a new farmer), built}] for the fresh Excel."""
    out = {}
    for r in read_master(master, FRESH_REQUIRED):
        s = out.setdefault(r["Sol ID"], {"sol": r["Sol ID"], "branch": r["Branch Name"], "rows": 0, "pending": 0})
        s["rows"] += 1
        s["pending"] += 1
    for s in out.values():
        s["built"] = fresh_path(s["sol"]).exists()
    return sorted(out.values(), key=lambda s: s["sol"])


def _fresh_rows(csv_path) -> list[dict]:
    """Fresh work-list rows with Status / Loan App No / Detail from the progress file (it wins over the CSV)."""
    import fasalrin_fresh as ff
    with open(csv_path, newline="", encoding="utf-8-sig") as fh:
        rows = list(csv.DictReader(fh))
    rec = ff.progress_records(csv_path)
    for r in rows:
        e = rec.get((r.get(f.COL_ACCT) or "").strip())
        if e:
            r.update(e)
    return rows


def fresh_bucket(status: str) -> str:
    import fasalrin_fresh as ff
    s = (status or "").strip().upper()
    if s in ("COMPLETED", "ALREADY_ON_PORTAL"):
        return "finished"
    if s.startswith(ff.HOLD):
        return "check"
    if s in ff.HAND:
        return "hand"
    return "error" if s.startswith("ERROR") else "todo"


def build_fresh(master, sol: str) -> dict:
    """branches/<SOL>/fresh/<SOL>_fresh.csv = that SOL's rows of the fresh Excel, keeping the progress made."""
    import fasalrin_fresh as ff
    sol = str(sol).strip()
    rows = [r for r in read_master(master, FRESH_REQUIRED) if r["Sol ID"] == sol]
    if not rows:
        raise ValueError(f"SOL {sol} has no rows in this file")
    path = fresh_path(sol)
    ff.write_worklist(path, rows)
    done = collections.Counter(fresh_bucket(r.get("Status") or "") for r in _fresh_rows(path))
    area = ff.area_column(list(rows[0]))
    return {"csv": str(path.relative_to(ROOT)), "sol": sol, "branch": rows[0]["Branch Name"], "master_rows": len(rows),
            "cc004": sum(1 for r in rows if r.get("Scheme Code", "").upper() == "CC004"),
            "cc043": sum(1 for r in rows if r.get("Scheme Code", "").upper() == "CC043"),
            "area_col": area or "", "finished": done["finished"], "hand": done["hand"], "check": done["check"],
            "errors": done["error"], "to_do": done["todo"]}


def pri_key(acct, disb) -> str:
    return f"{str(acct).strip()}|{str(disb).strip()}"


def _pri(r) -> float:
    return f.money_to_float(r.get("3% PRI", ""))        # NULL / blank -> 0


def pri_records(csv_path) -> dict:
    """account|Disb. Date -> latest entry of <stem>_claims.csv (written by fasalrin_pri.py)."""
    p = Path(csv_path)
    p = p.with_name(p.stem + "_claims.csv")
    out = {}
    if p.exists():
        with open(p, newline="", encoding="utf-8-sig") as fh:
            for e in csv.DictReader(fh):
                if (e.get(f.COL_ACCT) or "").strip():
                    out[pri_key(e[f.COL_ACCT], e.get("Disb. Date", ""))] = e
    return out


def sols_pri(master, kind: str = "pri") -> list[dict]:
    """[{sol, branch, rows, pending (3% PRI > 0), built}] for the PRI additional (kind 'pri') or
    PRI regular (kind 'prireg') master."""
    out = {}
    for r in read_master(master, PRI_KINDS[kind]):
        s = out.setdefault(r["Sol ID"], {"sol": r["Sol ID"], "branch": r["Branch Name"], "rows": 0, "pending": 0})
        s["rows"] += 1
        s["pending"] += _pri(r) > 0
    for s in out.values():
        s["built"] = pri_path(s["sol"], kind).exists()
    return sorted(out.values(), key=lambda s: s["sol"])


def sols_prireg(master) -> list[dict]:
    return sols_pri(master, "prireg")


def build_prireg(master, sol: str) -> dict:
    return build_pri(master, sol, "prireg")


def build_pri(master, sol: str, kind: str = "pri") -> dict:
    """branches/<SOL>/pri/<SOL>_pri.csv = that SOL's PRI additional rows with 3% PRI > 0 (NULL / 0 left out),
    compared with the branch's own PRI records (so a rebuild keeps progress)."""
    sol = str(sol).strip()
    rows = [r for r in read_master(master, PRI_KINDS[kind]) if r["Sol ID"] == sol]
    if not rows:
        raise ValueError(f"SOL {sol} is not in the {'PRI regular' if kind == 'prireg' else 'PRI additional'} master")
    work = [r for r in rows if _pri(r) > 0 and r[f.COL_ACCT].startswith(sol)]
    other = sum(1 for r in rows if _pri(r) > 0 and not r[f.COL_ACCT].startswith(sol))
    path = pri_path(sol, kind)
    path.parent.mkdir(parents=True, exist_ok=True)
    keep = [c for c in rows[0] if c not in PRI_OURS]
    header = keep + PRI_OURS
    ix = {c: i for i, c in enumerate(header)}
    out_rows = [header] + [[r.get(c, "") for c in keep] + [""] * len(PRI_OURS) for r in work]
    recs = pri_records(path)
    for r in out_rows[1:]:
        e = recs.get(pri_key(r[ix[f.COL_ACCT]], r[ix["Disb. Date"]]))
        if e:
            r[ix[f.COL_STATUS]] = e.get("Result", "")
            r[ix["PRI Claimed"]] = e.get("PRI Claimed", "")
            r[ix["Claim App No"]] = e.get("Claim No", "")
            r[ix[f.COL_DETAIL]] = e.get(f.COL_DETAIL, "")
    f.OUTPUT_CSV = str(path)
    f.save_rows(header, out_rows)
    st = collections.Counter(additional_bucket(r[ix[f.COL_STATUS]]) for r in out_rows[1:])
    return {"sol": sol, "branch": rows[0]["Branch Name"], "csv": str(path.relative_to(ROOT)),
            "master_rows": len(rows), "with_pri": len(work), "zero_pri": len(rows) - len(work) - other,
            "other_branch": other, "claimed": st["claimed"], "on_portal": st["on_portal"],
            "skipped": st["skipped"], "check": st["check"], "errors": st["error"], "to_do": st["todo"]}


def _pri_rows(csv_path) -> list[dict]:
    """PRI work-list rows with Status / PRI Claimed / Claim App No / Detail from the records files;
    a MISMATCH from approval wins (approvals are keyed by claim number)."""
    with open(csv_path, newline="", encoding="utf-8-sig") as fh:
        rows = list(csv.DictReader(fh))
    recs = pri_records(csv_path)
    appr = {}
    ap = Path(csv_path).with_name(Path(csv_path).stem + "_approvals.csv")
    if ap.exists():
        with open(ap, newline="", encoding="utf-8-sig") as fh:
            for e in csv.DictReader(fh):
                appr[(e.get("Claim No") or "").strip()] = e
    for r in rows:
        e = recs.get(pri_key(r.get(f.COL_ACCT, ""), r.get("Disb. Date", "")))
        if e:
            r[f.COL_STATUS], r["PRI Claimed"] = e.get("Result", ""), e.get("PRI Claimed", "")
            r["Claim App No"], r[f.COL_DETAIL] = e.get("Claim No", ""), e.get(f.COL_DETAIL, "")
        a = appr.get((r.get("Claim App No") or "").strip()) if r.get("Claim App No") else None
        if a and a.get("Result") in ("MISMATCH", "APPROVED"):
            r["Approval"] = a["Result"]
            if a["Result"] == "MISMATCH":
                r[f.COL_STATUS], r[f.COL_DETAIL] = "MISMATCH", a.get(f.COL_DETAIL, "")
    return rows


def scheme_of(csv_path) -> str:
    """'fresh' / 'prireg' / 'pri' / 'additional' / 'regular' for a work list path."""
    if is_fresh_list(csv_path):
        return "fresh"
    if is_prireg_list(csv_path):
        return "prireg"
    return "pri" if is_pri_list(csv_path) else "additional" if is_additional_list(csv_path) else "regular"


def branch_names(sol: str) -> set[str]:
    """Every Branch Name this SOL has in any of its work lists (masters spell some names differently,
    e.g. KIDIYA / Kidia; the portal says KIDIA) - for the login branch check by SOL."""
    names = set()
    for p in (worklist_path(sol), additional_path(sol), pri_path(sol), pri_path(sol, "prireg"), fresh_path(sol)):
        if p.exists():
            with open(p, newline="", encoding="utf-8-sig") as fh:
                for r in csv.DictReader(fh):
                    if (r.get("Branch Name") or "").strip():
                        names.add(r["Branch Name"].strip())
    return names


# ----------------------------------------------------------------------------
# applications that need work by hand, split by reason
# ----------------------------------------------------------------------------
REASONS = [   # (status or prefix, sheet name, what to do)
    ("NOT_IN_SYSTEM",        "Not in system",        "Aadhaar has no record on the portal: add the farmer by hand"),
    ("AADHAAR_REVERIFY",     "Aadhaar reverify",     "Portal asks to REVERIFY Aadhaar on the applicant tab"),
    ("NO_AADHAAR",           "No Aadhaar",           "Aadhaar missing / not 12 digits in the master"),
    ("DP_ZERO",              "DP zero",              "DP is 0 or blank in the master"),
    ("NO_ACTIVITY",          "No activity",          "Activity tab empty: add land / crop / survey no / khata"),
    ("APPLICANT_INCOMPLETE", "Applicant incomplete", "Applicant tab missing state / district / village etc."),
    ("ACCT_NOT_IN_DROPDOWN", "Account not offered",  "Portal does not offer this account for the Aadhaar"),
    ("NO_ACCT_DROPDOWN",     "No account list",      "Farmer exists but the popup shows no account list"),
    ("SCHEME_MISMATCH",      "Scheme mismatch",      "Activity does not match the Primary Activity (e.g. Animal Husbandry): "
                                                     "add an activity under it or change the primary activity"),
    ("OTHER_BRANCH",         "Other branch",         "Account does not start with this SOL"),
    ("CHECK_PORTAL",         "Check on portal",      "CONFIRM was clicked but the result is unknown: check the portal"),
    ("ERROR",                "Errors (retried)",     "Script error; the next run tries again (see Detail)"),
]
EXPORT_COLS = ["Sol ID", "Branch Name", f.COL_ACCT, "Cust id", f.COL_AADH, f.COL_DISB, f.COL_DP,
               MIS_STATUS, f.COL_STATUS, f.COL_DETAIL, f.COL_APPNO]


ADD_REASONS = [   # IS additional (rollover) work lists
    ("NOT_FOUND",          "Not found",       "Account not in the portal's ROLLOVER list (FY 2024-2025): check by hand"),
    ("CLOSED",             "Closed",          "Rollover no longer available (already rolled / closed)"),
    ("NO_MATCH",           "No matching row", "No rollover row with start = Disb. Date and end 31/03/2025: check Disb. Date / the portal"),
    ("WRONG_CLAIM",        "Wrong claim",     "Claim filed with wrong data: Branch Head must not approve it; get it rejected / corrected on the portal"),
    ("MISMATCH",           "Mismatch",        "Branch Head check failed (claim no / IS / rollover date / FY): not approved"),
    ("CLAIM_CHECK_PORTAL", "Check on portal", "Saved or CONFIRM clicked but the result is unknown: check the portal"),
    ("CLAIM_ERROR",        "Errors",          "Script error while filing (see Detail)"),
]


PRI_REASONS = [   # PRI additional work lists
    ("NOT_FOUND",          "Not found",       "Account not in the portal's PRI PENDING list (PRI additional: FY 2024-2025; PRI regular: FY 2025-2026 - loan not entered / approved yet?): check by hand"),
    ("NO_MATCH",           "No matching row", "No ADD row with Sanction/Rollover Date within 3 days of Disb. Date: check Disb. Date / the portal"),
    ("NOT_ELIGIBLE",       "Not eligible",    "Portal shows 'Eligible for PRI' = No: check by hand"),
    ("MISMATCH",           "Mismatch",        "Branch Head check failed (claim no / PRI / dates / type): not approved"),
    ("CLAIM_CHECK_PORTAL", "Check on portal", "Saved or CONFIRM clicked but the result is unknown: check the portal"),
    ("CLAIM_ERROR",        "Errors",          "Script error while filing (see Detail)"),
]


FRESH_REASONS = [   # IS fresh work lists
    ("EXISTS_ON_PORTAL",     "Already on portal",   "FETCH found this Aadhaar: the farmer exists - use IS regular entry or check by hand"),
    ("VERIFY_FAILED",        "Aadhaar verify failed", "Aadhaar VERIFY did not pass with NAME AS PER ADHAR: correct the name in the Excel"),
    ("VILLAGE_NOT_FOUND",    "Village not found",   "VILLAGE is not (or not uniquely) in the portal's Kadana list: correct it in the Excel"),
    ("NO_LAND",              "No land details",     "Crop loan (CC004) needs SURVEY NO, KHATA NO and land area in the Excel"),
    ("BAD_DATA",             "Bad data",            "Aadhaar / dates / DP / village 'CHECK' wrong in the Excel: correct it and upload again"),
    ("SCHEME_UNKNOWN",       "Unknown scheme",      "Scheme Code is not CC004 / CC043"),
    ("APPLICANT_INCOMPLETE", "Applicant incomplete", "Applicant tab refused the data (see Detail)"),
    ("CHECK_PORTAL",         "Check on portal",     "Stopped after a save or after CONFIRM: check the portal (a draft may exist)"),
    ("ERROR",                "Errors (retried)",    "Script error before anything was saved; the next run tries again"),
]


def reasons_for(csv_path):
    if is_fresh_list(csv_path):
        return FRESH_REASONS
    return PRI_REASONS if is_pri_list(csv_path) else ADD_REASONS if is_additional_list(csv_path) else REASONS


def reason_of(status: str, reasons=None) -> str | None:
    s = status.strip().upper()
    for key, _, _ in reasons or REASONS:
        if s == key or s.startswith(key + ":"):
            return key
    return None


def _additional_rows(csv_path) -> list[dict]:
    """Work-list rows with Status / Applicable IS / Claim App No / Detail taken from the records files
    (the IS additional jobs never write the work CSV while running); a MISMATCH from approval wins."""
    with open(csv_path, newline="", encoding="utf-8-sig") as fh:
        rows = list(csv.DictReader(fh))
    recs = additional_records(csv_path)
    appr = {}
    ap = Path(csv_path).with_name(Path(csv_path).stem + "_approvals.csv")
    if ap.exists():
        with open(ap, newline="", encoding="utf-8-sig") as fh:
            for e in csv.DictReader(fh):
                appr[(e.get(f.COL_ACCT) or "").strip()] = e
    for r in rows:
        e = recs.get((r.get(f.COL_ACCT) or "").strip())
        if e:
            r[f.COL_STATUS], r["Applicable IS (entered)"] = e.get("Result", ""), e.get("IS Claimed", "")
            r["Claim App No"], r[f.COL_DETAIL] = e.get("Claim No", ""), e.get(f.COL_DETAIL, "")
        a = appr.get((r.get(f.COL_ACCT) or "").strip())
        if a and a.get("Result") in ("MISMATCH", "APPROVED"):
            r["Approval"] = a["Result"]
            if a["Result"] == "MISMATCH":
                r[f.COL_STATUS], r[f.COL_DETAIL] = "MISMATCH", a.get(f.COL_DETAIL, "")
    return rows


def hand_work(csv_path) -> dict:
    """{reason: [row dicts]} for every row that is not finished and not simply to do."""
    reasons = reasons_for(csv_path)
    if is_fresh_list(csv_path):
        rows = _fresh_rows(csv_path)
    elif is_pri_list(csv_path):
        rows = _pri_rows(csv_path)
    elif is_additional_list(csv_path):
        rows = _additional_rows(csv_path)
    else:
        with open(csv_path, newline="", encoding="utf-8-sig") as fh:
            rows = list(csv.DictReader(fh))
    out = {k: [] for k, _, _ in reasons}
    for r in rows:
        k = reason_of(r.get(f.COL_STATUS) or "", reasons)
        if k:
            out[k].append(r)
    return out


def hand_work_counts(csv_path) -> list[dict]:
    hw = hand_work(csv_path)
    return [{"reason": k, "sheet": name, "todo": todo, "count": len(hw[k])}
            for k, name, todo in reasons_for(csv_path) if hw[k]]


# ---- export: one CSV per reason, for the branch to fill in --------------------------------------
FIXED = "Fixed (Y)"
EDITABLE = [f.COL_AADH, f.COL_DISB, f.COL_DP]       # the columns a correction may change
EXTRA_COLS = ["Reason", "What to do", FIXED]          # added after all the work list's own columns
NOT_EXPORTED = {"ERROR"}                            # retried by the script anyway


def hand_work_dir(csv_path) -> Path:
    return Path(csv_path).parent / "hand_work"


def export_reason_csvs(csv_path) -> list[Path]:
    """hand_work/<stem>_<REASON>.csv per reason (+ <stem>_summary.csv). Old exports are replaced."""
    csv_path = Path(csv_path)
    d = hand_work_dir(csv_path)
    d.mkdir(exist_ok=True)
    for old in d.glob(f"{csv_path.stem}_*.csv"):
        old.unlink()
    hw = hand_work(csv_path)
    with open(csv_path, newline="", encoding="utf-8-sig") as fh:
        all_cols = next(csv.reader(fh))                # all data: every column of the work list
    cols = [c for c in all_cols if c not in EXTRA_COLS] + EXTRA_COLS
    files, summary = [], []
    for key, name, todo in reasons_for(csv_path):
        rows = hw[key]
        if not rows or key in NOT_EXPORTED:
            continue
        p = d / f"{csv_path.stem}_{key}.csv"
        with open(p, "w", newline="", encoding="utf-8-sig") as fh:   # BOM: Excel opens it as UTF-8
            w = csv.writer(fh)
            w.writerow(cols)
            for r in rows:
                vals = {**r, "Reason": name, "What to do": todo, FIXED: ""}
                w.writerow([vals.get(c, "") for c in cols])
        files.append(p)
        summary.append([name, len(rows), p.name, todo])
    p = d / f"{csv_path.stem}_summary.csv"
    with open(p, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        w.writerow(["Reason", "Applications", "File", "What to do"])
        w.writerows(summary)
        w.writerow([])
        w.writerow(["How to clear: correct Aadhaar No. / Disb. Date / DP where needed, or fix the case on the portal,"
                    " then put Y in 'Fixed (Y)' and upload the file again in the control panel."])
        w.writerow(["Keep Account No. and Aadhaar No. as TEXT in Excel (not numbers), or the long digits get damaged."])
    return [p] + files


def export_zip(csv_path) -> bytes:
    import io as _io
    import zipfile
    buf = _io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for p in sorted(hand_work_dir(csv_path).glob(f"{Path(csv_path).stem}_*.csv")):
            z.write(p, p.name)
    return buf.getvalue()


# ---- re-upload: rows marked Fixed = Y go back into the work list as RETRY -------------------------
def _digits(v: str, field: str, n: int | None = None) -> str:
    s = (v or "").strip().strip('="').strip()
    if re.search(r"[eE][+-]?\d", s):
        raise ValueError(f"{field} {s!r} was changed by Excel into a number like 1.2E+11 — format the column as Text")
    s = re.sub(r"[\s-]", "", s)
    if s.endswith(".0"):
        s = s[:-2]
    if not s.isdigit() or (n and len(s) != n):
        raise ValueError(f"{field} {v!r} is not {n or 'only'} digits" if n else f"{field} {v!r} is not a number")
    return s


def _date(v: str) -> str:
    s = (v or "").strip()
    for fmt in ("%d-%m-%Y", "%d/%m/%Y", "%d.%m.%Y", "%Y-%m-%d", "%d-%m-%y", "%d/%m/%y"):
        try:
            return datetime.strptime(s, fmt).strftime("%d-%m-%Y")
        except ValueError:
            pass
    raise ValueError(f"Disb. Date {v!r} is not a date (use dd-mm-yyyy)")


def corrections_path(csv_path) -> Path:
    p = Path(csv_path)
    return p.with_name(p.stem + "_corrections.csv")


def load_corrections(csv_path) -> dict:
    """account -> latest {Aadhaar No., Disb. Date, DP} correction."""
    p, out = corrections_path(csv_path), {}
    if p.exists():
        with open(p, newline="", encoding="utf-8-sig") as fh:
            for e in csv.DictReader(fh):
                out[e[f.COL_ACCT]] = {c: e[c] for c in EDITABLE}
    return out


def apply_additional_corrections(csv_path, rows_in) -> dict:
    """IS additional re-upload: rows with Fixed = Y -> RETRY in the records file (the job reads that file),
    with a corrected Int Sub Amt if one was entered. Logged to <stem>_corrections.csv too."""
    csv_path = Path(csv_path)
    sol = csv_path.parent.parent.name
    with open(csv_path, newline="", encoding="utf-8-sig") as fh:
        work = {r[f.COL_ACCT].strip(): r for r in csv.DictReader(fh)}
    rec_path = csv_path.with_name(csv_path.stem + "_claims.csv")
    applied, unmarked, problems, fixes = 0, 0, [], []
    for n, u in enumerate(rows_in, start=2):
        if (u.get(FIXED) or "").strip().upper() not in ("Y", "YES"):
            unmarked += 1
            continue
        try:
            acct = _digits(u.get(f.COL_ACCT, ""), f.COL_ACCT)
            if not acct.startswith(sol):
                raise ValueError(f"account {acct} is not branch {sol}")
            if acct not in work:
                raise ValueError(f"account {acct} is not in this work list")
            amt = _digits(u.get("Int Sub Amt", "") or work[acct].get("Int Sub Amt", ""), "Int Sub Amt")
            if int(amt) <= 0:
                raise ValueError("Int Sub Amt must be more than 0")
        except ValueError as e:
            problems.append(f"row {n}: {e}")
            continue
        fixes.append((acct, amt))
        applied += 1
    if fixes:
        new = not rec_path.exists()
        with open(rec_path, "a", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            if new:
                w.writerow(["Time", f.COL_ACCT, "Claim No", "First Disbursal", "Rollover Date", "Loan Sanctioned",
                            "Max Allowed Claim", "Int Sub Amt", "IS Claimed", "Result", f.COL_DETAIL])
            for acct, amt in fixes:
                w.writerow([datetime.now().strftime("%Y-%m-%d %H:%M:%S"), acct, "", "", "", "", "", amt, "",
                            f.RETRY_STATUS, f"fixed by hand, uploaded {datetime.now():%d-%m-%Y %H:%M}"])
            fh.flush()
            os.fsync(fh.fileno())
        # a corrected Int Sub Amt goes into the work list itself
        f.INPUT_CSV = f.OUTPUT_CSV = str(csv_path)
        header, rows, ix = f.load_rows()
        amt_of = dict(fixes)
        for r in rows[1:]:
            a = r[ix[f.COL_ACCT]].strip()
            if a in amt_of and "Int Sub Amt" in ix:
                r[ix["Int Sub Amt"]] = amt_of[a]
        f.save_rows(header, rows)
    return {"applied": applied, "not_marked": unmarked, "problems": problems[:50], "problem_count": len(problems)}


def apply_pri_corrections(csv_path, rows_in) -> dict:
    """PRI re-upload: rows with Fixed = Y -> RETRY in the records file (keyed account + Disb. Date), with a
    corrected '3% PRI' / 'MAX(Dis_Amount)' written into the work list if one was entered."""
    csv_path = Path(csv_path)
    sol = csv_path.parent.parent.name
    f.INPUT_CSV = f.OUTPUT_CSV = str(csv_path)
    header, rows, ix = f.load_rows()
    work = {pri_key(r[ix[f.COL_ACCT]], r[ix["Disb. Date"]]): r for r in rows[1:]}
    rec_path = csv_path.with_name(csv_path.stem + "_claims.csv")
    applied, unmarked, problems, fixes = 0, 0, [], []
    for n, u in enumerate(rows_in, start=2):
        if (u.get(FIXED) or "").strip().upper() not in ("Y", "YES"):
            unmarked += 1
            continue
        try:
            acct = _digits(u.get(f.COL_ACCT, ""), f.COL_ACCT)
            if not acct.startswith(sol):
                raise ValueError(f"account {acct} is not branch {sol}")
            key = pri_key(acct, (u.get("Disb. Date") or "").strip())
            if key not in work:
                raise ValueError(f"account {acct} with Disb. Date {u.get('Disb. Date')!r} is not in this work list")
            pri = f.money_to_float(u.get("3% PRI", "") or work[key][ix["3% PRI"]])
            if pri <= 0:
                raise ValueError("3% PRI must be more than 0")
        except ValueError as e:
            problems.append(f"row {n}: {e}")
            continue
        fixes.append((key, acct, work[key][ix["Disb. Date"]], u.get("3% PRI", "").strip(), u.get("MAX(Dis_Amount)", "").strip()))
        applied += 1
    if fixes:
        from fasalrin_pri import HEADER as PRI_HEADER   # one header for the records file
        new = not rec_path.exists()
        with open(rec_path, "a", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            if new:
                w.writerow(PRI_HEADER)
            for key, acct, disb, _, _ in fixes:
                rec = {"Time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), f.COL_ACCT: acct, "Disb. Date": disb,
                       "Result": f.RETRY_STATUS, f.COL_DETAIL: f"fixed by hand, uploaded {datetime.now():%d-%m-%Y %H:%M}"}
                w.writerow([rec.get(c, "") for c in PRI_HEADER])
            fh.flush()
            os.fsync(fh.fileno())
        for key, _, _, pri, maxdis in fixes:
            if pri:
                work[key][ix["3% PRI"]] = pri
            if maxdis:
                work[key][ix["MAX(Dis_Amount)"]] = maxdis
        f.save_rows(header, rows)
    return {"applied": applied, "not_marked": unmarked, "problems": problems[:50], "problem_count": len(problems)}


# ---- IS regular: reset hand-work rows so the next entry run tries them again -----------------------
NOT_RESETTABLE = {"CHECK_PORTAL", "OTHER_BRANCH", "ERROR"}   # could double-submit / never ours / retried anyway


def reset_hand_work(csv_path, reasons: list[str]) -> dict:
    """IS regular work list: rows whose hand-work reason is in `reasons` -> Status RETRY (work list + progress
    file), so the next entry run tries them again. Returns {reason: rows reset}."""
    csv_path = Path(csv_path)
    if scheme_of(csv_path) != "regular":
        raise ValueError("reset is for IS regular work lists only")
    want = [k for k in reasons if k in {r[0] for r in REASONS} and k not in NOT_RESETTABLE]
    if not want:
        raise ValueError("tick at least one reason that can be reset")
    f.INPUT_CSV = f.OUTPUT_CSV = str(csv_path)
    f.PROGRESS_CSV = str(csv_path.with_name(csv_path.stem + "_progress.csv"))
    header, rows, ix = f.load_rows()
    done = collections.Counter()
    stamp = datetime.now().strftime("%d-%m-%Y %H:%M")
    for r in rows[1:]:
        k = reason_of(r[ix[f.COL_STATUS]] or "")
        if k not in want:
            continue
        old = r[ix[f.COL_STATUS]]
        r[ix[f.COL_STATUS]] = f.RETRY_STATUS
        r[ix[f.COL_DETAIL]] = f"reset to retry {stamp} (was {old})"
        f.log_progress(r[ix[f.COL_ACCT]].strip(), *(r[ix[c]] for c in OURS))
        done[k] += 1
    if done:
        f.save_rows(header, rows)
    return {"reset": dict(done), "total": sum(done.values())}


def apply_corrections(csv_path, text: str) -> dict:
    """Uploaded reason CSV: every row with Fixed = Y -> corrected values + Status RETRY in the work list."""
    csv_path = Path(csv_path)
    rows_in = list(csv.DictReader(io.StringIO(text.lstrip("﻿"))))
    if not rows_in or f.COL_ACCT not in rows_in[0] or FIXED not in rows_in[0]:
        raise ValueError(f"not an exported hand-work file (needs columns '{f.COL_ACCT}' and '{FIXED}')")
    if is_pri_list(csv_path):
        return apply_pri_corrections(csv_path, rows_in)
    if is_additional_list(csv_path):
        return apply_additional_corrections(csv_path, rows_in)
    f.INPUT_CSV = f.OUTPUT_CSV = str(csv_path)
    f.PROGRESS_CSV = str(csv_path.with_name(csv_path.stem + "_progress.csv"))
    header, rows, ix = f.load_rows()
    by_acct = {r[ix[f.COL_ACCT]].strip(): r for r in rows[1:]}
    sol = csv_path.stem if csv_path.stem == csv_path.parent.name else ""
    applied, unmarked, problems, saved = 0, 0, [], []
    for n, u in enumerate(rows_in, start=2):
        if (u.get(FIXED) or "").strip().upper() not in ("Y", "YES"):
            unmarked += 1
            continue
        try:
            acct = _digits(u.get(f.COL_ACCT, ""), f.COL_ACCT)
            if sol and not acct.startswith(sol):
                raise ValueError(f"account {acct} is not branch {sol}")
            r = by_acct.get(acct)
            if r is None:
                raise ValueError(f"account {acct} is not in this work list")
            fix = {f.COL_AADH: _digits(u.get(f.COL_AADH, ""), f.COL_AADH, 12),
                   f.COL_DISB: _date(u.get(f.COL_DISB, "")),
                   f.COL_DP: _digits(u.get(f.COL_DP, ""), f.COL_DP)}
            if int(fix[f.COL_DP]) <= 0:
                raise ValueError("DP must be more than 0")
        except ValueError as e:
            problems.append(f"row {n}: {e}")
            continue
        for c, v in fix.items():
            r[ix[c]] = v
        r[ix[f.COL_STATUS]] = f.RETRY_STATUS
        r[ix[f.COL_DETAIL]] = f"fixed by hand, uploaded {datetime.now():%d-%m-%Y %H:%M}"
        f.log_progress(acct, r[ix[f.COL_STATUS]], r[ix[f.COL_DL]], r[ix[f.COL_APPNO]], r[ix[f.COL_DETAIL]])
        saved.append([datetime.now().strftime("%Y-%m-%d %H:%M:%S"), acct, *(fix[c] for c in EDITABLE)])
        applied += 1
    if saved:
        cp = corrections_path(csv_path)
        new = not cp.exists()
        with open(cp, "a", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            if new:
                w.writerow(["Time", f.COL_ACCT, *EDITABLE])
            w.writerows(saved)
            fh.flush()
            os.fsync(fh.fileno())
        f.save_rows(header, rows)
    return {"applied": applied, "not_marked": unmarked, "problems": problems[:50], "problem_count": len(problems)}


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    if sys.argv[1].lower().endswith(".csv"):            # python branches.py branches/3106/3106.csv -> reason CSVs
        if len(sys.argv) == 3:                          # ... <work list.csv> <corrected reason.csv> -> re-upload
            print(apply_corrections(sys.argv[1], open(sys.argv[2], encoding="utf-8-sig").read()))
            sys.exit()
        for c in hand_work_counts(sys.argv[1]):
            print(f"{c['sheet']:<22} {c['count']:>5}  {c['todo']}")
        for p in export_reason_csvs(sys.argv[1]):
            print("saved", p)
        sys.exit()
    if len(sys.argv) == 2:
        for s in sols(sys.argv[1]):
            print(f"{s['sol']}  {s['branch']:<14} rows {s['rows']:>5}  pending {s['pending']:>5}"
                  f"  {'built' if s['built'] else ''}")
    else:
        res = build(sys.argv[1], sys.argv[2])
        for k, v in res.items():
            print(f"{k:>15}: {v}")


# ----------------------------------------------------------------------------
# portal reports: uploaded in the panel, listed per work list, matched on request
#   IS regular     -> Loan Application report(s) (Approved / Pending for approval): Account Number,
#                     Application ID, Application Status -> ALREADY_ON_PORTAL in work list + progress file
#   IS additional  -> Claim Application report (IS): approved rollover claims on 2024-25 loans
#   PRI additional -> Claim Application report (PRI): approved PRI claims on 2024-25 loans
#   additional / PRI -> ALREADY_ON_PORTAL appended to the records file (the job reads it first)
# ----------------------------------------------------------------------------
REPORT_EXT = (".zip", ".xlsx")


def reports_dir(csv_path) -> Path:
    return Path(csv_path).parent / "reports"


def _report_parts(path):
    import zipfile
    path = Path(path)
    if path.suffix.lower() == ".xlsx":
        return [path.read_bytes()]
    with zipfile.ZipFile(path) as z:
        return [z.read(n) for n in z.namelist() if n.lower().endswith(".xlsx")]


def report_info(path) -> dict:
    """{kind: 'loan' / 'claim IS' / 'claim PRI' / '?', rows, statuses, sols} - header + counts only
    (sols = SOL id prefix of the account numbers -> rows: a report belongs to one branch)."""
    import openpyxl
    kind, n, st, sols = "?", 0, collections.Counter(), collections.Counter()
    for data in _report_parts(path):
        wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True)
        it = wb.active.iter_rows(values_only=True)
        head = [str(c or "").strip() for c in next(it, [])]
        ia = head.index("Account Number") if "Account Number" in head else None
        if ia is not None:
            rows_ = [r for r in it if any(c not in (None, "") for c in r)]
            for r in rows_:
                a = r[ia]
                a = str(int(a)) if isinstance(a, float) else str(a or "").strip()
                sols[a[:4]] += 1
            it = iter(rows_)
        if "Claim Number" in head:
            ct, ist = head.index("Claim Type"), head.index("Application Status")
            for r in it:
                if any(c not in (None, "") for c in r):
                    n += 1
                    kind = f"claim {str(r[ct] or '').strip().upper()}"
                    st[str(r[ist] or "").strip()] += 1
        elif "Application ID" in head and "Account Number" in head:
            ist = head.index("Application Status")
            kind = "loan"
            for r in it:
                if any(c not in (None, "") for c in r):
                    n += 1
                    st[str(r[ist] or "").strip()] += 1
        wb.close()
    return {"kind": kind, "rows": n, "statuses": dict(st.most_common(4)), "sols": dict(sols.most_common(3))}


def wants_kind(csv_path) -> str:
    return {"regular": "loan", "fresh": "loan", "additional": "claim IS", "pri": "claim PRI",
            "prireg": "claim PRI"}[scheme_of(csv_path)]


def list_reports(csv_path) -> list[dict]:
    d = reports_dir(csv_path)
    out = []
    if d.exists():
        for p in sorted((p for p in d.iterdir() if p.suffix.lower() in REPORT_EXT),
                        key=lambda p: p.stat().st_mtime, reverse=True):
            try:
                info = report_info(p)
            except Exception as e:
                info = {"kind": "unreadable", "rows": 0, "statuses": {}, "error": str(e)[:80]}
            out.append({"name": p.name, "time": datetime.fromtimestamp(p.stat().st_mtime).strftime("%d-%m-%Y %H:%M"),
                        "size": p.stat().st_size, "fits": info["kind"] == wants_kind(csv_path), **info})
    return out


def save_report(csv_path, name: str, data: bytes) -> str:
    """Save an uploaded report next to the work list (never overwrites); refuses files of the wrong kind."""
    base = os.path.basename(name or "").strip()
    if not re.fullmatch(r"[\w .()%&+,-]{1,100}\.(zip|xlsx)", base, re.I):
        raise ValueError("report must be a .zip or .xlsx file")
    if not data.startswith(b"PK"):
        raise ValueError("not a zip / xlsx file")
    d = reports_dir(csv_path)
    d.mkdir(parents=True, exist_ok=True)
    dest = d / base
    if dest.exists():
        dest = d / f"{dest.stem}_{datetime.now():%Y%m%d_%H%M%S}{dest.suffix}"
    dest.write_bytes(data)
    try:
        info = report_info(dest)
    except Exception as e:
        dest.unlink(missing_ok=True)
        raise ValueError(f"cannot read the report: {str(e)[:120]}")
    if info["kind"] != wants_kind(csv_path):
        dest.unlink(missing_ok=True)
        raise ValueError(f"this is a '{info['kind']}' report; this work list needs a '{wants_kind(csv_path)}' report")
    sol = sol_of(csv_path)
    other = {k: v for k, v in info["sols"].items() if k != sol}
    if sol and other:                                  # reports are per branch: never another branch's
        dest.unlink(missing_ok=True)
        raise ValueError(f"this report has accounts of branch {', '.join(other)} - upload it on that branch's work list "
                         f"(this list is SOL {sol})")
    return dest.name


def sol_of(csv_path) -> str:
    p = Path(csv_path)
    return p.parent.parent.name if p.parent.name in ("additional", "pri", "prireg") else p.parent.name


def _append_records(rec_path: Path, header: list, recs: list[dict]):
    new = not rec_path.exists()
    with open(rec_path, "a", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        if new:
            w.writerow(header)
        for rec in recs:
            w.writerow([rec.get(c, "") for c in header])
        fh.flush()
        os.fsync(fh.fileno())


def match_report(csv_path, report_name: str, dry_run: bool = False) -> dict:
    """Match one saved report with the work list: rows the report shows as already on the portal (and not yet
    finished by us) -> ALREADY_ON_PORTAL. Returns counts. dry_run: only count, write nothing."""
    csv_path = Path(csv_path)
    path = reports_dir(csv_path) / os.path.basename(report_name)
    if not path.exists():
        raise ValueError("report not found")
    scheme = scheme_of(csv_path)
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    tag = f"portal report {path.name}"

    if scheme == "fresh":                              # approved loan report: those farmers are done
        import fasalrin_fresh as ff
        found = f.read_report(path)
        rows = _fresh_rows(csv_path)
        marked = already = 0
        for r in rows:
            acct = (r.get(f.COL_ACCT) or "").strip()
            if acct not in found:
                continue
            if fresh_bucket(r.get("Status") or "") in ("finished", "check"):
                already += 1
                continue
            marked += 1
            if not dry_run:
                ff.log_progress(csv_path, acct, "ALREADY_ON_PORTAL", found[acct][0], f"{tag}: {found[acct][1]}")
        return {"report_rows": len(found), "marked": marked, "already_done": already,
                "in_report_not_in_list": len(set(found) - {(r.get(f.COL_ACCT) or "").strip() for r in rows})}

    if scheme == "regular":
        found = f.read_report(path)                    # {acct: (application id, status)}
        f.INPUT_CSV = f.OUTPUT_CSV = str(csv_path)
        f.PROGRESS_CSV = str(csv_path.with_name(csv_path.stem + "_progress.csv"))
        header, rows, ix = f.load_rows()
        marked = already = 0
        for r in rows[1:]:
            hit = found.get(r[ix[f.COL_ACCT]].strip())
            if not hit:
                continue
            if f.is_done(r[ix[f.COL_STATUS]]):
                already += 1
                continue
            marked += 1
            if dry_run:
                continue
            r[ix[f.COL_STATUS]], r[ix[f.COL_APPNO]] = "ALREADY_ON_PORTAL", hit[0]
            r[ix[f.COL_DETAIL]] = f"{tag}: {hit[1]} (entered outside the script)"
            f.log_progress(r[ix[f.COL_ACCT]].strip(), *(r[ix[c]] for c in OURS))
        if marked and not dry_run:
            f.save_rows(header, rows)
        return {"report_rows": len(found), "marked": marked, "already_done": already,
                "in_report_not_in_list": len(set(found) - {r[ix[f.COL_ACCT]].strip() for r in rows[1:]})}

    if scheme == "additional":
        import fasalrin_additional as ad
        found = ad.read_claim_report(path)             # {acct: (claim no, user)}
        recs = additional_records(csv_path)
        with open(csv_path, newline="", encoding="utf-8-sig") as fh:
            work = list(csv.DictReader(fh))
        new, already = [], 0
        for r in work:
            acct = (r.get(f.COL_ACCT) or "").strip()
            if acct not in found:
                continue
            st = (recs.get(acct) or {}).get("Result", "").strip()
            if st in ("CLAIMED", "ALREADY_ON_PORTAL") or st.startswith("CLAIM_CHECK_PORTAL"):
                already += 1
                continue
            no, user = found[acct]
            new.append({"Time": now, f.COL_ACCT: acct, "Claim No": no, "Int Sub Amt": r.get("Int Sub Amt", ""),
                        "Result": "ALREADY_ON_PORTAL", f.COL_DETAIL: f"{tag}: approved claim {no} (filed by {user or '?'})"})
        if new and not dry_run:
            _append_records(csv_path.with_name(csv_path.stem + "_claims.csv"), ad.HEADER, new)
        return {"report_rows": len(found), "marked": len(new), "already_done": already,
                "in_report_not_in_list": len(set(found) - {(r.get(f.COL_ACCT) or "").strip() for r in work})}

    import fasalrin_pri as pri
    loan_fy = "2025-2026" if scheme == "prireg" else "2024-2025"   # PRI regular / PRI additional claims
    found = pri.read_pri_report(path, loan_fy)         # {acct: [(claim no, loan disbursal date, user)]}
    recs = pri_records(csv_path)
    with open(csv_path, newline="", encoding="utf-8-sig") as fh:
        work = list(csv.DictReader(fh))
    new, already = [], 0
    for r in work:
        acct, disb_s = (r.get(f.COL_ACCT) or "").strip(), (r.get("Disb. Date") or "").strip()
        disb = pri.dmy(disb_s)
        hit = next(((no, d, u) for no, d, u in found.get(acct, [])
                    if d is None or disb is None or abs((d - disb).days) <= pri.SLACK_DAYS), None)
        if not hit:
            continue
        st = (recs.get(pri_key(acct, disb_s)) or {}).get("Result", "").strip()
        if st in ("CLAIMED", "ALREADY_ON_PORTAL") or st.startswith("CLAIM_CHECK_PORTAL"):
            already += 1
            continue
        new.append({"Time": now, f.COL_ACCT: acct, "Disb. Date": disb_s, "Claim No": hit[0], "3% PRI": r.get("3% PRI", ""),
                    "Result": "ALREADY_ON_PORTAL", f.COL_DETAIL: f"{tag}: approved PRI claim {hit[0]} (filed by {hit[2] or '?'})"})
    if new and not dry_run:
        _append_records(csv_path.with_name(csv_path.stem + "_claims.csv"), pri.HEADER, new)
    return {"report_rows": sum(len(v) for v in found.values()), "marked": len(new), "already_done": already,
            "in_report_not_in_list": len(set(found) - {(r.get(f.COL_ACCT) or "").strip() for r in work})}
