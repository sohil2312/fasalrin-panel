import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import portal
import fasalrin_regular as regular
import fasalrin_verify as verify
import fasalrin_claim_verify as claims


class DirectApprovalTests(unittest.TestCase):
    def test_start_without_worklist_preserves_branch_and_confirmation(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(portal, 'ROOT', Path(tmp)), \
             patch.object(portal, 'RUNNING_DIR', Path(tmp) / '.running'), \
             patch.object(portal, 'STOP_FLAG', Path(tmp) / 'stop.flag'), \
             patch.object(portal, 'job', {}), patch.object(portal, 'busy_elsewhere', return_value=''), \
             patch.object(portal.subprocess, 'Popen') as spawn:
            spawn.return_value = Mock(pid=123, poll=Mock(return_value=None))
            self.assertEqual(portal.start_job('approve', '', '', approval_sol='3106'), 'tick the confirmation')
            self.assertIsNotNone(portal.start_job('approve', '', 'yes', approval_sol='../x'))
            self.assertIsNotNone(portal.start_job('entry', '', 'yes', approval_sol='3106'))
            self.assertEqual(portal.start_job('entry', '', 'yes'), 'pick a CSV')
            spawn.assert_not_called()
            for kind, suffix in [('approve', '3106.csv'), ('isclaim_approve', 'isclaim/3106_isclaim.csv')]:
                portal.job.clear()
                self.assertIsNone(portal.start_job(kind, '', 'yes', approval_sol='3106'))
                argv = spawn.call_args.args[0]
                self.assertIn(f'branches/3106/{suffix}', argv)
                self.assertIn('--branch-code', argv)
                self.assertIn('--yes', argv)
                if kind == 'isclaim_approve':
                    self.assertIn('--others', argv)
                self.assertFalse((Path(tmp) / 'branches/3106' / suffix).exists())
                portal.job['logfh'].close()

    def test_missing_list_only_allowed_for_approval(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(regular, 'INPUT_CSV', str(Path(tmp) / 'missing.csv')):
            with self.assertRaises(FileNotFoundError):
                regular.load_rows()
            sheet = verify.Sheet(allow_missing=True)
            self.assertEqual(sheet.by_acct, {})
            self.assertIn(regular.COL_ACCT, sheet.header)

    def test_existing_list_is_still_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / 'list.csv'
            p.write_text('Account No.,Branch Name\n31060001,Example\n')
            with patch.object(regular, 'INPUT_CSV', str(p)):
                header, rows, idx = regular.load_rows(allow_missing=True)
                self.assertEqual(rows[1][idx[regular.COL_ACCT]], '31060001')
                self.assertEqual(regular.branch_of_list(rows, idx), 'Example')

    def test_external_claim_still_checks_preview_and_rejects_rollover(self):
        row = {'acct': '31060001', 'claim': '123'}
        d = {'Account No.': row['acct'], 'Claim No.': '123', 'Applicable IS': '100',
             'Maximum Allowed Claim': '150', '_status': 'SUBMITTED', 'Is Rollover Claim': 'No'}
        self.assertEqual(claims.check_claim(d, row, {'any': True}, ''), [])
        for key, value in [('Account No.', '99990001'), ('Claim No.', '999'),
                           ('Applicable IS', '200'), ('_status', 'DRAFT'), ('Is Rollover Claim', 'Yes')]:
            with self.subTest(key=key):
                self.assertTrue(claims.check_claim(dict(d, **{key: value}), row, {'any': True}, ''))


if __name__ == '__main__':
    unittest.main()
