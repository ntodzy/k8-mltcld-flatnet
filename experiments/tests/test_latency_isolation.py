import json
from pathlib import Path
import sys
import unittest
import tempfile
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import latency_study
import latency_trace
import path_control
import analyze_latency
import cpu_latency


class LatencyIsolationTests(unittest.TestCase):
    def test_noop_cpu_policy_is_rejected_and_all_requests_close(self):
        import threading
        created = []
        class Request:
            def __init__(self, target, limit, directory):
                self.records = [{'kind': 'ready', 'before_us': 0, 'effective_us': 0}]
                self.ready = threading.Event(); self.ready.set(); self.closed = False
                created.append(self)
            def stop(self):self.closed = True
        with patch.object(latency_study, 'targets', return_value={'h1': ('host1', 'worker-1', 'enoX'), 'h2': ('host2', 'worker-2', 'enoX')}), patch.object(cpu_latency, 'CPULatencyRequest', Request):
            with self.assertRaisesRegex(RuntimeError, 'already constrained'):
                cpu_latency.requests(10, Path('/tmp/unused'))
        self.assertEqual(len(created), 2)
        self.assertTrue(all(request.closed for request in created))
    def test_idle_interaction_is_a_matched_difference_of_differences(self):
        rows = [dict(idle_pair=1, cpu_latency_us=limit, condition=condition, mean_us=value)
                for limit, condition, value in [(None, 'bypass', 700), (None, 'tunnel', 1000),
                                                 (10, 'bypass', 100), (10, 'tunnel', 200)]]
        result = analyze_latency.idle_interactions(rows)[0]
        self.assertEqual(result['interaction_us'], 200)
        controls = latency_study.idle_controls()
        self.assertEqual(len([step for step in controls if step['kind'] == 'measurement']), 12)
        self.assertTrue(all('pair' not in step for step in controls))
        self.assertEqual(controls[-1]['kind'], 'restore')
    def test_pairs_do_not_mix_instrumented_observations(self):
        rows = [dict(mode='vm-vxlan-1450', pair=1, condition='bypass', trace=False, mean_us=500, p99_us=900),
                dict(mode='vm-vxlan-1450', pair=1, condition='tunnel', trace=False, mean_us=700, p99_us=1000),
                dict(mode='vm-vxlan-1450', pair=1, condition='tunnel', trace=True, mean_us=2000, p99_us=3000)]
        self.assertEqual(analyze_latency.paired_differences(rows),
                         [dict(mode='vm-vxlan-1450', pair=1, extra_mean_us=200, extra_sample_p99_us=100)])

    def test_bpftrace_stats_and_histogram_bounds_keep_units_and_counts(self):
        with tempfile.TemporaryDirectory() as temp:
            work = Path(temp)
            path = work / 'traces/worker-1/trace.jsonl'; path.parent.mkdir(parents=True)
            records = [dict(kind='ready', functions=['vxlan_xmit'], specification={'devices': ['experiment']}, kernel='6.8'),
                       dict(kind='result', exit_code=0, records=[
                           {'type': 'stats', 'data': {'@function_ns': {'0': {'count': 3, 'average': 1200, 'total': 3600}}}},
                           {'type': 'hist', 'data': {'@function_hist_ns': {'0': [{'min': 1024, 'max': 2047, 'count': 3}]}}}])]
            path.write_text('\n'.join(json.dumps(record) for record in records))
            result = analyze_latency.trace_rows(work, {'mode': 'vm-vxlan-1450', 'condition': 'tunnel'})[0]
            self.assertEqual(result['mean_us'], 1.2)
            self.assertEqual(result['p99_bin_upper_us'], 2.047)
            self.assertEqual(result['count'], 3)

    def test_balanced_paired_conditions_and_separate_traces(self):
        plan = latency_study.plan()
        for mode in ('host-vxlan-1450', 'vm-vxlan-1450'):
            trials = [step for step in plan if step['mode'] == mode and 'pair' in step]
            self.assertEqual(len(trials), 10)
            for number in range(1, 6):
                self.assertEqual({step['condition'] for step in trials if step['pair'] == number}, {'tunnel', 'bypass'})
            orders = [step['condition'] for step in trials[::2]]
            self.assertEqual(sorted([orders.count('tunnel'), orders.count('bypass')]), [2, 3])
        self.assertEqual(sum(step.get('trace', False) for step in plan), 5)
        self.assertEqual(plan[-1]['name'], 'restore-original-host-mode')

    def test_vm_control_builds_complete_direct_and_static_vtep_mapping(self):
        hosts = {'control-1': {'experiment_ipv4': '10.74.0.11/24'},
                 'worker-1': {'experiment_ipv4': '10.74.0.12/24'},
                 'worker-2': {'experiment_ipv4': '10.74.0.21/24'}}
        nodes = {'items': [{'metadata': {'name': name}, 'spec': {'podCIDR': f'10.244.{i}.0/24'}}
                           for i, name in enumerate(hosts)]}
        specs = path_control.specifications('vm-vxlan-1450', 'bypass', {}, hosts, nodes)
        peers = specs['worker-1']['peers']
        self.assertEqual({peer['name'] for peer in peers}, {'control-1', 'worker-2'})
        self.assertEqual(next(peer['vtep'] for peer in peers if peer['name'] == 'worker-2'), '10.245.0.3')
        routes = [{'dst': peer['cidr'], 'gateway': peer['ip'], 'dev': 'experiment', 'protocol': '243'} for peer in peers]
        snapshot = {'links': '[]', 'routes': json.dumps(routes)}
        path_control.validate(snapshot, 'worker-1', hosts, 'vm-vxlan-1450', 'bypass', nodes)
        routes[0]['gateway'] = '10.74.0.99'
        with self.assertRaisesRegex(ValueError, 'next hop'):
            path_control.validate({'links': '[]', 'routes': json.dumps(routes)}, 'worker-1', hosts, 'vm-vxlan-1450', 'bypass', nodes)

    def test_trace_uses_migration_safe_keys_and_scoped_header_helpers(self):
        available = 'vxlan_xmit [vxlan]\nvxlan_rcv [vxlan]\nvxlan_build_skb.isra.0 [vxlan]\n__iptunnel_pull_header\nip_forward\n'
        functions = latency_trace.select_functions(available)
        program = latency_trace.build_program({'tunnel': 'cs740vx', 'devices': ['experiment'], 'duration': 15,
                                              'handoff_functions': {'napi_gro_receive': 1}}, functions, {100: 1}, 200)
        self.assertIn('tid, (tid==0 ? cpu+1 : 0)', program)
        self.assertIn('@depth[0,tid,', program)
        self.assertIn('@depth[1,tid,', program)
        self.assertIn('@receive_deferred_ns', program)
        self.assertIn('pid==200 && comm=="cs740-mark"', program)
        self.assertIn('kprobe:vxlan_build_skb.isra.0', program)
        with self.assertRaises(ValueError):
            latency_trace.build_program({'tunnel': 'bad;name', 'devices': [], 'duration': 15}, functions, {}, 1)


if __name__ == '__main__':
    unittest.main()
