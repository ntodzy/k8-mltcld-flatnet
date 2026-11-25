import csv
import io
import json
import math
from pathlib import Path
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import aggregate as analysis


def save(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload))


def fixture(root, status='passed', samples=2):
    directory = root / '01-vxlan-attempt-1/measurement'
    phases = []
    for sample in range(1, samples + 1):
        phase = f'phases/sample-{sample:02d}-pod-single'
        phases.append({'directory': phase, 'sample': sample, 'network': 'pod',
                       'test_type': 'single', 'status': 'passed'})
        child = directory / phase / 'measurement'
        save(child / 'metadata.json', {'status': 'passed', 'samples': 1, 'mode': 'vxlan',
                                     'test_type': 'single', 'network': 'pod'})
        payload = {'dataItems': [{'data': {'TCP_STREAM_pod-to-pod_throughput': sample * 1000},
                                 'unit': 'Mb/s', 'labels': {'node': 'other-node'}}]}
        save(child / 'reports/result.json', payload)
        # Deliberate report mirror: aggregation must not count this copy.
        save(directory / 'reports' / Path(phase).name / 'result.json', payload)
        save(child.parent / 'summary.json', {'worker-1': {
            'per_core': {'cpu': {'busy_percent': sample * 10},
                         'cpu0': {'busy_percent': sample * 15},
                         'cpu1': {'busy_percent': sample * 5}},
            'snmp_delta': {'Tcp:RetransSegs': sample},
            'softnet_delta': {'dropped': 0},
            'nic_counter_delta': {'eth0': {'rx_queue_0_packets': sample * 100}}, 'warnings': []}})
    save(directory / 'metadata.json', {'status': status, 'diagnostics': True, 'mode': 'vxlan',
                                     'samples': samples, 'phases': phases})
    return directory


