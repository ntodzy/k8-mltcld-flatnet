"""Exercise deploy.sh's actual SSH artifact generation without deploying VMs."""
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]


@unittest.skipUnless(shutil.which('jq') and shutil.which('ssh'), 'requires project prerequisites jq and OpenSSH')
class SshAccessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='cs740 SSH access ')
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        records = self.directory / 'records.jsonl'
        records.write_text('\n'.join(json.dumps({
            'name': name, 'hypervisor': hypervisor, 'hypervisor_address': address,
            'hypervisor_user': 'existing-user', 'vm_user': 'ubuntu',
            'management_ipv4': '192.168.122.10', 'role': role,
            'ipv4_address': experiment, 'disk_gib': 30,
        }) for name, hypervisor, address, role, experiment in [
            ('control-1', 'node1', 'node1.example.invalid', 'controller', '10.74.0.11/24'),
            ('worker-2', 'node2', 'node2.example.invalid', 'worker', '10.74.0.21/24'),
        ]) + '\n')
        source = (ROOT / 'infrastructure/deploy.sh').read_text()
        start = source.rindex('\nif [[ "${action}" == "apply" ]]; then')
        end = source.index('\nlog "Deployment', start)
        script = 'log() { :; }\n' + source[start:end]
        subprocess.run(['bash', '-Eeuo', 'pipefail', '-c', script], check=True,
                       env=dict(os.environ, action='apply', SCRIPT_DIR=str(self.directory),
                                guest_records=str(records), private_key=str(self.directory / 'existing key')))
        self.generated = self.directory / 'generated'
        self.config = self.generated / 'ssh_config'
        inventory = json.loads((self.generated / 'guest_inventory.json').read_text())
        self.hosts = inventory['all']['children']['virtual_machines']['hosts']

    def effective(self, options, target):
        result = subprocess.run(['ssh', '-G', '-T', *options, target], check=True,
                                text=True, capture_output=True)
        return dict(line.split(' ', 1) for line in result.stdout.splitlines() if ' ' in line)

    def test_ansible_ip_targets_use_noninteractive_accept_new(self):
        for name, host in self.hosts.items():
            with self.subTest(name=name):
                policy = self.effective(shlex.split(host['ansible_ssh_common_args']), host['ansible_host'])
                self.assertEqual(policy['stricthostkeychecking'], 'accept-new')
                self.assertEqual(policy['batchmode'], 'yes')
                self.assertEqual(policy['proxyjump'], 'cs740-' + host['hypervisor'] + '-jump')
                self.assertEqual(policy['hostkeyalias'], 'cs740-' + host['hypervisor'] + '-' + name)
                self.assertIn(str(self.generated / 'known_hosts'), policy['userknownhostsfile'])

    def test_named_guest_and_jump_hosts_also_use_noninteractive_policy(self):
        for target in ('control-1', 'worker-2', 'cs740-node1-jump', 'cs740-node2-jump'):
            with self.subTest(target=target):
                policy = self.effective(['-F', str(self.config)], target)
                self.assertEqual(policy['stricthostkeychecking'], 'accept-new')
                self.assertEqual(policy['batchmode'], 'yes')

    def test_same_private_ip_on_two_hypervisors_keeps_distinct_host_key_aliases(self):
        aliases = []
        for host in self.hosts.values():
            policy = self.effective(shlex.split(host['ansible_ssh_common_args']), host['ansible_host'])
            aliases.append(policy['hostkeyalias'])
        self.assertEqual(len(set(aliases)), 2)
        for name in ('ssh_config', 'guest_inventory.json', 'known_hosts'):
            self.assertEqual((self.generated / name).stat().st_mode & 0o777, 0o600)


if __name__ == '__main__':
    unittest.main()
