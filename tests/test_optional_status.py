import csv
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
import branches


class OptionalStatusTests(unittest.TestCase):
    def test_missing_blank_and_explicit_status_in_import_and_build(self):
        for include_status in (False, True):
            with self.subTest(include_status=include_status), tempfile.TemporaryDirectory() as tmp:
                master = Path(tmp) / 'master.xlsx'
                master.touch()
                header = list(branches.REGULAR_REQUIRED) + (['Status'] if include_status else [])
                records = []
                for i, status in enumerate(['', '  ', 'Pending', 'Draft', 'Approved']):
                    row = dict(zip(branches.REGULAR_REQUIRED,
                                   ['3106', 'Example', f'3106000{i}', '123456789012', '01-04-2025', '1000']))
                    row['Status'] = status
                    records.append([row[c] for c in header])
                sheet = Mock()
                sheet.iter_rows.side_effect = lambda **kw: iter([header] + records)
                workbook = Mock(worksheets=[sheet])
                with patch('openpyxl.load_workbook', return_value=workbook), \
                     patch.object(branches, '_cache', {}), patch.object(branches, 'ROOT', Path(tmp)), \
                     patch.object(branches, 'BRANCHES', Path(tmp) / 'branches'), \
                     patch.multiple(branches.f, INPUT_CSV='', OUTPUT_CSV='', PROGRESS_CSV='', REPORTS_DIR=Path(tmp)):
                    rows = branches.read_master(master)
                    self.assertEqual(rows[0]['Status'], 'Pending')
                    self.assertEqual(rows[1]['Status'], 'Pending')
                    expected = 4 if include_status else 5
                    self.assertEqual(branches.sols(master)[0]['pending'], expected)
                    branches.build(master, '3106')
                    with branches.worklist_path('3106').open() as fh:
                        built = list(csv.DictReader(fh))
                    self.assertEqual(len(built), expected)
                    self.assertEqual(built[0]['MIS Status'], 'Pending')
                    if include_status:
                        self.assertEqual(rows[-1]['Status'], 'Approved')
