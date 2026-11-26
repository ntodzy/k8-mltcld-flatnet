#!/usr/bin/env python3
"""Collate the six-cell VXLAN study without treating repetitions as rebuilds."""
import argparse
from collections import defaultdict
import csv
import json
from pathlib import Path
import statistics

import aggregate
from common import save_json
from head_to_head import ARCHITECTURES, WORKLOADS, DEFAULT_QOS

METRICS = {'mean_us': ('rr', 'mean', 1), 'p99_us': ('rr', 'p99', 1),
           'rr_ops_s': ('rr', 'throughput', 1), 'single_gbps': ('single', 'throughput', .001),
           'four_gbps': ('multi', 'throughput', .001)}


def load(directory, partial=False):
    state = json.loads((directory/'collection.json').read_text())
    if state['study_type'] != 'vxlan-head-to-head':
        raise ValueError('Expected a VXLAN head-to-head collection')
    if state['status'] != 'passed' and not partial:
        raise ValueError('Collection incomplete; use --partial for explicitly interim results')
    rows = []
    for step in state['plan']:
        if step['status'] != 'passed':
            continue
        attempt = step['attempts'][-1]
        if attempt['status'] != 'passed':
            raise ValueError('Selected attempt did not pass')
        work = directory/attempt['directory']
        proof = json.loads((work/'path-validation/validation.json').read_text())
        if proof.get('status') != 'passed' or proof.get('mode') != step['mode']:
            raise ValueError('Missing or mismatched path validation')
        for sample in range(1, state['samples']+1):
            for workload in WORKLOADS:
                trial = work/f'sample-{sample:02d}-{workload}'
                info, observations, warnings = aggregate.load_attempt(trial/'measurement')
                meta = info['metadata']
                expected = dict(mode=step['mode'], architecture=step['architecture'],
                                optimization_profile=step['profile'], rebuild_block=step['block'],
                                repetition=sample, quality=state['quality'])
                if any(meta.get(k) != v for k, v in expected.items()) or meta.get('instrumented'):
                    raise ValueError('Observation identity/quality mismatch: '+str(trial))
                if len(meta['phases']) != 1:
                    raise ValueError('Expected one workload per invocation')
                values = {}
                for item in observations:
                    if item['domain'] != 'performance':
                        continue
                    if item['metric'] in values:
                        raise ValueError('Duplicate metric in one workload')
                    values[item['metric']] = item['value']
                row = dict(architecture=step['architecture'], profile=step['profile'], block=step['block'],
                           repetition=sample, workload=workload, source=str(trial), warnings=sorted(warnings),
                           source_revision=attempt.get('source_revision', 0))
                for metric, (wanted, key, scale) in METRICS.items():
                    if workload == wanted:
                        if key not in values:
                            raise ValueError('Missing metric '+key+': '+str(trial))
                        row[metric] = values[key]*scale
                policy = list((trial/'cpu-latency').glob('*/request.jsonl'))
                if len(policy) != 2:
                    raise ValueError('Missing two-host idle policy proof')
                for path in policy:
                    records = [json.loads(line) for line in path.read_text().splitlines()]
                    ready = next(r for r in records if r['kind'] == 'ready')
                    restored = next(r for r in records if r['kind'] == 'restored')
                    wanted = 10 if step['profile'] == 'optimized' else DEFAULT_QOS
                    if ready['before_us'] != DEFAULT_QOS or ready['effective_us'] != wanted or restored['effective_us'] != restored['expected_us']:
                        raise ValueError('Idle policy/restoration mismatch')
                summary = json.loads((trial/'measurement'/meta['phases'][0]['directory']/'summary.json').read_text())
                for label, prefix in (('hypervisor-', 'host'), ('worker-', 'guest')):
                    selected = [v for k,v in summary.items() if k.startswith(label)]
                    if len(selected) == 2 and all(v.get('observed_active_seconds', 0) > 0 for v in selected):
                        row[prefix+'_busy_cores'] = sum(v['busy_core_seconds']/v['observed_active_seconds'] for v in selected)
                rows.append(row)
    return state, rows


