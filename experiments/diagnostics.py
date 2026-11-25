"""Phase-isolated diagnostics around the existing validated performance runner."""
import base64
import csv
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
import os
import re
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import threading
import time
import xml.etree.ElementTree as ET

from common import ROOT, save_json, ssh_command, remote


def phases(args):
    networks = ['host'] if args.mode == 'vm-local' else ['pod', 'host']
    if args.pod_only:
        networks = ['pod']
    if args.network != 'both':
        networks = [args.network]
    tests = ['rr', 'single', 'multi'] if args.test_type == 'all' else ['single', 'multi'] if args.test_type == 'throughput' else [args.test_type]
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


def data_sockets(output):
    """Separate netperf data sockets from its port-12865 control connections."""
    result = []
    for block in re.split(r'\n(?=\S)', output):
        lines = block.strip().splitlines()
        if not lines or not lines[0].startswith('ESTAB'):
            continue
        header = lines[0].split()
        if len(header) < 5 or any(endpoint.endswith(':12865') for endpoint in header[3:5]):
            continue
        entry = {'local': header[3], 'peer': header[4]}
        for name, pattern in [('cwnd', r'\bcwnd:(\d+)'), ('rtt_ms', r'\brtt:([\d.]+)'),
                              ('minrtt_ms', r'\bminrtt:([\d.]+)'), ('mss', r'\bmss:(\d+)'),
                              ('bytes_retrans', r'\bbytes_retrans:(\d+)'),
                              ('rwnd_limited_percent', r'\brwnd_limited:[^\s(]+\(([\d.]+)%\)')]:
            match = re.search(pattern, block)
            if match:
                entry[name] = float(match[1])
        result.append(entry)
    return result


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
        result['logical_cpus'] = len([name for name in result['per_core'] if name != 'cpu'])
        if first.get('clock_ticks'):
            a, b = cpu_rows(first['stat'])['cpu'], cpu_rows(last['stat'])['cpu']
            result['busy_core_seconds'] = sum(b[index] - a[index] for index in (0, 1, 2, 5, 6)) / first['clock_ticks']
        for field in ('snmp', 'netstat'):
            result[field + '_delta'] = counters_delta(paired_counters(first[field]), paired_counters(last[field]))
        result['softnet_delta'] = counters_delta(softnet_totals(first['softnet']), softnet_totals(last['softnet']))
        result['softirq_delta'] = counters_delta(softirq_rows(first['softirqs']), softirq_rows(last['softirqs']))
        result['pod_tcp'] = {}
        uids = {pod['uid'] for record in active for pod in record.get('pods', [])}
        for uid in uids:
            observations = [(record, pod) for record in active for pod in record.get('pods', []) if pod['uid'] == uid]
            if len(observations) < 2:
                continue
            a, b = observations[0][1], observations[-1][1]
            if a['netns'] != b['netns']:
                result['warnings'].append(f'Pod {uid} changed network namespace; TCP delta omitted')
                continue
            entry = {key: a.get(key) for key in ('uid', 'name', 'namespace', 'node', 'role', 'host_network', 'netns')}
            entry['observed_seconds'] = observations[-1][0]['received_epoch'] - observations[0][0]['received_epoch']
            for field in ('snmp', 'netstat'):
                if isinstance(a.get(field), str) and isinstance(b.get(field), str):
                    entry[field + '_delta'] = counters_delta(paired_counters(a[field]), paired_counters(b[field]))
            sockets = [pod['tcp'] for _, pod in observations if pod.get('tcp', {}).get('exit_code') == 0]
            for label, pattern in [('cwnd', r'\bcwnd:(\d+)'), ('rtt_ms', r'\brtt:([\d.]+)')]:
                values = [float(value) for socket in sockets for value in re.findall(pattern, socket['output'])]
                if values:
                    entry[label] = {'min': min(values), 'max': max(values), 'mean': sum(values) / len(values)}
            bulk = [item for socket in sockets for item in data_sockets(socket['output'])]
            entry['data_socket_observation_count'] = len(bulk)
            for label in ('cwnd', 'rtt_ms', 'minrtt_ms', 'rwnd_limited_percent'):
                values = [item[label] for item in bulk if label in item]
                if values:
                    entry['data_' + label] = {'min': min(values), 'max': max(values), 'mean': sum(values) / len(values)}
            result['pod_tcp'][uid] = entry
        if first.get('tasks') is not None:
            def task_key(task):
                return (task['pid'], task['tid'], task['starttime'])
            initial = {task_key(task): task for task in first['tasks']}
            duration = result['observed_active_seconds']
            ticks = first['clock_ticks']
            result['threads'] = []
            if str(first.get('scheduler_stats_enabled', '')).strip() == '0':
                result['warnings'].append('Kernel scheduler statistics are disabled; scheduler wait counters may be unavailable')
            for task in last.get('tasks', []):
                previous = initial.get(task_key(task))
                if previous is None:
                    continue
                used = task['utime'] + task['stime'] - previous['utime'] - previous['stime']
                processors = [candidate['processor'] for record in active for candidate in record.get('tasks', [])
                              if task_key(candidate) == task_key(task) and 'processor' in candidate]
                result['threads'].append({**task, 'cpu_percent_one_core': round(100 * used / ticks / duration, 3),
                                          'scheduler_wait_ms': (task['wait_ns'] - previous['wait_ns']) / 1e6,
                                          'observed_cpus': sorted(set(processors)),
                                          'sampled_cpu_changes': sum(a != b for a, b in zip(processors, processors[1:]))})
    else:
        result['warnings'].append('Too few active samples for workload CPU/counter attribution')
    snapshots = {r['kind']: r for r in records if r['kind'] in ('before', 'after')}
    if len(snapshots) == 2:
        before, after = snapshots['before'], snapshots['after']
        result['interface_counter_delta'] = {}
        if before.get('links', {}).get('exit_code') == after.get('links', {}).get('exit_code') == 0:
            try:
                links_a = {entry['ifname']: entry for entry in json.loads(before['links']['output'])}
                links_b = {entry['ifname']: entry for entry in json.loads(after['links']['output'])}
            except (ValueError, KeyError) as error:
                links_a, links_b = {}, {}
                result['warnings'].append(f'Cannot decode interface counter snapshot: {error}; raw command output retained')
            for name in links_a.keys() & links_b.keys():
                a = links_a[name].get('stats64', links_a[name].get('stats', {}))
                b = links_b[name].get('stats64', links_b[name].get('stats', {}))
                result['interface_counter_delta'][name] = {direction: counters_delta(a.get(direction, {}), b.get(direction, {}))
                                                           for direction in ('rx', 'tx')}
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
                    if record['kind'] in ('perf_data', 'drop_data', 'capture_data'):
                        name = {'perf_data': 'perf.data', 'drop_data': 'drops.data', 'capture_data': 'capture.pcap'}[record['kind']]
                        with (self.directory / name).open('ab') as perf:
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


