import sys
from pathlib import Path
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from host_latency_study import plan
from analyze_host_latency import paired, summarize, study_identity
from common import ROOT


class HostLatencyStudyTests(unittest.TestCase):
    def test_balanced_matched_policies_and_restoration(self):
        steps=plan(['host-vxlan-1450','vxlan'],5,['rr','single','multi'])
        self.assertEqual(len(steps),63)
        self.assertEqual(steps[-1]['name'],'restore-original-host-mode')
        for mode in ('host-vxlan-1450','vxlan'):
            for workload in ('rr','single','multi'):
                rows=[s for s in steps if s['kind']=='measurement' and s['mode']==mode and s['workload']==workload]
                for pair in range(1,6):
                    self.assertEqual([s['cpu_latency_us'] for s in rows if s['pair']==pair],
                                     [-1,10] if pair%2 else [10,-1])
                self.assertTrue(all(s['status']=='pending' and not s['attempts'] for s in rows))

    def test_analysis_does_not_pair_across_studies_or_workloads(self):
        rows=[]
        for study,base in [('first',900),('second',700)]:
            for workload in ['rr','single']:
                for policy,value in [('default',base),('10us',base-300)]:
                    rows.append(dict(study=study,mode='host-vxlan-1450',workload=workload,
                        duration=30,policy=policy,pair=1,mean_us=value if workload=='rr' else None,
                        p99_us=value+100 if workload=='rr' else None,throughput=1000,
                        host_busy_cores=2,host_cpu_cost_proxy=2000))
        self.assertEqual(len(summarize(rows)),8)
        differences=paired(rows)
        self.assertEqual(len(differences),4)
        self.assertEqual([r['mean_reduction_us'] for r in differences if r['workload']=='rr'],[300,300])
        self.assertTrue(all(r['mean_reduction_us'] is None for r in differences if r['workload']=='single'))

    def test_traces_are_separate_from_primary_measurements(self):
        steps=plan(['host-vxlan-routing-bpf-1450'],3,['rr'],True)
        self.assertEqual(sum(s['kind']=='trace' for s in steps),1)
        self.assertEqual(sum(s['kind']=='measurement' for s in steps),6)

    def test_single_policy_throughput_keeps_every_sample_without_duplicate_contrast(self):
        steps=plan(['host-vxlan-routing-bpf-1450','vxlan'],5,['single','multi'],policies=[-1])
        measured=[s for s in steps if s['kind']=='measurement']
        self.assertEqual(len(measured),20)
        self.assertEqual({s['cpu_latency_us'] for s in measured},{-1})
        self.assertEqual({s['pair'] for s in measured},set(range(1,6)))

    def test_nested_studies_with_same_basename_keep_distinct_identities(self):
        a=study_identity(ROOT/'results'/'first'/'throughput')
        b=study_identity(ROOT/'results'/'second'/'throughput')
        self.assertNotEqual(a,b)
        self.assertEqual(a,'first/throughput')


if __name__=='__main__':unittest.main()
