#!/usr/bin/env python3
"""
Fasalrin - approve 3% PRI REGULAR claims with the Branch Head login.

Same as fasalrin_pri_verify.py (FY 2025-2026 / PRI / SUBMITTED / ALL / Branch; only claims our PRI regular job
filed, after the Excel checks), with Loan Application FY = 2025-2026. PRI additional claims in the same list
(loan FY 2024-2025, other claim numbers) are walked past.

USAGE
  python fasalrin_prireg_verify.py branches/3106/prireg/3106_prireg.csv
"""
import fasalrin_pri_verify

if __name__ == "__main__":
    fasalrin_pri_verify.cli("regular")
