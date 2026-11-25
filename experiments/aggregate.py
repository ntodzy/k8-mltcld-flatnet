#!/usr/bin/env python3
"""Aggregate saved attempts or diagnostic phases into tables and static figures."""
import argparse
from collections import defaultdict
import csv
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import re
import statistics
import sys

from common import ROOT, save_json

FIELDS = ['attempt', 'mode', 'phase', 'sample', 'domain', 'network', 'node',
          'test', 'target', 'core', 'interface', 'metric', 'unit', 'scope', 'value', 'source']
GROUP_FIELDS = [key for key in FIELDS if key not in ('phase', 'sample', 'value', 'source')]
STATS = ['n', 'mean', 'stdev', 'median', 'min', 'max']
METRIC = re.compile(r'^(TCP_STREAM_MULTI|TCP_STREAM|TCP_RR)_(pod-to-pod|host-to-host)_(.+)$')


def read_json(path):
    return json.loads(path.read_text())


def finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def measurement(path):
    path = path.expanduser().resolve()
    if (path / 'measurement/metadata.json').is_file():
        return path / 'measurement'
    if (path / 'metadata.json').is_file():
        return path
    raise ValueError(f'{path}: expected an attempt, measurement, or diagnostic phase directory')


def row(context, **values):
    return {**dict.fromkeys(FIELDS, ''), **context, **values}


def perf_rows(directory, context, warnings):
    rows = []
    seen = set()
    for report in sorted((directory / 'reports').rglob('*.json')):
        for item in read_json(report).get('dataItems', []):
            labels = item.get('labels', {})
            for name, value in item.get('data', {}).items():
                match = METRIC.fullmatch(name)
                if not match or not finite(value):
                    continue
                test, network, metric = match.groups()
                unit = item.get('unit', '')
                identity = (labels.get('node', 'unknown'), network, test, metric, unit)
                if identity in seen:
                    raise ValueError(f'Duplicate performance observation in {directory}: {identity}')
                seen.add(identity)
                rows.append(row(context, domain='performance', network=network,
                                node=identity[0], test=test, metric=metric, unit=unit,
                                scope='benchmark', value=value, source=str(report)))
    # Cilium JSON omits mean/min/max latency. Supplement them from the console
    # only when it represents one sample, so multiple trials cannot be conflated.
    meta = read_json(directory / 'metadata.json')
    console = directory / 'console.log'
    if meta.get('samples') == 1 and console.is_file():
        console_seen = set()
        for line in console.read_text().splitlines():
            line = re.sub(r'\x1b\[[0-9;]*m', '', line)
            fields = [part.strip().replace('📋', '').strip() for part in line.split('|')]
            if len(fields) < 10 or fields[0] not in ('pod-to-pod', 'host-to-host') or fields[2] != 'TCP_RR':
                continue
            for name, index in (('min', 4), ('mean', 5), ('max', 6)):
                match = re.fullmatch(r'([\d.]+)\s*(ns|µs|μs|us|ms|s)', fields[index])
                if not match:
                    warnings.add(f'{console}: could not parse {name} latency')
                    continue
                scale = {'ns': .001, 'µs': 1, 'μs': 1, 'us': 1, 'ms': 1000, 's': 1000000}[match[2]]
                identity = (fields[1], fields[0], 'TCP_RR', name, 'us')
                if identity in console_seen:
                    raise ValueError(f'Duplicate console latency in {console}: {identity}')
                console_seen.add(identity)
                if identity in seen:
                    continue
                seen.add(identity)
                rows.append(row(context, domain='performance', network=fields[0], node=fields[1],
                                test='TCP_RR', metric=name, unit='us', scope='benchmark',
                                value=float(match[1]) * scale, source=str(console)))
    elif meta.get('samples', 1) > 1:
        warnings.add(f'{context["attempt"]}: combined JSON may retain only the final trial; n counts saved observations, not requested samples. Console trials were not reconstructed.')
    if not rows:
        raise ValueError(f'No supported performance observations found in {directory / "reports"}')
    return rows


