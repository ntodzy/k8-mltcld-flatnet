from contextlib import redirect_stdout
import csv
import fcntl
import hashlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import collect
from common import ROOT

BASE = json.loads((ROOT / 'infrastructure/config.example.json').read_text())


class CollectionTests(unittest.TestCase):
    def test_all_suites_keep_cross_host_pair_and_distinct_colocated_vms(self):
        endpoints = collect.pairs(BASE, list(collect.MODES))
        self.assertEqual(endpoints, {'cross': ['worker-1', 'worker-2'], 'local': ['worker-2', 'worker-3']})
        plan = collect.build_plan(list(collect.MODES), 2, endpoints)
        self.assertEqual([step['mode'] for step in plan[:len(collect.MODES)]], list(collect.MODES))
        self.assertEqual([step['mode'] for step in plan[len(collect.MODES):]], list(reversed(collect.MODES)))
        for step in plan:
            self.assertEqual([step['client'], step['server']], endpoints['local' if step['mode'] == 'vm-local' else 'cross'])

    def test_invalid_pair_is_rejected_before_reset(self):
        for args in [('worker-2', 'worker-3'), ('worker-1', None), ('control-1', 'worker-2')]:
            with self.assertRaises(ValueError):
                collect.pairs(BASE, ['vxlan'], *args)

    def manifest(self):
        return {'status': 'pending', 'parameters': {'config': '/tmp/existing-config.json',
                'duration': 30, 'samples': 5, 'streams': 4, 'rounds': 1,
                'host_profile': 'baseline', 'underlay_subnet': '10.75.0.0/24'},
                'plan': collect.build_plan(['vxlan', 'direct-1450'], 1, {'cross': ['worker-1', 'worker-2']})}

    def fake_runner(self, command, log):
        log.write_text('test command output\n')
        if '/experiments/run.py' not in command[1]:
            return
        directory = Path(command[command.index('--output') + 1])
        directory.mkdir()
        (directory / 'reports').mkdir()
        (directory / 'metadata.json').write_text(json.dumps({
            'status': 'passed', 'report_count': 1, 'mode': command[2], 'client': 'worker-1',
            'server': 'worker-2', 'samples': 5, 'duration_seconds': 30, 'streams': 4,
            'pod_only': '--pod-only' in command, 'diagnostics': '--diagnostics' in command}))
        (directory / 'reports/result.json').write_text(json.dumps({'dataItems': [{
            'data': {'TCP_STREAM_pod-to-pod_throughput': 5000.5}, 'unit': 'Mb/s',
            'labels': {'node': 'other-node', 'metric': 'Throughput'}}]}))

    def prepare(self, root):
        generated = root / 'generated'
        (generated / 'hypervisor-reports').mkdir(parents=True)
        (generated / 'hypervisor-reports/node1.json').write_text('{"host":"node1"}')
        output = root / 'collection'
        output.mkdir()
        return generated, output

    def test_success_resets_each_mode_preserves_reports_and_exports_metrics(self):
        with tempfile.TemporaryDirectory() as temp:
            generated, output = self.prepare(Path(temp))
            manifest = self.manifest()
            with patch.object(collect, 'GENERATED', generated), \
                    patch.object(collect, 'run_logged', side_effect=self.fake_runner) as execute:
                collect.execute_collection(output, manifest)
            self.assertEqual(manifest['status'], 'passed')
            setups = [call.args[0] for call in execute.call_args_list if '/experiments/setup.py' in call.args[0][1]]
            self.assertEqual(len(setups), 2)
            self.assertTrue(all('--reset' in command for command in setups))
            with (output / 'metrics.csv').open() as source:
                metrics = list(csv.DictReader(source))
            self.assertEqual(len(metrics), 2)
            self.assertEqual({row['mode'] for row in metrics}, {'vxlan', 'direct-1450'})
            self.assertTrue((output / metrics[0]['report']).is_file())
            self.assertTrue((output / manifest['plan'][0]['attempts'][0]['directory'] / 'host-network/node1.json').is_file())

    def test_diagnostics_is_forwarded_and_recorded_for_each_mode(self):
        with tempfile.TemporaryDirectory() as temp:
            generated, output = self.prepare(Path(temp))
            manifest = self.manifest()
            manifest['parameters']['diagnostics'] = True
            with patch.object(collect, 'GENERATED', generated), \
                    patch.object(collect, 'run_logged', side_effect=self.fake_runner) as execute:
                collect.execute_collection(output, manifest)
            commands = [call.args[0] for call in execute.call_args_list if '/experiments/run.py' in call.args[0][1]]
            self.assertEqual(len(commands), 2)
            self.assertTrue(all('--diagnostics' in command for command in commands))
            self.assertEqual(manifest['status'], 'passed')

    def test_failure_stops_then_resume_retries_without_overwriting_successful_runs(self):
        with tempfile.TemporaryDirectory() as temp:
            generated, output = self.prepare(Path(temp))
            manifest = self.manifest()
            def fail_second(command, log):
                if '/experiments/setup.py' in command[1] and command[2] == 'direct-1450':
                    raise RuntimeError('underlay failed')
                self.fake_runner(command, log)
            with patch.object(collect, 'GENERATED', generated), patch.object(collect, 'run_logged', side_effect=fail_second):
                with self.assertRaisesRegex(RuntimeError, 'underlay failed'):
                    collect.execute_collection(output, manifest)
            self.assertEqual(manifest['status'], 'failed')
            self.assertEqual([s['status'] for s in manifest['plan']], ['passed', 'failed'])
            successful = output / manifest['plan'][0]['attempts'][0]['directory'] / 'measurement/reports/result.json'
            before = successful.read_bytes()
            with patch.object(collect, 'GENERATED', generated), \
                    patch.object(collect, 'run_logged', side_effect=self.fake_runner) as execute:
                collect.execute_collection(output, manifest)
            self.assertEqual(manifest['status'], 'passed')
            self.assertEqual(len(manifest['plan'][1]['attempts']), 2)
            self.assertEqual(successful.read_bytes(), before)
            self.assertTrue(all('vxlan' not in call.args[0] for call in execute.call_args_list))

    def test_missing_reports_or_failed_metadata_cannot_pass(self):
        with tempfile.TemporaryDirectory() as temp:
            generated, output = self.prepare(Path(temp))
            manifest = self.manifest()
            def no_reports(command, log):
                self.fake_runner(command, log)
                if '/experiments/run.py' in command[1]:
                    directory = Path(command[command.index('--output') + 1])
                    (directory / 'metadata.json').write_text('{"status":"failed","report_count":0}')
            with patch.object(collect, 'GENERATED', generated), patch.object(collect, 'run_logged', side_effect=no_reports):
                with self.assertRaises(RuntimeError):
                    collect.execute_collection(output, manifest)
            self.assertEqual(manifest['status'], 'failed')
            self.assertEqual(manifest['plan'][1]['status'], 'pending')

    def test_dry_run_never_executes_commands_or_creates_results(self):
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / 'results'
            argv = ['collect.py', '--config', str(ROOT / 'infrastructure/config.example.json'),
                    '--output', str(output), '--dry-run']
            with patch.object(sys, 'argv', argv), patch.object(collect, 'run_logged') as execute, redirect_stdout(io.StringIO()):
                collect.main()
            execute.assert_not_called()
            self.assertFalse(output.exists())

    def test_background_parent_returns_a_pid_without_running_inline(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            generated = root / 'generated'
            output = root / 'results'
            argv = ['collect.py', '--config', str(ROOT / 'infrastructure/config.example.json'),
                    '--output', str(output), '--background']
            with patch.object(sys, 'argv', argv), patch.object(collect, 'GENERATED', generated), \
                    patch.object(collect.os, 'fork', return_value=12345), \
                    patch.object(collect, 'execute_collection') as execute, redirect_stdout(io.StringIO()):
                collect.main()
            execute.assert_not_called()
            self.assertEqual((output / 'pid').read_text().strip(), '12345')
            self.assertEqual(json.loads((output / 'collection.json').read_text())['status'], 'pending')

    def test_active_comparison_lock_prevents_any_reset_or_output_creation(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            generated = root / 'generated'
            generated.mkdir()
            output = root / 'results'
            argv = ['collect.py', '--config', str(ROOT / 'infrastructure/config.example.json'), '--output', str(output)]
            with (generated / 'collection.lock').open('a') as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                with patch.object(sys, 'argv', argv), patch.object(collect, 'GENERATED', generated), \
                        patch.object(collect, 'execute_collection') as execute, redirect_stdout(io.StringIO()):
                    with self.assertRaisesRegex(ValueError, 'active'):
                        collect.main()
            execute.assert_not_called()
            self.assertFalse(output.exists())

    def test_resume_rejects_config_change_before_touching_cluster(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = root / 'config.json'
            original = json.dumps(BASE).encode()
            config.write_bytes(original)
            manifest = self.manifest()
            manifest['parameters']['config'] = str(config)
            manifest['config_sha256'] = hashlib.sha256(original).hexdigest()
            (root / 'collection.json').write_text(json.dumps(manifest))
            config.write_text(json.dumps(BASE) + ' ')
            with patch.object(sys, 'argv', ['collect.py', '--resume', str(root)]), \
                    patch.object(collect, 'execute_collection') as execute:
                with self.assertRaisesRegex(ValueError, 'config changed'):
                    collect.main()
            execute.assert_not_called()

    def test_matched_collection_forwards_pod_only_and_checks_recorded_workload(self):
        with tempfile.TemporaryDirectory() as temp:
            generated, output = self.prepare(Path(temp))
            manifest = self.manifest()
            manifest['parameters']['pod_only'] = True
            manifest['plan'] = collect.build_plan(
                ['vxlan', 'vm-vxlan-1450', 'host-vxlan-1450'], 1, {'cross': ['worker-1', 'worker-2']})
            with patch.object(collect, 'GENERATED', generated), \
                    patch.object(collect, 'run_logged', side_effect=self.fake_runner) as execute:
                collect.execute_collection(output, manifest)
            measurements = [call.args[0] for call in execute.call_args_list if '/experiments/run.py' in call.args[0][1]]
            self.assertEqual(len(measurements), 3)
            self.assertTrue(all('--pod-only' in command for command in measurements))
            self.assertEqual(manifest['status'], 'passed')


if __name__ == '__main__':
    unittest.main()
