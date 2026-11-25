import json
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import with_cpu_latency as wrapper
from cpu_latency import CPULatencyRequest


class CPUPolicyWrapperTests(unittest.TestCase):
    def run_command(self,code,expired=False):
        holder=SimpleNamespace(process=SimpleNamespace(poll=lambda:0 if expired else None))
        children=[];real_popen=subprocess.Popen
        def launch(*args,**kwargs):
            child=real_popen(*args,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,**kwargs)
            children.append(child);return child
        with tempfile.TemporaryDirectory() as temp:
            output=Path(temp)/'policy'
            with patch.object(wrapper,'requests',return_value=[holder]), \
                 patch.object(wrapper,'close_requests') as close, \
                 patch.object(wrapper.subprocess,'Popen',side_effect=launch):
                result=wrapper.execute([sys.executable,'-c',code],output)
                close.assert_called_once_with([holder])
            metadata=json.loads((output/'policy.json').read_text())
            self.assertIsNotNone(children[0].poll())
            return result,metadata

    def test_success_requires_lease_and_verified_cleanup(self):
        result,metadata=self.run_command('pass')
        self.assertEqual(result,0)
        self.assertEqual(metadata['status'],'passed')
        self.assertTrue(metadata['restoration_verified'])

    def test_failed_command_keeps_exit_status_and_restores(self):
        result,metadata=self.run_command('raise SystemExit(7)')
        self.assertEqual(result,7)
        self.assertEqual(metadata['status'],'failed')
        self.assertTrue(metadata['restoration_verified'])

    def test_expired_lease_stops_command_and_invalidates_success(self):
        result,metadata=self.run_command('import time; time.sleep(60)',expired=True)
        self.assertNotEqual(result,0)
        self.assertIn('lease',metadata['error'])
        self.assertTrue(metadata['restoration_verified'])

    def test_invalid_lease_does_not_start_ssh(self):
        for lifetime in (0,7201):
            with self.assertRaises(ValueError):CPULatencyRequest('unused',10,Path('/unused'),lifetime)


if __name__=='__main__':unittest.main()
