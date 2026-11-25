import importlib.util
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common import ROOT

SPEC = importlib.util.spec_from_file_location('macvtap_mtu', ROOT / 'infrastructure/ansible/files/set-macvtap-mtu.py')
macvtap = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(macvtap)

XML = '''<domain><devices>
<interface type="network"><mac address="52:54:00:74:11:11"/><target dev="vnet0"/></interface>
<interface type="direct"><mac address="52:54:00:74:01:11"/>
<source dev="eno1d1" mode="bridge"/><target dev="macvtap7"/></interface>
</devices></domain>'''


class MacvtapMtuTests(unittest.TestCase):
    def setUp(self):
        self.mtu = 1500
        self.parent_mtu = 1550
        self.calls = []

    def command(self, argv):
        self.calls.append(argv)
        if argv[0] == 'virsh':
            return XML
        if argv[:3] == ['ip', 'link', 'set']:
            self.mtu = int(argv[-1])
            return ''
        if argv[-1] == 'eno1d1':
            return json.dumps([{'ifname': 'eno1d1', 'mtu': self.parent_mtu}])
        return json.dumps([{'ifname': 'macvtap7', 'mtu': self.mtu,
                            'linkinfo': {'info_kind': 'macvtap'}}])

    def reconcile(self, **kwargs):
        values = dict(domain='control-1', mac='52:54:00:74:01:11', parent='eno1d1', mtu=1550)
        values.update(kwargs)
        return macvtap.reconcile(**values)

    def test_updates_only_the_domain_experiment_target_and_verifies_it(self):
        with patch.object(macvtap, 'command', side_effect=self.command):
            result = self.reconcile()
            self.assertTrue(result['changed'])
            self.assertEqual(self.mtu, 1550)
            self.assertFalse(self.reconcile()['changed'])
        mutations = [argv for argv in self.calls if argv[:3] == ['ip', 'link', 'set']]
        self.assertEqual(mutations, [['ip', 'link', 'set', 'dev', 'macvtap7', 'mtu', '1550']])

    def test_wrong_mac_or_parent_cannot_mutate_another_interface(self):
        with patch.object(macvtap, 'command', side_effect=self.command):
            for arguments in ({'mac': '52:54:00:74:11:11'}, {'parent': 'eno1'}):
                with self.assertRaises(ValueError):
                    self.reconcile(**arguments)
        self.assertFalse(any(argv[:3] == ['ip', 'link', 'set'] for argv in self.calls))

    def test_rejects_insufficient_parent_mtu(self):
        self.parent_mtu = 1500
        with patch.object(macvtap, 'command', side_effect=self.command), self.assertRaisesRegex(ValueError, 'physical'):
            self.reconcile()
        self.assertFalse(any(argv[:3] == ['ip', 'link', 'set'] for argv in self.calls))

    def test_refuses_non_macvtap_devices(self):
        def command(argv):
            output = self.command(argv)
            if argv[-1] == 'macvtap7':
                return json.dumps([{'mtu': 1500, 'linkinfo': {'info_kind': 'tun'}}])
            return output
        with patch.object(macvtap, 'command', side_effect=command), self.assertRaisesRegex(ValueError, 'not a macvtap'):
            self.reconcile()
        self.assertFalse(any(argv[:3] == ['ip', 'link', 'set'] for argv in self.calls))


if __name__ == '__main__':
    unittest.main()
