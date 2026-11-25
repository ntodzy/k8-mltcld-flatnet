#!/usr/bin/env python3
"""Report matched host idle-policy experiments without pooling separate boots/studies."""
import argparse
from collections import defaultdict
import csv
import json
from pathlib import Path
import statistics

import aggregate
from common import save_json


def load(directory, partial=False):
    state=json.loads((directory/'collection.json').read_text())
    if state['study_type']!='host-latency-optimization':
        raise ValueError('Expected a host latency study')
    if state['status']!='passed' and not partial:
        raise ValueError('Study incomplete; use --partial to label interim output')
    rows=[]
    for step in state['plan']:
        if step['kind']!='measurement' or step['status']!='passed':continue
        work=directory/step['attempts'][-1]['directory']
        info, observations, warnings=aggregate.load_attempt(work/'measurement')
        if info['metadata']['status']!='passed':raise ValueError('Unsuccessful measurement selected')
        values={r['metric']:r['value'] for r in observations if r['domain']=='performance'}
        phases=info['metadata']['phases']
        if len(phases)!=1:raise ValueError('One phase per controlled observation required')
        summary=json.loads((work/'measurement'/phases[0]['directory']/'summary.json').read_text())
        busy_cores=sum(v['busy_core_seconds']/v['observed_active_seconds']
                       for k,v in summary.items() if k.startswith('hypervisor-'))
        row=dict(study=directory.name,mode=step['mode'],workload=step['workload'],pair=step['pair'],
                 policy='default' if step['cpu_latency_us']==-1 else '10us',
                 duration=state['duration'],mean_us=values.get('mean'),p50_us=values.get('p50'),
                 p90_us=values.get('p90'),p99_us=values.get('p99'),
                 throughput=values['throughput'],host_busy_cores=busy_cores,source=str(work),warnings=sorted(warnings))
        # CLI reports stream throughput in Mb/s and RR in transactions/sec.
        row['host_cpu_cost_proxy']=(busy_cores*1e6/values['throughput'] if step['workload']=='rr'
                                    else busy_cores/(values['throughput']/8000))
        policies=[]
        for recordfile in (work/'cpu-latency').glob('*/request.jsonl'):
            records=[json.loads(line) for line in recordfile.read_text().splitlines()]
            initial=next(r for r in records if r['kind']=='ready')
            final=next(r for r in records if r['kind']=='restored')
            if final['effective_us']!=final['expected_us']:raise ValueError('Policy restoration failed')
            expected=2000000000 if step['cpu_latency_us']==-1 else 10
            if initial['effective_us']!=expected:raise ValueError('Unexpected effective policy')
            policies.append(dict(target=recordfile.parent.name,effective_us=initial['effective_us'],
                deep_idle_entries=sum(s['usage']-initial['idle'][name]['usage']
                                      for name,s in final['idle'].items() if s['latency_us']>10)))
        if len(policies)!=2:raise ValueError('Missing hypervisor policy proof')
        if row['policy']=='10us' and any(p['deep_idle_entries'] for p in policies):
            raise ValueError('Deep idle occurred during constrained interval')
        row['policies']=policies
        rows.append(row)
    return state,rows


def summarize(rows):
    groups=defaultdict(list)
    for row in rows:groups[tuple(row[k] for k in ('study','mode','workload','duration','policy'))].append(row)
    result=[]
    for key,items in groups.items():
        entry=dict(zip(('study','mode','workload','duration','policy'),key));entry['n']=len(items)
        for metric in ('mean_us','p50_us','p90_us','p99_us','throughput','host_busy_cores','host_cpu_cost_proxy'):
            values=[r[metric] for r in items if r.get(metric) is not None]
            if values:
                entry[metric]=statistics.mean(values)
                entry[metric+'_sd']=statistics.stdev(values) if len(values)>1 else 0
        result.append(entry)
    return result


def paired(rows):
    groups=defaultdict(dict)
    for row in rows:groups[tuple(row[k] for k in ('study','mode','workload','pair'))][row['policy']]=row
    output=[]
    for key,group in groups.items():
        if set(group)!={'default','10us'}:continue
        a,b=group['default'],group['10us']
        output.append(dict(zip(('study','mode','workload','pair'),key),
                           mean_reduction_us=(a['mean_us']-b['mean_us']) if a['mean_us'] is not None else None,
                           p99_reduction_us=(a['p99_us']-b['p99_us']) if a['p99_us'] is not None else None,
                           throughput_change=b['throughput']-a['throughput']))
    return output


def csv_file(path,rows):
    if not rows:return
    fields=list(dict.fromkeys(k for r in rows for k in r))
    with path.open('w') as stream:
        writer=csv.DictWriter(stream,fieldnames=fields);writer.writeheader();writer.writerows(rows)


