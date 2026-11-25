"""Phase-isolated diagnostics around the existing validated performance runner."""
import base64
import csv
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import threading
import time

from common import ROOT, save_json, ssh_command


def phases(args):
    networks = ['host'] if args.mode == 'vm-local' else ['pod', 'host']
    if args.pod_only:
        networks = ['pod']
    if args.network != 'both':
        networks = [args.network]
    tests = ['rr', 'single', 'multi'] if args.test_type == 'all' else [args.test_type]
    return [(sample, network, test) for sample in range(1, args.samples + 1)
            for network in networks for test in tests]


def cpu_rows(raw):
    return {parts[0]: [int(v) for v in parts[1:9]]
            for line in raw.splitlines() if (parts := line.split()) and parts[0].startswith('cpu')}


def cpu_delta(before, after):
    result = {}
    for core in before.keys() & after.keys():
        delta = [b - a for a, b in zip(before[core], after[core])]
        total = sum(delta)
        if total <= 0 or min(delta) < 0:
            continue
        result[core] = {'busy_percent': round(100 * (total - delta[3] - delta[4] - delta[7]) / total, 3),
                        'softirq_percent': round(100 * delta[6] / total, 3),
                        'steal_percent': round(100 * delta[7] / total, 3)}
    return result


def paired_counters(raw):
    """Decode /proc/net/{snmp,netstat} header/value pairs without resetting them."""
    lines = raw.splitlines()
    values = {}
    for header, row in zip(lines[::2], lines[1::2]):
        names, numbers = header.split(), row.split()
        if names and numbers and names[0] == numbers[0]:
            values.update({names[0] + key: int(value) for key, value in zip(names[1:], numbers[1:])})
    return values


def counters_delta(before, after):
    return {key: after[key] - before[key] for key in before.keys() & after.keys()}


def softnet_totals(raw):
    rows = [[int(v, 16) for v in line.split()] for line in raw.splitlines()]
    return {name: sum(row[index] for row in rows) for index, name in
            enumerate(('processed', 'dropped', 'time_squeeze'))}


def numeric_lines(raw):
    result = {}
    for line in raw.splitlines():
        fields = line.replace(':', ' ').split()
        if len(fields) >= 2:
            try:
                result[fields[0]] = int(fields[1])
            except ValueError:
                pass
    return result


def softirq_rows(raw):
    return {fields[0].rstrip(':') + f'/cpu{index}': int(value)
            for line in raw.splitlines()[1:] if (fields := line.split())
            for index, value in enumerate(fields[1:])}


def summary(records, active_start=None, active_end=None):
    samples = [r for r in records if r['kind'] == 'sample']
    active = [r for r in samples if active_start is not None and
              active_start <= r['received_epoch'] <= active_end]
    result = {'sample_count': len(samples), 'active_sample_count': len(active),
              'client_window_start_epoch': active_start, 'client_window_end_epoch': active_end,
              'scope': 'CPU/TCP/softnet use the interior of the client-observed netperf window; NIC counters cover the entire invocation, including setup',
              'alignment': 'local SSH receive timestamps; not hardware-synchronized',
              'warnings': []}
    if len(active) >= 2:
        first, last = active[0], active[-1]
        result['observed_active_seconds'] = last['received_epoch'] - first['received_epoch']
        result['per_core'] = cpu_delta(cpu_rows(first['stat']), cpu_rows(last['stat']))
        for field in ('snmp', 'netstat'):
            result[field + '_delta'] = counters_delta(paired_counters(first[field]), paired_counters(last[field]))
        result['softnet_delta'] = counters_delta(softnet_totals(first['softnet']), softnet_totals(last['softnet']))
        result['softirq_delta'] = counters_delta(softirq_rows(first['softirqs']), softirq_rows(last['softirqs']))
    else:
        result['warnings'].append('Too few active samples for workload CPU/counter attribution')
    snapshots = {r['kind']: r for r in records if r['kind'] in ('before', 'after')}
    if len(snapshots) == 2:
        before, after = snapshots['before'], snapshots['after']
        result['nic_counter_delta'] = {}
        for interface in before['interfaces'].keys() & after['interfaces'].keys():
            a, b = before['interfaces'][interface]['-S'], after['interfaces'][interface]['-S']
            if a.get('exit_code') == b.get('exit_code') == 0:
                result['nic_counter_delta'][interface] = counters_delta(
                    numeric_lines(a['output']), numeric_lines(b['output']))
        for snapshot in snapshots.values():
            for name, values in snapshot['interfaces'].items():
                for flag, command in values.items():
                    if command.get('exit_code') != 0:
                        result['warnings'].append(f'{snapshot["kind"]}: {name} ethtool {flag} unavailable; see raw output')
    else:
        result['warnings'].append('Missing before/after NIC snapshot')
    return result


