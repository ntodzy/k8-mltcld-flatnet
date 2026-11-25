#!/usr/bin/env python3
"""Paired tunnel/bypass latency controls and separate kernel timing runs."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import random
import signal
import subprocess
import sys
import time

from collect import run_logged, write_manifest
from common import GENERATED, ROOT, guests, remote, save_json
from latency_trace import TraceSession
from path_control import MARKER, switch


def plan(pairs=5, seed=20261125):
    steps = []
    for mode in ('host-vxlan-1450', 'vm-vxlan-1450'):
        steps.append(dict(name='prepare-' + mode, kind='setup', mode=mode))
        orders = [['tunnel', 'bypass'] if index % 2 == 0 else ['bypass', 'tunnel'] for index in range(pairs)]
        random.Random(seed).shuffle(orders)
        for pair, order in enumerate(orders, 1):
            for condition in order:
                steps.append(dict(name=f'{mode}-pair-{pair}-{condition}', kind='measurement',
                                  mode=mode, pair=pair, condition=condition))
        for condition in ('tunnel', 'bypass'):
            steps.append(dict(name=f'{mode}-{condition}-trace', kind='measurement',
                              mode=mode, condition=condition, trace=True))
    steps.extend([dict(name='prepare-vxlan-reference', kind='setup', mode='vxlan'),
                  dict(name='vxlan-reference', kind='measurement', mode='vxlan', samples=5),
                  dict(name='vxlan-reference-trace', kind='measurement', mode='vxlan', trace=True),
                  dict(name='restore-original-host-mode', kind='setup', mode='host-vxlan-1450')])
    return [dict(step, status='pending', attempts=[]) for step in steps]


def targets():
    inventory = guests(GENERATED / 'guest_inventory.json')
    config = json.loads((ROOT / 'infrastructure/config.json').read_text())
    result = {name: (name, None, 'experiment') for name in ('worker-1', 'worker-2')}
    for name in ('worker-1', 'worker-2'):
        host = next(h for h in config['hypervisors'] if h['name'] == inventory[name]['hypervisor'])
        result['hypervisor-' + host['name']] = ('cs740-' + host['name'] + '-jump', name, host['network']['source'])
    return result


def environment():
    script = '''import json,os,subprocess
from pathlib import Path
role=__import__('sys').argv[1]
d={'kernel':os.uname().release,'boot_id':Path('/proc/sys/kernel/random/boot_id').read_text().strip()}
if not role:
 d['channels']=subprocess.check_output(['ethtool','-l','experiment'],text=True)
 d['interface']={k:v for k,v in __import__('json').loads(subprocess.check_output(['ip','-j','link','show','experiment'],text=True))[0].items() if k in ('ifindex','address','mtu')}
else:
 q=int(Path('/run/libvirt/qemu/'+role+'.pid').read_text());d['worker_qemu']=q;d['affinity']={}
 for task in Path('/proc',str(q),'task').iterdir():
  name=(task/'comm').read_text().strip()
  if name.startswith(('CPU ','vhost-')):d['affinity'][task.name]={'comm':name,'cpus':sorted(os.sched_getaffinity(int(task.name)))}
print(json.dumps(d))
'''
    def read(item):
        label, (target, domain, _) = item
        return label, json.loads(remote(GENERATED / 'ssh_config', target,
                            ['sudo', '-n', 'python3', '-c', script, domain or '']))
    with ThreadPoolExecutor(max_workers=4) as pool:
        return dict(pool.map(read, targets().items()))


def restore_path(directory):
    if MARKER.exists():
        marker = json.loads(MARKER.read_text())
        output = directory / ('path-restoration-' + datetime.now(timezone.utc).strftime('%H%M%S.%f') + '.json')
        switch(marker['mode'], 'tunnel', output)
        MARKER.unlink()


def measured(command, work, mode, trace):
    sessions = {}
    process = None
    armed = False
    try:
        if trace:
            for label, (target, domain, interface) in targets().items():
                tunnel = 'cs740hvx' if domain else 'cilium_vxlan' if mode == 'vxlan' else 'cs740vx'
                devices = [interface, tunnel] + (['cs740br'] if domain else [])
                sessions[label] = TraceSession(target, dict(domain=domain, tunnel=tunnel, devices=devices, duration=15), work / 'traces' / label)
            deadline = time.monotonic() + 150
            for label, session in sessions.items():
                session.ready.wait(max(0, deadline - time.monotonic()))
                if not any(record['kind'] == 'ready' for record in session.records):
                    raise RuntimeError(f'{label}: tracer did not attach; see traces/{label}/ssh-stderr.log')
        with (work / 'measurement.log').open('w') as log:
            process = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
            while process.poll() is None:
                if trace and not armed:
                    sample = work / 'measurement/phases/sample-01-pod-rr/worker-1/samples.jsonl'
                    if sample.exists():
                        for line in sample.read_text().splitlines()[-8:]:
                            try:
                                item = json.loads(line)
                                if item.get('kind') == 'sample' and item.get('netperf'):
                                    for session in sessions.values():
                                        session.start()
                                    armed = True
                                    break
                            except ValueError:
                                continue
                time.sleep(.5)
            if process.returncode:
                raise RuntimeError(f'Benchmark exited {process.returncode}; see {work / "measurement.log"}')
    finally:
        if process is not None and process.poll() is None:
            os.kill(process.pid, signal.SIGINT)
            try:
                process.wait(timeout=180)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL); process.wait()
        if sessions:
            with ThreadPoolExecutor(max_workers=4) as pool:
                list(pool.map(lambda session: session.stop(), sessions.values()))
    if trace:
        if not armed:
            raise RuntimeError('No identified benchmark netperf process armed the tracers')
        for label, session in sessions.items():
            result = next((r for r in session.records if r['kind'] == 'result'), None)
            if result is None or result['exit_code'] != 0 or session.error:
                raise RuntimeError(f'{label}: tracing failed; see traces/{label}/trace.jsonl')


def execute(directory, state):
    state.update(status='running', pid=os.getpid())
    state.pop('error', None)
    try:
        for step in state['plan']:
            if step['status'] == 'passed':
                continue
            work = directory / f'{step["name"]}-attempt-{len(step["attempts"]) + 1}'
            work.mkdir()
            attempt = dict(directory=work.name, status='running', started_utc=datetime.now(timezone.utc).isoformat())
            step['attempts'].append(attempt); step['status'] = 'running'; state['phase'] = step['name']
            write_manifest(directory, state)
            try:
                if step['kind'] == 'setup':
                    restore_path(work)
                    active = GENERATED / 'active-experiment.json'
                    reuse = (step['name'] == 'prepare-host-vxlan-1450' and active.exists()
                             and json.loads(active.read_text())['mode'] == step['mode'])
                    if not reuse:
                        run_logged([sys.executable, str(ROOT / 'experiments/setup.py'), step['mode'], '--reset'], work / 'setup.log')
                    current = environment()
                    if step['name'] == 'restore-original-host-mode':
                        state['restored_environment'] = current
                    else:
                        state.setdefault('environments', {})[step['mode']] = current
                    attempt['reused_setup'] = reuse
                else:
                    if step.get('condition'):
                        switch(step['mode'], step['condition'], work / 'path-audit.json')
                    current = environment()
                    save_json(work / 'environment.json', current)
                    if current != state['environments'][step['mode']]:
                        raise RuntimeError('Kernel, boot identity, worker affinity, queues or experiment NIC changed within the controlled block')
                    command = [sys.executable, str(ROOT / 'experiments/run.py'), step['mode'],
                               '--client', 'worker-1', '--server', 'worker-2', '--pod-only', '--diagnostics',
                               '--test-type', 'rr', '--samples', str(step.get('samples', 1)), '--duration', str(state['duration']),
                               '--output', str(work / 'measurement')]
                    if step.get('condition'):
                        command += ['--path-control', step['condition']]
                    measured(command, work, step['mode'], step.get('trace', False))
                    metadata = json.loads((work / 'measurement/metadata.json').read_text())
                    if metadata['status'] != 'passed':
                        raise RuntimeError('Measurement did not pass')
                step['status'] = attempt['status'] = 'passed'
            except BaseException as error:
                step['status'] = attempt['status'] = 'failed'; attempt['error'] = str(error)
                raise
            finally:
                attempt['finished_utc'] = datetime.now(timezone.utc).isoformat(); write_manifest(directory, state)
        state.update(status='passed', phase='complete')
    except BaseException as error:
        state.update(status='failed', error=str(error))
        raise
    finally:
        try:
            restore_path(directory)
        except BaseException as error:
            state.update(status='failed', restoration_error=str(error))
        state['finished_utc'] = datetime.now(timezone.utc).isoformat(); write_manifest(directory, state)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--resume', type=Path)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--pairs', type=int, default=5)
    parser.add_argument('--duration', type=int, default=30)
    parser.add_argument('--background', action='store_true')
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()
    if args.pairs < 2 or args.duration < 20:
        parser.error('Use at least two pairs and 20-second tests')
    digest = hashlib.sha256((ROOT / 'infrastructure/config.json').read_bytes()).hexdigest()
    if args.resume:
        directory = args.resume.resolve(); state = json.loads((directory / 'collection.json').read_text())
        if state['config_sha256'] != digest:
            raise ValueError('Base configuration changed')
    else:
        directory = (args.output or ROOT / 'results' / ('latency-isolation-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ'))).resolve()
        state = dict(kind='extended-study', study_type='latency-isolation', status='pending', phase='pending',
                     plan=plan(args.pairs), duration=args.duration, config_sha256=digest,
                     git_snapshot=subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip(),
                     created_utc=datetime.now(timezone.utc).isoformat())
    print('Study:', directory, flush=True)
    if args.dry_run:
        for step in state['plan']:
            print(step['name'])
        return
    with (GENERATED / 'collection.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if not args.resume:
            directory.mkdir(parents=True, exist_ok=False)
        if args.background:
            write_manifest(directory, state); sys.stdout.flush()
            pid = os.fork()
            if pid:
                (directory / 'pid').write_text(str(pid) + '\n'); print('Started PID', pid, flush=True); return
            os.setsid()
            with (directory / 'collection.log').open('a') as log:
                os.dup2(log.fileno(), 1); os.dup2(log.fileno(), 2)
                execute(directory, state)
        else:
            execute(directory, state)


if __name__ == '__main__':
    main()
