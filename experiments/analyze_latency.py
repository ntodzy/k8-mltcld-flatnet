#!/usr/bin/env python3
"""Analyze paired marginal tunnel latency and separate instrumented stage timings."""
import argparse
import csv
import json
import math
from pathlib import Path
import statistics

import aggregate
from common import save_json


def describe(values):
    if not values:
        return 'unavailable'
    return f'{statistics.mean(values):.3f}' + (f' ± {statistics.stdev(values):.3f}' if len(values) > 1 else '')


def histogram_upper(bins, quantile=.99):
    total = sum(bucket['count'] for bucket in bins)
    if not total:
        return None
    rank, cumulative = math.ceil(total * quantile), 0
    for bucket in sorted(bins, key=lambda bucket: bucket['min']):
        cumulative += bucket['count']
        if cumulative >= rank:
            return bucket['max'] / 1000


def trace_rows(directory, step):
    result = []
    for raw in (directory / 'traces').glob('*/trace.jsonl'):
        with raw.open() as source:
            records = [json.loads(line) for line in source]
        ready = next(record for record in records if record['kind'] == 'ready')
        final = next(record for record in records if record['kind'] == 'result')
        if final['exit_code'] != 0:
            raise ValueError(f'Failed trace: {raw}')
        maps = {}
        for record in final['records']:
            if record.get('type') in ('stats', 'map', 'hist'):
                maps.update(record['data'])
        functions = ready['functions']
        devices = ready['specification']['devices']
        for name, family in [('@function_ns', 'function'), ('@wakeup_ns', 'wakeup'),
                             ('@queue_ns', 'queue'), ('@offcpu_ns', 'offcpu'),
                             ('@halt_ns', 'halt'),
                             ('@receive_handoff_ns', 'receive_handoff'), ('@receive_deferred_ns', 'receive_deferred')]:
            for key, values in maps.get(name, {}).items():
                if family == 'function':
                    label = functions[int(key)]
                elif family == 'queue':
                    label = devices[int(key) - 1]
                elif family == 'wakeup':
                    label = {'1': 'vcpu', '2': 'vhost', '3': 'netperf', '4': 'netserver', '5': 'qemu-other'}.get(key, key)
                elif family == 'halt':
                    label = 'waited=' + key
                else:
                    label = key
                prefix = name.removesuffix('_ns')
                bins = maps.get(prefix + '_hist_ns', {}).get(key, [])
                result.append(dict(mode=step['mode'], condition=step.get('condition', 'reference'),
                                   target=raw.parent.name, kernel=ready['kernel'], family=family, stage=label,
                                   count=values['count'], mean_us=values['average'] / 1000,
                                   total_us=values['total'] / 1000,
                                   max_us=maps.get(prefix + '_max_ns', {}).get(key, 0) / 1000 or None,
                                   p99_bin_upper_us=histogram_upper(bins)))
    return result


def paired_differences(trials):
    paired = {}
    for row in trials:
        if row.get('pair') is not None and not row['trace']:
            paired.setdefault((row['mode'], row['pair']), {})[row['condition']] = row
    return [dict(mode=mode, pair=pair, extra_mean_us=values['tunnel']['mean_us'] - values['bypass']['mean_us'],
                 extra_sample_p99_us=values['tunnel']['p99_us'] - values['bypass']['p99_us'])
            for (mode, pair), values in paired.items() if set(values) == {'tunnel', 'bypass'}]


def load(directory, state):
    trials, traces = [], []
    for step in state['plan']:
        if step['kind'] != 'measurement' or step['status'] != 'passed':
            continue
        work = directory / step['attempts'][-1]['directory']
        item, observations, _ = aggregate.load_attempt(work / 'measurement')
        for phase in item['metadata']['phases']:
            sample = phase['sample']
            values = {row['metric']: row['value'] for row in observations
                      if row['domain'] == 'performance' and row['sample'] == sample and row['test'] == 'TCP_RR'}
            summary = json.loads((work / 'measurement' / phase['directory'] / 'summary.json').read_text())
            row = dict(mode=step['mode'], condition=step.get('condition', 'reference'), pair=step.get('pair'),
                       trace=step.get('trace', False), sample=sample, mean_us=values['mean'],
                       p99_us=values['p99'], ops=values['throughput'], source=work.name)
            for target in ('worker-1', 'worker-2'):
                means = [pod['data_rtt_ms']['mean'] * 1000 for pod in summary[target].get('pod_tcp', {}).values()
                         if pod.get('data_rtt_ms')]
                row[target + '_socket_srtt_us'] = statistics.mean(means) if means else None
            trials.append(row)
        if step.get('trace'):
            traces.extend(trace_rows(work, step))
    return trials, traces


