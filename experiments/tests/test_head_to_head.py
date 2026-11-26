import copy
import json
from pathlib import Path
import struct
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import common
import setup
import run as runner
import head_to_head as study
import head_to_head_path as path_check
from analyze_head_to_head import block_means, summarize, contrasts


class HeadToHeadTests(unittest.TestCase):
    def test_six_cells_per_rebuild_and_recorded_order(self):
        steps = study.plan(3, 42)
        self.assertEqual(len(steps), 18)
        self.assertEqual(steps, study.plan(3, 42))
        self.assertNotEqual(steps, study.plan(3, 43))
        expected = {(a,p) for a in study.ARCHITECTURES for p in ('baseline','optimized')}
        for block in range(1,4):
            selected = [s for s in steps if s['block']==block]
            self.assertEqual({(s['architecture'],s['profile']) for s in selected}, expected)
        self.assertEqual(len({s['name'] for s in steps}), 18)

    def test_six_blocks_cover_every_order_position(self):
        steps = study.plan(6,42)
        for architecture in study.ARCHITECTURES:
            for profile in ('baseline','optimized'):
                self.assertEqual({i%6 for i,s in enumerate(steps) if (s['architecture'],s['profile'])==(architecture,profile)},set(range(6)))

    def test_cilium_attachment_variants_preserve_base_and_default_suite(self):
        source = json.loads((common.ROOT/'infrastructure/config.example.json').read_text())
        original = copy.deepcopy(source)
        for mode in common.CILIUM_TUNNEL_MODES:
            configured = setup.deployment_config(source,mode,Path('/tmp'))
            for host in configured['hypervisors']:
                self.assertEqual(host['network']['mode'],'bridge' if mode in common.DIRECT_BRIDGE_MODES else 'direct')
                self.assertEqual(host['network']['mtu'],1500)
            inventory = setup.hypervisor_inventory(source,mode,Path('/tmp/ssh'),'10.75.0.0/24')
            self.assertEqual(inventory['all']['vars']['experiment_mode'],'vxlan')
            self.assertEqual(inventory['all']['vars']['experiment_direct_bridge'],mode in common.DIRECT_BRIDGE_MODES)
        self.assertEqual(source,original)
        self.assertEqual(len(common.MODES),8)
        self.assertFalse(set(common.CILIUM_VARIANTS)&set(common.MODES))

    def test_optimized_cilium_requires_tunnel_and_bpf_without_native_fallback(self):
        status = {'routing':{'inter-host-routing-mode':'Tunnel','intra-host-routing-mode':'BPF'}}
        for mode in ('vxlan-bpf','vxlan-bridge-bpf'):
            runner.validate_runtime_routing(status,mode)
            runner.validate_forwarding({'kube-proxy-replacement':'true','enable-bpf-masquerade':'true',
                                        'enable-host-legacy-routing':'false'},mode)
            with self.assertRaises(ValueError):
                runner.validate_runtime_routing({'routing':{'inter-host-routing-mode':'Native','intra-host-routing-mode':'BPF'}},mode)

    def test_packet_captures_reject_nested_and_fragmented_paths(self):
        good = dict(port=8472, outer={'fragment':0},inner={'protocol':6,'fragment':0,'length':1450})
        self.assertEqual(path_check.validate_packets([good],8472)['matched_tcp_packets'],1)
        for key, value in [('port',4789),('inner',{'protocol':17,'fragment':0,'length':1450}),
                           ('outer',{'fragment':8192})]:
            broken = dict(good); broken[key]=value
            with self.assertRaises(ValueError):path_check.validate_packets([broken],8472)
        with self.assertRaises(ValueError):path_check.validate_packets([],8472)

    def test_pcap_extracts_actual_pod_endpoints_and_inner_length(self):
        def ip(src,dst,length,protocol):
            import ipaddress
            return struct.pack('!BBHHHBBH4s4s',0x45,0,length,1,0x4000,64,protocol,0,
                               ipaddress.IPv4Address(src).packed,ipaddress.IPv4Address(dst).packed)
        ethernet = bytes(12)+b'\x08\x00'
        frame = ethernet+ip('10.74.0.1','10.74.0.2',1500,17)+struct.pack('!HHHH',50000,8472,1480,0)
        frame += b'\x08'+bytes(7)+ethernet+ip('10.244.1.2','10.244.2.3',1450,6)+bytes(20)
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'capture.pcap'
            path.write_bytes(struct.pack('<IHHIIII',0xa1b2c3d4,2,4,0,0,128,1)+struct.pack('<IIII',0,0,len(frame),len(frame))+frame)
            packets=path_check.vxlan_packets(path,{'10.244.1.2','10.244.2.3'})
            self.assertEqual(len(packets),1)
            self.assertEqual(packets[0]['inner']['length'],1450)
            self.assertEqual(path_check.vxlan_packets(path,{'10.244.1.99','10.244.2.3'}),[])
            path.write_bytes(path.read_bytes()[:-1])
            with self.assertRaisesRegex(ValueError,'Truncated'):path_check.vxlan_packets(path,{'10.244.1.2','10.244.2.3'})

    def test_repetitions_are_averaged_before_between_rebuild_statistics(self):
        rows=[dict(architecture='host-vxlan',profile='baseline',block=b,workload='rr',mean_us=v)
              for b,values in [(1,[100,100,100]),(2,[300])] for v in values]
        blocks=block_means(rows)
        stats=summarize(blocks)[0]
        self.assertEqual(stats['mean_us'],200)
        self.assertEqual(stats['rebuilds'],2)
        self.assertEqual(stats['invocations'],4)
        self.assertAlmostEqual(stats['mean_us_between_boot_sd'],141.4213562)

    def test_contrasts_do_not_pair_different_rebuilds(self):
        blocks=[dict(architecture='host-vxlan',profile=p,block=b,workload='rr',mean_us=v)
                for b,p,v in [(1,'baseline',100),(1,'optimized',50),(2,'baseline',300)]]
        rows=contrasts(blocks)
        self.assertEqual(len(rows),1)
        self.assertEqual(rows[0]['change_percent'],-50)

    def test_audit_rejects_wrong_attachment_queues_affinity_and_host_tunnel(self):
        mode='vxlan-bridge-bpf'
        audit={'worker-1':dict(experiment_mac='aa:bb:cc:dd:ee:ff',physical_interface='enoX',
            xml='<domain><devices><interface type="bridge"><mac address="aa:bb:cc:dd:ee:ff"/><model type="virtio"/>'
                '<source bridge="cs740br"/><driver name="vhost" queues="2"/></interface></devices></domain>',
            links=[dict(ifname='enoX',mtu=1500,master='cs740br'),dict(ifname='cs740br',mtu=1500,ifindex=2)],
            vhost={'123':[2]})}
        env={'worker-1':dict(channels='Current hardware settings:\nCombined: 2',interface={'mtu':1500})}
        study.validate_audit(mode,'optimized',audit,env)
        for mutation in ('tunnel','affinity','queues','attachment'):
            broken=copy.deepcopy(audit)
            if mutation=='tunnel':broken['worker-1']['links'].append(dict(ifname='cs740hvx'))
            elif mutation=='affinity':broken['worker-1']['vhost']['123']=[1,2]
            elif mutation=='queues':broken['worker-1']['xml']=broken['worker-1']['xml'].replace('queues="2"','queues="1"')
            else:broken['worker-1']['xml']=broken['worker-1']['xml'].replace('type="bridge"','type="direct"')
            with self.assertRaises(ValueError):study.validate_audit(mode,'optimized',broken,env)


if __name__=='__main__':unittest.main()
