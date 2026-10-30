"""Read-only entry preflight shared by the panel and both application runners."""
import collections
import csv
import re
from decimal import Decimal, InvalidOperation
from pathlib import Path

import fasalrin_regular as regular


def positive(value):
    try:
        number = Decimal(str(value or '').strip().replace(',', '').removeprefix('₹').strip())
        return number.is_finite() and number > 0
    except InvalidOperation:
        return False


def reasons(row, mode):
    """All blocking data problems for one row; preserve the existing script validations."""
    import fasalrin_fresh as fresh
    problems = []
    if not positive(row.get('DP')):
        problems.append('Missing, zero or invalid DP')
    aadhaar = re.sub(r'\D', '', row.get('Aadhaar No.', ''))
    if not aadhaar or not aadhaar.strip('0'):
        problems.append('Missing or zero Aadhaar')
    elif len(aadhaar) != 12 or not regular.aadhaar_valid(aadhaar):
        problems.append('Invalid Aadhaar')
    account, sol = row.get('Account No.', '').strip(), row.get('Sol ID', '').strip()
    if not account:
        problems.append('Missing account number')
    elif sol and not account.startswith(sol):
        problems.append('Account belongs to another branch')
    try:
        regular.parse_dmy(row.get('Disb. Date', ''))
    except (ValueError, TypeError):
        problems.append('Missing or invalid disbursement date')
    if mode == 'fresh':
        for col, label in [('DOB', 'birthdate'), ('RELATIVE NAME', 'relative name'), ('VILLAGE', 'village'),
                           ('SURVEY NO', 'survey number'), ('KHATA NO', 'khata number'),
                           ('NAME AS PER ADHAR', 'applicant name'), ('GENDER', 'gender'), ('ADDRESS', 'address')]:
            if not row.get(col, '').strip():
                problems.append('Missing ' + label)
        area = fresh.area_column(list(row))
        if not positive(row.get(area, '') if area else ''):
            problems.append('Missing, zero or invalid land area')
        # Keep the fresh script's age, scheme and portal-data checks as well.
        if not problems:
            prepared = dict(row, _area=row.get(area, ''))
            issue = fresh.precheck(prepared)
            if issue:
                problems.append(issue[1])
    return problems


def select_rows(rows, mode, only_reverify=False):
    import fasalrin_fresh as fresh
    if mode not in ('old', 'fresh'):
        raise ValueError('unknown entry mode')
    selected, counts = [], collections.Counter()
    skipped, filtered = 0, 0
    for index, row in enumerate(rows):
        status = row.get('Status', '').strip().upper()
        retry_fresh_account = mode == 'fresh' and status == 'CHECK_PORTAL:ACCOUNT'
        if status in {'COMPLETED', 'ALREADY_ON_PORTAL', 'DRY_OK'} or (
            status.startswith('CHECK_PORTAL') and not retry_fresh_account
        ):
            skipped += 1
            continue
        problems = reasons(row, mode)
        if problems:
            filtered += 1
            counts.update(problems)
            continue
        retry_aadhaar = (status == 'AADHAAR_REVERIFY' and regular.REVERIFY_RETRIED not in row.get('Detail', '')) or (
            status == 'RETRY' and row.get('Detail', '').strip() == regular.REVERIFY_RETRY_DETAIL)
        if mode == 'old':
            held = regular.is_done(status) and not retry_aadhaar
            held = held or (only_reverify and not retry_aadhaar)
        else:
            # Fixed data can run again; old NOT_IN_SYSTEM / NO_ACTIVITY rows may enter fresh.
            held = fresh.is_done(status) and status not in {'BAD_DATA', 'NO_LAND', 'SCHEME_UNKNOWN'}
        if held:
            skipped += 1
        else:
            selected.append(index)
    summary = {'total': len(rows), 'ready': len(selected), 'filtered': filtered, 'skipped': skipped,
               'reasons': dict(counts)}
    return selected, summary


def preview(csv_path, mode, only_reverify=False):
    import fasalrin_fresh as fresh
    with open(csv_path, newline='', encoding='utf-8-sig') as fh:
        rows = list(csv.DictReader(fh))
    records = fresh.progress_records(Path(csv_path))
    for row in rows:
        if row.get('Status', '').strip().upper() != 'RETRY':
            row.update(records.get(row.get('Account No.', '').strip(), {}))
    return select_rows(rows, mode, only_reverify)[1]
