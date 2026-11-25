import base64
from pathlib import Path
import struct
import subprocess
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'prototypes'))
import vxlan_lab_agent as agent


class VXLANPrototypeTests(unittest.TestCase):
    def test_tc_program_id_formats_and_missing_attachment(self):
        self.assertEqual(agent.program_id([{}, {'options': {'prog': {'id': 42}}}]), 42)
        self.assertEqual(agent.program_id([{'options': {'id': 17}}]), 17)
        with self.assertRaisesRegex(RuntimeError, 'program ID'):
            agent.program_id([{'options': {'name': 'not-an-id'}}])

    def test_map_abi_preserves_64_bit_counters(self):
        data = struct.pack('<III6s6sI4xQQQ', 0, 0xffffff00, 4, b'abcdef',
                           b'ghijkl', 1450, 2**40 + 9, 3, 2)
        decoded = agent.route_counters({'value': [hex(b) for b in data]})
        self.assertEqual(decoded, {'redirected': 2**40+9, 'fallback': 3, 'dropped': 2})
        with self.assertRaisesRegex(RuntimeError, 'ABI'):
            agent.route_counters({'value': ['0x00']})

    def test_partial_setup_deletes_only_created_owned_namespace(self):
        calls = []
        def run(argv, **kwargs):
            calls.append(argv)
            # Fail while bringing up loopback in the first namespace.
            failed = argv[:3] == ['ip', 'netns', 'exec']
            return subprocess.CompletedProcess(argv, 1 if failed else 0, '', 'injected' if failed else '')
        with patch.object(agent.os, 'geteuid', return_value=0), patch.object(agent.subprocess, 'run', side_effect=run):
            report = agent.run_lab({'object': base64.b64encode(b'object').decode()})
        self.assertEqual(report['status'], 'failed')
        self.assertEqual(report['cleanup_failed'], [])
        deletions = [argv for argv in calls if argv[:3] == ['ip', 'netns', 'del']]
        self.assertEqual(deletions, [['ip', 'netns', 'del', report['namespaces']['a']]])
        self.assertTrue(report['namespaces']['a'].startswith('c740b-'))


if __name__ == '__main__':
    unittest.main()