def diagnostic_rows(path, context, warnings):
    if not path.is_file():
        warnings.add(f'{context["attempt"]}/{context["phase"]}: diagnostic summary unavailable')
        return []
    rows = []
    test = {'rr': 'TCP_RR', 'single': 'TCP_STREAM', 'multi': 'TCP_STREAM_MULTI'}.get(context.get('test'), context.get('test'))
    network = {'pod': 'pod-to-pod', 'host': 'host-to-host'}.get(context.get('network'), context.get('network'))
    for target, data in read_json(path).items():
        base = {**context, 'test': test, 'network': network, 'node': 'other-node',
                'target': target, 'source': str(path)}
        for warning in data.get('warnings', []):
            warnings.add(f'{context["attempt"]}/{target}: {warning}')
        for core, metrics in data.get('per_core', {}).items():
            for metric, value in metrics.items():
                if finite(value):
                    rows.append(row(base, domain='cpu', core=core, metric=metric, unit='%',
                                    scope='observed_workload', value=value))
        for pod in data.get('pod_tcp', {}).values():
            pod_scope = 'host_network_namespace' if pod.get('host_network') else 'benchmark_pod_namespace'
            for field, metrics in [('snmp_delta', ('Tcp:RetransSegs', 'Tcp:OutSegs', 'Tcp:InSegs')),
                                   ('netstat_delta', ('TcpExt:TCPTimeouts', 'TcpExt:TCPFastRetrans', 'TcpExt:TCPLossProbes'))]:
                for metric in metrics:
                    value = pod.get(field, {}).get(metric)
                    if finite(value):
                        rows.append(row(base, domain='pod-tcp', metric=metric, unit='count',
                                        scope=pod_scope, value=value))
            for metric in ('cwnd', 'rtt_ms', 'data_cwnd', 'data_rtt_ms', 'data_minrtt_ms', 'data_rwnd_limited_percent'):
                for stat, value in pod.get(metric, {}).items():
                    if finite(value):
                        rows.append(row(base, domain='pod-tcp', metric=f'{metric}/{stat}',
                                        unit='segments' if metric in ('cwnd', 'data_cwnd') else '%' if metric.endswith('_percent') else 'ms',
                                        scope=pod_scope, value=value))
        for task in data.get('threads', []):
            for metric, unit in [('cpu_percent_one_core', '% of one CPU'), ('scheduler_wait_ms', 'ms')]:
                value = task.get(metric)
                if finite(value):
                    rows.append(row(base, domain='thread', core=f'{task["comm"]}:{task["tid"]}',
                                    interface=task.get('domain') or '', metric=metric, unit=unit,
                                    scope='observed_workload', value=value))
        for interface, directions in data.get('interface_counter_delta', {}).items():
            for direction, metrics in directions.items():
                for metric, value in metrics.items():
                    if finite(value):
                        rows.append(row(base, domain='interface', interface=interface, metric=f'{direction}/{metric}',
                                        unit='bytes' if metric == 'bytes' else 'count',
                                        scope='whole_invocation', value=value))
        busy = [values['busy_percent'] for core, values in data.get('per_core', {}).items()
                if core != 'cpu' and finite(values.get('busy_percent'))]
        if busy:
            rows.append(row(base, domain='cpu', core='busiest', metric='busy_percent', unit='%',
                            scope='observed_workload', value=max(busy)))
        for field, keys in [('snmp_delta', ('Tcp:RetransSegs', 'Tcp:InSegs', 'Tcp:OutSegs')),
                            ('netstat_delta', ('TcpExt:TCPTimeouts',)),
                            ('softnet_delta', ('processed', 'dropped', 'time_squeeze'))]:
            for metric in keys:
                value = data.get(field, {}).get(metric)
                if finite(value):
                    rows.append(row(base, domain='counter', metric=field + '/' + metric,
                                    unit='count', scope='observed_workload', value=value))
        for interface, metrics in data.get('nic_counter_delta', {}).items():
            for metric, value in metrics.items():
                if finite(value):
                    if value < 0:
                        warnings.add(f'{context["attempt"]}/{target}/{interface}: negative NIC delta for {metric}; omitted as possible reset')
                        continue
                    rows.append(row(base, domain='nic', interface=interface, metric=metric,
                                    unit='driver counter', scope='whole_invocation', value=value))
    return rows


