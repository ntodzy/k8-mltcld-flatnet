#!/usr/bin/env python3
"""Generate an evidence-based report and figures from the extended study."""
import argparse
from collections import defaultdict
import json
from pathlib import Path
import re
import statistics

import aggregate
from common import MODES, save_json


def describe(values, scale=1):
    if not values:
        return 'unavailable'
    mean = statistics.mean(values) * scale
    sd = statistics.stdev(values) * scale if len(values) > 1 else None
    return f'{mean:.3f}' + (f' ± {sd:.3f}' if sd is not None else '')


def load_measurements(directory, state):
    measurements = {}
    for step in state['plan']:
        if step['kind'] != 'measurement' or step['status'] != 'passed':
            continue
        work = directory / step['attempts'][-1]['directory'] / 'measurement'
        item, rows, warnings = aggregate.load_attempt(work)
        observations = defaultdict(list)
        for row in rows:
            if row['domain'] == 'performance':
                observations[(row['test'], row['metric'])].append(row['value'])
        phase_data = []
        for phase in item['metadata']['phases']:
            phase_path = work / phase['directory']
            phase_data.append((phase, json.loads((phase_path / 'summary.json').read_text()), phase_path))
        measurements[step['name']] = dict(step=step, work=work, item=item, rows=rows,
                                          warnings=warnings, metrics=observations, phases=phase_data)
    return measurements


def diagnose(measurement, test='multi'):
    result = defaultdict(list)
    details = []
    for phase, targets, path in measurement['phases']:
        if phase['test_type'] != test:
            continue
        cores_busy = 0
        for target, data in targets.items():
            queue_sets = [[value for key, value in counters.items()
                           if re.fullmatch(r'rx(?:\d+|_queue_\d+)_packets', key) and value >= 0]
                          for counters in data.get('nic_counter_delta', {}).values()]
            queues = max(queue_sets, key=sum, default=[])
            if sum(queues) > 0:
                result[target + '/rx_active_queues'].append(sum(value > max(1000, sum(queues) * .01) for value in queues))
                result[target + '/rx_largest_queue_percent'].append(100 * max(queues) / sum(queues))
            if target.startswith('hypervisor-'):
                count = data.get('logical_cpus', len(data.get('per_core', {})) - 1)
                busy = data.get('per_core', {}).get('cpu', {}).get('busy_percent')
                if busy is not None:
                    cores_busy += busy / 100 * count
                threads = [task for task in data.get('threads', []) if task['comm'].startswith('vhost-')]
                if threads:
                    result[target + '/busiest_vhost_cpu'].append(max(task['cpu_percent_one_core'] for task in threads))
                for interface, dirs in data.get('interface_counter_delta', {}).items():
                    for direction, counters in dirs.items():
                        for metric in ('dropped', 'errors'):
                            if counters.get(metric, 0) > 0:
                                details.append(f'{phase["directory"]}: {target}/{interface} {direction}/{metric}={counters[metric]} (whole invocation)')
            if not target.startswith('hypervisor-'):
                busy = data.get('per_core', {}).get('cpu', {}).get('busy_percent')
                if busy is not None:
                    result[target + '/busy'].append(busy)
                for pod in data.get('pod_tcp', {}).values():
                    if pod.get('host_network'):
                        continue
                    counters = pod.get('snmp_delta', {})
                    retrans, sent = counters.get('Tcp:RetransSegs'), counters.get('Tcp:OutSegs')
                    if retrans is not None:
                        result[target + '/retrans'].append(retrans)
                    if sent and retrans is not None:
                        result[target + '/retrans_per_1000_outsegs'].append(1000 * retrans / sent)
                    for metric in ('TcpExt:TCPDSACKRecv', 'TcpExt:TCPSACKReorder', 'TcpExt:TCPTSReorder'):
                        value = pod.get('netstat_delta', {}).get(metric)
                        if value is not None:
                            result[target + '/' + metric].append(value)
                    # Recompute data-socket RTT if early summaries predate its parser.
                    if pod.get('data_rtt_ms'):
                        result[target + '/data_rtt_ms'].append(pod['data_rtt_ms']['mean'])
                    else:
                        raw = path / target / 'samples.jsonl'
                        values = []
                        with raw.open() as stream:
                            for line in stream:
                                record = json.loads(line)
                                if record.get('kind') != 'sample' or not data['client_window_start_epoch'] <= record['received_epoch'] <= data['client_window_end_epoch']:
                                    continue
                                for observation in record.get('pods', []):
                                    if observation['uid'] == pod['uid'] and observation.get('tcp', {}).get('exit_code') == 0:
                                        values.extend(socket['rtt_ms'] for socket in aggregate_data_sockets(observation['tcp']['output']) if 'rtt_ms' in socket)
                        if values:
                            result[target + '/data_rtt_ms'].append(statistics.mean(values))
        result['host_busy_logical_cpus'].append(cores_busy)
    metric = 'TCP_STREAM_MULTI' if test == 'multi' else 'TCP_STREAM' if test == 'single' else 'TCP_RR'
    rates = measurement['metrics'].get((metric, 'throughput'), [])
    if test in ('single', 'multi') and len(rates) == len(result['host_busy_logical_cpus']):
        result['estimated_host_cpu_seconds_per_GB'] = [cores / (rate / 8000)
                     for cores, rate in zip(result['host_busy_logical_cpus'], rates)]
    return dict(result), details


