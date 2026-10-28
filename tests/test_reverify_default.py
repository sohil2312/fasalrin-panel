import ast
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import fasalrin_regular as regular


class ReverifyDefaultTests(unittest.TestCase):
    def test_cli_defaults_to_retry_without_only_filter(self):
        tree = ast.parse(Path(regular.__file__).read_text())
        cli = tree.body[-1]
        self.assertIsInstance(cli, ast.If)
        scope = dict(vars(regular), main=Mock())
        with tempfile.NamedTemporaryFile(suffix='.csv') as csv_file, patch('sys.argv', ['fasalrin_regular.py', csv_file.name]):
            exec(compile(ast.Module(body=cli.body, type_ignores=[]), regular.__file__, 'exec'), scope)
        self.assertTrue(regular.RETRY_REVERIFY)
        self.assertTrue(scope['RETRY_REVERIFY'])
        self.assertFalse(scope['ONLY_REVERIFY'])
        scope['main'].assert_called_once()

    def test_automatic_retry_keeps_already_retried_and_completed_rows(self):
        tree = ast.parse(Path(regular.__file__).read_text())
        main = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'main')
        retry = next(n for n in ast.walk(main) if isinstance(n, ast.If)
                     and isinstance(n.test, ast.Name) and n.test.id == 'RETRY_REVERIFY')
        rows = [['account', 'status', 'detail', 'dl', 'app'],
                ['31060001', 'AADHAAR_REVERIFY', 'needs verify', '', ''],
                ['31060002', 'AADHAAR_REVERIFY', regular.REVERIFY_RETRIED, '', ''],
                ['31060003', 'COMPLETED', '', '', ''],
                ['31060004', '', '', '', '']]
        scope = dict(vars(regular), rows=rows, header=rows[0], first=1,
                     a_i=0, s_i=1, de_i=2, dl_i=3, no_i=4, reverify_retry=set(),
                     log_progress=Mock(), save_rows=Mock())
        exec(compile(ast.Module(body=[retry], type_ignores=[]), regular.__file__, 'exec'), scope)
        self.assertEqual(rows[1][1], regular.RETRY_STATUS)
        self.assertEqual(scope['reverify_retry'], {'31060001'})
        self.assertEqual([r[1] for r in rows[2:]], ['AADHAAR_REVERIFY', 'COMPLETED', ''])
        scope['log_progress'].assert_called_once()
        scope['save_rows'].assert_called_once()