def capture_is_empty(result):
    if result is None or result.get('exit_code') != 0 or result.get('direction') != 'out':
        return False
    # libpcap's received counter may include frames before direction filtering.
    for label in ('packets captured', 'packets dropped by kernel'):
        match = re.search(r'(\d+) ' + label, result.get('log', ''))
        if match is None or int(match[1]) != 0:
            return False
    return True


def local_link_evidence(records, interface, expected_macs, application_bytes):
    """Distinguish local macvtap capture visibility from physical NIC transmission."""
    before = next(record for record in records if record['kind'] == 'before')
    after = next(record for record in records if record['kind'] == 'after')
    for domain, mac in expected_macs.items():
        xml = before.get('domain_xml', {}).get(domain, {})
        root = ET.fromstring(xml.get('output', ''))
        matches = [nic for nic in root.findall('devices/interface')
                   if nic.find('mac') is not None and nic.find('mac').get('address', '').lower() == mac.lower()]
        if len(matches) != 1 or matches[0].get('type') != 'direct' or matches[0].find('source') is None:
            raise ValueError('Local path requires the expected direct experiment attachment')
        source = matches[0].find('source')
        if source.get('dev') != interface or source.get('mode') != 'bridge':
            raise ValueError('Local endpoints must use macvtap bridge mode on the same lower device')
    def counters(record):
        stats = record['interfaces'][interface]['-S']
        if stats['exit_code']:
            raise ValueError('Physical NIC counter coverage is required for local-path validation')
        return {key: int(value) for key, value in re.findall(r'^\s+(\S+):\s*(\d+)\s*$', stats['output'], re.M)}
    a, b = counters(before), counters(after)
    name = 'tx_bytes_phy' if 'tx_bytes_phy' in a and 'tx_bytes_phy' in b else 'tx_bytes'
    delta = b[name] - a[name]
    if application_bytes <= 0 or delta < 0:
        raise ValueError('Invalid local-path byte accounting')
    return {'verified_macvtap_bridge_domains': sorted(expected_macs), 'physical_tx_counter': name,
            'physical_tx_bytes': delta, 'estimated_application_bytes': application_bytes,
            'physical_tx_fraction_of_application': delta / application_bytes,
            'physical_tx_below_one_percent': delta < application_bytes * .01}


