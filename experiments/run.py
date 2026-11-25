#!/usr/bin/env python3
"""Run pinned Cilium CLI performance tests through the existing controller SSH alias."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import shlex
import subprocess
import tarfile
import uuid

from common import GENERATED, MODES, ALL_MODES, ROOT, VM_VXLAN_MODES, guests, remote, save_json, ssh_command, mtu_profile

KUBECTL = ['sudo', '-n', 'kubectl', '--kubeconfig=/etc/kubernetes/admin.conf']
CILIUM = ['sudo', '-n', '/usr/local/bin/cilium', '--kubeconfig=/etc/kubernetes/admin.conf']


def select_pair(hosts, mode, client, server):
    for name in (client, server):
        if name not in hosts or hosts[name]['cs740_role'] != 'worker':
            raise ValueError(f'{name} must be a worker in the generated inventory')
    if client == server:
        raise ValueError('Select two different VMs; same-node means one VM in Cilium')
    same_host = hosts[client]['hypervisor'] == hosts[server]['hypervisor']
    if mode == 'vm-local' and not same_host:
        raise ValueError('vm-local requires two VMs on the same hypervisor')
    if mode != 'vm-local' and same_host:
        raise ValueError('This experiment requires VMs on different hypervisors; use vm-local for the local baseline')


def perf_arguments(args, namespace, report_dir, client_hostname, server_hostname):
    test = getattr(args, 'test_type', 'all')
    network = getattr(args, 'network', 'both')
    return [
        'connectivity', 'perf', '--duration', f'{args.duration}s',
        '--samples', str(args.samples), '--streams', str(args.streams),
        '--node-selector-client', f'kubernetes.io/hostname={client_hostname}',
        '--node-selector-server', f'kubernetes.io/hostname={server_hostname}',
        '--same-node=' + str(args.same_node).lower(), '--other-node=true',
        '--host-net=' + str(not args.pod_only and network != 'pod').lower(),
        '--pod-net=' + str(args.mode != 'vm-local' and network != 'host').lower(),
        '--rr=' + str(test in ('all', 'rr')).lower(),
        '--throughput=' + str(test in ('all', 'throughput', 'single')).lower(),
        '--throughput-multi=' + str(test in ('all', 'throughput', 'multi')).lower(),
        '--test-namespace', namespace,
        '--namespace-labels', 'pod-security.kubernetes.io/enforce=privileged',
        '--report-dir', report_dir,
    ]


def cpu_counts(text):
    values = next(line for line in text.splitlines() if line.startswith('cpu ')).split()[1:]
    # guest and guest_nice are already included in user/nice, so exclude them.
    ticks = [int(value) for value in values[:8]]
    return {'total': sum(ticks), 'idle': ticks[3] + ticks[4]}


def cpu_utilization(before, after):
    total = after['total'] - before['total']
    idle = after['idle'] - before['idle']
    return round(100 * (total - idle) / total, 3) if total > 0 else None


def machine_snapshot(config, target):
    script = '''import json, pathlib, subprocess
print(json.dumps({"cpu": pathlib.Path("/proc/stat").read_text(),
 "links": subprocess.check_output(["ip", "-j", "-d", "link", "show"], text=True),
 "routes": subprocess.check_output(["ip", "-j", "-4", "route", "show"], text=True)}))'''
    return json.loads(remote(config, target, ['python3', '-c', script]))


def unpack_reports(archive, destination):
    """Copy regular JSON files only; never extract links or arbitrary tar paths."""
    count = 0
    with tarfile.open(archive, 'r:gz') as reports:
        for member in reports:
            name = Path(member.name)
            if not member.isfile() or name.suffix != '.json':
                continue
            if name.is_absolute() or '..' in name.parts:
                raise ValueError(f'Unsafe report path: {member.name}')
            output = destination / name
            output.parent.mkdir(parents=True, exist_ok=True)
            data = reports.extractfile(member).read()
            json.loads(data)  # Refuse to label invalid output as a JSON report.
            output.write_bytes(data)
            count += 1
    return count


def positive(value):
    result = int(value)
    if result < 1:
        raise argparse.ArgumentTypeError('must be a positive integer')
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=ALL_MODES)
    parser.add_argument('--client', required=True)
    parser.add_argument('--server', required=True)
    parser.add_argument('--duration', type=positive, default=30, help='Seconds per test/sample (default: 30)')
    parser.add_argument('--samples', type=positive, default=5)
    parser.add_argument('--streams', type=positive, default=4)
    parser.add_argument('--pod-only', action='store_true', help='Measure Pod traffic only, excluding host-network controls')
    parser.add_argument('--same-node', action='store_true', help='Also measure the single-VM path on the server VM')
    parser.add_argument('--output', type=Path, help='New result directory; defaults to results/MODE-UTC_TIMESTAMP')
    parser.add_argument('--dry-run', action='store_true', help='Validate placement and print the command without SSH')
    parser.add_argument('--test-type', choices=('all', 'rr', 'single', 'multi', 'throughput'), default='all')
    parser.add_argument('--network', choices=('both', 'pod', 'host'), default='both')
    parser.add_argument('--diagnostics', action='store_true', help='Separate every sample/path/test and monitor endpoint VMs and hypervisors')
    parser.add_argument('--profile', action='store_true', help='Separate diagnostic run with system-wide perf profiling; requires --diagnostics')
    args = parser.parse_args()
    if args.pod_only and args.mode == 'vm-local':
        parser.error('vm-local is a host-network baseline and cannot use --pod-only')
    if args.pod_only and args.network == 'host':
        parser.error('--pod-only cannot use --network host')
    if args.mode == 'vm-local' and args.network == 'pod':
        parser.error('vm-local cannot use --network pod')
    if args.profile and not args.diagnostics:
        parser.error('--profile requires --diagnostics; use a separate run from final measurements')
    if args.diagnostics and args.same_node:
        parser.error('--diagnostics requires cross-VM tests; omit --same-node')
    hosts = guests(GENERATED / 'guest_inventory.json')
    select_pair(hosts, args.mode, args.client, args.server)
    controllers = [name for name, host in hosts.items() if host['cs740_role'] == 'controller']
    if len(controllers) != 1:
        raise ValueError('Inventory must contain exactly one controller')
    controller = controllers[0]
    ssh_config = GENERATED / 'ssh_config'
    if not ssh_config.is_file():
        raise ValueError('Existing generated SSH config is missing; deploy the VMs first')
    namespace = 'cs740-perf-' + uuid.uuid4().hex[:10]
    if args.diagnostics:
        from diagnostics import run_diagnostics
        run_diagnostics(args, hosts, ssh_config)
        return
    if args.dry_run:
        command = CILIUM + perf_arguments(args, namespace, '/tmp/REPORTS', args.client, args.server)
        print(shlex.join(ssh_command(ssh_config, controller, command)))
        return
    active = json.loads((GENERATED / 'active-experiment.json').read_text())
    if active['mode'] != args.mode:
        raise ValueError(f"Prepared mode is {active['mode']}; run experiments/setup.py {args.mode} --reset first")
    timestamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ')
    output = (args.output or ROOT / 'results' / f'{args.mode}-{timestamp}').resolve()
    output.mkdir(parents=True, exist_ok=False)
    metadata = {
        'mode': args.mode, 'started_utc': timestamp, 'controller': controller,
        'client': args.client, 'server': args.server,
        'placement': {name: {key: hosts[name][key] for key in ('hypervisor', 'experiment_ipv4')}
                      for name in (args.client, args.server)},
        'duration_seconds': args.duration, 'samples': args.samples, 'streams': args.streams,
        'same_node': args.same_node, 'pod_only': args.pod_only, 'namespace': namespace + '-1',
        'test_type': args.test_type, 'network': args.network,
        'setup': active, 'mtu_profile': mtu_profile(args.mode), 'status': 'running',
    }
    save_json(output / 'metadata.json', metadata)
    remote_dir = None
    before = {}
    try:
        nodes = json.loads(remote(ssh_config, controller, KUBECTL + ['get', 'nodes', '-o', 'json']))
        save_json(output / 'nodes-before.json', nodes)
        node_map = {node['metadata']['name']: node for node in nodes['items']}
        for name in (args.client, args.server):
            conditions = node_map[name]['status']['conditions']
            if not any(c['type'] == 'Ready' and c['status'] == 'True' for c in conditions):
                raise ValueError(f'{name} is not Ready')
        config = json.loads(remote(ssh_config, controller, KUBECTL + [
            '-n', 'kube-system', 'get', 'configmap', 'cilium-config', '-o', 'json']))
        save_json(output / 'cilium-config.json', config)
        data = config['data']
        expected_routing = 'tunnel' if args.mode == 'vxlan' else 'native'
        expected_direct = 'false' if args.mode == 'vxlan' or args.mode in VM_VXLAN_MODES else 'true'
        expected_mtu = mtu_profile(args.mode)['cilium_mtu']
        if (data.get('routing-mode') != expected_routing or
                data.get('auto-direct-node-routes') != expected_direct or
                int(data.get('mtu', 0)) != expected_mtu):
            raise ValueError('Live Cilium routing/MTU does not match the requested experiment')
        (output / 'cilium-version.txt').write_text(remote(ssh_config, controller, CILIUM + ['version']))
        (output / 'cilium-status.txt').write_text(remote(ssh_config, controller, CILIUM + ['status', '--wait']))
        targets = {name: name for name in (args.client, args.server)}
        for name in (args.client, args.server):
            host = hosts[name]['hypervisor']
            targets['hypervisor-' + host] = 'cs740-' + host + '-jump'
        with ThreadPoolExecutor(max_workers=len(targets)) as pool:
            snapshots = list(pool.map(lambda item: (item[0], machine_snapshot(ssh_config, item[1])), targets.items()))
        for label, snapshot in snapshots:
            before[label] = cpu_counts(snapshot['cpu'])
            (output / f'{label}-links.json').write_text(snapshot['links'])
            (output / f'{label}-routes.json').write_text(snapshot['routes'])
        remote_dir = remote(ssh_config, controller, ['mktemp', '-d', '/tmp/cs740-perf.XXXXXXXX']).strip()
        metadata['remote_report_directory'] = remote_dir
        if not re.fullmatch(r'/tmp/cs740-perf\.[A-Za-z0-9]+', remote_dir):
            raise ValueError('Unexpected remote report directory')
        command = ['sudo', '-n', 'flock', '-n', '/run/lock/cs740-performance.lock',
                   '/usr/local/bin/cilium', '--kubeconfig=/etc/kubernetes/admin.conf']
        command += perf_arguments(args, namespace, remote_dir,
                                  node_map[args.client]['metadata']['labels']['kubernetes.io/hostname'],
                                  node_map[args.server]['metadata']['labels']['kubernetes.io/hostname'])
        metadata['command'] = shlex.join(command)
        save_json(output / 'metadata.json', metadata)
        print(f'Results: {output}\nRunning {args.samples} samples of each test ({args.duration}s per sample).', flush=True)
        with (output / 'console.log').open('w') as log:
            with subprocess.Popen(ssh_command(ssh_config, controller, command), text=True,
                                  stdout=subprocess.PIPE, stderr=subprocess.STDOUT) as process:
                for line in process.stdout:
                    print(line, end='', flush=True)
                    log.write(line)
                    log.flush()
                metadata['exit_code'] = process.wait()
        # Save performance reports before collecting optional diagnostic snapshots.
        with (output / 'reports.tar.gz').open('wb') as archive:
            subprocess.run(ssh_command(ssh_config, controller,
                                       ['sudo', '-n', 'tar', '-C', remote_dir, '-czf', '-', '.']),
                           stdout=archive, check=True)
        metadata['report_count'] = unpack_reports(output / 'reports.tar.gz', output / 'reports')
        cpu = {}
        for label, target in targets.items():
            try:
                after = cpu_counts(remote(ssh_config, target, ['cat', '/proc/stat']))
                cpu[label] = {'before': before[label], 'after': after,
                              'busy_percent': cpu_utilization(before[label], after)}
            except (OSError, subprocess.CalledProcessError) as error:
                metadata.setdefault('diagnostic_errors', []).append(str(error))
        save_json(output / 'cpu-whole-run.json', cpu)
        try:
            pods = remote(ssh_config, controller, KUBECTL + ['-n', namespace + '-1', 'get', 'pods', '-o', 'json'])
            (output / 'test-pods.json').write_text(pods)
        except (OSError, subprocess.CalledProcessError) as error:
            metadata.setdefault('diagnostic_errors', []).append(str(error))
        if metadata['exit_code']:
            raise ValueError(f"Cilium perf exited with status {metadata['exit_code']}; see console.log")
        if not metadata['report_count']:
            raise ValueError('Cilium produced no JSON reports; see console.log')
        metadata['status'] = 'passed'
    except BaseException as error:
        metadata['status'] = 'failed'
        metadata['error'] = str(error)
        raise
    finally:
        # Remove only this invocation's namespace, never cilium-test or another run.
        # Completed netperf workloads have no persistent state; shorten their
        # termination grace instead of spending 30 seconds per diagnostic phase.
        try:
            remote(ssh_config, controller, KUBECTL + [
                '-n', namespace + '-1', 'delete', 'pods', '--all', '--ignore-not-found',
                '--grace-period=1', '--wait=false'])
        except (OSError, subprocess.CalledProcessError) as error:
            metadata.setdefault('cleanup_errors', []).append(str(error))
        try:
            remote(ssh_config, controller, KUBECTL + [
                'delete', 'namespace', namespace + '-1', '--ignore-not-found', '--wait=true', '--timeout=120s'])
        except (OSError, subprocess.CalledProcessError) as error:
            metadata.setdefault('cleanup_errors', []).append(str(error))
        if remote_dir and 'report_count' in metadata and re.fullmatch(r'/tmp/cs740-perf\.[A-Za-z0-9]+', remote_dir):
            try:
                remote(ssh_config, controller, ['sudo', '-n', 'rm', '-rf', '--', remote_dir])
            except (OSError, subprocess.CalledProcessError) as error:
                metadata.setdefault('cleanup_errors', []).append(str(error))
        metadata['finished_utc'] = datetime.now(timezone.utc).isoformat()
        save_json(output / 'metadata.json', metadata)
    print(f'Saved {metadata["report_count"]} JSON reports to {output / "reports"}')


if __name__ == '__main__':
    try:
        main()
    except (ValueError, KeyError, RuntimeError, OSError, subprocess.CalledProcessError) as error:
        raise SystemExit(f'Experiment failed: {error}')
