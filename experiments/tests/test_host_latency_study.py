import sys
from pathlib import Path
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from host_latency_study import plan


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


if __name__=='__main__':unittest.main()
