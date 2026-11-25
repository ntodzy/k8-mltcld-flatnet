import copy
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import configure
from common import ROOT


def base():
    config = json.loads((ROOT / 'infrastructure/config.example.json').read_text())
    config['ssh']['user'] = 'existing-user'
    for index, host in enumerate(config['hypervisors']):
        host['address'] = f'ms{1200 + index}.utah.cloudlab.us'
    return config


class ConfigurationTests(unittest.TestCase):
    def test_changes_only_requested_fields_and_keeps_credentials_and_vms(self):
        source = base()
        original = copy.deepcopy(source)
        result = configure.update_config(source, {'node1': 'ms2000.utah.cloudlab.us'}, {'node2': 'enp5s0'}, {})
        expected = copy.deepcopy(source)
        expected['hypervisors'][0]['address'] = 'ms2000.utah.cloudlab.us'
        expected['hypervisors'][1]['network']['source'] = 'enp5s0'
        self.assertEqual(source, original)
        self.assertEqual(result, expected)

    def test_unknown_names_and_duplicate_guest_addresses_are_rejected(self):
        with self.assertRaisesRegex(ValueError, 'Unknown hypervisor'):
            configure.update_config(base(), {'wrong': 'ms2000.utah.cloudlab.us'}, {}, {})
        with self.assertRaisesRegex(ValueError, 'unique'):
            configure.update_config(base(), {}, {}, {'worker-2': '10.74.0.11/24'})
        with self.assertRaisesRegex(ValueError, 'prefix'):
            configure.update_config(base(), {}, {}, {'worker-2': '10.74.0.21'})

    def test_stale_custom_libvirt_uri_requires_explicit_update(self):
        source = base()
        source['hypervisors'][0]['libvirt_uri'] = 'qemu+ssh://existing-user@ms1200.utah.cloudlab.us/system'
        with self.assertRaisesRegex(ValueError, 'custom libvirt URI'):
            configure.update_config(source, {'node1': 'ms2000.utah.cloudlab.us'}, {}, {})
        uri = 'qemu+ssh://existing-user@ms2000.utah.cloudlab.us/system'
        result = configure.update_config(source, {'node1': 'ms2000.utah.cloudlab.us'}, {}, {}, libvirt_uris={'node1': uri})
        self.assertEqual(result['hypervisors'][0]['libvirt_uri'], uri)

    def test_invalid_hostname_interface_and_duplicate_assignments_are_rejected(self):
        for addresses, interfaces in [({'node1': 'ssh user@host'}, {}), ({}, {'node1': '../invalid'})]:
            with self.assertRaises(ValueError):
                configure.update_config(base(), addresses, interfaces, {})
        with self.assertRaises(ValueError):
            configure.assignments(['node1=one', 'node1=two'], 'host')

    def test_dry_run_preserves_file_and_does_not_prompt(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'config.json'
            content = json.dumps(base())
            path.write_text(content)
            with patch.object(sys, 'argv', ['configure.py', '--config', str(path), '--dry-run']), \
                    patch('builtins.input') as prompt, redirect_stdout(io.StringIO()):
                configure.main()
            prompt.assert_not_called()
            self.assertEqual(path.read_text(), content)
            self.assertFalse(path.with_suffix('.json.bak').exists())

    def test_interactive_write_preserves_keys_and_backs_up_original(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'config.json'
            content = json.dumps(base())
            path.write_text(content)
            answers = ['', 'ms2000.utah.cloudlab.us', '', '', 'enp5s0']
            with patch.object(sys, 'argv', ['configure.py', '--config', str(path)]), \
                    patch('builtins.input', side_effect=answers), redirect_stdout(io.StringIO()):
                configure.main()
            result = json.loads(path.read_text())
            self.assertEqual(result['hypervisors'][0]['address'], 'ms2000.utah.cloudlab.us')
            self.assertEqual(result['hypervisors'][1]['network']['source'], 'enp5s0')
            self.assertEqual(result['ssh'], base()['ssh'])
            self.assertEqual(path.with_suffix('.json.bak').read_text(), content)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_first_run_builds_config_from_example_without_copying_keys(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'config.json'
            argv = ['configure.py', '--config', str(path), '--ssh-user', 'existing-user',
                    '--host', 'node1=ms2000.utah.cloudlab.us', '--host', 'node2=ms2001.utah.cloudlab.us']
            with patch.object(sys, 'argv', argv), redirect_stdout(io.StringIO()):
                configure.main()
            result = json.loads(path.read_text())
            self.assertEqual(result['ssh']['private_key_file'], '~/.ssh/id_ed25519')
            self.assertEqual(len(result['hypervisors']), 2)
            self.assertFalse(path.with_suffix('.json.bak').exists())


if __name__ == '__main__':
    unittest.main()