def block_means(rows):
    groups = defaultdict(list)
    for row in rows:
        groups[(row['architecture'], row['profile'], row['block'], row['workload'])].append(row)
    output = []
    for key, items in groups.items():
        result = dict(zip(('architecture','profile','block','workload'), key), repetitions=len(items))
        for metric in (*METRICS, 'host_busy_cores', 'guest_busy_cores'):
            values = [r[metric] for r in items if metric in r]
            if values:
                result[metric] = statistics.mean(values)
                result[metric+'_within_boot_sd'] = statistics.stdev(values) if len(values)>1 else None
        output.append(result)
    return output


def summarize(blocks):
    groups = defaultdict(list)
    for row in blocks:
        groups[(row['architecture'], row['profile'], row['workload'])].append(row)
    output = []
    for key, items in groups.items():
        result = dict(zip(('architecture','profile','workload'), key), rebuilds=len(items),
                      invocations=sum(r['repetitions'] for r in items))
        for metric in (*METRICS, 'host_busy_cores', 'guest_busy_cores'):
            values = [r[metric] for r in items if metric in r]
            if values:
                result[metric] = statistics.mean(values)
                result[metric+'_between_boot_sd'] = statistics.stdev(values) if len(values)>1 else None
        output.append(result)
    return output


def contrasts(blocks):
    index = {(r['architecture'],r['profile'],r['block'],r['workload']):r for r in blocks}
    comparisons = [(a+' optimized vs baseline',(a,'baseline'),(a,'optimized')) for a in ARCHITECTURES]
    for profile in ('baseline','optimized'):
        comparisons.extend([(profile+' attachment: bridge vs macvtap',('cilium-macvtap',profile),('cilium-bridge',profile)),
                            (profile+' placement: host vs guest bridge',('cilium-bridge',profile),('host-vxlan',profile)),
                            (profile+' provider vs original Cilium',('cilium-macvtap',profile),('host-vxlan',profile))])
    output = []
    for label, reference, treatment in comparisons:
        for (architecture, profile, block, workload), first in index.items():
            if (architecture,profile) != reference:
                continue
            second = index.get((*treatment,block,workload))
            if second is None:
                continue
            for metric in METRICS:
                if metric in first and metric in second:
                    output.append(dict(comparison=label, block=block, workload=workload, metric=metric,
                                       reference=first[metric], treatment=second[metric],
                                       difference=second[metric]-first[metric],
                                       change_percent=100*(second[metric]/first[metric]-1)))
    return output


