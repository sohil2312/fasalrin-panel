#!/usr/bin/env python3
"""
Fasalrin - file 3% PRI REGULAR claims (2025-26 loans) with the Branch User login.

Claim only: the loan application itself is entered (and approved) through IS regular first. Same steps and
rules as PRI additional (fasalrin_pri.py), with:
  IS/PRI Claim Application: FY 2025-2026 / PRI / PENDING -> search -> ADD the row whose Sanction/Rollover Date
  is within 3 days of Disb. Date (none / not in the list -> NO_MATCH / NOT_FOUND, hand work)
  form: COMPLETE, start = Disb. Date, end = Repay. Date (nearest allowed), Max Withdrawal = the portal's Loan
        Sanctioned Amount (flowchart), Applicable PRI = lower of 3% PRI and Maximum Allowed Claim
  -> SAVE & CONTINUE -> SUBMIT -> CONFIRM

USAGE
  python fasalrin_prireg.py branches/3106/prireg/3106_prireg.csv
"""
import fasalrin_pri

if __name__ == "__main__":
    fasalrin_pri.cli("regular")
