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
                    'CHECK_PORTAL:DRAFT', 'COMPLETED']
        with path.open('w', newline='') as fh:
            writer = csv.DictWriter(fh, fieldnames=cols)
            writer.writeheader()
            for n, status in enumerate(statuses, 1):
                writer.writerow({regular.COL_ACCT: f'3106000{n}',
                                 regular.COL_STATUS: status,
                                 regular.COL_DL: '1000', regular.COL_APPNO: '',
                                 regular.COL_DETAIL: status})

    def test_regular_worklist_resets_only_all_errors(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'branches/3106/3106.csv'
            self.write_rows(path)
            with patch.multiple(regular, INPUT_CSV=str(path), OUTPUT_CSV=str(path),
                                PROGRESS_CSV=str(path.with_name('3106_progress.csv'))):
                result = branches.reset_hand_work(path, ['ERROR', 'CHECK_PORTAL'])
            self.assertEqual(result, {'reset': {'ERROR': 2}, 'total': 2})
            with path.open(newline='') as fh:
                rows = list(csv.DictReader(fh))
            self.assertEqual([r[regular.COL_STATUS] for r in rows],
                             ['RETRY', 'RETRY', 'CHECK_PORTAL:DRAFT', 'COMPLETED'])

    def test_fresh_worklist_resets_only_all_errors(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'branches/3106/fresh/3106_fresh.csv'
            self.write_rows(path)
            result = branches.reset_hand_work(path, ['ERROR', 'CHECK_PORTAL'])
            self.assertEqual(result, {'reset': {'ERROR': 2}, 'total': 2})
            records = fresh.progress_records(path)
            self.assertEqual([records[f'3106000{n}']['Status'] for n in (1, 2)], ['', ''])
            self.assertNotIn('31060003', records)
            self.assertNotIn('31060004', records)
