#!/usr/bin/env python3
"""Reset, configure, and collect Cilium performance data across experiment modes."""
import argparse
import csv
from datetime import datetime, timezone
import fcntl
import itertools
import hashlib
import json
import os
from pathlib import Path
import shlex
import signal
import subprocess
import sys

from common import GENERATED, MODES, ROOT, mtu_profile, save_json
from run import positive, select_pair
from setup import deployment_config


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def write_manifest(directory, manifest):
    manifest['updated_utc'] = utc_now()
    temporary = directory / 'collection.json.tmp'
    temporary.write_text(json.dumps(manifest, indent=2) + '\n')
    temporary.replace(directory / 'collection.json')


def pairs(config, modes, client=None, server=None, local_client=None, local_server=None):
    hosts = {vm['name']: {'cs740_role': vm['role'], 'hypervisor': host['name']}
             for host in config['hypervisors'] for vm in host['vms']}
    workers = sorted(name for name, vm in hosts.items() if vm['cs740_role'] == 'worker')
    combinations = list(itertools.combinations(workers, 2))
    result = {}
    for local, names in ((False, (client, server)), (True, (local_client, local_server))):
        relevant = 'vm-local' in modes if local else any(mode != 'vm-local' for mode in modes)
        if not relevant:
            continue
        if any(names) and not all(names):
            raise ValueError('Specify both client and server for each pair')
        if not all(names):
            names = next((pair for pair in combinations
                          if (hosts[pair[0]]['hypervisor'] == hosts[pair[1]]['hypervisor']) == local), None)
            if names is None:
                raise ValueError('No suitable ' + ('same-hypervisor' if local else 'cross-hypervisor') + ' worker pair')
        select_pair(hosts, 'vm-local' if local else 'native', *names)
        result['local' if local else 'cross'] = list(names)
    return result


def build_plan(modes, rounds, endpoints):
    plan = []
    for index in range(rounds):
        # Reverse alternating rounds to reduce systematic ordering bias.
        order = modes if index % 2 == 0 else list(reversed(modes))
        for mode in order:
            client, server = endpoints['local' if mode == 'vm-local' else 'cross']
            plan.append({'round': index + 1, 'mode': mode, 'client': client,
                         'server': server, 'status': 'pending', 'attempts': []})
    return plan


def run_logged(command, log_path):
    print('+ ' + shlex.join(command), flush=True)
    with log_path.open('a') as log:
        log.write('\n+ ' + shlex.join(command) + '\n')
        log.flush()
        with subprocess.Popen(command, cwd=ROOT, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, text=True, start_new_session=True) as process:
            try:
                for line in process.stdout:
                    log.write(line)
                    log.flush()
                    print(line, end='', flush=True)
                code = process.wait()
            except BaseException:
                # Stop the active child tree before releasing the collection lock.
                if process.poll() is None:
                    try:
                        os.killpg(process.pid, signal.SIGTERM)
                    except ProcessLookupError:
                        pass
                try:
                    process.wait(timeout=20)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait()
                raise
    if code:
        raise RuntimeError(f'Command exited with status {code}; see {log_path}')


def summarize(directory, manifest):
    """Export original numeric report values without inventing sample statistics."""
    columns = ['round', 'mode', 'client', 'server', 'metric', 'value', 'unit',
               'labels', 'report', 'data_item']
    with (directory / 'metrics.csv').open('w', newline='') as output:
        writer = csv.DictWriter(output, fieldnames=columns)
        writer.writeheader()
        for step in manifest['plan']:
            if step['status'] != 'passed':
                continue
            run_directory = directory / step['attempts'][-1]['directory'] / 'measurement'
            for report in sorted((run_directory / 'reports').rglob('*.json')):
                payload = json.loads(report.read_text())
                for index, item in enumerate(payload.get('dataItems', [])):
                    for metric, value in item.get('data', {}).items():
                        if isinstance(value, bool) or not isinstance(value, (int, float)):
                            continue
                        writer.writerow({
                            **{key: step[key] for key in ('round', 'mode', 'client', 'server')},
                            'metric': metric, 'value': value, 'unit': item.get('unit', ''),
                            'labels': json.dumps(item.get('labels', {}), sort_keys=True),
                            'report': str(report.relative_to(directory)), 'data_item': index,
                        })


def current_setup(manifest):
    """Check local successful-setup evidence; run.py still validates the live cluster."""
    pending = next((step for step in manifest['plan'] if step['status'] != 'passed'), None)
    if pending is None:
        raise ValueError('Collection has no unfinished measurements to resume')
    if not manifest.get('host_preparation_passed'):
        raise ValueError('Cannot reuse setup before the collection host preparation has succeeded')
    path = GENERATED / 'active-experiment.json'
    if not path.is_file():
        raise ValueError('No successful current setup; run setup.py for the next mode first')
    active = json.loads(path.read_text())
    mode = pending['mode']
    cluster_mode = 'native' if mode == 'vm-local' else mode
    expected = {key: value for key, value in mtu_profile(mode).items() if key != 'cilium_mtu'}
    expected.update(mode=mode, cluster_mode=cluster_mode)
    if any(active.get(key) != value for key, value in expected.items()):
        raise ValueError(f'Current successful setup does not match next mode {mode} and its MTUs; run setup.py {mode} first')
    return active


