import copy
from contextlib import redirect_stdout
import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

EXPERIMENTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(EXPERIMENTS))
import hypervisors
from common import ROOT

SPEC = importlib.util.spec_from_file_location('host_tuner', ROOT / 'infrastructure/ansible/roles/hypervisor_network/files/configure-network.py')
tuner = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(tuner)
BASE = json.loads((ROOT / 'infrastructure/config.example.json').read_text())


class HypervisorInventoryTests(unittest.TestCase):
    def test_reuses_existing_credentials_without_guest_inventory(self):
        source = copy.deepcopy(BASE)
        source['ssh']['private_key_file'] = 'keys/existing'
        inventory = hypervisors.build_inventory(source, Path('/tmp/base'), 'optimized', None, False)
        host = inventory['all']['children']['hypervisors']['hosts']['node1']
        self.assertEqual(host['ansible_host'], source['hypervisors'][0]['address'])
        self.assertEqual(host['ansible_ssh_private_key_file'], '/tmp/base/keys/existing')
        self.assertEqual(host['hypervisor_experiment_interface'], 'eno1d1')
        self.assertNotIn('ProxyJump', host['ansible_ssh_common_args'])
        self.assertEqual(source['ssh']['private_key_file'], 'keys/existing')

    def test_rejects_bridges_and_invalid_interface_names(self):
        source = copy.deepcopy(BASE)
        source['hypervisors'][0]['network']['mode'] = 'bridge'
        with self.assertRaises(ValueError):
            hypervisors.build_inventory(source, Path('/tmp/base'), 'baseline', None, False)
        source['hypervisors'][0]['network'] = {'mode': 'direct', 'source': '../../escape'}
        with self.assertRaises(ValueError):
            hypervisors.build_inventory(source, Path('/tmp/base'), 'baseline', None, False)

    def test_dry_run_does_not_run_ansible(self):
        with patch.object(sys, 'argv', ['hypervisors.py', '--config', str(ROOT / 'infrastructure/config.example.json'), '--dry-run']), \
                patch.object(hypervisors.subprocess, 'run') as execute, redirect_stdout(io.StringIO()):
            hypervisors.main()
        execute.assert_not_called()


class NicTuningTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.current = {'generic-receive-offload': False, 'generic-segmentation-offload': True,
                        'tx-tcp-segmentation': False}
        self.queues = 4
        self.commands = []
        self.config = {'interface': 'eno1d1', 'profile': 'optimized', 'queues': None}

    def command(self, argv):
        self.commands.append(argv)
        if argv[1] == '-k':
            return '\n'.join(f'{name}: {"on" if enabled else "off"}' for name, enabled in self.current.items()) + '\ntx-udp_tnl-segmentation: off [fixed]\n'
        if argv[1] == '-l':
            return f'Pre-set maximums:\nCombined: 8\nCurrent hardware settings:\nCombined: {self.queues}\n'
        if argv[1] == '-K':
            self.current[argv[3]] = argv[4] == 'on'
        if argv[1] == '-L':
            self.queues = int(argv[4])
            # Some drivers reset offloads when channels are changed.
            self.current['generic-segmentation-offload'] = False
        return ''

    def test_reapply_preserves_original_and_baseline_restores_exact_settings(self):
        original = copy.deepcopy(self.current)
        with patch.object(tuner, 'command', side_effect=self.command):
            result = tuner.configure(self.config, self.directory)
            self.assertTrue(result['changed'])
            self.assertIn('tx-udp_tnl-segmentation', result['unsupported_or_fixed'])
            self.assertFalse(tuner.configure(self.config, self.directory)['changed'])
            self.config['profile'] = 'baseline'
            tuner.configure(self.config, self.directory)
            self.assertEqual(self.current, original)
            self.assertFalse((self.directory / 'eno1d1.json').exists())
            self.assertFalse(tuner.configure(self.config, self.directory)['changed'])

    def test_queue_limit_is_checked_before_any_mutation(self):
        self.config['queues'] = 16
        with patch.object(tuner, 'command', side_effect=self.command), self.assertRaisesRegex(ValueError, 'maximum'):
            tuner.configure(self.config, self.directory)
        self.assertFalse(any(argv[1] in ('-K', '-L') for argv in self.commands))
        self.assertFalse((self.directory / 'eno1d1.json').exists())

    def test_queues_and_offloads_restore_even_if_driver_resets_features(self):
        original = copy.deepcopy(self.current)
        self.config['queues'] = 2
        with patch.object(tuner, 'command', side_effect=self.command):
            tuner.configure(self.config, self.directory)
            self.assertEqual(self.queues, 2)
            self.assertTrue(all(self.current.values()))
            self.config['profile'] = 'baseline'
            self.config['queues'] = None
            tuner.configure(self.config, self.directory)
        self.assertEqual(self.queues, 4)
        self.assertEqual(self.current, original)

    def test_original_state_survives_partial_failure(self):
        original = copy.deepcopy(self.current)
        def fail(argv):
            if argv[1] == '-K' and argv[3] == 'tx-tcp-segmentation':
                raise OSError('driver rejected requested feature')
            return self.command(argv)
        with patch.object(tuner, 'command', side_effect=fail), self.assertRaises(OSError):
            tuner.configure(self.config, self.directory)
        self.assertTrue((self.directory / 'eno1d1.json').exists())
        self.config['profile'] = 'baseline'
        with patch.object(tuner, 'command', side_effect=self.command):
            tuner.configure(self.config, self.directory)
        self.assertEqual(self.current, original)

    def test_unsupported_combined_channels_are_rejected(self):
        with self.assertRaises(ValueError):
            tuner.channels('Pre-set maximums:\nCombined: 0\nCurrent hardware settings:\nCombined: 0\n')


if __name__ == '__main__':
    unittest.main()