def write_csv(path, rows):
    if not rows:
        path.write_text('')
        return
    columns = list(dict.fromkeys(key for row in rows for key in row))
    with path.open('w', newline='') as output:
        writer = csv.DictWriter(output, fieldnames=columns)
        writer.writeheader(); writer.writerows(rows)


def plots(output, trials, differences, traces):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    modes = ('host-vxlan-1450', 'vm-vxlan-1450')
    fig, axes = plt.subplots(1, 2, figsize=(10, 4.5), layout='constrained')
    for axis, mode in zip(axes, modes):
        rows = [row for row in trials if row['mode'] == mode and not row['trace']]
        for pair in sorted({row['pair'] for row in rows}):
            selected = {row['condition']: row for row in rows if row['pair'] == pair}
            if set(selected) == {'tunnel', 'bypass'}:
                axis.plot([0, 1], [selected['bypass']['mean_us'], selected['tunnel']['mean_us']], 'o-', alpha=.75, label=f'Pair {pair}')
        axis.set_xticks([0, 1], ['Bypass', 'Tunnel'])
        axis.set_ylabel('Mean TCP_RR transaction latency (µs)')
        axis.set_title(mode); axis.grid(axis='y', alpha=.2)
    fig.suptitle('Matched VMs/attachments/MTUs/queues: each line is one untraced pair')
    for extension in ('svg', 'png'):
        fig.savefig(output / f'paired-latency.{extension}', dpi=180)
    plt.close(fig)
    fig, axis = plt.subplots(figsize=(9, 3.5), layout='constrained')
    for index, mode in enumerate(modes):
        values = [row['extra_mean_us'] for row in differences if row['mode'] == mode]
        if values:
            axis.scatter(values, [index] * len(values), alpha=.7)
            axis.errorbar(statistics.mean(values), index, xerr=statistics.stdev(values) if len(values) > 1 else 0,
                          color='#d76500', fmt='D', capsize=4)
    axis.axvline(0, color='black', linewidth=.7); axis.set_yticks([0, 1], modes)
    axis.set_xlabel('Tunnel − bypass mean transaction latency (µs)'); axis.grid(axis='x', alpha=.2)
    axis.set_title('Paired marginal path cost: samples and mean ± sample SD')
    for extension in ('svg', 'png'):
        fig.savefig(output / f'paired-difference.{extension}', dpi=180)
    plt.close(fig)
    active = [row for row in traces if row['family'] == 'function' and row['condition'] != 'bypass']
    if active:
        fig, axes = plt.subplots(2, 2, figsize=(13, 8), layout='constrained')
        for axis, stage in zip(axes.flat, ('vxlan_build_skb', '__iptunnel_pull_header', 'vxlan_xmit', 'vxlan_rcv')):
            selected = [row for row in active if row['stage'].startswith(stage)]
            for index, row in enumerate(selected):
                axis.scatter(row['mean_us'], index, color='#3971ad')
                if row['p99_bin_upper_us']:
                    axis.scatter(row['p99_bin_upper_us'], index, marker='^', color='#d76500')
            axis.set_yticks(range(len(selected)), [row['mode'].replace('-1450', '') + '/' + row['target'].replace('hypervisor-', '') for row in selected])
            axis.set_xscale('log'); axis.set_xlabel('µs (log scale)'); axis.set_title(stage); axis.grid(axis='x', alpha=.2)
        fig.suptitle('Separate instrumented runs: mean (circle), histogram P99 upper bin (triangle)\nInclusive durations overlap; probe overhead is included; these are per invocation, not per transaction', fontsize=10)
        for extension in ('svg', 'png'):
            fig.savefig(output / f'kernel-functions.{extension}', dpi=180)
        plt.close(fig)