def aggregate_data_sockets(output):
    from diagnostics import data_sockets
    return data_sockets(output)


def profile_self_rows(output):
    rows = []
    idle = ('cpuidle_', 'pv_native_safe_halt', 'native_safe_halt', 'poll_idle', 'default_idle_call', 'arch_cpu_idle', 'do_idle')
    for line in output.splitlines():
        match = re.match(r'\s*([\d.]+)%\s+(?:([\d.]+)%\s+)?(.+)', line)
        if match is None:
            continue
        overhead = float(match[2] if match[2] is not None else match[1])
        rest = match[3]
        symbol_match = re.search(r'\[(?:k|\.)\]\s+(.+)', rest)
        if not symbol_match or overhead <= 0:
            continue
        symbol = symbol_match[1].strip()
        if symbol.startswith(idle):
            continue
        rows.append((overhead, rest.split()[0], symbol))
    return sorted(rows, reverse=True)


def export_group(output, name, measurements):
    selected = list(measurements)
    if not selected:
        return None
    target = output / name
    target.mkdir()
    rows = [row for measurement in selected for row in measurement['rows']]
    stats = aggregate.aggregate(rows)
    warnings = set().union(*(m['warnings'] for m in selected))
    figures = aggregate.make_plots(target, rows, stats)
    aggregate.write_csv(target / 'observations.csv', aggregate.FIELDS, rows)
    aggregate.write_csv(target / 'aggregate.csv', aggregate.GROUP_FIELDS + aggregate.STATS, stats)
    aggregate.write_report(target, [m['item'] for m in selected], stats, warnings, figures)
    save_json(target / 'summary.json', {'inputs': [m['item'] for m in selected],
                                      'statistics': stats, 'warnings': sorted(warnings), 'figures': figures})
    return f'{name}/summary.md'


