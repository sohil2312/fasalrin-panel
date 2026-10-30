import unittest
from unittest.mock import patch
import entry_filter


class EntryFilterTests(unittest.TestCase):
    def setUp(self):
        self.row = {'Sol ID':'3106', 'Account No.':'31060001', 'Aadhaar No.':'123456789012',
                    'DP':'1000', 'Disb. Date':'01-04-2025', 'DOB':'01-01-1980',
                    'RELATIVE NAME':'Relative', 'VILLAGE':'Village', 'SURVEY NO':'1', 'KHATA NO':'2',
                    'LAND':'0.25', 'NAME AS PER ADHAR':'Test', 'GENDER':'M', 'ADDRESS':'Test address',
                    'Scheme Code':'CC004', 'Status':'', 'Detail':''}
        self.check = patch('fasalrin_regular.aadhaar_valid', return_value=True)
        self.check.start()
        self.addCleanup(self.check.stop)

    def test_old_does_not_require_fresh_details(self):
        row={k:v for k,v in self.row.items() if k in ('Sol ID','Account No.','Aadhaar No.','DP','Disb. Date')}
        self.assertEqual(entry_filter.select_rows([row], 'old')[1]['ready'], 1)
        summary=entry_filter.select_rows([row], 'fresh')[1]
        self.assertEqual(summary['filtered'], 1)
        self.assertIn('Missing birthdate', summary['reasons'])
        self.assertGreater(sum(summary['reasons'].values()), summary['filtered'])

    def test_zero_missing_and_invalid_values(self):
        for mode in ('old','fresh'):
            for col in ('DP','Aadhaar No.'):
                for value in ('', '0', '000000000000'):
                    with self.subTest(mode=mode,col=col,value=value):
                        self.assertEqual(entry_filter.select_rows([dict(self.row, **{col:value})],mode)[1]['ready'],0)
        for value in ('-1','NaN','Infinity','oops'):
            self.assertFalse(entry_filter.positive(value))

    def test_fresh_requires_each_extra_field_even_for_dairy(self):
        for col in ('DOB','RELATIVE NAME','VILLAGE','SURVEY NO','KHATA NO','LAND'):
            row=dict(self.row, **{col:'', 'Scheme Code':'CC043'})
            self.assertEqual(entry_filter.select_rows([row],'fresh')[1]['filtered'],1)
        self.assertEqual(entry_filter.select_rows([self.row],'fresh')[1]['ready'],1)

    def test_progress_and_new_farmer_modes(self):
        import fasalrin_fresh as fresh
        rows=[dict(self.row, Status=s) for s in ('COMPLETED','CHECK_PORTAL:submitting','NOT_IN_SYSTEM','','CHECK_PORTAL:ACCOUNT')]
        self.assertEqual(entry_filter.select_rows(rows,'old')[0],[3])
        self.assertEqual(entry_filter.select_rows(rows,'fresh')[0],[2,3,4])
        self.assertFalse(fresh.is_done('CHECK_PORTAL:ACCOUNT'))
        self.assertEqual(rows[2]['Status'],'NOT_IN_SYSTEM')

    def test_once_only_reverify_filter(self):
        rows=[dict(self.row,Status='AADHAAR_REVERIFY'),
              dict(self.row,Status='AADHAAR_REVERIFY',Detail='(retried once)'),self.row]
        self.assertEqual(entry_filter.select_rows(rows,'old',True)[0],[0])
        self.assertEqual(entry_filter.select_rows(rows,'old')[0],[0,2])

    def test_empty_queue_does_not_open_browser(self):
        import csv, tempfile
        from pathlib import Path
        import fasalrin_regular as regular
        import fasalrin_fresh as fresh
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'3106.csv'
            row=dict(self.row, DP='0', **{'Loan App No':'', regular.COL_DL:''})
            with path.open('w',newline='') as fh:
                w=csv.DictWriter(fh,fieldnames=list(row)); w.writeheader(); w.writerow(row)
            with patch.multiple(regular, INPUT_CSV=str(path), OUTPUT_CSV=str(path),
                                PROGRESS_CSV=str(path.with_name('3106_progress.csv')), START_ROW=0, ONLY_REVERIFY=False), \
                 patch.object(regular,'sync_playwright') as old_browser, patch.object(fresh,'sync_playwright') as fresh_browser:
                regular.main()
                fresh.run(path,regular_worklist=True)
                old_browser.assert_not_called()
                fresh_browser.assert_not_called()

    def test_shared_fresh_progress_stops_old_and_fresh_reruns(self):
        import csv, tempfile
        from pathlib import Path
        from contextlib import ExitStack
        from unittest.mock import MagicMock
        import fasalrin_regular as regular
        import fasalrin_fresh as fresh
        with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
            path=Path(tmp)/'3106.csv'
            row=dict(self.row, Status='NOT_IN_SYSTEM', **{'Loan App No':'', regular.COL_DL:'1000'})
            with path.open('w',newline='') as fh:
                w=csv.DictWriter(fh,fieldnames=list(row)); w.writeheader(); w.writerow(row)
            stack.enter_context(patch.multiple(regular, INPUT_CSV=str(path), OUTPUT_CSV=str(path),
                PROGRESS_CSV=str(path.with_name('3106_progress.csv')), STOP_FLAG=str(Path(tmp)/'stop.flag')))
            regular.log_progress(row['Account No.'],'NOT_IN_SYSTEM','1000','','not found')
            browser=MagicMock()
            browser.return_value.__enter__.return_value.chromium.launch_persistent_context.return_value.pages=[MagicMock()]
            stack.enter_context(patch.object(fresh,'sync_playwright',browser))
            for fn in ('zoom_out','no_scroll'):
                stack.enter_context(patch.object(regular,fn))
            stack.enter_context(patch.object(regular,'is_logged_out',return_value=False))
            stack.enter_context(patch.object(regular,'require_role',return_value=True))
            stack.enter_context(patch.object(regular,'logged_in_branch',return_value=''))
            process=stack.enter_context(patch.object(fresh,'process_row',return_value=('COMPLETED','TEST123','submitted')))
            fresh.run(path,regular_worklist=True)
            process.assert_called_once()
            rec=regular.load_progress()[row['Account No.']]
            self.assertEqual(rec[:3],('COMPLETED','1000','TEST123'))
            for mode in ('old','fresh'):
                summary=entry_filter.preview(path,mode)
                self.assertEqual(summary['ready'],0)
                self.assertEqual(summary['skipped'],1)
            with path.open() as fh:
                saved=list(csv.DictReader(fh))
            self.assertEqual(saved[0]['Status'],'COMPLETED')
            self.assertEqual(saved[0]['RELATIVE NAME'],'Relative')

    def test_fresh_opens_known_farmer_when_account_not_offered(self):
        from unittest.mock import MagicMock
        import fasalrin_fresh as fresh
        page = MagicMock()
        page.locator.return_value.count.return_value = 1
        with patch.object(fresh.f, 'choose_account', return_value=False) as choose, \
             patch.object(fresh.f, 'active_pane', return_value='formTabs-tabpane-1'), \
             patch.object(fresh, 'wait_for', side_effect=lambda page, fn, seconds, what: fn()), \
             patch.object(fresh, 'to_dashboard') as dashboard:
            detail = fresh.open_known_farmer(page, self.row['Account No.'])
        self.assertIsNone(detail)
        choose.assert_called_once_with(page, self.row['Account No.'])
        page.locator.return_value.first.click.assert_called_once()
        dashboard.assert_not_called()

    def test_fresh_holds_when_unlisted_account_cannot_open_form(self):
        from unittest.mock import MagicMock
        import fasalrin_fresh as fresh
        page = MagicMock()
        page.locator.return_value.count.return_value = 1
        with patch.object(fresh.f, 'choose_account', return_value=False), \
             patch.object(fresh.f, 'active_pane', return_value=''), \
             patch.object(fresh, 'wait_for', side_effect=fresh.StuckError('not opened')), \
             patch.object(fresh.f, 'shot'), patch.object(fresh, 'to_dashboard') as dashboard:
            detail = fresh.open_known_farmer(page, self.row['Account No.'])
        self.assertIn('did not open', detail)
        dashboard.assert_called_once_with(page)

    def test_fresh_checks_known_account_tab_before_editing(self):
        from unittest.mock import MagicMock
        import fasalrin_fresh as fresh
        page = MagicMock()
        with patch.object(fresh.f, 'ensure_account', return_value='another account') as ensure, \
             patch.object(fresh, 'fill_text') as fill:
            detail = fresh.fill_account_tab(page, self.row['Account No.'], known=True)
        self.assertEqual(detail, 'another account')
        ensure.assert_called_once_with(page, self.row['Account No.'])
        fill.assert_not_called()

    def test_account_picker_waits_for_late_portal_option(self):
        from unittest.mock import MagicMock
        import fasalrin_regular as regular
        page = MagicMock()
        sel = page.locator.return_value.first
        sel.evaluate.side_effect = [[''], ['', self.row['Account No.']]]
        self.assertTrue(regular.choose_account(page, self.row['Account No.']))
        sel.select_option.assert_called_once_with(self.row['Account No.'])

    def test_known_account_tab_never_overwrites_another_account(self):
        from unittest.mock import MagicMock
        import fasalrin_regular as regular
        import fasalrin_fresh as fresh
        page = MagicMock()
        with patch.object(regular, 'pane', return_value=MagicMock()), \
             patch.object(fresh, 'form_values', return_value={'accountNumber': '99990001'}), \
             patch.object(fresh, 'fill_text') as fill:
            detail = regular.ensure_account(page, self.row['Account No.'])
        self.assertIn('another account', detail)
        fill.assert_not_called()

    def test_backend_routes_fresh_and_blocks_empty_queue(self):
        import csv, tempfile
        from pathlib import Path
        from unittest.mock import Mock
        import portal
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); path=root/'branches/3106/3106.csv'; path.parent.mkdir(parents=True)
            with path.open('w',newline='') as fh:
                w=csv.DictWriter(fh,fieldnames=list(self.row));w.writeheader();w.writerow(self.row)
            with patch.object(portal,'ROOT',root), patch.object(portal,'RUNNING_DIR',root/'.running'), \
                 patch.object(portal,'STOP_FLAG',root/'stop.flag'), patch.object(portal,'job',{}), \
                 patch.object(portal,'busy_elsewhere',return_value=''), patch.object(portal.subprocess,'Popen') as spawn:
                spawn.return_value=Mock(pid=123,poll=Mock(return_value=None))
                name='branches/3106/3106.csv'
                self.assertEqual(portal.start_job('entry_fresh',name,''),'tick the confirmation')
                self.assertIsNone(portal.start_job('entry_fresh',name,'yes', limit=2))
                self.assertIn('fasalrin_fresh.py',spawn.call_args.args[0])
                self.assertIn('--regular-worklist',spawn.call_args.args[0])
                self.assertIn(name,spawn.call_args.args[0])
                self.assertNotIn('--limit',spawn.call_args.args[0])
                portal.job['logfh'].close(); portal.job.clear(); spawn.reset_mock()
                with path.open('w',newline='') as fh:
                    w=csv.DictWriter(fh,fieldnames=list(self.row));w.writeheader();w.writerow(dict(self.row,DP='0'))
                self.assertIn('No eligible entries',portal.start_job('entry_fresh',name,'yes'))
                self.assertIn('No eligible entries',portal.start_job('entry',name,'yes'))
                spawn.assert_not_called()