def describe(row,metric,scale=1):
    if metric not in row:return '—'
    return f'{row[metric]*scale:.3f} ± {row[metric+"_sd"]*scale:.3f}'


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directories',type=Path,nargs='+')
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--partial',action='store_true')
    args=parser.parse_args()
    args.output.mkdir(parents=True,exist_ok=False)
    rows=[];states=[]
    for directory in args.directories:
        state,items=load(directory.resolve(),args.partial);rows.extend(items);states.append(state)
    stats=summarize(rows);differences=paired(rows)
    save_json(args.output/'observations.json',rows);save_json(args.output/'summary.json',stats)
    save_json(args.output/'paired.json',differences)
    csv_file(args.output/'observations.csv',[{k:v for k,v in r.items() if k not in ('policies','warnings')} for r in rows])
    csv_file(args.output/'summary.csv',stats);csv_file(args.output/'paired.csv',differences)
    lines=['# Host VXLAN latency optimization'+(' — INTERIM' if args.partial else ''),'',
           'Mean ± sample standard deviation across separate invocations. The same worker-1 → worker-2 Pod TCP endpoints, MTU profiles and diagnostics are used. '
           'Policies are alternated in balanced AB/BA order within each unchanged VM boot/attachment block. '
           'Every default case holds the same SSH/descriptor helper as its 10 µs contrast. '
           'Mode changes reset the cluster; cross-mode differences also include different boots and attachments. '
           'Separate studies and durations remain separate groups. Instrumented traces and failed attempts are excluded.','',
           'CPU latency QoS restricts eligible hypervisor idle states; it does not set a packet latency SLA, CPU frequency or busy polling. '
           'Requests last only for each invocation and restoration is checked. Idle counters cover the entire invocation including startup. '
           'Energy was not measured; keeping cores awake can increase power consumption.','',
           '| Study | Mode | Policy | n | Mean RR µs | P50 µs | P99 µs | RR ops/s | Host CPU µs/op proxy |',
           '|---|---|---|---:|---:|---:|---:|---:|---:|']
    for r in stats:
        if r['workload']=='rr':lines.append('| '+' | '.join([r['study'],r['mode'],r['policy'],str(r['n']),
             *[describe(r,k) for k in ('mean_us','p50_us','p99_us','throughput','host_cpu_cost_proxy')]])+' |')
    lines+=['','| Study | Mode | Workload | Policy | n | Gb/s | Host CPU-s/GB proxy |',
            '|---|---|---|---|---:|---:|---:|']
    for r in stats:
        if r['workload']!='rr':lines.append('| '+' | '.join([r['study'],r['mode'],r['workload'],r['policy'],str(r['n']),
                  describe(r,'throughput',.001),describe(r,'host_cpu_cost_proxy')])+' |')
    lines+=['','Host CPU proxies sum both hypervisors’ busy core equivalents during the client-observed active window, '
            'then divide by CLI application rate. They include background activity, are not exclusive VXLAN CPU cost, '
            'and omit physical power consumption. P99 columns average per-invocation P99s; they are not pooled packet percentiles.','',
            '## Matched idle-policy effects','',
            '| Study | Mode | Workload | Pairs | Mean reduction µs | P99 reduction µs | Rate change (ops/s or Mb/s) |',
            '|---|---|---|---:|---:|---:|---:|']
    grouped=defaultdict(list)
    for r in differences:grouped[(r['study'],r['mode'],r['workload'])].append(r)
    for key,items in grouped.items():
        cells=[]
        for metric in ('mean_reduction_us','p99_reduction_us','throughput_change'):
            values=[r[metric] for r in items if r[metric] is not None]
            cells.append((f'{statistics.mean(values):.3f} ± {statistics.stdev(values):.3f}' if len(values)>1
                          else f'{values[0]:.3f}' if values else '—'))
        lines.append('| '+' | '.join([*key,str(len(items)),*cells])+' |')
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    for workload,metric,ylabel in [('rr','mean_us','Mean transaction latency (µs)'),('rr','p99_us','Per-invocation P99 (µs)'),
                                   ('single','throughput','Single-stream throughput (Gb/s)'),('multi','throughput','Four-stream throughput (Gb/s)')]:
        selected=[r for r in stats if r['workload']==workload]
        if not selected:continue
        fig,ax=plt.subplots(figsize=(max(9,len(selected)*1.1),5))
        scale=.001 if metric=='throughput' else 1
        for x,entry in enumerate(selected):
            values=[r[metric]*scale for r in rows if all(r[k]==entry[k] for k in ('study','mode','workload','policy','duration'))]
            ax.errorbar(x,entry[metric]*scale,yerr=entry[metric+'_sd']*scale,fmt='o',capsize=4,color='#147d92' if entry['policy']=='10us' else '#606060')
            ax.scatter([x+(i-(len(values)-1)/2)*.035 for i in range(len(values))],values,s=14,alpha=.6)
        ax.set_xticks(range(len(selected)),[r['mode']+'\n'+r['policy']+'\n'+r['study'].removeprefix('host-latency-') for r in selected],rotation=30,ha='right',fontsize=8)
        ax.set_ylabel(ylabel);ax.set_ylim(bottom=0);ax.grid(axis='y',alpha=.2)
        fig.tight_layout();name=f'{workload}-{metric}'
        for extension in ('svg','png'):fig.savefig(args.output/f'{name}.{extension}',dpi=160)
        plt.close(fig);lines+=['',f'![{ylabel}]({name}.svg)']
    lines+=['','Raw observations retain phase sources, sample identity, actual policy/restoration proof, deep-idle counters and warnings. '
            'A favorable tuned-versus-default comparison is an optimization result, not an isolated tunnel-placement effect.']
    (args.output/'report.md').write_text('\n'.join(lines)+'\n')
    print(args.output/'report.md')


if __name__=='__main__':main()