def load_attempt(path, allow_partial=False):
    directory = measurement(path)
    meta = read_json(directory / 'metadata.json')
    label = directory.parent.name if directory.name == 'measurement' else directory.name
    warnings = set()
    if meta.get('status') != 'passed':
        if not allow_partial:
            raise ValueError(f'{directory}: status={meta.get("status")}; use --allow-partial to summarize only completed phases')
        warnings.add(f'{label}: PARTIAL snapshot, status={meta.get("status")}; completed phases only')
    context = {'attempt': label, 'mode': meta.get('mode', 'unknown')}
    rows = []
    phase_status = []
    if meta.get('diagnostics'):
        phase_paths, phase_samples = set(), set()
        for phase in meta.get('phases', []):
            relative = Path(phase['directory'])
            child = (directory / relative).resolve()
            if not child.is_relative_to(directory):
                raise ValueError(f'Phase path escapes measurement: {relative}')
            identity = (phase.get('sample'), phase.get('network'), phase.get('test_type'))
            if child in phase_paths or identity in phase_samples:
                raise ValueError(f'Duplicate phase or sample identity in {directory}: {relative}')
            phase_paths.add(child)
            phase_samples.add(identity)
            status = phase.get('status', 'unknown')
            phase_status.append({'phase': str(relative), 'sample': phase.get('sample'),
                                 'network': phase.get('network'), 'test': phase.get('test_type'), 'status': status})
            if status != 'passed':
                if not allow_partial:
                    raise ValueError(f'{child}: phase has status {status}')
                warnings.add(f'{label}/{relative}: excluded {status} phase')
                continue
            phase_context = {**context, 'phase': child.name, 'sample': phase.get('sample', ''),
                             'network': phase.get('network', ''), 'test': phase.get('test_type', '')}
            child_meta = read_json(child / 'measurement/metadata.json')
            if child_meta.get('status') != 'passed':
                raise ValueError(f'{child}: parent phase passed but child measurement did not')
            rows.extend(perf_rows(child / 'measurement', phase_context, warnings))
            rows.extend(diagnostic_rows(child / 'summary.json', phase_context, warnings))
        # The manifest's actual phase list is authoritative, including restricted runs.
        if meta.get('status') != 'passed':
            warnings.add(f'{label}: {sum(p["status"] == "passed" for p in phase_status)} completed phases; later phases may not yet appear in the manifest')
    else:
        if meta.get('status') != 'passed':
            raise ValueError(f'{directory}: partial combined runs have no independently completed phase manifest')
        phase_context = {**context, 'phase': directory.parent.name if directory.name == 'measurement' else 'combined',
                         'sample': 1 if meta.get('samples') == 1 else 'report-only'}
        rows.extend(perf_rows(directory, phase_context, warnings))
        if (directory.parent / 'summary.json').is_file():
            phase_context.update(network=meta.get('network'), test=meta.get('test_type'))
            rows.extend(diagnostic_rows(directory.parent / 'summary.json', phase_context, warnings))
    if not rows:
        raise ValueError(f'{directory}: no completed observations to aggregate')
    return {'directory': str(directory), 'attempt': label, 'metadata': meta,
            'phases': phase_status}, rows, warnings


def aggregate(rows):
    groups = defaultdict(list)
    for entry in rows:
        groups[tuple(entry[key] for key in GROUP_FIELDS)].append(entry['value'])
    result = []
    for key, values in sorted(groups.items()):
        result.append({**dict(zip(GROUP_FIELDS, key)), 'n': len(values),
                       'mean': statistics.mean(values),
                       'stdev': statistics.stdev(values) if len(values) > 1 else None,
                       'median': statistics.median(values), 'min': min(values), 'max': max(values)})
    return result


