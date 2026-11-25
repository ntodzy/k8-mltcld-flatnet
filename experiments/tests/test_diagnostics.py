import argparse
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import diagnostics
import run as runner


def args(**changes):
    values = dict(mode='vxlan', client='worker-1', server='worker-2', duration=30,
                  samples=2, streams=4, pod_only=False, network='both', test_type='all',
                  profile=False, output=None, dry_run=False, same_node=False)
    values.update(changes)
    return argparse.Namespace(**values)


def record(epoch, active=False, ticks=0):
    return {'kind': 'sample', 'received_epoch': epoch, 'netperf': [{'pid': 1}] if active else [],
            'stat': f'cpu {ticks} 0 0 {ticks} 0 0 {ticks} {ticks} 999 0\ncpu0 {ticks} 0 0 {ticks} 0 0 {ticks} {ticks}\n',
            'snmp': f'Tcp: InSegs RetransSegs\nTcp: {ticks} {ticks}\n',
            'netstat': f'TcpExt: TCPTimeouts\nTcpExt: {ticks}\n',
            'softnet': f'{ticks:x} 00000000 00000000\n',
            'softirqs': f' CPU0\nNET_RX: {ticks}\n'}


class DiagnosticsTests(unittest.TestCase):
    def test_each_sample_path_and_test_is_isolated(self):
        plan = diagnostics.phases(args(samples=5))
        self.assertEqual(len(plan), 30)
        self.assertEqual(len(set(plan)), 30)
        self.assertEqual(diagnostics.phases(args(samples=1, pod_only=True, test_type='multi')),
                         [(1, 'pod', 'multi')])
        self.assertEqual({p[1] for p in diagnostics.phases(args(mode='vm-local'))}, {'host'})
        for network, test in [('pod', 'single'), ('host', 'multi'), ('pod', 'rr')]:
            flags = runner.perf_arguments(args(network=network, test_type=test), 'ns', '/tmp/reports', 'a', 'b')
            self.assertIn('--host-net=' + str(network == 'host').lower(), flags)
            self.assertIn('--pod-net=' + str(network == 'pod').lower(), flags)
            self.assertIn('--rr=' + str(test == 'rr').lower(), flags)
            self.assertIn('--throughput=' + str(test == 'single').lower(), flags)
            self.assertIn('--throughput-multi=' + str(test == 'multi').lower(), flags)

    def test_cpu_accounts_for_steal_without_double_counting_guest(self):
        before = diagnostics.cpu_rows('cpu0 100 0 0 100 0 0 100 100 999 0\n')
        after = diagnostics.cpu_rows('cpu0 110 0 0 110 0 0 110 110 9999 0\n')
        result = diagnostics.cpu_delta(before, after)['cpu0']
        self.assertEqual(result, {'busy_percent': 50, 'softirq_percent': 25, 'steal_percent': 25})
        self.assertEqual(diagnostics.cpu_delta(after, before), {})

    def test_summary_excludes_setup_and_preserves_tcp_and_softirq_deltas(self):
        records = [record(0, ticks=0), record(1, True, 100), record(2, True, 110), record(3, ticks=999)]
        result = diagnostics.summary(records, 1, 2)
        self.assertEqual(result['active_sample_count'], 2)
        self.assertEqual(result['snmp_delta']['Tcp:RetransSegs'], 10)
        self.assertEqual(result['softnet_delta']['processed'], 10)
        self.assertEqual(result['softirq_delta']['NET_RX/cpu0'], 10)
        self.assertEqual(result['per_core']['cpu0']['steal_percent'], 25)
        empty = diagnostics.summary(records)
        self.assertNotIn('per_core', empty)
        self.assertTrue(empty['warnings'])

    def test_unsupported_ethtool_is_reported_and_queue_deltas_are_kept(self):
        def snapshot(kind, count):
            return {'kind': kind, 'interfaces': {'eth0': {
                '-S': {'exit_code': 0, 'output': f'NIC statistics:\n rx_queue_0_packets: {count}\n'},
                '-x': {'exit_code': 95, 'output': 'Operation not supported'}}}}
        result = diagnostics.summary([snapshot('before', 100), snapshot('after', 130)])
        self.assertEqual(result['nic_counter_delta']['eth0']['rx_queue_0_packets'], 30)
        self.assertTrue(any('ethtool -x unavailable' in w for w in result['warnings']))

    def test_sampler_stops_on_eof_without_external_dependencies(self):
        # Exercise the actual transport/termination protocol without sudo or a host connection.
        script = Path(diagnostics.__file__).with_name('diagnostic_agent.py').read_text()
        script = script.replace("if __name__ == '__main__':\n    main()", '')
        script += '\nsnapshot = lambda: {}\nmain()\n'
        process = subprocess.run([sys.executable, '-u', '-c', script], input='', text=True,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10)
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertEqual([json.loads(line)['kind'] for line in process.stdout.splitlines()],
                         ['before', 'ready', 'after'])

    def test_monitor_streams_real_proc_samples_and_stops_over_control_channel(self):
        # Run the actual sampler locally through the same pipe protocol as SSH.
        # Only NIC snapshots are stubbed; sampling reads the real local /proc.
        script = Path(diagnostics.__file__).with_name('diagnostic_agent.py').read_text()
        script = script.replace("if __name__ == '__main__':\n    main()", '')
        script += '\nsnapshot = lambda: {}\nmain()\n'
        with tempfile.TemporaryDirectory() as temp, patch.object(diagnostics, 'ssh_command',
                return_value=[sys.executable, '-u', '-c', script]):
            monitor = diagnostics.Monitor(Path('/tmp/unused'), 'local', Path(temp) / 'monitor')
            try:
                self.assertTrue(monitor.ready.wait(5))
                deadline = time.monotonic() + 5
                while len([r for r in monitor.records if r['kind'] == 'sample']) < 2 and time.monotonic() < deadline:
                    time.sleep(0.05)
            finally:
                monitor.stop()
            self.assertIsNone(monitor.error)
            samples = [r for r in monitor.records if r['kind'] == 'sample']
            self.assertGreaterEqual(len(samples), 2)
            self.assertIn('cpu0', diagnostics.cpu_rows(samples[0]['stat']))
            self.assertEqual(monitor.records[-1]['kind'], 'after')

    def test_orchestration_saves_each_report_and_stops_monitors_on_failure(self):
        hosts = {'worker-1': {'hypervisor': 'node1'}, 'worker-2': {'hypervisor': 'node2'}}
        for failure in ('none', 'ready', 'measurement'):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as temp:
                monitors = []
                class FakeMonitor:
                    def __init__(self, config, target, directory, profile):
                        directory.mkdir()
                        self.ready = threading.Event()
                        self.ready.set()
                        self.records = ([] if failure == 'ready' else [{'kind': 'ready'},
                                        record(1, True, 100), record(2, True, 110)])
                        self.error = None
                        self.stopped = False
                        monitors.append(self)
                    def stop(self):
                        self.stopped = True

                class FakeProcess:
                    def __init__(self, command, **kw):
                        dest = Path(command[command.index('--output') + 1])
                        (dest / 'reports').mkdir(parents=True)
                        (dest / 'reports/perf.json').write_text('{"dataItems": []}')
                        (dest / 'metadata.json').write_text('{"status":"passed","report_count":1}')
                    def poll(self): return 1 if failure == 'measurement' else 0
                    def wait(self): return self.poll()
                    def __enter__(self): return self
                    def __exit__(self, *unused): pass

                output = Path(temp) / 'results'
                with patch.object(diagnostics, 'Monitor', FakeMonitor), \
                        patch.object(diagnostics.subprocess, 'Popen', FakeProcess), redirect_stdout(io.StringIO()):
                    if failure != 'none':
                        with self.assertRaisesRegex(RuntimeError, 'failed to become ready' if failure == 'ready' else 'failed; see'):
                            diagnostics.run_diagnostics(args(output=output, network='pod', test_type='single'), hosts, Path('/tmp/config'))
                    else:
                        diagnostics.run_diagnostics(args(output=output, network='pod', test_type='single'), hosts, Path('/tmp/config'))
                self.assertTrue(all(m.stopped for m in monitors))
                result = json.loads((output / 'metadata.json').read_text())
                self.assertEqual(result['status'], 'passed' if failure == 'none' else 'failed')
                if failure == 'none':
                    self.assertEqual(result['report_count'], 2)
                    self.assertEqual(len(list((output / 'reports').rglob('*.json'))), 2)


if __name__ == '__main__':
    unittest.main()
