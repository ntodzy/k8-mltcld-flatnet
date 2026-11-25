#!/usr/bin/env python3
"""Export explicitly separated A/B/C/D paper cohorts and their source selections."""
import argparse
import csv
import json
from pathlib import Path
import statistics

import aggregate
from analyze_host_latency import load, summarize
from common import save_json

BASELINE_STEPS={'vxlan':'vxlan-baseline','vm-vxlan-1450':'vm-vxlan-1450-baseline',
                'host-vxlan-1450':'host-1450-final-baseline'}
LABELS={'vxlan':'Cilium VXLAN reference','vm-vxlan-1450':'Guest VXLAN',
        'host-vxlan-1450':'Hypervisor VXLAN',
        'host-vxlan-routing-legacy-1450':'Hypervisor VXLAN · legacy prerequisite control',
        'host-vxlan-routing-bpf-1450':'Hypervisor VXLAN · BPF guest forwarding',
        'vm-vxlan-bpf-1450':'Guest VXLAN · BPF guest forwarding'}


def baseline(directory):
    manifest=json.loads((directory/'collection.json').read_text())
    if manifest['status']!='passed':raise ValueError('Baseline collection must have passed')
    output=[];selection=[]
    for mode,name in BASELINE_STEPS.items():
        step=next(s for s in manifest['plan'] if s['name']==name)
        if step['status']!='passed' or step.get('profile') or step.get('trace'):
            raise ValueError('Expected an uninstrumented successful baseline: '+name)
        work=directory/step['attempts'][-1]['directory']/'measurement'
        info,observations,_=aggregate.load_attempt(work)
        selection.append(dict(group='A2',mode=mode,source=str(work)))
        for test,metric in [('TCP_RR','mean'),('TCP_RR','p99'),
                            ('TCP_STREAM','throughput'),('TCP_STREAM_MULTI','throughput')]:
            values=[r['value'] for r in observations if r['domain']=='performance'
                    and r['test']==test and r['metric']==metric]
            if len(values)!=5:raise ValueError('Expected five observations for '+name+'/'+test+'/'+metric)
            output.append(dict(group='A2',mode=mode,policy='default',test=test,metric=metric,
                               mean=statistics.mean(values),sd=statistics.stdev(values),n=len(values)))
    return output,selection


def optimized(directory,group,policy):
    state,rows=load(directory)
    controls={(r['mode'],r['workload'],r['pair']):r['source'] for r in rows if r['policy']=='default'}
    rows=[r for r in rows if r['policy']==policy]
    stats=summarize(rows)
    output=[]
    for entry in stats:
        test={'rr':'TCP_RR','single':'TCP_STREAM','multi':'TCP_STREAM_MULTI'}[entry['workload']]
        for source,metric in [('mean_us','mean'),('p99_us','p99'),('throughput','throughput'),
                              ('host_cpu_cost_proxy','host_cpu_cost_proxy')]:
            if source in entry:
                output.append(dict(group=group,mode=entry['mode'],policy=policy,test=test,metric=metric,
                    mean=entry[source],sd=entry[source+'_sd'],n=entry['n']))
    selection=[dict(group=group,mode=r['mode'],policy=policy,workload=r['workload'],
                    pair=r['pair'],source=r['source'],
                    default_control_source=controls.get((r['mode'],r['workload'],r['pair'])) if policy=='10us' else None)
               for r in rows]
    return output,selection


def fmt(row,scale=1):
    return f'{row["mean"]*scale:.3f} ± {row["sd"]*scale:.3f}'