def write_csv(path, fields, rows):
    with path.open('w', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def display(value):
    if value is None:
        return '—'
    return f'{value:,.3f}'.rstrip('0').rstrip('.') if isinstance(value, (int, float)) else str(value)


def table(headers, data):
    def cell(value):
        return display(value).replace('|', '\\|').replace('\n', ' ')
    return '\n'.join(['| ' + ' | '.join(headers) + ' |',
                       '| ' + ' | '.join(['---'] * len(headers)) + ' |'] +
                      ['| ' + ' | '.join(cell(v) for v in values) + ' |' for values in data])


def make_plots(output, rows, stats):
    try:
        import matplotlib
        matplotlib.use('Agg')
        import matplotlib.pyplot as plt
    except ImportError as error:
        raise ValueError('Graphs require Matplotlib. Install experiments/requirements-analysis.txt, or use --no-plots for tables only.') from error
    figures = []
    plots = output / 'figures'
    plots.mkdir()
    # Keep units, network placement, and metric families separate.
    selected = [r for r in stats if r['domain'] == 'performance']
    attempts_by_mode = defaultdict(set)
    for entry in stats:
        attempts_by_mode[entry['mode']].add(entry['attempt'])
    def short(entry):
        return entry['mode'] if len(attempts_by_mode[entry['mode']]) == 1 else entry['attempt']
    def workload(entry):
        return {'TCP_STREAM': 'single', 'TCP_STREAM_MULTI': 'multi', 'TCP_RR': 'RR'}.get(entry['test'], entry['test'])
    charts = defaultdict(list)
    for entry in selected:
        charts[(entry['unit'], 'latency' if entry['test'] == 'TCP_RR' and entry['unit'] == 'us'
                else 'transaction-rate' if entry['unit'] == 'ops/s' else 'throughput')].append(entry)
    for index, ((unit, family), entries) in enumerate(sorted(charts.items()), 1):
        entries.sort(key=lambda r: (r['attempt'], r['network'], r['node'], r['test'], r['metric']))
        fig, ax = plt.subplots(figsize=(11, max(3.5, len(entries) * .38)))
        for y, entry in enumerate(entries):
            values = [r['value'] for r in rows if all(r[k] == entry[k] for k in GROUP_FIELDS)]
            scale = .001 if unit == 'Mb/s' else 1
            offsets = [(i - (len(values) - 1) / 2) * .045 for i in range(len(values))]
            ax.scatter([v * scale for v in values], [y + offset for offset in offsets],
                       alpha=.7, color='#386cb0', s=25)
            ax.errorbar(entry['mean'] * scale, y,
                        xerr=entry['stdev'] * scale if entry['stdev'] is not None else None,
                        fmt='D', color='#d95f02', capsize=4, markersize=5)
        labels = [f'{short(e)} | {e["network"].replace("-to-", "→")} | {e["node"]} | {workload(e)} {e["metric"]} (n={e["n"]})'
                  for e in entries]
        ax.set_yticks(range(len(entries)), labels)
        ax.invert_yaxis()
        ax.set_xlim(left=0)
        ax.set_xlabel('Gb/s' if unit == 'Mb/s' else unit)
        ax.set_title(f'{family.replace("-", " ").title()}: samples and mean ± sample SD')
        ax.scatter([], [], color='#386cb0', label='Individual samples')
        ax.plot([], [], 'D', color='#d95f02', label='Mean; bars = sample SD')
        ax.legend(loc='best', fontsize=8)
        ax.grid(axis='x', alpha=.25)
        fig.tight_layout()
        name = f'{index:02d}-{family}'
        for suffix in ('svg', 'png'):
            fig.savefig(plots / f'{name}.{suffix}', dpi=160)
        plt.close(fig)
        figures.append((family.replace('-', ' ').title(), f'figures/{name}.svg'))
    cpu = [e for e in stats if e['domain'] == 'cpu' and e['core'] in ('cpu', 'busiest') and e['metric'] == 'busy_percent']
    if cpu:
        # Whole-machine average versus the maximum individual-core busy value.
        cpu.sort(key=lambda r: (r['attempt'], r['target'], r['network'], r['test'], r['core']))
        fig, ax = plt.subplots(figsize=(11, max(4, len(cpu) * .26)))
        for y, entry in enumerate(cpu):
            ax.errorbar(entry['mean'], y, xerr=entry['stdev'], fmt='o', capsize=3,
                        color='#386cb0' if entry['core'] == 'cpu' else '#d95f02')
        ax.set_yticks(range(len(cpu)), [f'{short(e)} | {e["target"]} | {e["network"].split("-")[0]} {workload(e)} | {e["core"]}' for e in cpu])
        ax.invert_yaxis()
        ax.set_xlim(0, 105)
        ax.set_xlabel('Busy CPU (%) — mean ± sample SD')
        ax.set_title('System average (blue) and busiest core per phase (orange)')
        ax.grid(axis='x', alpha=.25)
        fig.tight_layout()
        for suffix in ('svg', 'png'):
            fig.savefig(plots / f'cpu-busy.{suffix}', dpi=160)
        plt.close(fig)
        figures.append(('CPU utilization', 'figures/cpu-busy.svg'))
    targets = sorted({e['target'] for e in stats if e['domain'] == 'cpu'})
    for target in targets:
        entries = [e for e in stats if e['domain'] == 'cpu' and e['target'] == target
                   and re.fullmatch(r'cpu\d+', e['core']) and e['metric'] == 'busy_percent']
        if not entries:
            continue
        columns = sorted({(e['attempt'], e['network'], e['test']) for e in entries})
        cores = sorted({e['core'] for e in entries}, key=lambda c: int(c[3:]))
        values = {(e['core'], (e['attempt'], e['network'], e['test'])): e['mean'] for e in entries}
        grid = [[values.get((core, col), float('nan')) for col in columns] for core in cores]
        fig, ax = plt.subplots(figsize=(max(6, len(columns) * 1.2), max(3, len(cores) * .35)))
        cmap = plt.get_cmap('YlOrRd').copy()
        cmap.set_bad('#dddddd')
        heat = ax.imshow(grid, aspect='auto', vmin=0, vmax=100, cmap=cmap)
        ax.set_yticks(range(len(cores)), cores)
        representatives = {e['attempt']: e for e in entries}
        ax.set_xticks(range(len(columns)), [f'{short(representatives[col[0]])}\n{col[1].split("-")[0]} {workload({"test": col[2]})}' for col in columns], rotation=25, ha='right')
        if len(cores) * len(columns) <= 120:
            for y, cells in enumerate(grid):
                for x, value in enumerate(cells):
                    if math.isfinite(value):
                        ax.text(x, y, f'{value:.1f}', ha='center', va='center', fontsize=8,
                                color='white' if value > 65 else 'black')
        ax.set_title(f'{target}: mean per-core busy CPU across samples')
        fig.colorbar(heat, ax=ax, label='Busy CPU (%)')
        fig.tight_layout()
        name = 'cpu-cores-' + re.sub(r'[^A-Za-z0-9_-]', '_', target)
        for suffix in ('svg', 'png'):
            fig.savefig(plots / f'{name}.{suffix}', dpi=160)
        plt.close(fig)
        figures.append((f'Per-core CPU: {target}', f'figures/{name}.svg'))
    return figures


def write_report(output, inputs, stats, warnings, figures):
    text = ['# Attempt measurement summary', '',
            'Each observation comes from a completed phase. Samples are repeated measurements within an attempt, not independent cluster rebuilds. '
            'SD is sample standard deviation, not a confidence interval; it is unavailable for n=1. '
            'Latency percentiles are averaged across samples, not pooled across transactions. '
            'Missing observations are never filled with zeros.', '', '## Inputs', '']
    for item in inputs:
        meta = item['metadata']
        text.append(f'- `{item["directory"]}` — mode `{meta.get("mode")}`, status `{meta.get("status")}`, '
                    f'client `{meta.get("client")}` → server `{meta.get("server")}`, '
                    f'duration {meta.get("duration_seconds", meta.get("duration"))} s, '
                    f'requested samples {meta.get("samples")}, streams {meta.get("streams")}, profiling {meta.get("profile", False)}.')
    text += ['', '## Performance', '', table(
        ['Attempt', 'Network', 'Placement', 'Test', 'Metric', 'Unit', 'n', 'Mean', 'SD', 'Median', 'Min', 'Max'],
        [[r[k] for k in ('attempt', 'network', 'node', 'test', 'metric', 'unit', *STATS)]
         for r in stats if r['domain'] == 'performance'])]
    cpu = [r for r in stats if r['domain'] == 'cpu' and r['core'] in ('cpu', 'busiest')]
    if cpu:
        text += ['', '## CPU diagnostics', '',
                 '`cpu` is the machine-wide average; `busiest` is the maximum individual-core busy percentage in each phase, then averaged across samples. '
                 'It need not be the same core in every phase. Busy excludes steal and I/O wait; softirq is part of busy. '
                 'Guest and hypervisor percentages must not be added. Full per-core results are in aggregate.csv.', '',
                 table(['Attempt', 'Target', 'Network', 'Test', 'Core', 'Metric', 'n', 'Mean %', 'SD'],
                       [[r[k] for k in ('attempt', 'target', 'network', 'test', 'core', 'metric', 'n', 'mean', 'stdev')] for r in cpu])]
    counters = [r for r in stats if r['domain'] == 'counter' and any(s in r['metric'] for s in ('Retrans', 'Timeout', 'dropped', 'time_squeeze'))]
    if counters:
        text += ['', '## TCP and backlog counters', '',
                 'Host-wide deltas within the observed workload window, counted once per target and phase. '
                 'These are counts, not rates. NIC/queue counters in aggregate.csv cover the whole invocation, including setup and cleanup.', '',
                 table(['Attempt', 'Target', 'Network', 'Test', 'Counter', 'n', 'Mean count', 'SD', 'Max'],
                       [[r[k] for k in ('attempt', 'target', 'network', 'test', 'metric', 'n', 'mean', 'stdev', 'max')] for r in counters])]
    pod_tcp = [r for r in stats if r['domain'] == 'pod-tcp']
    if pod_tcp:
        text += ['', '## Benchmark Pod TCP diagnostics', '',
                 'Counters come from the identified benchmark Pod network namespaces. Socket cwnd/RTT summaries include observed sockets in that Pod; '
                 'they are not transaction latency and are not weighted by bytes. Older host-namespace TCP counters do not establish benchmark retransmissions.', '',
                 table(['Attempt', 'Target', 'Test', 'Metric', 'Unit', 'n', 'Mean', 'SD', 'Max'],
                       [[r[k] for k in ('attempt', 'target', 'test', 'metric', 'unit', 'n', 'mean', 'stdev', 'max')] for r in pod_tcp])]
    threads = [r for r in stats if r['domain'] == 'thread' and r['metric'] == 'cpu_percent_one_core']
    if threads:
        top = sorted(threads, key=lambda r: -r['mean'])[:40]
        text += ['', '## Busiest observed threads', '',
                 'Thread CPU is normalized to one CPU, separately from machine-wide averages. QEMU vCPU threads include guest execution; '
                 'do not add them to guest CPU measurements. All thread and scheduler-wait statistics are in aggregate.csv.', '',
                 table(['Attempt', 'Target', 'Test', 'Thread', 'VM domain', 'n', 'Mean % of one CPU', 'Max'],
                       [[r[k] for k in ('attempt', 'target', 'test', 'core', 'interface', 'n', 'mean', 'max')] for r in top])]
    if figures:
        text += ['', '## Figures', '']
        for title, path in figures:
            text += [f'![{title}]({path})', '']
    text += ['', '## Coverage and limitations', '',
             'Workload timing uses approximate client netperf detection and SSH receive timestamps. '
             'CPU and TCP observations include background work and do not isolate tunnel processing. '
             'Counts from multiple interfaces or guest/host layers may describe the same traffic and must not be summed as independent events. '
             'Driver-specific NIC counter names/units are preserved. No automatic causal or statistical-significance claim is made.', '']
    if warnings:
        text += [f'{len(warnings)} warnings are preserved in summary.json. First 20:', '']
        text += ['- ' + w.replace('\n', ' ') for w in sorted(warnings)[:20]]
    else:
        text.append('No input coverage warnings were recorded.')
    text += ['', 'Raw observations: [observations.csv](observations.csv). '
             'All aggregate statistics: [aggregate.csv](aggregate.csv). '
             'Metadata, phase coverage, statistics, and warnings: [summary.json](summary.json).', '']
    (output / 'summary.md').write_text('\n'.join(text))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('attempts', nargs='+', type=Path, help='Attempt, measurement, or diagnostic phase directories')
    parser.add_argument('--output', type=Path, help='New output directory; defaults to results/aggregate-UTC_TIMESTAMP')
    parser.add_argument('--allow-partial', action='store_true', help='Include only completed phases from running/failed diagnostic attempts')
    parser.add_argument('--no-plots', action='store_true', help='Generate tables/CSV/JSON without Matplotlib')
    args = parser.parse_args()
    inputs, rows, warnings = [], [], set()
    directories, labels = set(), set()
    for path in args.attempts:
        item, observations, notices = load_attempt(path, args.allow_partial)
        if item['directory'] in directories or item['attempt'] in labels:
            raise ValueError('Duplicate measurement or ambiguous attempt name; summarize these inputs separately')
        directories.add(item['directory'])
        labels.add(item['attempt'])
        inputs.append(item)
        rows.extend(observations)
        warnings.update(notices)
    # Comparisons remain separate even when workloads differ; call out the mismatch.
    signatures = {json.dumps({k: item['metadata'].get(k) for k in
                  ('client', 'server', 'duration_seconds', 'streams', 'pod_only', 'profile')}, sort_keys=True)
                  for item in inputs}
    if len(signatures) > 1:
        warnings.add('Input workload/endpoint/profile settings differ; groups remain separate and should not be treated as a controlled comparison')
    stats = aggregate(rows)
    if not args.no_plots:
        try:
            import matplotlib  # Check dependency before creating output.
        except ImportError as error:
            raise ValueError(f'Matplotlib or a required dependency could not load: {error}. Install experiments/requirements-analysis.txt for graphs, or pass --no-plots.') from error
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ')
    output = (args.output or ROOT / 'results' / f'aggregate-{stamp}').resolve()
    output.mkdir(parents=True, exist_ok=False)
    write_csv(output / 'observations.csv', FIELDS, rows)
    write_csv(output / 'aggregate.csv', GROUP_FIELDS + STATS, stats)
    figures = [] if args.no_plots else make_plots(output, rows, stats)
    save_json(output / 'summary.json', {'created_utc': datetime.now(timezone.utc).isoformat(),
              'inputs': inputs, 'warnings': sorted(warnings), 'statistics': stats,
              'observation_count': len(rows), 'figures': figures})
    write_report(output, inputs, stats, warnings, figures)
    print(f'Saved {len(rows)} observations, {len(stats)} aggregate groups, {len(figures)} figures.')
    print(f'Report: {output / "summary.md"}')
    if warnings:
        print(f'Coverage/capability warnings: {len(warnings)}; see summary.json')


if __name__ == '__main__':
    try:
        main()
    except (ValueError, KeyError, OSError) as error:
        raise SystemExit(f'Aggregation failed: {error}')