def compact_figures(output, main, measurements):
    """Export paper-sized comparisons with short labels and visible samples."""
    import matplotlib.pyplot as plt

    def panel(axis, names, values, title, unit):
        for index, samples in enumerate(values):
            if not samples:
                continue
            mean = statistics.mean(samples)
            sd = statistics.stdev(samples) if len(samples) > 1 else 0
            axis.scatter(samples, [index] * len(samples), color='#3971ad', alpha=.65, s=20)
            axis.errorbar(mean, index, xerr=sd, fmt='D', color='#d76500', capsize=3, markersize=5)
        axis.set_yticks(range(len(names)), names)
        axis.invert_yaxis()
        axis.set_xlim(left=0)
        axis.set_title(title)
        axis.set_xlabel(unit)
        axis.grid(axis='x', alpha=.2)
        axis.set_axisbelow(True)

    labels = [mode for mode in (*MODES, 'bridge-1450') if mode in main]
    if labels:
        fig, axes = plt.subplots(2, 2, figsize=(13, max(7, len(labels) * .85)), layout='constrained')
        for axis, test, metric, scale, title, unit in (
            (axes[0, 0], 'TCP_STREAM', 'throughput', .001, 'Single-stream throughput', 'Gb/s'),
            (axes[0, 1], 'TCP_STREAM_MULTI', 'throughput', .001, 'Four-stream throughput', 'Gb/s'),
            (axes[1, 0], 'TCP_RR', 'mean', 1, 'Request/response mean latency', 'µs'),
        ):
            panel(axis, labels, [[value * scale for value in main[mode]['metrics'].get((test, metric), [])]
                                for mode in labels], title, unit)
        panel(axes[1, 1], labels, [diagnose(main[mode])[0].get('estimated_host_cpu_seconds_per_GB', [])
                                  for mode in labels], 'Physical-host processing cost proxy', 'CPU-seconds / application GB')
        fig.suptitle('Individual samples; orange diamonds = mean ± sample SD\nVM-local uses host-network traffic on one host; other modes use cross-host Pod traffic', fontsize=11)
        for suffix in ('svg', 'png'):
            fig.savefig(output / f'mode-overview.{suffix}', dpi=180)
        plt.close(fig)

    names = [name for name in ('host-1450-baseline', 'host-1450-two-queues', 'host-1450-restored') if name in measurements]
    if names:
        labels = [{'host-1450-baseline': 'One queue (before)', 'host-1450-two-queues': 'Two queues',
                   'host-1450-restored': 'One queue (restored)'}[name] for name in names]
        fig, axes = plt.subplots(1, 2, figsize=(12, 3.5), layout='constrained')
        panel(axes[0], labels, [[value * .001 for value in measurements[name]['metrics'].get(('TCP_STREAM_MULTI', 'throughput'), [])]
                                for name in names], 'Four-stream throughput', 'Gb/s')
        panel(axes[1], labels, [diagnose(measurements[name])[0].get('estimated_host_cpu_seconds_per_GB', [])
                                for name in names], 'Physical-host processing cost proxy', 'CPU-seconds / application GB')
        fig.suptitle('Queue intervention and reversal: individual samples and mean ± sample SD', fontsize=11)
        for suffix in ('svg', 'png'):
            fig.savefig(output / f'queue-overview.{suffix}', dpi=180)
        plt.close(fig)


