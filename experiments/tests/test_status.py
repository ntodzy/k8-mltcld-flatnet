from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import status


class StatusTests(unittest.TestCase):
    def test_comparison_counts_only_measurements_not_setup(self):
        state = {'status': 'running', 'phase': 'vxlan', 'modes': ['direct-1450', 'vxlan'],
                 'steps': [{'phase': 'direct-1450', 'exit_code': 0}, {'phase': 'setup-vxlan', 'exit_code': 0}]}
        text, value = status.progress(state)
        self.assertIn('1/2 measurements complete', text)
        self.assertEqual(value, 'running')

    def test_watch_returns_when_successful(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'collection.json'
            state = {'status': 'running', 'phase': 'measure-vxlan', 'plan': [{'status': 'running'}]}
            path.write_text(json.dumps(state))
            def complete(_interval):
                state.update(status='passed', phase='complete', plan=[{'status': 'passed'}])
                path.write_text(json.dumps(state))
            stream = io.StringIO()
            with redirect_stdout(stream):
                code = status.monitor(path, watch=True, sleep=complete)
            self.assertEqual(code, 0)
            self.assertIn('DONE: 1/1 measurements complete', stream.getvalue())

    def test_failure_is_reported_and_returns_nonzero(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'comparison.json'
            path.write_text(json.dumps({'status': 'failed', 'error': 'setup failed', 'modes': ['vxlan']}))
            stream = io.StringIO()
            with redirect_stdout(stream):
                code = status.monitor(path, watch=True)
            self.assertEqual(code, 1)
            self.assertIn('FAILED', stream.getvalue())
            self.assertIn('setup failed', stream.getvalue())

    def test_latest_manifest_discovery_supports_both_formats(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for name, file in [('first', 'comparison.json'), ('second', 'collection.json')]:
                directory = root / 'results' / name
                directory.mkdir(parents=True)
                (directory / file).write_text('{}')
            import os
            older = root / 'results/first/comparison.json'
            newer = root / 'results/second/collection.json'
            os.utime(older, (10, 10))
            os.utime(newer, (20, 20))
            with patch.object(status, 'ROOT', root):
                self.assertEqual(status.find_manifest(), newer)
                self.assertEqual(status.find_manifest(older.parent), older)


if __name__ == '__main__':
    unittest.main()