def charts(directory,rows):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    images=[]
    for group,title in [('A2','A2 — Mode-only baseline; no additional optimizations'),
                        ('B2','B2 — BPF guest forwarding; DEFAULT idle policy'),
                        ('C','C — Idle-policy intervention; forwarding profile retained')]:
        entries=[r for r in rows if r['group']==group and r['test']=='TCP_RR' and r['metric']=='mean']
        if group=='C':
            entries=[r for r in rows if r['group'] in ('B2','C') and r['test']=='TCP_RR' and r['metric']=='mean']
            entries.sort(key=lambda r:(r['mode'],r['policy']))
        fig,ax=plt.subplots(figsize=(11,max(3.5,len(entries)*.65)))
        for y,r in enumerate(entries):
            ax.errorbar(r['mean'],y,xerr=r['sd'],fmt='o',capsize=4,
                        color='#147d92' if r['policy']=='10us' else '#555555')
        ax.set_yticks(range(len(entries)),[LABELS[r['mode']]+'\n'+
            ('10 µs idle QoS' if r['policy']=='10us' else 'Default idle')+f' | n={r["n"]}' for r in entries],fontsize=8)
        ax.invert_yaxis();ax.set_xlim(left=0);ax.set_xlabel('Mean TCP request/response latency (µs)')
        ax.set_title(title,fontsize=10);ax.grid(axis='x',alpha=.2);fig.tight_layout()
        stem=group+'-latency'
        for ext in ('svg','png'):fig.savefig(directory/(stem+'.'+ext),dpi=160)
        plt.close(fig);images.append((title,stem+'.svg'))
    for group in ('A2','D'):
        entries=[r for r in rows if r['group']==group and r['test'] in ('TCP_STREAM','TCP_STREAM_MULTI')
                 and r['metric']=='throughput']
        if not entries:continue
        modes=list(dict.fromkeys(r['mode'] for r in entries))
        fig,ax=plt.subplots(figsize=(10,4.5))
        for test,offset,color,label in [('TCP_STREAM',-.18,'#386cb0','One stream'),
                                        ('TCP_STREAM_MULTI',.18,'#147d92','Four streams')]:
            selected=[next(r for r in entries if r['mode']==m and r['test']==test) for m in modes]
            ax.bar([i+offset for i in range(len(modes))],[r['mean']/1000 for r in selected],width=.32,
                   yerr=[r['sd']/1000 for r in selected],capsize=4,label=label,color=color)
        ax.set_xticks(range(len(modes)),[LABELS[m].replace(' · ','\n') for m in modes],fontsize=8)
        ax.set_ylabel('Application throughput (Gb/s)');ax.set_ylim(bottom=0)
        title=group+' — '+('Mode-only baseline' if group=='A2' else 'BPF-profile validation')+'; DEFAULT idle, one queue'
        ax.set_title(title,fontsize=10);ax.legend();ax.grid(axis='y',alpha=.2);fig.tight_layout()
        stem=group+'-throughput'
        for ext in ('svg','png'):fig.savefig(directory/(stem+'.'+ext),dpi=160)
        plt.close(fig);images.append((title,stem+'.svg'))
    return images


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline',type=Path,required=True)
    parser.add_argument('--latency',type=Path,required=True)
    parser.add_argument('--throughput',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--partial',action='store_true',help='Allow pending throughput; omit its estimates entirely')
    args=parser.parse_args()
    args.output.mkdir(parents=True,exist_ok=False)
    rows,selection=baseline(args.baseline.resolve())
    for group,policy in [('B2','default'),('C','10us')]:
        data,sources=optimized(args.latency.resolve(),group,policy);rows+=data;selection+=sources
    state_path=args.throughput/'collection.json'
    throughput_status=json.loads(state_path.read_text())['status'] if state_path.exists() else 'pending'
    if throughput_status=='passed':
        data,sources=optimized(args.throughput.resolve(),'D','default');rows+=data;selection+=sources
    elif not args.partial:raise ValueError('Throughput collection is incomplete; use --partial to omit its estimates')
    save_json(args.output/'selection.json',selection)
    save_json(args.output/'metrics.json',rows)
    with (args.output/'metrics.csv').open('w') as stream:
        writer=csv.DictWriter(stream,fieldnames=list(rows[0]));writer.writeheader();writer.writerows(rows)
    images=charts(args.output,rows)
    lines=['# Paper cohorts: A mode-only, B BPF forwarding, C idle policy, D throughput','',
        'Status: '+('COMPLETE' if throughput_status=='passed' else 'INTERIM — throughput omitted ('+throughput_status+')')+'.','',
        'All values are mean ± sample SD across invocations, not confidence intervals. '
        'A2 uses the selected monitored baseline rerun; it is separate from the earlier A1 original head-to-head. '
        'B2 uses default-idle forwarding profiles including the legacy prerequisite control and Cilium reference. '
        'C applies 10µs physical-host idle QoS to each named profile. D uses default idle only. '
        'Do not merge these groups, reuse instrumented timings, or describe D throughput and C latency as one measured configuration.','',
        'Our custom hypervisor TC bridge shortcut (B1) has short correctness runs only; it is not the supported '
        'guest BPF forwarding change measured in B2. Queue/affinity results (E) are also excluded here.','',
        'Exact accepted inputs are in selection.json. Failed attempts, traces, profiles and mirrored report copies '
        'are excluded. No A/B/C observations are pooled across different collections. Power/energy was not measured.','',
        '| Group | Mode | Idle policy | Workload | Metric | n | Mean ± SD |',
        '|---|---|---|---|---|---:|---:|']
    for r in rows:
        scale=.001 if r['test']!='TCP_RR' and r['metric']=='throughput' else 1
        metric=(r['metric']+' (Gb/s)' if scale==.001 else
                'Transactions/s' if r['metric']=='throughput' else
                r['metric']+' (µs)' if r['metric'] in ('mean','p99') else
                'Whole-host CPU µs/op proxy' if r['test']=='TCP_RR' else 'Whole-host CPU-s/GB proxy')
        lines.append('| '+' | '.join([r['group'],r['mode'],r['policy'],r['test'],metric,str(r['n']),fmt(r,scale)])+' |')
    for title,path in images:lines+=['',f'## {title}','',f'![{title}]({path})']
    (args.output/'report.md').write_text('\n'.join(lines)+'\n')
    print(args.output/'report.md')


if __name__=='__main__':main()
