import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import latency_study
import latency_trace
import path_control


class LatencyIsolationTests(unittest.TestCase):
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