class Monitor:
    def __init__(self, config, target, directory, profile=False):
        self.directory = directory
        directory.mkdir()
        self.ready = threading.Event()
        self.records = []
        self.error = None
        script = Path(__file__).with_name('diagnostic_agent.py').read_text()
        self.stderr = (directory / 'ssh-stderr.log').open('w')
        self.process = subprocess.Popen(ssh_command(config, target,
            ['sudo', '-n', 'python3', '-u', '-c', script] + (['--profile'] if profile else [])),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self.stderr, text=True)
        self.thread = threading.Thread(target=self.read, daemon=True)
        self.thread.start()

    def read(self):
        try:
            with (self.directory / 'samples.jsonl').open('w') as raw:
                for line in self.process.stdout:
                    record = json.loads(line)
                    record['received_epoch'] = time.time()
                    if record['kind'] == 'perf_data':
                        with (self.directory / 'perf.data').open('ab') as perf:
                            perf.write(base64.b64decode(record['base64'], validate=True))
                        continue
                    raw.write(json.dumps(record) + '\n')
                    raw.flush()
                    self.records.append(record)
                    if record['kind'] == 'ready':
                        self.ready.set()
        except BaseException as error:
            self.error = str(error)
        finally:
            self.ready.set()

    def control(self, message):
        self.process.stdin.write(message + '\n')
        self.process.stdin.flush()

    def stop(self):
        try:
            if self.process.poll() is None:
                self.control('stop')
            self.process.wait(timeout=90)
        except (OSError, subprocess.TimeoutExpired):
            self.process.kill()
            self.process.wait()
            self.error = self.error or 'Monitor did not stop cleanly; remote collector exits on SSH EOF or its two-hour deadline'
        finally:
            self.thread.join(timeout=10)
            self.process.stdin.close()
            self.process.stdout.close()
            self.stderr.close()
        if self.process.returncode:
            self.error = self.error or f'Monitor SSH exited {self.process.returncode}; see ssh-stderr.log'


