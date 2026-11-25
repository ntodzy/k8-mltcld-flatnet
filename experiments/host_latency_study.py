#!/usr/bin/env python3
"""Matched CPU-idle controls and full workloads for host VXLAN latency research."""
import argparse
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys

from collect import run_logged, write_manifest
from common import GENERATED, ROOT, save_json
from cpu_latency import requests, close_requests
from latency_study import environment, measured


def plan(modes, samples, workloads):
    steps = []
    for mode in modes:
        steps.append(dict(name='prepare-'+mode, kind='setup', mode=mode))
        for workload in workloads:
            for pair in range(1, samples+1):
                # Equal descriptor holders, balanced AB/BA order within a boot.
                for limit in ([-1, 10] if pair % 2 else [10, -1]):
                    policy = 'default' if limit == -1 else '10us'
                    steps.append(dict(name=f'{mode}-{workload}-pair-{pair}-{policy}',
                                      kind='measurement', mode=mode, pair=pair,
                                      workload=workload, cpu_latency_us=limit))
    steps.append(dict(name='restore-original-host-mode', kind='setup', mode='host-vxlan-1450'))
    return [dict(step, status='pending', attempts=[]) for step in steps]


def execute(directory, state):
    state.update(status='running', pid=os.getpid())
    state.pop('error', None)
    try:
        for step in state['plan']:
            if step['status'] == 'passed':
                continue
            work = directory / f'{step["name"]}-attempt-{len(step["attempts"])+1}'
            work.mkdir()
            attempt = dict(directory=work.name, status='running', started_utc=datetime.now(timezone.utc).isoformat())
            step['attempts'].append(attempt)
            step['status'] = 'running'
            state['phase'] = step['name']
            write_manifest(directory, state)
            try:
                if step['kind'] == 'setup':
                    active = GENERATED/'active-experiment.json'
                    reuse = (step == state['plan'][0] and active.exists()
                             and json.loads(active.read_text())['mode'] == step['mode'])
                    if not reuse:
                        run_logged([sys.executable, str(ROOT/'experiments/setup.py'), step['mode'], '--reset'], work/'setup.log')
                    state.setdefault('environments', {})[step['mode']] = environment()
                    attempt['reused_setup'] = reuse
                else:
                    current = environment()
                    save_json(work/'environment.json', current)
                    if current != state['environments'][step['mode']]:
                        raise RuntimeError('Boot, kernel, queues, NIC or affinity changed within mode block')
                    command = [sys.executable, str(ROOT/'experiments/run.py'), step['mode'],
                               '--client', 'worker-1', '--server', 'worker-2', '--pod-only',
                               '--diagnostics', '--test-type', step['workload'], '--samples', '1',
                               '--duration', str(state['duration']), '--output', str(work/'measurement')]
                    holders = requests(step['cpu_latency_us'], work/'cpu-latency')
                    try:
                        measured(command, work, step['mode'], False)
                        # Refuse data if the bounded holder expired during startup/testing.
                        if any(h.process.poll() is not None for h in holders):
                            raise RuntimeError('CPU policy holder exited before benchmark finished')
                    finally:
                        close_requests(holders)
                    if json.loads((work/'measurement/metadata.json').read_text())['status'] != 'passed':
                        raise RuntimeError('Measurement did not pass')
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
    from common import ALL_MODES
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--modes', nargs='+', choices=ALL_MODES, default=['host-vxlan-1450'])
    parser.add_argument('--samples', type=int, default=3)
    parser.add_argument('--duration', type=int, default=20)
    parser.add_argument('--workloads', nargs='+', choices=['rr', 'single', 'multi'], default=['rr'])
    parser.add_argument('--output', type=Path)
    parser.add_argument('--resume', type=Path)
    parser.add_argument('--background', action='store_true')
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()
    if args.samples < 3 or not 15 <= args.duration <= 120:
        parser.error('Use at least three samples and 15–120 seconds per sample')
    if len(set(args.modes)) != len(args.modes):
        parser.error('Mode names must be unique within a collection')
    digest = hashlib.sha256((ROOT/'infrastructure/config.json').read_bytes()).hexdigest()
    if args.resume:
        directory = args.resume.resolve()
        state = json.loads((directory/'collection.json').read_text())
        if state['study_type'] != 'host-latency-optimization' or state['config_sha256'] != digest:
            raise ValueError('Study type or base configuration changed')
    else:
        directory = (args.output or ROOT/'results'/('host-latency-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ'))).resolve()
        state = dict(kind='extended-study', study_type='host-latency-optimization',
                     status='pending', phase='pending', plan=plan(args.modes, args.samples, args.workloads),
                     duration=args.duration, config_sha256=digest,
                     git_snapshot=subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip(),
                     created_utc=datetime.now(timezone.utc).isoformat())
    print('Study:', directory, flush=True)
    if args.dry_run:
        print(json.dumps(state, indent=2)); return
    with (GENERATED/'collection.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX|fcntl.LOCK_NB)
        if any((GENERATED/name).exists() for name in ('path-control.json','forwarding-control.json')):
            raise ValueError('Restore active path/forwarding controls first')
        if not args.resume:
            directory.mkdir(parents=True, exist_ok=False)
        if args.background:
            write_manifest(directory,state)
            sys.stdout.flush()
            pid = os.fork()
            if pid:
                (directory/'pid').write_text(str(pid)+'\n')
                print('Started PID',pid,flush=True); return
            os.setsid()
            with (directory/'collection.log').open('a') as log:
                os.dup2(log.fileno(),1); os.dup2(log.fileno(),2)
                signal.signal(signal.SIGTERM, lambda *_: sys.exit('Study terminated'))
                execute(directory,state)
        else:
            execute(directory,state)


if __name__ == '__main__':
    main()
