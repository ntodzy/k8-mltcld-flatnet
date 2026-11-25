import argparse
import copy
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch, MagicMock

EXPERIMENTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(EXPERIMENTS))
import run as runner
import setup
from common import ROOT, ssh_command

HOSTS = json.loads((ROOT / 'infrastructure/kubernetes/inventory.example.json').read_text())['all']['children']['virtual_machines']['hosts']
BASE = json.loads((ROOT / 'infrastructure/config.example.json').read_text())


def make_archive(path, name='summary.json', content=b'{"throughput": 1000}'):
    with tarfile.open(path, 'w:gz') as archive:
        member = tarfile.TarInfo(name)
        member.size = len(content)
        archive.addfile(member, io.BytesIO(content))


class SetupTests(unittest.TestCase):
    def test_all_profiles_preserve_base_and_keys(self):
        original = copy.deepcopy(BASE)
        for mode in setup.MODES:
            with self.subTest(mode=mode):
                config = setup.deployment_config(BASE, mode, Path('/tmp/config-base'))
                self.assertEqual(BASE, original)
                self.assertEqual(config['ssh']['private_key_file'], str(Path('~/.ssh/id_ed25519').expanduser()))
                network = config['hypervisors'][0]['network']
                self.assertEqual(network['mode'], 'bridge' if mode in ('host-vxlan', 'host-vxlan-1450') else 'direct')
                self.assertEqual(network['mtu'], 1550 if mode == 'vm-vxlan' else (1450 if mode == 'host-vxlan-1450' else 1500))

    def test_relative_credentials_keep_original_config_location(self):
        source = copy.deepcopy(BASE)
        source['ssh']['private_key_file'] = 'keys/existing'
        config = setup.deployment_config(source, 'native', Path('/tmp/config-base'))
        self.assertEqual(config['ssh']['private_key_file'], '/tmp/config-base/keys/existing')

    def test_host_inventory_uses_existing_ssh_aliases_and_separate_subnet(self):
        inventory = setup.hypervisor_inventory(BASE, 'host-vxlan', Path('/tmp/ssh config'), '10.75.0.0/24')
        first = inventory['all']['children']['hypervisors']['hosts']['node1']
        self.assertEqual(first['ansible_host'], 'cs740-node1-jump')
        self.assertEqual(first['ansible_ssh_common_args'], "-F '/tmp/ssh config'")
        self.assertEqual(first['vxlan_underlay_ipv4'], '10.75.0.1')
        self.assertNotIn('ansible_ssh_private_key_file', first)
        self.assertEqual(inventory['all']['vars']['experiment_guest_mtu'], 1500)
        with self.assertRaisesRegex(ValueError, 'overlaps'):
            setup.hypervisor_inventory(BASE, 'host-vxlan', Path('/tmp/config'), '10.74.0.0/24')
        with self.assertRaisesRegex(ValueError, 'enough'):
            setup.hypervisor_inventory(BASE, 'host-vxlan', Path('/tmp/config'), '10.75.0.1/32')


