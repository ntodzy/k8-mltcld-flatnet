#!/usr/bin/env python3
"""Run the overnight mode/queue investigation sequentially with durable progress."""
import argparse
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

from common import GENERATED, ROOT, MODES
from collect import run_logged, write_manifest


def plan():
    steps = []
    def add(name, kind, mode, **settings):
        steps.append(dict(name=name, kind=kind, mode=mode, status='pending', attempts=[], **settings))
    add('host-1450-baseline', 'measurement', 'host-vxlan-1450')
    add('host-1450-profile', 'measurement', 'host-vxlan-1450', profile=True, samples=1, test_type='multi')
    add('enable-two-queues', 'queues', 'host-vxlan-1450', queues=2)
    add('host-1450-two-queues', 'measurement', 'host-vxlan-1450', test_type='throughput')
    add('restore-one-queue', 'queues', 'host-vxlan-1450', restore=True)
    add('host-1450-restored', 'measurement', 'host-vxlan-1450', samples=3, test_type='multi')
    for mode in ('native', 'direct-1450', 'vm-local', 'vxlan', 'vm-vxlan-1450', 'vm-vxlan', 'host-vxlan'):
        add('setup-' + mode, 'setup', mode)
        add(mode + '-baseline', 'measurement', mode)
    add('setup-bridge-1450', 'setup', 'bridge-1450')
    add('bridge-1450-baseline', 'measurement', 'bridge-1450')
    return steps


def execute(directory, state):
    from queues import configure
    state.update(status='running', pid=os.getpid())
    state.pop('error', None)
    write_manifest(directory, state)
    try:
        for step in state['plan']:
            if step['status'] == 'passed':
                continue
            number = len(step['attempts']) + 1
            work = directory / f'{step["name"]}-attempt-{number}'
            work.mkdir()
            attempt = {'directory': work.name, 'status': 'running', 'started_utc': datetime.now(timezone.utc).isoformat()}
            step['attempts'].append(attempt)
            step['status'] = 'running'
            state['phase'] = step['name']
            write_manifest(directory, state)
            try:
                if step['kind'] == 'queues':
                    configure(['worker-1', 'worker-2'], step.get('queues'), step.get('restore', False), work / 'queue-audit.json')
                elif step['kind'] == 'setup':
                    run_logged([sys.executable, str(ROOT / 'experiments/setup.py'), step['mode'], '--reset'], work / 'setup.log')
                else:
                    local = step['mode'] == 'vm-local'
                    command = [sys.executable, str(ROOT / 'experiments/run.py'), step['mode'],
                               '--client', 'worker-2' if local else 'worker-1',
                               '--server', 'worker-3' if local else 'worker-2',
                               '--diagnostics', '--samples', str(step.get('samples', 5)),
                               '--duration', '30', '--streams', '4',
                               '--test-type', step.get('test_type', 'all'), '--output', str(work / 'measurement')]
                    if not local:
                        command.append('--pod-only')
                    if step.get('profile'):
                        command.append('--profile')
                    run_logged(command, work / 'measurement.log')
                    metadata = json.loads((work / 'measurement/metadata.json').read_text())
                    if metadata.get('status') != 'passed' or not metadata.get('report_count'):
                        raise RuntimeError('Measurement did not pass/report results')
                step['status'] = attempt['status'] = 'passed'
            except BaseException as error:
                step['status'] = attempt['status'] = 'failed'
                attempt['error'] = str(error)
                raise
            finally:
                attempt['finished_utc'] = datetime.now(timezone.utc).isoformat()
                write_manifest(directory, state)
        state.update(status='passed', phase='complete')
    except BaseException as error:
        state.update(status='failed', error=str(error))
        raise
    finally:
        state['finished_utc'] = datetime.now(timezone.utc).isoformat()
        write_manifest(directory, state)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--resume', type=Path)
    parser.add_argument('--background', action='store_true')
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()
    config = ROOT / 'infrastructure/config.json'
    digest = hashlib.sha256(config.read_bytes()).hexdigest()
    if args.resume:
        directory = args.resume.resolve()
        state = json.loads((directory / 'collection.json').read_text())
        if state['config_sha256'] != digest:
            raise ValueError('Deployment config changed since this study started')
    else:
        stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
        directory = (args.output or ROOT / 'results' / f'extended-study-{stamp}').resolve()
        state = {'kind': 'extended-study', 'status': 'pending', 'phase': 'pending',
                 'created_utc': datetime.now(timezone.utc).isoformat(), 'plan': plan(),
                 'config_sha256': digest,
                 'git_snapshot': subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip()}
    print(f'Study: {directory}', flush=True)
    for step in state['plan']:
        print(step['name'], step['status'])
    if args.dry_run:
        return
    with (GENERATED / 'collection.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if not args.resume:
            active = json.loads((GENERATED / 'active-experiment.json').read_text())
            if active['mode'] != 'host-vxlan-1450':
                raise ValueError('Initial mode must be host-vxlan-1450 for the baseline and queue controls')
            directory.mkdir(parents=True, exist_ok=False)
        if args.background:
            write_manifest(directory, state)
            sys.stdout.flush()
            pid = os.fork()
            if pid:
                (directory / 'pid').write_text(str(pid) + '\n')
                print(f'Started PID {pid}', flush=True)
                return
            os.setsid()
            with open(os.devnull) as source, (directory / 'collection.log').open('a') as log:
                os.dup2(source.fileno(), 0)
                os.dup2(log.fileno(), 1)
                os.dup2(log.fileno(), 2)
        execute(directory, state)


if __name__ == '__main__':
    main()