def report(directory, output, partial=False):
    state = json.loads((directory / 'collection.json').read_text())
    if state['status'] != 'passed' and not partial:
        raise ValueError('Incomplete study; use --partial for an explicitly interim report')
    trials, traces = load(directory, state)
    differences = paired_differences(trials)
    output.mkdir(parents=True, exist_ok=False)
    write_csv(output / 'trials.csv', trials); write_csv(output / 'paired-differences.csv', differences)
    write_csv(output / 'kernel-timings.csv', traces)
    save_json(output / 'evidence.json', dict(trials=trials, paired_differences=differences, kernel_timings=traces,
                                           environments=state.get('environments'), source=str(directory)))
    plots(output, trials, differences, traces)
    text = ['# Isolating custom VXLAN latency', '',
            '**Completed study.**' if state['status'] == 'passed' else '**INTERIM: study incomplete.**', '',
            'The custom tunnel and bypass conditions reuse the same cluster, VMs, guest attachments, MTUs, queues, '
            'worker CPU masks and software configuration within each mode. VM bypass changes only remote PodCIDR routes; '
            'the VXLAN device, static neighbors and FDB remain in place. Host bypass switches the same bridge between its VXLAN port '
            'and physical uplink while retaining the same guest TAP interfaces. Five paired rounds use balanced AB/BA order. '
            'Each untraced observation is a separate 30-second Cilium CLI TCP_RR test with guest/hypervisor monitoring. '
            'Boot/kernel/queue/affinity checks guard each block. Cilium is a separate reference configuration, not a route-only toggle.', '',
            '## Paired end-to-end measurements', '']
    rows = []
    for mode in ('host-vxlan-1450', 'vm-vxlan-1450'):
        selected = [row for row in differences if row['mode'] == mode]
        delta = [row['extra_mean_us'] for row in selected]
        values = {condition: [row['mean_us'] for row in trials if row['mode'] == mode and row['condition'] == condition and not row['trace']]
                  for condition in ('tunnel', 'bypass')}
        rows.append([mode, len(delta), describe(values['bypass']), describe(values['tunnel']), describe(delta),
                     f'{min(delta):.3f}..{max(delta):.3f}' if delta else 'unavailable'])
    text += [aggregate.table(['Mode', 'Complete pairs', 'Bypass mean µs ± SD', 'Tunnel mean µs ± SD', 'Paired extra µs ± SD', 'Pair difference range µs'], rows), '',
             '![Paired latency](paired-latency.png)', '', '![Paired differences](paired-difference.png)', '',
             'These differences estimate the marginal complete tunnel path with attachment and VM state held fixed. '
             'They include outer lookup/headers, receive handling, batching, interrupts and scheduling effects induced by the path; '
             'they are not an isolated header-writing CPU cost. Sample SD describes variation across five pairs; '
             'these are not independent host reservations or randomized cluster deployments. P99 differences in the CSV '
             'are differences between per-run percentiles, not a percentile of paired individual packets.', '',
             '## Tracer perturbation and Cilium reference', '']
    rows = []
    for mode, condition in sorted({(row['mode'], row['condition']) for row in trials}):
        untraced = [row['mean_us'] for row in trials if row['mode'] == mode and row['condition'] == condition and not row['trace']]
        instrumented = [row['mean_us'] for row in trials if row['mode'] == mode and row['condition'] == condition and row['trace']]
        rows.append([mode, condition, describe(untraced), describe(instrumented),
                     f'{100 * (statistics.mean(instrumented) / statistics.mean(untraced) - 1):+.1f}%' if untraced and instrumented else 'unavailable'])
    text += [aggregate.table(['Mode', 'Condition', 'Untraced mean µs ± SD', 'Instrumented mean µs', 'Descriptive change'], rows), '',
             'Instrumented runs are separate single observations and are excluded from the primary paired estimates. '
             'Their change versus the untraced average is a perturbation check, with order/ordinary run variability still possible. '
             'Function durations are inclusive wall time with kprobe/kretprobe bookkeeping overhead. Nested functions overlap, '
             'and small helper durations may be dominated by the instrumentation floor. Do not sum these means into a transaction-latency budget.', '',
             '## Kernel function timing', '']
    selected = [row for row in traces if row['family'] == 'function']
    text += [aggregate.table(['Mode/condition', 'Machine/kernel', 'Function', 'Calls', 'Mean µs', 'P99 upper bin µs'],
                            [[row['mode'] + '/' + row['condition'], row['target'] + '/' + row['kernel'], row['stage'], row['count'],
                              f'{row["mean_us"]:.3f}', f'{row["p99_bin_upper_us"]:.3f}' if row['p99_bin_upper_us'] else 'unavailable'] for row in selected]), '']
    if selected:
        text += ['![Kernel function timings](kernel-functions.png)', '']
    text += ['The narrow vxlan_build_skb helper includes header preparation, headroom and offload setup; '
             '__iptunnel_pull_header is scoped to an active VXLAN receive callback. Full vxlan_xmit and vxlan_rcv include '
             'additional nested work and are not pure header construction/removal. ip_forward and br_forward observations '
             'select IPv4 TCP between Pod addresses in 10.244.0.0/16; tunnel and backend observations are device/task scoped '
             'and can include other traffic. No guest/host or cross-host clocks are subtracted.', '',
             '## Scheduling, receive handoff and device queue observations', '']
    selected = [row for row in traces if row['family'] in ('wakeup', 'receive_handoff', 'receive_deferred', 'queue', 'halt')]
    text += [aggregate.table(['Mode/condition', 'Machine', 'Metric/stage', 'Count', 'Mean µs', 'P99 upper bin µs'],
                            [[row['mode'] + '/' + row['condition'], row['target'], row['family'] + '/' + row['stage'], row['count'],
                              f'{row["mean_us"]:.3f}', f'{row["p99_bin_upper_us"]:.3f}' if row['p99_bin_upper_us'] else 'unavailable'] for row in selected]), '',
             'Wakeup-to-run measures scheduler delay for the selected worker QEMU/vhost tasks and locally observed '
             'netperf/netserver tasks. It does not include every interrupt-delivery or hypervisor delay. '
             'Receive handoff matches the same skb pointer from VXLAN receive entry to its first subsequent receive-stage probe; '
             'receive_deferred starts at the receive callback return only where that return precedes the match. '
             'Counts show coverage; dropped, cloned or coalesced buffers may not match. Queue-to-driver timings are device-wide '
             'and include tracing/bookkeeping overhead. Off-CPU statistics remain in the CSV: blocked time often includes '
             'legitimate waiting for network data and must not be counted entirely as scheduler overhead. '
             'KVM halt duration and its waited flag describe halt polling/blocking behavior for the selected worker vCPUs; '
             'the duration includes waiting for an event, so it is not itself additional encapsulation overhead.', '',
             '## Reproducibility and boundaries', '',
             f'Source: `{directory}`. Starting Git snapshot: `{state["git_snapshot"]}`. '
             'Original reports, traces, generated BPF programs, path audits, environment guards, retry history and restoration evidence '
             'remain in their attempt directories. Unsupported/inlined functions limit coverage. '
             'Cilium and custom paths continue to differ in forwarding policy, metadata, receive handling and VXLAN UDP port. '
             'The paired controls remove attachment/reset differences within each custom mode; they do not make all implementations identical.', '',
             '[Trial observations](trials.csv) · [Paired differences](paired-differences.csv) · '
             '[Kernel timing observations](kernel-timings.csv) · [Machine-readable evidence](evidence.json)', '']
    (output / 'report.md').write_text('\n'.join(text))
    print('Analysis:', output / 'report.md')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('study', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--partial', action='store_true')
    args = parser.parse_args(); report(args.study.resolve(), args.output.resolve(), args.partial)


if __name__ == '__main__':
    main()