class RunnerTests(unittest.TestCase):
    def test_same_host_means_distinct_vms_on_same_hypervisor(self):
        runner.select_pair(HOSTS, 'vm-local', 'worker-2', 'worker-3')
        for client, server in [('worker-1', 'worker-2'), ('worker-2', 'worker-2')]:
            with self.assertRaises(ValueError):
                runner.select_pair(HOSTS, 'vm-local', client, server)
        with self.assertRaises(ValueError):
            runner.select_pair(HOSTS, 'native', 'worker-2', 'worker-3')
        with self.assertRaises(ValueError):
            runner.select_pair(HOSTS, 'native', 'control-1', 'worker-2')

    def test_perf_flags_pin_host_baseline_and_cross_host_pod_tests(self):
        args = argparse.Namespace(mode='vm-local', duration=30, samples=5, streams=4, same_node=False, pod_only=False)
        argv = runner.perf_arguments(args, 'cs740-perf-test', '/tmp/reports', 'worker-2', 'worker-3')
        self.assertIn('--same-node=false', argv)
        self.assertIn('--other-node=true', argv)
        self.assertIn('--pod-net=false', argv)
        self.assertIn('kubernetes.io/hostname=worker-2', argv)
        args.mode = 'native'
        argv = runner.perf_arguments(args, 'cs740-perf-test', '/tmp/reports', 'worker-1', 'worker-2')
        self.assertIn('--pod-net=true', argv)

    def test_cpu_avoids_double_counting_guest_ticks(self):
        before = runner.cpu_counts('cpu  100 0 50 800 20 10 10 10 80 0\ncpu0 1 2\n')
        after = runner.cpu_counts('cpu  150 0 50 850 20 10 10 10 120 0\n')
        self.assertEqual(before, {'total': 1000, 'idle': 820})
        self.assertEqual(runner.cpu_utilization(before, after), 50)

    def test_report_copy_rejects_traversal_and_invalid_json(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            archive = root / 'reports.tar.gz'
            make_archive(archive)
            self.assertEqual(runner.unpack_reports(archive, root / 'output'), 1)
            self.assertTrue((root / 'output/summary.json').is_file())
            make_archive(archive, '../escaped.json')
            with self.assertRaises(ValueError):
                runner.unpack_reports(archive, root / 'output')
            self.assertFalse((root / 'escaped.json').exists())
            make_archive(archive, content=b'not json')
            with self.assertRaises(ValueError):
                runner.unpack_reports(archive, root / 'output')

    def test_ssh_quotes_remote_arguments(self):
        argv = ssh_command(Path('/tmp/key config'), 'worker-1', ['echo', '$(do-not-run)'])
        self.assertEqual(argv[-1], "echo '$(do-not-run)'")

    def test_success_failure_and_report_collection_cleanup(self):
        for exit_code, archive_failure in [(0, False), (1, False), (0, True)]:
            with self.subTest(exit_code=exit_code, archive_failure=archive_failure), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                generated = root / 'generated'
                generated.mkdir()
                (generated / 'guest_inventory.json').write_text(json.dumps({'all': {'children': {'virtual_machines': {'hosts': HOSTS}}}}))
                (generated / 'ssh_config').touch()
                (generated / 'active-experiment.json').write_text('{"mode":"native"}')
                archive = root / 'fixture.tar.gz'
                make_archive(archive)
                nodes = {'items': [{'metadata': {'name': name, 'labels': {'kubernetes.io/hostname': name}},
                                    'status': {'conditions': [{'type': 'Ready', 'status': 'True'}]}}
                                   for name in HOSTS]}

                def remote(_config, _host, argv):
                    if argv[:2] == ['python3', '-c']:
                        return json.dumps({'cpu': 'cpu 100 0 0 900 0 0 0 0 0 0\n', 'links': '[]', 'routes': '[]'})
                    if 'nodes' in argv:
                        return json.dumps(nodes)
                    if 'configmap' in argv:
                        return json.dumps({'data': {'routing-mode': 'native', 'auto-direct-node-routes': 'true', 'mtu': '0'}})
                    if '/proc/stat' in argv:
                        return 'cpu 100 0 0 900 0 0 0 0 0 0\n'
                    if 'mktemp' in argv:
                        return '/tmp/cs740-perf.ABC12345\n'
                    if 'ip' in argv or 'pods' in argv:
                        return '[]'
                    return 'ok\n'

                def collect(_argv, stdout, **_kwargs):
                    if archive_failure:
                        raise OSError('simulated transfer failure')
                    stdout.write(archive.read_bytes())

                process = MagicMock()
                process.__enter__.return_value = process
                process.stdout = io.StringIO('performance summary\n')
                process.wait.return_value = exit_code
                output = root / 'result'
                with patch.object(runner, 'GENERATED', generated), patch.object(runner, 'remote', side_effect=remote) as ssh, \
                        patch.object(runner.subprocess, 'Popen', return_value=process), \
                        patch.object(runner.subprocess, 'run', side_effect=collect), \
                        patch.object(sys, 'argv', ['run.py', 'native', '--client', 'worker-1', '--server', 'worker-2', '--output', str(output)]), \
                        redirect_stdout(io.StringIO()):
                    if exit_code or archive_failure:
                        with self.assertRaises((ValueError, OSError)):
                            runner.main()
                    else:
                        runner.main()
                metadata = json.loads((output / 'metadata.json').read_text())
                self.assertEqual(metadata['status'], 'failed' if exit_code or archive_failure else 'passed')
                self.assertIn('performance summary', (output / 'console.log').read_text())
                calls = [call.args[2] for call in ssh.call_args_list]
                deletes = [argv for argv in calls if 'delete' in argv and 'namespace' in argv]
                self.assertEqual(len(deletes), 1)
                self.assertTrue(deletes[0][deletes[0].index('namespace') + 1].startswith('cs740-perf-'))
                removals = [argv for argv in calls if 'rm' in argv]
                self.assertEqual(bool(removals), not archive_failure)
                if not archive_failure:
                    self.assertTrue((output / 'reports/summary.json').is_file())
                else:
                    self.assertEqual(metadata['remote_report_directory'], '/tmp/cs740-perf.ABC12345')


if __name__ == '__main__':
    unittest.main()
