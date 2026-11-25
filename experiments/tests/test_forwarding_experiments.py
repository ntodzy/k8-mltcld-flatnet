import argparse
import copy
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import common
import host_bypass
import run as runner
import setup


class ForwardingExperimentTests(unittest.TestCase):
    def test_optional_modes_keep_original_default_suite(self):
        self.assertEqual(len(common.MODES),8)
        self.assertFalse(set(common.BPF_MODES)&set(common.MODES))
        for mode,base in common.BPF_MODES.items():
            self.assertIn(mode,common.ALL_MODES)
            self.assertEqual(common.mtu_profile(mode),common.mtu_profile(base))
        source=json.loads((common.ROOT/'infrastructure/config.example.json').read_text())
        original=copy.deepcopy(source)
        for mode in common.BPF_MODES:
            configured=setup.deployment_config(source,mode,Path('/tmp/config'))
            self.assertEqual(configured['hypervisors'][0]['network']['mode'],
                             'bridge' if mode in common.HOST_BPF_MODES else 'direct')
            inventory=setup.hypervisor_inventory(source,mode,Path('/tmp/ssh'),'10.75.0.0/24')
            self.assertEqual(inventory['all']['vars']['experiment_mode'],common.base_mode(mode))
        self.assertEqual(source,original)

    def test_guest_profile_requires_all_prerequisites_and_correct_routing(self):
        data={'kube-proxy-replacement':'true','enable-bpf-masquerade':'true',
              'enable-host-legacy-routing':'false'}
        runner.validate_forwarding(data,'vm-vxlan-bpf-1450')
        with self.assertRaisesRegex(ValueError,'enable-host-legacy-routing'):
            runner.validate_forwarding(data,'vm-vxlan-bpf-legacy-1450')
        for key in data:
            broken=dict(data);broken.pop(key)
            with self.assertRaises(ValueError):runner.validate_forwarding(broken,'vm-vxlan-bpf-1450')

    def test_only_ready_unique_nonhost_benchmark_endpoints_are_selected(self):
        def pod(node,ip,ready=True,host=False):
            return {'spec':{'nodeName':node,'hostNetwork':host},
                    'status':{'podIP':ip,'phase':'Running','conditions':[
                        {'type':'Ready','status':'True' if ready else 'False'}]}}
        payload={'items':[pod('a','10.244.1.2'),pod('b','10.244.2.3'),pod('a','10.74.0.1',host=True)]}
        self.assertEqual(host_bypass.endpoint_ips(payload,'a','b'),{'a':'10.244.1.2','b':'10.244.2.3'})
        payload['items'].append(pod('a','10.244.1.9'))
        self.assertIsNone(host_bypass.endpoint_ips(payload,'a','b'))
        self.assertIsNone(host_bypass.endpoint_ips({'items':[pod('a','10.244.1.2',ready=False),pod('b','10.244.2.3')]},'a','b'))

    def test_host_controls_have_equal_pretest_delay_and_pod_only_arguments(self):
        for mode in common.HOST_BPF_MODES:
            args=argparse.Namespace(mode=mode,duration=30,samples=1,streams=4,
                                    same_node=False,pod_only=True)
            argv=runner.perf_arguments(args,'ns','/tmp/reports','a','b')
            self.assertEqual(argv[-2:],['--setup-delay','60s'])
            self.assertIn('--host-net=false',argv)
            self.assertIn('--pod-net=true',argv)

    def test_guest_helm_profiles_differ_only_in_legacy_routing(self):
        try:from jinja2 import Environment
        except ImportError:self.skipTest('Jinja2 comes with the required Ansible installation')
        source=(common.ROOT/'infrastructure/ansible/roles/cilium/templates/values.yml.j2').read_text()
        env=Environment()
        variables=dict(cilium_network_mode='vm-vxlan-1450',cilium_vm_vxlan_modes=['vm-vxlan-1450'],
                       cilium_host_vxlan_modes=[],cilium_inner_mtu=1450,kubernetes_pod_subnet='10.244.0.0/16',
                       groups={'kubernetes_controllers':['control']},hostvars={'control':{'kubernetes_node_ip':'10.74.0.11'}})
        legacy=env.from_string(source).render(**variables,cilium_forwarding_profile='bpf-legacy')
        fast=env.from_string(source).render(**variables,cilium_forwarding_profile='bpf')
        self.assertEqual(legacy.replace('hostLegacyRouting: true','hostLegacyRouting: false'),fast)
        self.assertIn('kubeProxyReplacement: true',fast)
        self.assertIn('masquerade: true',fast)
        init=(common.ROOT/'infrastructure/ansible/roles/kubernetes_control_plane/templates/kubeadm-init.yml.j2').read_text()
        self.assertIn('addon/kube-proxy',env.from_string(init).render(cilium_forwarding_profile='bpf'))
        self.assertNotIn('skipPhases',env.from_string(init).render(cilium_forwarding_profile='baseline'))

    def test_failed_host_cleanup_preserves_marker_and_attempts_every_agent(self):
        stopped=[]
        class Agent:
            records=[]
            def __init__(self,name):self.target=name
            def stop(self):
                stopped.append(self.target)
                if self.target=='bad':raise RuntimeError('injected cleanup failure')
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);marker=root/'marker.json';marker.write_text('{}')
            session=host_bypass.HostBypass.__new__(host_bypass.HostBypass)
            session.agents=[Agent('good'),Agent('bad')]
            session.state={};session.directory=root
            session.args=SimpleNamespace(mode='host-vxlan-bpf-1450')
            with patch.object(host_bypass,'MARKER',marker):
                with self.assertRaisesRegex(RuntimeError,'cleanup failure'):
                    session.stop(require_traffic=False)
            self.assertEqual(stopped,['bad','good'])
            self.assertEqual(json.loads(marker.read_text())['status'],'cleanup-failed')

    def test_recovery_refuses_a_live_owner(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);marker=root/'marker.json';marker.write_text('{"pid":123}')
            (root/'config.json').write_text('{"hypervisors":[]}')
            (root/'infrastructure').mkdir()
            (root/'infrastructure/config.json').write_text('{"hypervisors":[]}')
            with patch.object(host_bypass,'ROOT',root),patch.object(host_bypass,'GENERATED',root),\
                 patch.object(host_bypass,'MARKER',marker),patch.object(host_bypass.os,'kill',return_value=None):
                with self.assertRaisesRegex(RuntimeError,'owning process still exists'):host_bypass.recover()
            self.assertTrue(marker.exists())


if __name__=='__main__':unittest.main()