def execute_collection(directory, manifest, reuse_setup=False):
    # Reject a mismatched reuse request before changing status or creating an attempt.
    reused = current_setup(manifest) if reuse_setup else None
    parameters = manifest['parameters']
    manifest['status'] = 'running'
    manifest.pop('error', None)
    manifest.pop('finished_utc', None)
    manifest['pid'] = os.getpid()
    write_manifest(directory, manifest)
    try:
        if not manifest.get('host_preparation_passed'):
            manifest['phase'] = 'host-preparation'
            write_manifest(directory, manifest)
            run_logged([sys.executable, str(ROOT / 'experiments/hypervisors.py'),
                        '--config', parameters['config'], '--profile', parameters['host_profile']],
                       directory / 'host-preparation.log')
            manifest['host_preparation_passed'] = True
            write_manifest(directory, manifest)
        for index, step in enumerate(manifest['plan']):
            if step['status'] == 'passed':
                continue
            attempt = {'directory': f'{index + 1:02d}-{step["mode"]}-attempt-{len(step["attempts"]) + 1}',
                       'started_utc': utc_now(), 'status': 'running'}
            step['attempts'].append(attempt)
            step['status'] = 'running'
            work = directory / attempt['directory']
            work.mkdir()
            manifest['current_step'] = index
            manifest['phase'] = 'setup-' + step['mode']
            write_manifest(directory, manifest)
            try:
                if reused is not None:
                    attempt['setup_source'] = 'current-successful-setup'
                    save_json(work / 'reused-setup.json', reused)
                    (work / 'setup.log').write_text(
                        'Reused successful current setup by explicit --use-current-setup request.\n'
                        'No reset or setup was run for this attempt. Live measurement validation still applies.\n')
                    print(f'Reusing current setup for {step["mode"]}; skipping reset/setup.', flush=True)
                    # Reuse applies only to the first unfinished step. Later modes rebuild normally.
                    reused = None
                else:
                    run_logged([sys.executable, str(ROOT / 'experiments/setup.py'), step['mode'],
                                '--config', parameters['config'], '--reset',
                                '--underlay-subnet', parameters['underlay_subnet']], work / 'setup.log')
                # Record physical NIC capabilities/effective offloads for every mode.
                manifest['phase'] = 'audit-' + step['mode']
                write_manifest(directory, manifest)
                run_logged([sys.executable, str(ROOT / 'experiments/hypervisors.py'),
                            '--config', parameters['config'], '--audit'], work / 'host-audit.log')
                import shutil
                shutil.copytree(GENERATED / 'hypervisor-reports', work / 'host-network')
                manifest['phase'] = 'measure-' + step['mode']
                write_manifest(directory, manifest)
                measurement = [sys.executable, str(ROOT / 'experiments/run.py'), step['mode'],
                            '--client', step['client'], '--server', step['server'],
                            '--duration', str(parameters['duration']), '--samples', str(parameters['samples']),
                            '--streams', str(parameters['streams']), '--output', str(work / 'measurement')]
                if parameters.get('pod_only', False):
                    measurement.append('--pod-only')
                if parameters.get('diagnostics', False):
                    measurement.append('--diagnostics')
                run_logged(measurement, work / 'measurement.log')
                result = json.loads((work / 'measurement/metadata.json').read_text())
                if (result.get('status') != 'passed' or not result.get('report_count') or
                        result.get('mode') != step['mode'] or
                        any(result.get(key) != step[key] for key in ('client', 'server')) or
                        result.get('samples') != parameters['samples'] or
                        result.get('duration_seconds') != parameters['duration'] or
                        result.get('streams') != parameters['streams'] or
                        result.get('diagnostics', False) != parameters.get('diagnostics', False) or
                        result.get('pod_only', False) != parameters.get('pod_only', False)):
                    raise RuntimeError('Measurement metadata does not describe a successful run')
                step['status'] = attempt['status'] = 'passed'
            except BaseException as error:
                step['status'] = attempt['status'] = 'failed'
                attempt['error'] = str(error)
                raise
            finally:
                attempt['finished_utc'] = utc_now()
                write_manifest(directory, manifest)
                summarize(directory, manifest)
        manifest['status'] = 'passed'
        manifest['phase'] = 'complete'
    except BaseException as error:
        manifest['status'] = 'interrupted' if isinstance(error, KeyboardInterrupt) else 'failed'
        manifest['error'] = str(error) or type(error).__name__
        raise
    finally:
        manifest['finished_utc'] = utc_now()
        write_manifest(directory, manifest)
        summarize(directory, manifest)


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, default=ROOT / 'infrastructure/config.json')
    parser.add_argument('--modes', nargs='+', choices=MODES, default=list(MODES))
    parser.add_argument('--client')
    parser.add_argument('--server')
    parser.add_argument('--local-client')
    parser.add_argument('--local-server')
    parser.add_argument('--duration', type=positive, default=30)
    parser.add_argument('--samples', type=positive, default=5)
    parser.add_argument('--streams', type=positive, default=4)
    parser.add_argument('--pod-only', action='store_true', help='Exclude host-network controls; requires modes excluding vm-local')
    parser.add_argument('--diagnostics', action='store_true', help='Isolate each sample/test/path and collect VM/hypervisor diagnostics')
    parser.add_argument('--rounds', type=positive, default=1)
    parser.add_argument('--host-profile', choices=('baseline', 'optimized'), default='baseline')
    parser.add_argument('--underlay-subnet', default='10.75.0.0/24')
    parser.add_argument('--output', type=Path, help='New collection directory under which every run is saved')
    parser.add_argument('--resume', type=Path, help='Resume a collection using its saved plan and parameters')
    parser.add_argument('--use-current-setup', action='store_true',
                        help='With --resume, reuse the successful current setup for only the first unfinished mode')
    parser.add_argument('--background', action='store_true', help='Detach collection and write output to collection.log')
    parser.add_argument('--dry-run', action='store_true', help='Print the plan without SSH or file changes')
    return parser.parse_args()