def verify_local_path(args, hosts, ssh_config, output):
    """Separate validation run: require full-size local VM TCP to stay off the link."""
    directory = output / 'local-path-validation'
    directory.mkdir()
    hypervisor = hosts[args.client]['hypervisor']
    config = json.loads((ROOT / 'infrastructure/config.json').read_text())
    interface = next(host['network']['source'] for host in config['hypervisors'] if host['name'] == hypervisor)
    ips = [hosts[name]['experiment_ipv4'].split('/')[0] for name in (args.client, args.server)]
    monitor = Monitor(ssh_config, 'cs740-' + hypervisor + '-jump', directory / 'capture')
    try:
        if not monitor.ready.wait(120):
            raise RuntimeError('Local-path capture monitor did not become ready')
        monitor.control(json.dumps({'capture': {'interface': interface, 'direction': 'out',
                                              'filter': f'tcp and host {ips[0]} and host {ips[1]}'}}))
        deadline = time.monotonic() + 20
        while not any(record['kind'] == 'capture_started' for record in monitor.records):
            if monitor.error or monitor.process.poll() is not None or time.monotonic() > deadline:
                raise RuntimeError('Local-path physical capture did not start')
            time.sleep(.1)
        subprocess.run([sys.executable, str(ROOT / 'experiments/run.py'), 'vm-local',
                        '--client', args.client, '--server', args.server, '--test-type', 'multi',
                        '--network', 'host', '--samples', '1', '--duration', '5', '--streams', str(args.streams),
                        '--output', str(directory / 'measurement')], check=True)
    finally:
        monitor.stop()
    result = next((record for record in monitor.records if record['kind'] == 'capture_result'), None)
    if result is None or result['exit_code'] != 0:
        raise RuntimeError('Local-path physical capture did not finish successfully')
    pods = json.loads((directory / 'measurement/test-pods.json').read_text())['items']
    expected = {args.client: ips[0], args.server: ips[1]}
    if ({pod['spec']['nodeName'] for pod in pods} != set(expected) or
            any(not pod['spec'].get('hostNetwork') or pod['status'].get('podIP') != expected.get(pod['spec']['nodeName']) for pod in pods)):
        raise RuntimeError('Local-path capture filter did not match the actual benchmark host IPs')
    rates = [value for report in (directory / 'measurement/reports').glob('*.json')
             for item in json.loads(report.read_text())['dataItems'] for key, value in item['data'].items()
             if key == 'TCP_STREAM_MULTI_host-to-host_throughput' and item['unit'] == 'Mb/s']
    if len(rates) != 1:
        raise RuntimeError('Local-path validation requires one multistream throughput report')
    # Include unrelated background traffic in the whole-invocation NIC counter.
    configured_macs = {vm['name']: vm['mac_address'] for host in config['hypervisors'] for vm in host['vms']}
    evidence = local_link_evidence(monitor.records, interface,
                {name: configured_macs[name] for name in (args.client, args.server)}, rates[0] * 1e6 / 8 * 5)
    passed = capture_is_empty(result) and evidence['physical_tx_below_one_percent']
    save_json(directory / 'validation.json', {'status': 'passed' if passed else 'failed',
              'interface': interface, 'endpoint_ips': ips, 'capture': result, 'link_evidence': evidence,
              'scope': 'Egress-only TCP capture plus live macvtap bridge attachments and whole-invocation physical TX byte counters; background traffic is included'})
    if not passed:
        raise RuntimeError('Local VM TCP egress/capture or physical TX byte validation failed; see validation.json')


def run_diagnostics(args, hosts, ssh_config):
    plan = phases(args)
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ')
    output = (args.output or ROOT / 'results' / f'{args.mode}-diagnostics-{stamp}').resolve()
    targets = {name: name for name in (args.client, args.server)}
    controller = next(name for name, host in hosts.items() if host.get('cs740_role') == 'controller')
    for host in sorted({entry['hypervisor'] for entry in hosts.values()}):
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
                ('mode', 'client', 'server', 'duration', 'samples', 'streams', 'pod_only', 'profile', 'test_type', 'network')}
    manifest.update(duration_seconds=args.duration, status='running', diagnostics=True, phases=[],
                    targets=targets, report_count=0, started_utc=datetime.now(timezone.utc).isoformat())
    save_json(output / 'metadata.json', manifest)
    try:
        if args.mode == 'vm-local':
            verify_local_path(args, hosts, ssh_config, output)
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
                for label, monitor in monitors.items():
                    monitor.control(json.dumps({'pods': [], 'require_identity': label in (args.client, args.server)}))
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
                        identity_ready = False
                        next_discovery = 0
                        try:
                            while process.poll() is None:
                                if not identity_ready and time.monotonic() >= next_discovery:
                                    next_discovery = time.monotonic() + 3
                                    metadata_path = directory / 'measurement/metadata.json'
                                    if metadata_path.exists():
                                        try:
                                            child = json.loads(metadata_path.read_text())
                                            payload = json.loads(remote(ssh_config, controller, [
                                                'sudo', '-n', 'kubectl', '--kubeconfig=/etc/kubernetes/admin.conf',
                                                '-n', child['namespace'], 'get', 'pods', '-o', 'json']))
                                            pods = [{'uid': pod['metadata']['uid'], 'name': pod['metadata']['name'],
                                                     'namespace': child['namespace'], 'node': pod['spec'].get('nodeName'),
                                                     'role': pod['metadata'].get('labels', {}).get('role'),
                                                     'host_network': pod['spec'].get('hostNetwork', False)}
                                                    for pod in payload['items'] if pod['spec'].get('nodeName')]
                                            if all(any(pod['node'] == node for pod in pods) for node in (args.client, args.server)):
                                                save_json(directory / 'pod-identities.json', pods)
                                                for label in (args.client, args.server):
                                                    monitors[label].control(json.dumps({'pods': [p for p in pods if p['node'] == label],
                                                                                       'require_identity': True}))
                                                identity_ready = True
                                        except (OSError, ValueError, KeyError, subprocess.CalledProcessError):
                                            pass  # Namespace/Pods may still be starting; retry before benchmark activity.
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
