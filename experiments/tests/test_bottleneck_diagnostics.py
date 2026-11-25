import json
from pathlib import Path
import sys
import unittest
import tempfile
import xml.etree.ElementTree as ET

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import diagnostic_agent
import diagnostics
import setup
import study
import analyze_study
import affinity_agent
from common import ROOT, mtu_profile
from queue_agent import driver_xml
from test_diagnostics import record


class BottleneckTests(unittest.TestCase):
    def test_affinity_selects_distinct_physical_cores_and_parses_numa_cpu_ranges(self):
        self.assertEqual(affinity_agent.cpulist('0-2,4,6-7'), {0, 1, 2, 4, 6, 7})
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for cpu, core in ((0, 0), (1, 0), (2, 1), (3, 1)):
                path = root / f'cpu{cpu}/topology'
                path.mkdir(parents=True)
                (path / 'physical_package_id').write_text('0')
                (path / 'core_id').write_text(str(core))
            chosen = affinity_agent.choose_cpus({0, 1, 2, 3}, root)
            self.assertEqual(chosen, [1, 2])
            with self.assertRaises(ValueError):
                affinity_agent.choose_cpus({0, 1}, root)

    def test_profiles_use_self_overhead_and_keep_network_work_in_idle_context(self):
        raw = (' 90.0% 80.0% swapper [kernel.kallsyms] [k] cpuidle_enter_state\n'
               ' 50.0% 1.0% vhost-123 [kernel.kallsyms] [k] csum_partial\n'
               ' 3.0% swapper [kernel.kallsyms] [k] mlx4_en_process_rx_cq\n')
        rows = analyze_study.profile_self_rows(raw)
        self.assertEqual(rows, [(3.0, 'swapper', 'mlx4_en_process_rx_cq'), (1.0, 'vhost-123', 'csum_partial')])

    def test_local_capture_requires_no_accepted_or_lost_packets(self):
        good = {'exit_code': 0, 'log': '0 packets captured\n0 packets received by filter\n0 packets dropped by kernel\n'}
        self.assertTrue(diagnostics.capture_is_empty(good))
        for label in ('packets captured', 'packets received by filter', 'packets dropped by kernel'):
            self.assertFalse(diagnostics.capture_is_empty({**good, 'log': good['log'].replace('0 ' + label, '1 ' + label)}))
        self.assertFalse(diagnostics.capture_is_empty({'exit_code': 0, 'log': ''}))
        self.assertFalse(diagnostics.capture_is_empty(None))

    def test_efficiency_uses_physical_host_cpu_without_adding_guest_cpu_again(self):
        phase = {'test_type': 'multi', 'directory': 'phase'}
        targets = {
            'hypervisor-node1': {'logical_cpus': 4, 'per_core': {'cpu': {'busy_percent': 50}},
                                 'threads': [{'comm': 'vhost-1', 'cpu_percent_one_core': 80}]},
            'hypervisor-node2': {'logical_cpus': 4, 'per_core': {'cpu': {'busy_percent': 25}}},
            'worker-1': {'per_core': {'cpu': {'busy_percent': 90}}, 'pod_tcp': {'uid': {
                'host_network': False, 'snmp_delta': {'Tcp:RetransSegs': 10, 'Tcp:OutSegs': 1000},
                'data_rtt_ms': {'mean': 10}}}}}
        measurement = {'phases': [(phase, targets, Path('/unused'))],
                       'metrics': {('TCP_STREAM_MULTI', 'throughput'): [8000]}}
        result, _ = analyze_study.diagnose(measurement)
        self.assertEqual(result['estimated_host_cpu_seconds_per_GB'], [3])
        self.assertEqual(result['worker-1/retrans_per_1000_outsegs'], [10])
        self.assertEqual(result['hypervisor-node1/busiest_vhost_cpu'], [80])

    def test_overnight_plan_reruns_all_modes_without_contention_or_tuning(self):
        from common import MODES
        plan = study.plan()
        baselines = {step['mode'] for step in plan if step['kind'] == 'measurement' and step['name'].endswith('baseline')}
        self.assertEqual(baselines, set(MODES) | {'bridge-1450'})
        self.assertEqual([step['queues'] for step in plan if step['kind'] == 'queues' and 'queues' in step], [2])
        self.assertEqual(sum(step.get('restore', False) for step in plan), 1)
        self.assertFalse(any('contention' in step['name'] or 'hubble' in step['name'] for step in plan))

    def test_data_socket_metrics_exclude_netperf_control_connections(self):
        raw = ('State Recv-Q Send-Q Local Peer Process\n'
               'ESTAB 0 0 10.0.0.1:40000 10.0.0.2:12865 users:netperf\n\t cubic cwnd:10 rtt:1/0.1\n'
               'ESTAB 0 1000 10.0.0.1:40001 10.0.0.2:50000 users:netperf\n'
               '\t cubic cwnd:1000 rtt:20/2 minrtt:0.8 rwnd_limited:20ms(12.5%)\n')
        data = diagnostics.data_sockets(raw)
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]['cwnd'], 1000)
        self.assertEqual(data[0]['rtt_ms'], 20)
        self.assertEqual(data[0]['rwnd_limited_percent'], 12.5)

    def test_malformed_optional_ip_json_does_not_discard_a_valid_measurement(self):
        records = [{'kind': 'before', 'interfaces': {}, 'links': {'exit_code': 0, 'output': '[{}]info: warning'}},
                   {'kind': 'after', 'interfaces': {}, 'links': {'exit_code': 0, 'output': '[]'}}]
        result = diagnostics.summary(records)
        self.assertEqual(result['interface_counter_delta'], {})
        self.assertTrue(any('Cannot decode interface' in warning for warning in result['warnings']))

    def test_uid_matching_accepts_systemd_underscore_form_without_foreign_processes(self):
        pod = {'uid': '12345678-abcd-1111-2222-123456789abc', 'name': 'benchmark'}
        pods = {pod['uid']: pod}
        self.assertEqual(diagnostic_agent.pod_for_cgroup('0::/kubepods/pod12345678_abcd_1111_2222_123456789abc.slice', pods), pod)
        self.assertIsNone(diagnostic_agent.pod_for_cgroup('0::/unrelated/netperf', pods))

    def test_thread_stat_handles_spaces_in_kernel_task_names(self):
        fields = ['S'] + ['0'] * 49
        for index, value in ((11, '10'), (12, '20'), (19, '123'), (36, '3')):
            fields[index] = value
        parsed = diagnostic_agent.task_stat('100 (CPU 0/KVM) ' + ' '.join(fields))
        self.assertEqual(parsed['comm'], 'CPU 0/KVM')
        self.assertEqual((parsed['utime'], parsed['stime'], parsed['starttime'], parsed['processor']), (10, 20, 123, 3))

    def test_pod_tcp_counters_are_separate_from_host_namespace_and_threads_use_one_core_units(self):
        records = [record(1, True, 100), record(3, True, 110)]
        for index, sample in enumerate(records):
            sample['pods'] = [{'uid': 'uid', 'name': 'perf-client', 'netns': 'net:[123]',
                              'snmp': f'Tcp: RetransSegs\nTcp: {index * 17}\n',
                              'netstat': 'TcpExt: TCPTimeouts\nTcpExt: 0\n',
                              'tcp': {'exit_code': 0, 'output': f'cwnd:{10 + index} rtt:1.5/0.2'}}]
            sample['clock_ticks'] = 100
            sample['tasks'] = [{'pid': 1, 'tid': 2, 'starttime': 123, 'utime': index * 100,
                                'stime': 0, 'wait_ns': index * 2000000, 'comm': 'vhost-1'}]
        result = diagnostics.summary(records, 1, 3)
        self.assertEqual(result['snmp_delta']['Tcp:RetransSegs'], 10)
        self.assertEqual(result['pod_tcp']['uid']['snmp_delta']['Tcp:RetransSegs'], 17)
        self.assertEqual(result['pod_tcp']['uid']['cwnd']['max'], 11)
        self.assertEqual(result['threads'][0]['cpu_percent_one_core'], 50)
        self.assertEqual(result['threads'][0]['scheduler_wait_ms'], 2)
        records[1]['pods'][0]['netns'] = 'net:[999]'
        changed = diagnostics.summary(records, 1, 3)
        self.assertNotIn('uid', changed['pod_tcp'])
        self.assertTrue(any('changed network namespace' in warning for warning in changed['warnings']))

    def test_queue_changes_touch_only_matching_experiment_driver_and_restore(self):
        original = '''<domain><uuid>uuid</uuid><vcpu>2</vcpu><devices>
        <interface type="network"><mac address="00:00:00:00:00:01"/><model type="virtio"/><source network="default"/></interface>
        <interface type="bridge"><mac address="00:00:00:00:00:02"/><model type="virtio"/><source bridge="cs740br"/></interface>
        </devices></domain>'''
        altered, saved = driver_xml(original, '00:00:00:00:00:02', queues=2)
        self.assertIsNone(saved)
        a, b = ET.fromstring(original), ET.fromstring(altered)
        self.assertEqual(ET.tostring(a.findall('./devices/interface')[0]), ET.tostring(b.findall('./devices/interface')[0]))
        self.assertEqual(b.findall('./devices/interface')[1].find('driver').get('queues'), '2')
        restored, _ = driver_xml(altered, '00:00:00:00:00:02', original=saved, restore=True)
        self.assertIsNone(ET.fromstring(restored).findall('./devices/interface')[1].find('driver'))
        with self.assertRaisesRegex(ValueError, 'vCPUs'):
            driver_xml(original, '00:00:00:00:00:02', queues=3)
        with self.assertRaises(ValueError):
            driver_xml(original, '00:00:00:00:00:99', queues=2)

    def test_bridge_control_keeps_base_config_and_matches_host_inner_mtu(self):
        original = json.loads((ROOT / 'infrastructure/config.example.json').read_text())
        before = json.dumps(original, sort_keys=True)
        altered = setup.deployment_config(original, 'bridge-1450', ROOT / 'infrastructure')
        self.assertEqual(json.dumps(original, sort_keys=True), before)
        for host in altered['hypervisors']:
            self.assertEqual(host['network'], {'mode': 'bridge', 'source': 'cs740br', 'mtu': 1450})
        self.assertEqual(mtu_profile('bridge-1450'), {'inner_mtu': 1450, 'guest_mtu': 1450,
                                                  'physical_mtu': 1500, 'cilium_mtu': 1450})


if __name__ == '__main__':
    unittest.main()
