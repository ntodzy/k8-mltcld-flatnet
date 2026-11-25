#!/usr/bin/env python3
"""Separate, instrumented forwarding-path validation for optional BPF experiments."""
import argparse
from datetime import datetime, timezone
import fcntl
import json
from pathlib import Path
import sys

from common import BPF_MODES, GENERATED, ROOT, save_json


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode',choices=tuple(BPF_MODES)+('host-vxlan-1450','vm-vxlan-1450'))
    parser.add_argument('--output',type=Path)
    parser.add_argument('--dry-run',action='store_true')
    args=parser.parse_args()
    # Reuse the previously validated four-machine tracer, including its fixed
    # worker-1 -> worker-2 diagnostic arming path. Do not silently trace others.
    output=(args.output or ROOT/'results'/('forwarding-trace-'+args.mode+'-'+
            datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ'))).resolve()
    command=[sys.executable,str(ROOT/'experiments/run.py'),args.mode,'--client','worker-1',
             '--server','worker-2','--pod-only','--diagnostics','--test-type','rr',
             '--duration','30','--samples','1','--output',str(output/'measurement')]
    if args.dry_run:
        print(json.dumps({'command':command,'instrumented':True,'tracing_seconds':15,
                          'benchmark_seconds':30,'output':str(output)},indent=2));return
    with (GENERATED/'collection.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        active=json.loads((GENERATED/'active-experiment.json').read_text())
        if active['mode']!=args.mode:raise ValueError('Prepare the matching experiment first')
        if any((GENERATED/name).exists() for name in ('path-control.json','forwarding-control.json')):
            raise ValueError('Restore existing path/forwarding controls first')
        output.mkdir(parents=True,exist_ok=False)
        metadata={'mode':args.mode,'instrumented':True,'status':'running',
                  'benchmark_seconds':30,'tracing_seconds':15,'command':command,
                  'purpose':'Executed stages and counts; exclude from untraced latency estimates'}
        save_json(output/'trace-metadata.json',metadata)
        try:
            from latency_study import measured
            from analyze_latency import trace_rows,write_csv
            measured(command,output,args.mode,True)
            rows=trace_rows(output,{'mode':args.mode,'condition':'instrumented'})
            save_json(output/'kernel-observations.json',rows)
            write_csv(output/'kernel-observations.csv',rows)
            # Keep instrumentation visible even when a measurement is aggregated.
            for path in (output/'measurement').rglob('metadata.json'):
                item=json.loads(path.read_text());item['instrumented']=True
                item['instrumentation']='bounded-forwarding-trace';save_json(path,item)
            metadata['status']='passed'
        except BaseException as error:
            metadata.update(status='failed',error=str(error));raise
        finally:save_json(output/'trace-metadata.json',metadata)
    print('Instrumented forwarding observations: '+str(output))


if __name__=='__main__':main()
