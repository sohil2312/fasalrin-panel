import csv
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import branches
import fasalrin_fresh as fresh
import fasalrin_regular as regular


class ResetErrorsTests(unittest.TestCase):
    def write_rows(self, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        cols = [regular.COL_ACCT, regular.COL_STATUS, regular.COL_DL,
                regular.COL_APPNO, regular.COL_DETAIL]
        statuses = ['ERROR:TIMEOUTERROR', 'ERROR:STUCKERROR',
                    'CHECK_PORTAL:DRAFT', 'COMPLETED', 'CHECK_PORTAL:submitting']
        with path.open('w', newline='') as fh:
            writer = csv.DictWriter(fh, fieldnames=cols)
            writer.writeheader()
            for n, status in enumerate(statuses, 1):
                writer.writerow({regular.COL_ACCT: f'3106000{n}',
                                 regular.COL_STATUS: status,
                                 regular.COL_DL: '1000', regular.COL_APPNO: '',
                                 regular.COL_DETAIL: status})
        approval = path.parents[1] / '3106_approvals.csv' if path.parent.name == 'fresh' else path.parent / '3106_approvals.csv'
        with approval.open('w', newline='') as fh:
            writer = csv.DictWriter(fh, fieldnames=[regular.COL_ACCT, 'Approval'])
            writer.writeheader()
            writer.writerow({regular.COL_ACCT: '31060005', 'Approval': 'APPROVED'})

    def test_regular_worklist_can_reset_errors_and_portal_checks(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'branches/3106/3106.csv'
            self.write_rows(path)
            with patch.object(branches, 'BRANCHES', Path(tmp) / 'branches'), \
                 patch.multiple(regular, INPUT_CSV=str(path), OUTPUT_CSV=str(path),
                                PROGRESS_CSV=str(path.with_name('3106_progress.csv'))):
                counts = {r['reason']: r['count'] for r in branches.hand_work_counts(path)}
                result = branches.reset_hand_work(path, ['ERROR', 'CHECK_PORTAL'])
            self.assertEqual(counts['CHECK_PORTAL'], 1)
            self.assertEqual(result, {'reset': {'ERROR': 2, 'CHECK_PORTAL': 1}, 'total': 3})
            with path.open(newline='') as fh:
                rows = list(csv.DictReader(fh))
            self.assertEqual([r[regular.COL_STATUS] for r in rows],
                             ['RETRY', 'RETRY', 'RETRY', 'COMPLETED', 'CHECK_PORTAL:submitting'])
            self.assertIn('was CHECK_PORTAL:DRAFT', rows[2][regular.COL_DETAIL])

    def test_fresh_worklist_can_reset_errors_and_portal_checks(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'branches/3106/fresh/3106_fresh.csv'
            self.write_rows(path)
            with patch.object(branches, 'BRANCHES', Path(tmp) / 'branches'):
                result = branches.reset_hand_work(path, ['ERROR', 'CHECK_PORTAL'])
            self.assertEqual(result, {'reset': {'ERROR': 2, 'CHECK_PORTAL': 1}, 'total': 3})
            records = fresh.progress_records(path)
            self.assertEqual([records[f'3106000{n}']['Status'] for n in (1, 2)], ['', ''])
            self.assertEqual(records['31060003']['Status'], '')
            self.assertIn('was CHECK_PORTAL:DRAFT', records['31060003']['Detail'])
            self.assertNotIn('31060004', records)
            self.assertNotIn('31060005', records)

    def test_shared_loan_list_includes_fresh_failure_reasons(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'branches/3106/3106.csv'
            self.write_rows(path)
            with path.open('a', newline='') as fh:
                writer = csv.writer(fh)
                writer.writerow(['31060006', 'VILLAGE_NOT_FOUND', '', '', 'village missing'])
                writer.writerow(['31060007', 'AADHAAR_MISMATCH', '', '', 'name mismatch'])
            counts = {r['reason']: r['count'] for r in branches.hand_work_counts(path)}
            self.assertEqual(counts['VILLAGE_NOT_FOUND'], 1)
            self.assertEqual(counts['AADHAAR_MISMATCH'], 1)
            with patch.object(branches, 'BRANCHES', Path(tmp) / 'branches'), \
                 patch.multiple(regular, INPUT_CSV=str(path), OUTPUT_CSV=str(path),
                                PROGRESS_CSV=str(path.with_name('3106_progress.csv'))):
                result = branches.reset_hand_work(path, ['VILLAGE_NOT_FOUND', 'AADHAAR_MISMATCH'])
            self.assertEqual(result['total'], 2)