def report(directory, output, partial=False):
    state = json.loads((directory / 'collection.json').read_text())
    if state['status'] != 'passed' and not partial:
        raise ValueError('Study is incomplete; use --partial for a clearly labeled interim report')
    output.mkdir(parents=True, exist_ok=False)
    measurements = load_measurements(directory, state)
    if not measurements:
        raise ValueError('No successful measurements to analyze')
    main = {m['step']['mode']: m for name, m in measurements.items() if name.endswith('-baseline')}
    groups = {
        'all-modes': list(main.values()),
        'matched-vxlan': [main[mode] for mode in ('vxlan', 'vm-vxlan-1450', 'host-vxlan-1450') if mode in main],
        'queue-control': [measurements[name] for name in ('host-1450-baseline', 'host-1450-two-queues', 'host-1450-restored') if name in measurements],
        'cilium-queue-control': [measurements[name] for name in ('vxlan-one-queue-control', 'vxlan-two-queue-control', 'vxlan-restored-queue-control') if name in measurements],
        'vm-queue-control': [measurements[name] for name in ('vm-vxlan-1450-one-queue-control', 'vm-vxlan-1450-two-queue-control', 'vm-vxlan-1450-restored-queue-control') if name in measurements],
        'affinity-control': [measurements[name] for name in ('host-1450-two-queues-repeat', 'host-1450-pinned', 'host-1450-unpinned') if name in measurements],
        'attachment-control': [main[mode] for mode in ('direct-1450', 'bridge-1450', 'host-vxlan-1450') if mode in main],
        'mtu-control': [main[mode] for mode in ('native', 'direct-1450', 'vm-vxlan', 'vm-vxlan-1450', 'host-vxlan', 'host-vxlan-1450') if mode in main],
    }
    links = {name: export_group(output, name, group) for name, group in groups.items() if group}
    # The first host baseline is named differently from later mode baselines.
    if 'host-vxlan-1450' not in main and 'host-1450-baseline' in measurements:
        main['host-vxlan-1450'] = measurements['host-1450-baseline']
    evidence = {}
    for name, measurement in measurements.items():
        diag, details = diagnose(measurement)
        evidence[name] = {'diagnostics': diag, 'nonzero_interface_drop_error_counters': details,
                          'metrics': {test + '/' + metric: values for (test, metric), values in measurement['metrics'].items()}}
    save_json(output / 'evidence.json', evidence)
    compact_figures(output, main, measurements)
    text = ['# Extended networking study: performance and bottleneck analysis', '',
            ('**INTERIM: the study has not completed.**' if state['status'] != 'passed' else '**Completed study.**'), '',
            f'Source: `{directory}`. Initial Git snapshot: `{state.get("git_snapshot")}`. '
            'The main comparisons use five 30-second samples, four streams, fixed endpoint placement, and guest plus hypervisor monitoring. '
            'Cross-host measurements use Pod traffic; VM-local uses host-network traffic on two different VMs. '
            'Values below are means ± sample SD, not confidence intervals. Profiles and reversal checks are separate controls.', '',
            '## Mode results', '']
    rows = []
    for mode in (*MODES, 'bridge-1450'):
        if mode not in main:
            rows.append([mode, 'not completed', '—', '—', '—'])
            continue
        m = main[mode]['metrics']
        rows.append([mode, describe(m.get(('TCP_STREAM', 'throughput'), []), .001),
                     describe(m.get(('TCP_STREAM_MULTI', 'throughput'), []), .001),
                     describe(m.get(('TCP_RR', 'mean'), [])), describe(m.get(('TCP_RR', 'p99'), []))])
    text.append(aggregate.table(['Mode', 'Single Gb/s', 'Four-stream Gb/s', 'Mean transaction µs', 'Mean sample P99 µs'], rows))
    text += ['', '![Mode performance and physical-host cost](mode-overview.png)', '',
             '[Export figure as SVG](mode-overview.svg)', '']
    text += ['', 'P99 values are averages of per-sample percentiles, not pooled percentiles. '
             'VM-local bypasses the Pod path and physical link and should not be ranked as an equivalent overlay workload.', '',
             '## Why the host path scales differently', '']
    if 'vm-local' in main:
        validation = json.loads((main['vm-local']['work'] / 'local-path-validation/validation.json').read_text())
        link = validation.get('link_evidence', {})
        text[-2:-2] = [
            'The separate VM-local validation passed with an egress-only capture and verified same-parent macvtap bridge attachments. '
            f'Physical TX increased by {link.get("physical_tx_bytes", "unavailable")} bytes over the whole validation invocation, '
            f'compared with approximately {link.get("estimated_application_bytes", "unavailable")} application bytes. '
            'Background traffic is included in the NIC counter. Bidirectional tcpdump can see local macvtap frames at the lower device taps '
            'without physical transmission '
            '([Linux macvlan forwarding source](https://github.com/torvalds/linux/blob/v6.8/drivers/net/macvlan.c#L497)).', '']
    queue_distribution = []
    for mode, measurement in main.items():
        if mode == 'vm-local':
            continue
        d = evidence[measurement['step']['name']]['diagnostics']
        queue_distribution.append([mode, describe(d.get('hypervisor-node2/rx_active_queues', [])),
            describe(d.get('hypervisor-node2/rx_largest_queue_percent', [])),
            describe(d.get('worker-2/rx_active_queues', []))])
    text += [aggregate.table(['Mode', 'Receiver-host significant RX queues', 'Largest physical RX queue share %',
                              'Receiver-VM significant RX queues'], queue_distribution), '',
             'Queue statistics use whole-invocation vendor packet-counter deltas on the interface with the most queue RX packets. '
             'A significant queue exceeds both 1000 packets and 1% of the summed queue packets. '
             'These describe packet distribution, not an exact number of flows; background traffic and unequal flow rates remain possible. '
             'Physical distribution across several queues can coexist with a single guest queue or a busy vhost thread.', '']
    queue_names = ('host-1450-baseline', 'host-1450-two-queues', 'host-1450-restored')
    table = []
    for name in queue_names:
        if name not in measurements:
            continue
        m = measurements[name]
        d = evidence[name]['diagnostics']
        table.append([name, describe(m['metrics'].get(('TCP_STREAM_MULTI', 'throughput'), []), .001),
                      describe(d.get('worker-1/retrans_per_1000_outsegs', [])),
                      describe(d.get('worker-1/data_rtt_ms', [])),
                      describe(d.get('hypervisor-node1/busiest_vhost_cpu', [])),
                      describe(d.get('estimated_host_cpu_seconds_per_GB', []))])
    text.append(aggregate.table(['Control', 'Multi Gb/s', 'Sender retrans / 1000 OutSegs', 'Sender data RTT ms',
                                 'Busiest sender-host vhost % of one CPU', 'Estimated host CPU-s / GB'], table))
    if table:
        text += ['', '![Queue intervention and reversal](queue-overview.png)', '',
                 '[Export figure as SVG](queue-overview.svg)', '']
    if all(name in measurements for name in queue_names[:2]):
        a = statistics.mean(measurements[queue_names[0]]['metrics'][('TCP_STREAM_MULTI', 'throughput')])
        b = statistics.mean(measurements[queue_names[1]]['metrics'][('TCP_STREAM_MULTI', 'throughput')])
        change = 100 * (b / a - 1)
        text += ['', f'Two queues changed mean multistream throughput by **{change:+.1f}%** relative to the current one-queue baseline. '
                 + ('The observed improvement supports virtual-NIC/backend parallelism as a contributor to the limit; it does not isolate every bridge, segmentation, or TCP effect.'
                    if change > 10 else 'This result does not demonstrate a large throughput benefit from additional queues. Inspect the reversal, per-thread load, socket state and drop evidence before assigning a cause.')]
    text += ['', 'A vhost thread can consume most of one CPU while the machine-wide average remains low, especially if the scheduler moves it between CPUs. '
             'The thread observations and queue intervention are more informative than a low host-average CPU number alone. '
             'Retransmissions are measured in identified benchmark Pod namespaces; they can reflect loss or spurious retransmission following reordering, and are not an exact packet-loss percentage. '
             'Data-socket RTT excludes netperf control connections on port 12865. Elevated loaded RTT can reflect buffering and ACK behavior; it is not the same metric as TCP_RR transaction latency.', '',
             'The host CPU-s/GB column estimates monitored physical-host busy logical CPUs divided by measured application GB/s. '
             'It includes guest execution, background control-plane work and monitoring, and does not add guest CPU again. '
             'The CPU and application-rate windows are approximately aligned, so this is a resource-cost proxy, not isolated encapsulation cost or energy efficiency.', '',
             '## Queue controls across tunnel implementations', '']
    cross_queue_rows = []
    for mode in ('vxlan', 'vm-vxlan-1450'):
        for suffix in ('one-queue-control', 'two-queue-control', 'restored-queue-control'):
            name = mode + '-' + suffix
            if name in measurements:
                m, d = measurements[name], evidence[name]['diagnostics']
                cross_queue_rows.append([name, len(m['metrics'].get(('TCP_STREAM_MULTI', 'throughput'), [])),
                    describe(m['metrics'].get(('TCP_STREAM_MULTI', 'throughput'), []), .001),
                    describe(d.get('worker-1/retrans_per_1000_outsegs', [])),
                    describe(d.get('estimated_host_cpu_seconds_per_GB', []))])
    if cross_queue_rows:
        text += [aggregate.table(['Control', 'n', 'Multi Gb/s', 'Sender retrans/1000 OutSegs', 'Estimated host CPU-s/GB'], cross_queue_rows), '',
                 'These per-mode one/two/restored-queue controls test whether virtual-NIC/backend parallelism helps other tunnel paths too. '
                 'One-queue checks use three samples; two-queue single/multistream checks use five. '
                 'Compare within each mode and retain the different sample counts; a queue gain alone is not a placement-specific advantage.']
    else:
        text.append('The additional Cilium and per-VM queue controls have not completed.')
    text += ['',
             '## Vhost affinity control', '']
    affinity_rows = []
    for name in ('host-1450-two-queues-repeat', 'host-1450-pinned', 'host-1450-unpinned'):
        if name in measurements:
            m, d = measurements[name], evidence[name]['diagnostics']
            affinity_rows.append([name, len(m['metrics'].get(('TCP_STREAM_MULTI', 'throughput'), [])),
                describe(m['metrics'].get(('TCP_STREAM_MULTI', 'throughput'), []), .001),
                describe(d.get('worker-1/retrans_per_1000_outsegs', [])), describe(d.get('worker-1/data_rtt_ms', []))])
    if affinity_rows:
        text.append(aggregate.table(['Control', 'n', 'Multi Gb/s', 'Sender retrans/1000 OutSegs', 'Sender data RTT ms'], affinity_rows))
        text.append('Only worker vhost thread CPU masks change in this control. Two distinct physical cores are selected within the NIC NUMA node where available, avoiding SMT siblings and preferring nonzero logical CPU IDs. '
                    'Guest vCPU and IRQ masks are not pinned. Original masks are restored and a second unpinned run checks reversibility. This is not full NUMA tuning or a contention test.')
    else:
        text.append('The affinity controls have not completed.')
    text += ['',
             '## MTU and attachment effects', '']
    for large, small in [('native', 'direct-1450'), ('vm-vxlan', 'vm-vxlan-1450'), ('host-vxlan', 'host-vxlan-1450'), ('bridge-1450', 'direct-1450')]:
        if large in main and small in main:
            a = statistics.mean(main[large]['metrics'][('TCP_STREAM_MULTI', 'throughput')])
            b = statistics.mean(main[small]['metrics'][('TCP_STREAM_MULTI', 'throughput')])
            text.append(f'- `{large}` versus `{small}`: mean multistream throughput changes by **{100 * (a / b - 1):+.1f}%** ({a / 1000:.3f} versus {b / 1000:.3f} Gb/s).')
    text += ['', 'Native 1500 versus direct-1450 tests reduced Pod MTU without a tunnel. Custom 1500-inner-MTU modes also raise transport MTU to 1550; '
             'their comparison measures that complete MTU configuration. A gain with larger packets is consistent with amortizing per-packet work, but does not by itself prove the limiting function. '
             'The bridge-backed native control assesses a different attachment from macvtap. Host VXLAN still changes the encapsulation location, packet format across virtio, and bridge forwarding arrangement. '
             'Do not subtract throughput differences and call them isolated bridge or encapsulation costs.', '',
             '## Profiling and drop evidence', '']
    profile = measurements.get('host-1450-profile')
    if profile:
        for _, _, path in profile['phases']:
            for raw in sorted(path.glob('*/samples.jsonl')):
                for line in raw.open():
                    entry = json.loads(line)
                    if entry['kind'] == 'profile_result':
                        original = entry.get('report', {}).get('output', '')
                        profile_dir = output / 'profiles'
                        profile_dir.mkdir(exist_ok=True)
                        name = raw.parent.name + '-perf-report.txt'
                        (profile_dir / name).write_text(original)
                        text += [f'### {raw.parent.name}', '',
                                 'Largest self-sample overheads after excluding named idle functions. Percentages use all CPU-clock samples, '
                                 'including idle time in the denominator; they are not percentages of one vhost thread or of busy time.', '',
                                 aggregate.table(['Self samples %', 'Command', 'Function'], profile_self_rows(original)[:15]), '',
                                 f'[Full original perf report](profiles/{name})', '']
                    if entry['kind'] == 'drop_profile_result':
                        report = entry.get('report') or {}
                        text.append(f'Drop trace exit={entry["exit_code"]}; reasons: `{json.dumps(report.get("reasons", {}), sort_keys=True)}`. '
                                    f'Recording log: `{entry.get("log", "").strip()}`.')
        text += ['', 'Profiles use software CPU-clock sampling; skb drop traces cover up to 15 seconds of a separate diagnostic run. '
                 'Drop reasons and call sites are system-wide and are not proof that every event belongs to the benchmark. '
                 'NOT_SPECIFIED events, particularly buffer-fragment frees at skb_release_data, must not be counted as lost benchmark packets. '
                 'Linux buffer-consumption/free paths can reach the same fragment cleanup machinery '
                 '([kernel source](https://github.com/torvalds/linux/blob/v6.8/net/core/skbuff.c)). '
                 'Raw profiles and diagnostic streams remain under the profile attempt. Profile throughput is excluded from the main comparisons.']
    else:
        text.append('Profiling has not completed.')
    text += ['', '## Evidence limits and reproducibility', '',
             'These are repeated samples within one overnight sequence, not independent randomized rounds. Mode order, resets, scheduling and monitoring may influence results. '
             'The main workload and software baseline stay fixed, but VXLAN deployment paths differ structurally. '
             'The queue reversal improves interpretation of the queue intervention; it is not a contention or multi-VM scaling test. '
             'Scheduler wait counters may be unavailable if kernel scheduler statistics are disabled. '
             'NIC/virtual-interface counter deltas cover setup and cleanup as well as the workload; no nonzero drop counter is automatically attributed to the benchmark. '
             'All capability and coverage warnings remain in the linked aggregate reports. Failed attempts are preserved and excluded from performance averages.', '',
             '## Tables, figures and raw evidence', '']
    text += [f'- [{name.replace("-", " ").title()}]({link})' for name, link in links.items()]
    text += ['', '- [Machine-readable findings](evidence.json)',
             '- Original reports, Pod identities, namespace socket observations, thread records and queue transition XML are retained in the study attempt directories.', '']
    (output / 'report.md').write_text('\n'.join(text))
    print(f'Analysis: {output / "report.md"}')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('study', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--partial', action='store_true')
    args = parser.parse_args()
    report(args.study.resolve(), args.output.resolve(), args.partial)


if __name__ == '__main__':
    main()
