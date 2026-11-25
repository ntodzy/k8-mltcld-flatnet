import argparse
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common import ROOT, MODES, mtu_profile
import setup
import run as runner

BASE = json.loads((ROOT / 'infrastructure/config.example.json').read_text())


class MatchedMtuTests(unittest.TestCase):
    def test_three_implementations_share_inner_and_physical_mtu(self):
        for mode in ('vxlan', 'vm-vxlan-1450', 'host-vxlan-1450'):
            with self.subTest(mode=mode):
                settings = mtu_profile(mode)
                self.assertEqual(settings['inner_mtu'], 1450)
                self.assertEqual(settings['physical_mtu'], 1500)
                inventory = setup.hypervisor_inventory(BASE, mode, Path('/tmp/config'), '10.75.0.0/24')
                self.assertEqual(inventory['all']['vars']['experiment_inner_mtu'], 1450)
                self.assertEqual(inventory['all']['vars']['experiment_underlay_mtu'], 1500)

    def test_guest_transport_mtu_leaves_room_for_where_encapsulation_happens(self):
        expected = {'vm-vxlan-1450': ('direct', 1500), 'host-vxlan-1450': ('bridge', 1450),
                    'vm-vxlan': ('direct', 1550), 'host-vxlan': ('bridge', 1500)}
        for mode, (attachment, mtu) in expected.items():
            config = setup.deployment_config(BASE, mode, Path('/tmp/config'))
            for host in config['hypervisors']:
                self.assertEqual(host['network']['mode'], attachment)
                self.assertEqual(host['network']['mtu'], mtu)
            inventory = setup.hypervisor_inventory(BASE, mode, Path('/tmp/config'), '10.75.0.0/24')
            self.assertEqual(inventory['all']['vars']['experiment_guest_mtu'], mtu)

    def test_old_jumbo_profiles_keep_1500_inner_and_1550_underlay(self):
        for mode in ('vm-vxlan', 'host-vxlan'):
            profile = mtu_profile(mode)
            self.assertEqual(profile['inner_mtu'], 1500)
            self.assertEqual(profile['physical_mtu'], 1550)
            self.assertEqual(profile['cilium_mtu'], 1500)

    def test_host_variant_runs_bridge_setup_and_vm_variant_does_not(self):
        for mode in ('vm-vxlan-1450', 'host-vxlan-1450'):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as temp:
                directory = Path(temp)
                config = directory / 'config.json'
                config.write_text(json.dumps(BASE))
                (directory / 'ssh_config').touch()
                (directory / 'guest_inventory.json').touch()
                stream = io.StringIO()
                with patch.object(setup, 'GENERATED', directory), patch.object(sys, 'argv', [
                        'setup.py', mode, '--config', str(config), '--reset', '--dry-run']), redirect_stdout(stream):
                    setup.main()
                self.assertEqual('playbooks/host-vxlan.yml' in stream.getvalue(), mode == 'host-vxlan-1450')
                self.assertIn('Physical experiment MTU: 1500', stream.getvalue())

    def test_pod_only_excludes_host_network_from_matched_benchmarks(self):
        for mode in ('vxlan', 'vm-vxlan-1450', 'host-vxlan-1450'):
            args = argparse.Namespace(mode=mode, duration=30, samples=5, streams=4,
                                      same_node=False, pod_only=True)
            flags = runner.perf_arguments(args, 'test', '/tmp/reports', 'worker-1', 'worker-2')
            self.assertIn('--host-net=false', flags)
            self.assertIn('--pod-net=true', flags)

    def test_new_modes_are_in_default_collection(self):
        self.assertEqual(len(MODES), 8)
        self.assertIn('vm-vxlan-1450', MODES)
        self.assertIn('host-vxlan-1450', MODES)


if __name__ == '__main__':
    unittest.main()