def main():
    args = parse_args()
    if args.resume:
        # The saved plan is authoritative; avoid silently ignoring parameter changes.
        if any(arg.split('=')[0] not in ('--resume', '--dry-run', '--background', '--use-current-setup') for arg in sys.argv[1:] if arg.startswith('--')):
            raise ValueError('--resume may only be combined with --dry-run/--background/--use-current-setup; parameters come from collection.json')
        directory = args.resume.expanduser().resolve()
        manifest = json.loads((directory / 'collection.json').read_text())
        current_digest = hashlib.sha256(Path(manifest['parameters']['config']).read_bytes()).hexdigest()
        if current_digest != manifest['config_sha256']:
            raise ValueError('Base deployment config changed; start a new collection to preserve comparability')
    else:
        if args.use_current_setup:
            raise ValueError('--use-current-setup requires --resume')
        if args.pod_only and 'vm-local' in args.modes:
            raise ValueError('--pod-only cannot include vm-local; select Pod experiments using --modes')
        config_path = args.config.expanduser().resolve()
        source = json.loads(config_path.read_text())
        deployment_config(source, args.modes[0], config_path.parent)
        endpoints = pairs(source, args.modes, args.client, args.server, args.local_client, args.local_server)
        stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ')
        directory = (args.output or ROOT / 'results' / ('collection-' + stamp)).expanduser().resolve()
        manifest = {'schema_version': 1, 'status': 'pending', 'created_utc': utc_now(),
                    'config_sha256': hashlib.sha256(config_path.read_bytes()).hexdigest(),
                    'parameters': {'config': str(config_path), 'duration': args.duration,
                                   'samples': args.samples, 'streams': args.streams, 'rounds': args.rounds,
                                   'host_profile': args.host_profile, 'underlay_subnet': args.underlay_subnet,
                                   'pod_only': args.pod_only, 'diagnostics': args.diagnostics},
                    'plan': build_plan(args.modes, args.rounds, endpoints)}
    print(f'Collection: {directory}', flush=True)
    for step in manifest['plan']:
        print(f'Round {step["round"]}: {step["mode"]}: {step["client"]} -> {step["server"]} [{step["status"]}]')
    if args.dry_run:
        if args.use_current_setup:
            active = current_setup(manifest)
            print(f'Reuse current setup for {active["mode"]} only; later modes reset/setup normally. No commands executed.')
        return
    GENERATED.mkdir(parents=True, exist_ok=True)
    with (GENERATED / 'collection.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError('Another collection/comparison is active; wait until it finishes')
        if args.use_current_setup:
            current_setup(manifest)  # Fail in the foreground before detaching.
        if not args.resume:
            directory.mkdir(parents=True, exist_ok=False)
        if args.background:
            write_manifest(directory, manifest)
            sys.stdout.flush()
            sys.stderr.flush()
            pid = os.fork()
            if pid:
                (directory / 'pid').write_text(str(pid) + '\n')
                print(f'Started PID {pid}. Progress: {directory / "collection.log"}')
                print('Watch until finished: ' + shlex.join([str(ROOT / 'experiments/status.py'), '--directory', str(directory), '--watch']))
                return
            os.setsid()
            with open(os.devnull, 'r') as source, (directory / 'collection.log').open('a') as log:
                os.dup2(source.fileno(), 0)
                os.dup2(log.fileno(), 1)
                os.dup2(log.fileno(), 2)
            # The child retains this file description's lock after its parent exits.
        if args.use_current_setup:
            execute_collection(directory, manifest, reuse_setup=True)
        else:
            execute_collection(directory, manifest)
    print(f'Collection passed. Metrics: {directory / "metrics.csv"}')


def handle_termination(_signum, _frame):
    raise KeyboardInterrupt


if __name__ == '__main__':
    signal.signal(signal.SIGTERM, handle_termination)
    try:
        main()
    except KeyboardInterrupt:
        raise SystemExit('Collection interrupted; resume using the saved results directory')
    except (ValueError, KeyError, OSError, RuntimeError, subprocess.CalledProcessError) as error:
        raise SystemExit(f'Collection failed: {error}')