def run_diagnostics(args, hosts, ssh_config):
    plan = phases(args)
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ')
    output = (args.output or ROOT / 'results' / f'{args.mode}-diagnostics-{stamp}').resolve()
    targets = {name: name for name in (args.client, args.server)}
    for name in (args.client, args.server):
        host = hosts[name]['hypervisor']
        targets['hypervisor-' + host] = 'cs740-' + host + '-jump'
    if args.dry_run:
        print(f'Diagnostics: {len(plan)} isolated invocations; {args.duration}s traffic each, plus setup/cleanup each')
        print('Monitors:', ', '.join(targets))
        print('Profile:', args.profile, '\nOutput:', output)
        for phase in plan:
            print('sample=%d network=%s test=%s' % phase)
        return
    output.mkdir(parents=True, exist_ok=False)
    manifest = {key: getattr(args, key) for key in
                ('mode', 'client', 'server', 'duration', 'samples', 'streams', 'pod_only', 'profile')}
    manifest.update(duration_seconds=args.duration, status='running', diagnostics=True, phases=[],
                    targets=targets, report_count=0, started_utc=datetime.now(timezone.utc).isoformat())
    save_json(output / 'metadata.json', manifest)
    try:
        for sample, network, test in plan:
            name = f'sample-{sample:02d}-{network}-{test}'
            directory = output / 'phases' / name
            directory.mkdir(parents=True)
            phase = {'sample': sample, 'network': network, 'test_type': test,
                     'directory': str(directory.relative_to(output)), 'status': 'running'}
            manifest['phases'].append(phase)
            save_json(output / 'metadata.json', manifest)
            print(f'[{len(manifest["phases"])}/{len(plan)}] {name}: starting monitors', flush=True)
            monitors = {}
            try:
                # All monitors must report ready before any benchmark starts.
                for label, target in targets.items():
                    monitors[label] = Monitor(ssh_config, target, directory / label, args.profile)
                deadline = time.monotonic() + 120
                for label, monitor in monitors.items():
                    if not monitor.ready.wait(max(0, deadline - time.monotonic())) or not any(
                            r['kind'] == 'ready' for r in monitor.records):
                        raise RuntimeError(f'{label} diagnostic sampler failed to become ready')
                command = [sys.executable, str(ROOT / 'experiments/run.py'), args.mode,
                           '--client', args.client, '--server', args.server,
                           '--duration', str(args.duration), '--samples', '1', '--streams', str(args.streams),
                           '--test-type', test, '--network', network, '--output', str(directory / 'measurement')]
                if args.pod_only:
                    command.append('--pod-only')
                with (directory / 'console.log').open('w') as log:
                    with subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT,
                                          start_new_session=True) as process:
                        profiling_started = False
                        try:
                            while process.poll() is None:
                                # Profile all targets only once the client's netperf is visible.
                                if args.profile and not profiling_started and any(
                                        r['kind'] == 'sample' and r['netperf'] for r in monitors[args.client].records):
                                    for monitor in monitors.values():
                                        monitor.control(f'profile {min(args.duration, 3600)}')
                                    profiling_started = True
                                if any(m.error or m.process.poll() is not None for m in monitors.values()):
                                    # Finish/reap the child and its cleanup before reporting telemetry failure.
                                    phase['monitor_interrupted'] = True
                                time.sleep(0.25)
                            phase['exit_code'] = process.wait()
                        except BaseException:
                            if process.poll() is None:
                                # Let run.py execute its namespace/report cleanup before stopping telemetry.
                                os.kill(process.pid, signal.SIGINT)
                                try:
                                    process.wait(timeout=180)
                                except subprocess.TimeoutExpired:
                                    os.killpg(process.pid, signal.SIGKILL)
                                    process.wait()
                            raise
            finally:
                with ThreadPoolExecutor(max_workers=len(targets)) as pool:
                    list(pool.map(lambda monitor: monitor.stop(), monitors.values()))
                active = [r['received_epoch'] for r in monitors[args.client].records
                          if r['kind'] == 'sample' and r['netperf']] if args.client in monitors else []
                summaries = {}
                for label, monitor in monitors.items():
                    summaries[label] = summary(monitor.records, min(active) if active else None,
                                               max(active) if active else None)
                    if monitor.error:
                        summaries[label]['warnings'].append(monitor.error)
                save_json(directory / 'summary.json', summaries)
            if phase['exit_code']:
                raise RuntimeError(f'{name} failed; see {directory / "console.log"}')
            if phase.get('monitor_interrupted') or any(m.error for m in monitors.values()):
                raise RuntimeError(f'{name} monitoring failed; see diagnostic logs')
            if not active:
                raise RuntimeError(f'{name}: no client netperf workload observed; diagnostics are incomplete')
            if args.profile and any(not any(r['kind'] == 'profile_result' and r['exit_code'] == 0
                    for r in m.records) for m in monitors.values()):
                raise RuntimeError(f'{name}: perf profiling unavailable or failed; see profile_result records')
            result = json.loads((directory / 'measurement/metadata.json').read_text())
            if result.get('status') != 'passed':
                raise RuntimeError(f'{name}: measurement did not pass')
            # Keep sample-specific reports intact; the collector's recursive export sees every sample.
            shutil.copytree(directory / 'measurement/reports', output / 'reports' / name)
            csv_path = output / 'diagnostics.csv'
            new_csv = not csv_path.exists()
            with csv_path.open('a', newline='') as stream:
                fields = ['sample', 'network', 'test_type', 'target', 'core', 'busy_percent',
                          'softirq_percent', 'steal_percent', 'observed_active_seconds',
                          'tcp_retransmissions', 'softnet_dropped', 'softnet_time_squeeze']
                writer = csv.DictWriter(stream, fieldnames=fields)
                if new_csv:
                    writer.writeheader()
                for label, values in summaries.items():
                    for core, utilization in values.get('per_core', {}).items():
                        writer.writerow({'sample': sample, 'network': network, 'test_type': test,
                                         'target': label, 'core': core, **utilization,
                                         'observed_active_seconds': values['observed_active_seconds'],
                                         'tcp_retransmissions': values['snmp_delta'].get('Tcp:RetransSegs'),
                                         'softnet_dropped': values['softnet_delta']['dropped'],
                                         'softnet_time_squeeze': values['softnet_delta']['time_squeeze']})
            manifest['report_count'] += result['report_count']
            phase['status'] = 'passed'
            save_json(output / 'metadata.json', manifest)
        manifest['status'] = 'passed'
    except BaseException as error:
        manifest.update(status='failed', error=str(error))
        if manifest['phases']:
            manifest['phases'][-1]['status'] = 'failed'
        raise
    finally:
        manifest['finished_utc'] = datetime.now(timezone.utc).isoformat()
        save_json(output / 'metadata.json', manifest)
    print(f'DONE: diagnostics saved to {output}', flush=True)