class AggregationTests(unittest.TestCase):
    def test_diagnostic_reports_are_not_double_counted_and_stats_are_sample_based(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = fixture(Path(temp))
            item, rows, warnings = analysis.load_attempt(directory.parent)
            performance = [r for r in rows if r['domain'] == 'performance']
            self.assertEqual(len(performance), 2)
            entry = analysis.aggregate(performance)[0]
            self.assertEqual(entry['n'], 2)
            self.assertEqual(entry['mean'], 1500)
            self.assertAlmostEqual(entry['stdev'], math.sqrt(500000))
            self.assertEqual(entry['median'], 1500)
            self.assertEqual([r['sample'] for r in performance], [1, 2])
            self.assertFalse(warnings)

    def test_counters_are_counted_once_per_host_not_once_per_core(self):
        with tempfile.TemporaryDirectory() as temp:
            _, rows, _ = analysis.load_attempt(fixture(Path(temp)))
            counters = [r for r in rows if r['metric'] == 'snmp_delta/Tcp:RetransSegs']
            self.assertEqual([r['value'] for r in counters], [1, 2])
            self.assertTrue(all(r['core'] == '' for r in counters))
            self.assertTrue(all(r['scope'] == 'whole_invocation' for r in rows if r['domain'] == 'nic'))
            busiest = [r['value'] for r in rows if r['core'] == 'busiest']
            self.assertEqual(busiest, [15, 30])

    def test_partial_runs_require_explicit_opt_in_and_exclude_unfinished_phases(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = fixture(Path(temp), status='running')
            meta = analysis.read_json(directory / 'metadata.json')
            meta['phases'][1]['status'] = 'running'
            save(directory / 'metadata.json', meta)
            with self.assertRaisesRegex(ValueError, 'allow-partial'):
                analysis.load_attempt(directory)
            _, rows, warnings = analysis.load_attempt(directory, True)
            self.assertEqual(len([r for r in rows if r['domain'] == 'performance']), 1)
            self.assertTrue(any('PARTIAL' in w for w in warnings))
            self.assertIsNone(analysis.aggregate([rows[0]])[0]['stdev'])

    def test_mean_latency_supplemented_without_pooling_percentiles(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = fixture(Path(temp), samples=1)
            child = directory / 'phases/sample-01-pod-single/measurement'
            (child / 'console.log').write_text(
                '📋 pod-to-pod | other-node | TCP_RR | 30s | 100µs | 0.750ms | 2ms | 700µs | 900µs | 1ms | 1000\n')
            _, rows, _ = analysis.load_attempt(directory)
            means = [r for r in rows if r['domain'] == 'performance' and r['metric'] == 'mean']
            self.assertEqual(len(means), 1)
            self.assertEqual(means[0]['value'], 750)
            self.assertEqual(means[0]['unit'], 'us')
            self.assertFalse(any(r['metric'] == 'p99' for r in rows))  # Never invent absent JSON metrics.

    def test_legacy_report_warns_about_lost_trial_identity(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = fixture(Path(temp), samples=1) / 'phases/sample-01-pod-single/measurement'
            meta = analysis.read_json(directory / 'metadata.json')
            meta['samples'] = 5
            save(directory / 'metadata.json', meta)
            _, rows, warnings = analysis.load_attempt(directory)
            self.assertTrue(any('final trial' in w for w in warnings))
            self.assertEqual(analysis.aggregate([r for r in rows if r['domain'] == 'performance'])[0]['n'], 1)

    def test_escaping_phase_paths_and_duplicate_metrics_are_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = fixture(Path(temp), samples=1)
            child = directory / 'phases/sample-01-pod-single/measurement'
            save(child / 'reports/duplicate.json', analysis.read_json(child / 'reports/result.json'))
            with self.assertRaisesRegex(ValueError, 'Duplicate performance'):
                analysis.load_attempt(directory)
            meta = analysis.read_json(directory / 'metadata.json')
            meta['phases'][0]['directory'] = '../outside'
            save(directory / 'metadata.json', meta)
            with self.assertRaisesRegex(ValueError, 'escapes'):
                analysis.load_attempt(directory)

    def test_network_placement_units_and_attempts_remain_separate(self):
        base = analysis.row({'attempt': 'one', 'mode': 'vxlan'}, domain='performance',
                            metric='throughput', test='TCP_STREAM', network='pod-to-pod',
                            node='other-node', unit='Mb/s', value=10)
        rows = [base, {**base, 'attempt': 'two'}, {**base, 'unit': 'ops/s'},
                {**base, 'node': 'same-node'}, {**base, 'network': 'host-to-host'}]
        self.assertEqual(len(analysis.aggregate(rows)), 5)
        self.assertFalse(analysis.finite(True))
        self.assertFalse(analysis.finite(float('nan')))

    def test_duplicate_manifest_phases_do_not_inflate_sample_count(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = fixture(Path(temp), samples=1)
            meta = analysis.read_json(directory / 'metadata.json')
            meta['phases'].append(meta['phases'][0])
            save(directory / 'metadata.json', meta)
            with self.assertRaisesRegex(ValueError, 'Duplicate phase'):
                analysis.load_attempt(directory)

    def test_cli_writes_tables_and_preserves_source_files(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = fixture(Path(temp))
            original = {p: p.read_bytes() for p in directory.rglob('*') if p.is_file()}
            output = Path(temp) / 'summary'
            argv = ['aggregate.py', str(directory.parent), '--no-plots', '--output', str(output)]
            with patch.object(sys, 'argv', argv), redirect_stdout(io.StringIO()):
                analysis.main()
            self.assertTrue((output / 'summary.md').is_file())
            with (output / 'aggregate.csv').open() as stream:
                self.assertTrue(list(csv.DictReader(stream)))
            self.assertTrue(all(p.read_bytes() == data for p, data in original.items()))
            with patch.object(sys, 'argv', argv), self.assertRaises(FileExistsError):
                analysis.main()


if __name__ == '__main__':
    unittest.main()