def write_csv(path, rows):
    fields = list(dict.fromkeys(k for row in rows for k in row))
    with path.open('w', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def report(directory, output, partial=False, plots=True):
    state, rows = load(directory, partial)
    blocks = block_means(rows)
    stats = summarize(blocks)
    differences = contrasts(blocks)
    output.mkdir(parents=True, exist_ok=False)
    for name, values in [('observations',rows),('block-means',blocks),('summary',stats),('contrasts',differences)]:
        save_json(output/(name+'.json'), values)
        write_csv(output/(name+'.csv'), [{k:v for k,v in r.items() if k != 'warnings'} for r in values])
    label = 'DEVELOPMENT SMOKE — not performance evidence' if state['smoke'] else 'Primary collection'
    lines = ['# VXLAN architecture head-to-head', '', label+(' — INTERIM' if partial else ''), '',
             'Pod MTU 1450; physical MTU 1500; worker-1 → worker-2 across hypervisors. '
             'Baseline retains normal Cilium BPF/offloads, one queue, original vhost masks, default idle policy and kube-proxy. '
             'Optimized uses two queues, pinned vhost tasks, a 10 µs physical-host idle QoS request, '
             'and supported BPF guest forwarding/service/masquerade settings. The custom host TC shortcut is excluded.', '',
             'Values below average rebuild means equally. SD describes variation between rebuild means, '
             'not individual transactions; one rebuild has no between-rebuild SD. P99 is the average of invocation P99s. '
             'The two Cilium cases encapsulate inside the VM; the provider case uses native Cilium guest routing and hypervisor VXLAN. '
             'Physical captures and their validation benchmarks are excluded.', '',
             '| Architecture | Mean RR baseline µs | Mean RR optimized µs | P99 baseline µs | P99 optimized µs | Single baseline Gb/s | Single optimized Gb/s | Four baseline Gb/s | Four optimized Gb/s |',
             '|---|---:|---:|---:|---:|---:|---:|---:|---:|']
    if state.get('source_revisions'):
        lines.insert(4, 'Development source revisions: '+str(len(state['source_revisions']))+
                     '. Earlier passed smoke cases are retained across explicit bug fixes; source revision identity remains in observations. '
                     'These mixed development revisions must never be interpreted as an efficacy comparison.\n')
    for architecture in ARCHITECTURES:
        cells = []
        for metric in ('mean_us','p99_us','single_gbps','four_gbps'):
            for profile in ('baseline','optimized'):
                entry = next((r for r in stats if r['architecture']==architecture and r['profile']==profile and metric in r), None)
                if entry is None:
                    cells.append('—')
                else:
                    sd = entry[metric+'_between_boot_sd']
                    cells.append(f'{entry[metric]:.3f}'+(f' ± {sd:.3f}' if sd is not None else '')+f' (B={entry["rebuilds"]}, n={entry["invocations"]})')
        lines.append('| '+architecture+' | '+' | '.join(cells)+' |')
    lines.extend(['', '## Contrasts within each rebuild block', '',
                  '| Comparison | Block | Metric | Reference | Treatment | Change % |', '|---|---:|---|---:|---:|---:|'])
    for r in differences:
        lines.append(f'| {r["comparison"]} | {r["block"]} | {r["metric"]} | {r["reference"]:.3f} | {r["treatment"]:.3f} | {r["change_percent"]:+.2f} |')
    if plots and rows:
        import matplotlib
        matplotlib.use('Agg')
        import matplotlib.pyplot as plt
        fig, axes = plt.subplots(2, 2, figsize=(13,9))
        for ax, metric in zip(axes.flat, ('mean_us','p99_us','single_gbps','four_gbps')):
            for x, architecture in enumerate(ARCHITECTURES):
                for profile, offset, color in [('baseline',-.14,'#666666'),('optimized',.14,'#147d92')]:
                    entry = next((r for r in stats if r['architecture']==architecture and r['profile']==profile and metric in r), None)
                    if entry is None:
                        continue
                    values = [r[metric] for r in blocks if r['architecture']==architecture and r['profile']==profile and metric in r]
                    ax.errorbar(x+offset, entry[metric], yerr=entry[metric+'_between_boot_sd'], fmt='o', color=color, capsize=4,
                                label=profile if x==0 else None)
                    ax.scatter([x+offset]*len(values),values,s=18,alpha=.6,color=color)
            ax.set_xticks(range(3), list(ARCHITECTURES), fontsize=9)
            ax.set_title(metric.replace('_',' '))
            ax.set_ylim(bottom=0)
            ax.grid(axis='y',alpha=.2)
        axes[0,0].legend()
        fig.suptitle(label+'\nPoints: rebuild means; error bars: between-rebuild SD')
        fig.tight_layout()
        for extension in ('svg','png'):
            fig.savefig(output/('head-to-head.'+extension),dpi=160)
        plt.close(fig)
        lines.extend(['', '![Head-to-head](head-to-head.svg)'])
    lines.extend(['', 'The optimized profile is a package comparison, not an isolated effect estimate for each setting. '
                  'Matched attachment controls reduce confounding; guest transport MTUs and forwarding paths still differ by design. '
                  'These results concern this fixed cluster and workload, not provider scalability or tenant isolation. '
                  'CPU busy-core metrics sum the two machines in each role during observed active windows; '
                  'they include background work and do not measure power. Raw sources, warnings and rebuild identities are preserved in observations.json.', '',
                  'Source: `'+str(directory)+'`'])
    (output/'report.md').write_text('\n'.join(lines)+'\n')
    return output/'report.md'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--partial', action='store_true')
    parser.add_argument('--no-plots', action='store_true')
    args = parser.parse_args()
    print(report(args.directory.resolve(), args.output.resolve(), args.partial, not args.no_plots))


if __name__ == '__main__':
    main()
