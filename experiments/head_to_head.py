#!/usr/bin/env python3
"""Six-cell VXLAN architecture/profile comparison, with rebuild-level identity."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import copy
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
import xml.etree.ElementTree as ET

import affinity
import queues
from collect import run_logged, write_manifest
from common import (GENERATED, ROOT, CILIUM_TUNNEL_MODES, DIRECT_BRIDGE_MODES,
                    HOST_VXLAN_MODES, guests, remote, save_json, mtu_profile)
from cpu_latency import requests, close_requests
from latency_study import environment, measured, targets

ARCHITECTURES = {
    'cilium-macvtap': {'baseline': 'vxlan', 'optimized': 'vxlan-bpf'},
    'cilium-bridge': {'baseline': 'vxlan-bridge', 'optimized': 'vxlan-bridge-bpf'},
    'host-vxlan': {'baseline': 'host-vxlan-1450', 'optimized': 'host-vxlan-routing-bpf-1450'},
}
WORKLOADS = ('rr', 'single', 'multi')
DEFAULT_QOS = 2000000000


def plan(blocks, seed):
    cells = [(a, p) for a in ARCHITECTURES for p in ('baseline', 'optimized')]
    random.Random(seed).shuffle(cells)
    result = []
    for block in range(1, blocks + 1):
        # Rotate a recorded seeded permutation; six blocks cover every position.
        shift = (block - 1) % len(cells)
        order = cells[shift:] + cells[:shift]
        if (block - 1) // len(cells) % 2:
            order = list(reversed(order))
        for architecture, profile in order:
            result.append(dict(name=f'block-{block:02d}-{architecture}-{profile}',
                               block=block, architecture=architecture, profile=profile,
                               mode=ARCHITECTURES[architecture][profile],
                               status='pending', attempts=[]))
    return result


def profile_settings(profile):
    return dict(queues=2 if profile == 'optimized' else 1,
                affinity='pinned' if profile == 'optimized' else 'original',
                cpu_latency_us=10 if profile == 'optimized' else -1,
                forwarding='bpf' if profile == 'optimized' else 'baseline')


def hardware_fingerprint(env, audit):
    """Compare fixed hardware settings across cells, excluding intended changes."""
    result = {}
    for label, data in env.items():
        if label.startswith('hypervisor-'):
            result[label] = {k:data[k] for k in ('boot_id','kernel','policy')}
        else:
            xml = ET.fromstring(audit[label]['xml'])
            memory = xml.find('memory')
            result[label] = dict(kernel=data['kernel'], vcpus=xml.findtext('vcpu'),
                                 memory=memory.text, memory_unit=memory.get('unit'))
    return copy.deepcopy(result)


def validate_services(data, proxy_present, profile, statuses):
    optimized = profile == 'optimized'
    expected = str(optimized).lower()
    if data.get('kube-proxy-replacement') != expected or data.get('enable-bpf-masquerade', 'false') != expected:
        raise ValueError('Service/masquerade settings do not match the profile')
    if proxy_present == optimized:
        raise ValueError('kube-proxy presence does not match the profile')
    if set(statuses) != {'worker-1','worker-2'}:
        raise ValueError('Both live Cilium agents require service/masquerade proof')
    for vm, status in statuses.items():
        proxy = str(status.get('kube-proxy-replacement', {}).get('mode', '')).lower()
        masquerading = status.get('masquerading', {})
        wanted = 'bpf' if optimized else 'iptables'
        if proxy != expected or str(masquerading.get('mode', '')).lower() != wanted or not masquerading.get('enabled'):
            raise ValueError('Live service/masquerade mode does not match the profile: '+vm)


def command(mode, workload, duration, output):
    return [sys.executable, str(ROOT/'experiments/run.py'), mode,
            '--client', 'worker-1', '--server', 'worker-2', '--pod-only',
            '--diagnostics', '--test-type', workload, '--samples', '1',
            '--duration', str(duration), '--streams', '4', '--output', str(output)]


def host_audit():
    source = json.loads((ROOT/'infrastructure/config.json').read_text())
    inventory = guests(GENERATED/'guest_inventory.json')
    script = r'''
import json,os,subprocess,sys
from pathlib import Path
domain,mac=sys.argv[1:]
q=int(Path('/run/libvirt/qemu/'+domain+'.pid').read_text())
tids=set()
for p in [*Path('/proc').iterdir(),*Path('/proc',str(q),'task').iterdir()]:
 try:
  if p.name.isdigit() and (p/'comm').read_text().strip()=='vhost-'+str(q):tids.add(int(p.name))
 except OSError:pass
print(json.dumps({'xml':subprocess.check_output(['virsh','dumpxml',domain],text=True),
 'links':json.loads(subprocess.check_output(['ip','-j','-d','link','show'],text=True)),
 'vhost':{str(t):sorted(os.sched_getaffinity(t)) for t in sorted(tids)},
 'affinity_backup':Path('/var/lib/cs740-network/affinity/'+domain+'-'+str(q)+'.json').exists()}))
'''
    def read(vm):
        host = next(h for h in source['hypervisors'] if h['name'] == inventory[vm]['hypervisor'])
        settings = next(v for v in host['vms'] if v['name'] == vm)
        value = json.loads(remote(GENERATED/'ssh_config', 'cs740-'+host['name']+'-jump',
                                 ['sudo', '-n', 'python3', '-c', script, vm, settings['mac_address']]))
        value.update(experiment_mac=settings['mac_address'], physical_interface=host['network']['source'])
        return vm, value
    with ThreadPoolExecutor(max_workers=2) as pool:
        return dict(pool.map(read, ('worker-1', 'worker-2')))


def validate_audit(mode, profile, audit, env):
    expected = profile_settings(profile)
    bridge = mode in DIRECT_BRIDGE_MODES or mode in HOST_VXLAN_MODES
    for vm, data in audit.items():
        matches = [i for i in ET.fromstring(data['xml']).findall('./devices/interface')
                   if i.find('mac').get('address', '').lower() == data['experiment_mac'].lower()]
        if len(matches) != 1:
            raise ValueError('Experiment NIC identity is not unique: '+vm)
        nic = matches[0]
        if nic.get('type') != ('bridge' if bridge else 'direct') or nic.find('model').get('type') != 'virtio':
            raise ValueError('Wrong attachment/model: '+vm)
        source = nic.find('source')
        if source.get('bridge' if bridge else 'dev') != ('cs740br' if bridge else data['physical_interface']):
            raise ValueError('Wrong experiment NIC parent: '+vm)
        driver = nic.find('driver')
        if driver is None or driver.get('name') != 'vhost' or int(driver.get('queues', '1')) != expected['queues']:
            raise ValueError('Wrong live queue configuration: '+vm)
        channels = env[vm]['channels'].split('Current hardware settings:')[-1]
        import re
        current = re.search(r'Combined:\s*(\d+)', channels)
        if not current or int(current[1]) != expected['queues']:
            raise ValueError('Wrong active guest queue count: '+vm)
        if env[vm]['interface']['mtu'] != mtu_profile(mode)['guest_mtu']:
            raise ValueError('Wrong guest transport MTU: '+vm)
        links = {l['ifname']: l for l in data['links']}
        physical = links[data['physical_interface']]
        if physical['mtu'] != 1500:
            raise ValueError('Wrong physical MTU: '+vm)
        host_tunnel = mode in HOST_VXLAN_MODES
        if ('cs740hvx' in links) != host_tunnel:
            raise ValueError('Unexpected or missing host VXLAN: '+vm)
        if bridge:
            if links['cs740br']['mtu'] != mtu_profile(mode)['guest_mtu']:
                raise ValueError('Wrong bridge MTU: '+vm)
            master = physical.get('master')
            attached = master in ('cs740br', links['cs740br']['ifindex'])
            if attached == host_tunnel:
                raise ValueError('Physical NIC bridge membership would bypass/nest the tunnel: '+vm)
            if host_tunnel and links['cs740hvx'].get('master') not in ('cs740br', links['cs740br']['ifindex']):
                raise ValueError('Host tunnel must be a bridge port: '+vm)
        elif physical.get('master') is not None:
            raise ValueError('Macvtap physical parent still enslaved: '+vm)
        if not data['vhost']:
            raise ValueError('No effective vhost affinity proof: '+vm)
        widths = [len(mask) for mask in data['vhost'].values()]
        if profile == 'optimized' and any(n != 1 for n in widths):
            raise ValueError('Optimized vhost tasks are not pinned: '+vm)
        if profile == 'baseline' and any(n <= 1 for n in widths):
            raise ValueError('Baseline vhost tasks remain pinned: '+vm)


def restore_affinity(directory):
    # A pin failure may affect only one worker. Restore only existing backups.
    audit = host_audit()
    inventory = guests(GENERATED/'guest_inventory.json')
    source = json.loads((ROOT/'infrastructure/config.json').read_text())
    records = []
    agent = (ROOT/'experiments/affinity_agent.py').read_text()
    failures = []
    for vm, data in audit.items():
        if not data['affinity_backup']:
            continue
        host = next(h for h in source['hypervisors'] if h['name'] == inventory[vm]['hypervisor'])
        try:
            record = json.loads(remote(GENERATED/'ssh_config', 'cs740-'+host['name']+'-jump',
                                ['sudo', '-n', 'python3', '-c', agent, vm, host['network']['source'], 'restore']))
            original = {str(t['tid']): t['cpus'] for t in record['baseline']['threads']}
            if any(t['cpus'] != original[str(t['tid'])] for t in record['current']):
                raise ValueError('Affinity restoration mismatch: '+vm)
            records.append(record)
        except Exception as error:
            failures.append(str(error))
    save_json(directory/'affinity-restoration.json', dict(records=records, errors=failures))
    if failures:
        raise RuntimeError('; '.join(failures))


def validate_policy(holders, profile):
    expected = 10 if profile == 'optimized' else DEFAULT_QOS
    if len(holders) != 2:
        raise ValueError('Both hypervisors require CPU policy holders')
    for holder in holders:
        ready = next((r for r in holder.records if r['kind'] == 'ready'), {})
        if holder.process.poll() is not None or ready.get('before_us') != DEFAULT_QOS or ready.get('effective_us') != expected:
            raise ValueError('CPU idle policy is not the requested isolated condition')


def execute_case(work, step, state, directory):
    mode, profile = step['mode'], step['profile']
    run_logged([sys.executable, str(ROOT/'experiments/setup.py'), mode, '--reset'], work/'setup.log')
    settings = profile_settings(profile)
    queues.configure(['worker-1', 'worker-2'], queues=settings['queues'], output=work/'queues.json')
    pinned = False
    try:
        if profile == 'optimized':
            pinned = True  # cleanup also covers a partially applied pin
            affinity.configure('pin', work/'affinity.json')
        env = environment(include_policy=True)
        audit = host_audit()
        save_json(work/'environment.json', env)
        save_json(work/'attachment-audit.json', audit)
        validate_audit(mode, profile, audit, env)
        fingerprint = hardware_fingerprint(env, audit)
        if fingerprint != state.setdefault('hardware_reference', fingerprint):
            raise ValueError('Physical host policy/boot/kernel or guest kernel/resources changed across configurations')
        from head_to_head_path import validate_path
        validate_path(work/'path-validation', mode, state['smoke'])
        from run import KUBECTL
        inventory = guests(GENERATED/'guest_inventory.json')
        controller = next(name for name,h in inventory.items() if h['cs740_role']=='controller')
        daemonsets = json.loads(remote(GENERATED/'ssh_config', controller,
                                     KUBECTL+['-n','kube-system','get','daemonsets','-o','json']))
        save_json(work/'daemonsets.json', daemonsets)
        config = json.loads((work/'path-validation/measurement/cilium-config.json').read_text())
        statuses = {}
        for vm in ('worker-1','worker-2'):
            pods = json.loads(remote(GENERATED/'ssh_config', controller, KUBECTL+
                ['-n','kube-system','get','pods','-l','k8s-app=cilium','--field-selector','spec.nodeName='+vm,'-o','json']))
            if len(pods['items']) != 1:
                raise ValueError('Cannot identify unique Cilium agent for '+vm)
            pod = pods['items'][0]['metadata']['name']
            statuses[vm] = json.loads(remote(GENERATED/'ssh_config', controller, KUBECTL+
                ['-n','kube-system','exec',pod,'--','cilium-dbg','status','--output','json']))
        save_json(work/'profile-status.json', statuses)
        validate_services(config['data'], any(d['metadata']['name']=='kube-proxy' for d in daemonsets['items']), profile, statuses)
        for sample in range(1, state['samples'] + 1):
            # Rotate workload ordering within each configuration/boot.
            shift = (sample + step['block'] - 2) % len(WORKLOADS)
            order = WORKLOADS[shift:] + WORKLOADS[:shift]
            for workload in order:
                state['phase'] = step['name']+f'-sample-{sample:02d}-'+workload
                write_manifest(directory, state)
                trial = work/f'sample-{sample:02d}-{workload}'
                trial.mkdir()
                current = environment(include_policy=True)
                if current != env:
                    raise ValueError('Boot, NIC, queues, affinity or host policy changed within configuration')
                current_audit = host_audit()
                validate_audit(mode, profile, current_audit, current)
                if any(current_audit[vm]['vhost'] != audit[vm]['vhost'] for vm in audit):
                    raise ValueError('Vhost task identity/affinity changed within configuration')
                holders = requests(settings['cpu_latency_us'], trial/'cpu-latency')
                try:
                    validate_policy(holders, profile)
                    measured(command(mode, workload, state['duration'], trial/'measurement'), trial, mode, False)
                    validate_policy(holders, profile)
                finally:
                    close_requests(holders)
                meta = json.loads((trial/'measurement/metadata.json').read_text())
                if meta['status'] != 'passed':
                    raise ValueError('Unsuccessful benchmark')
                meta.update(study_type='vxlan-head-to-head', quality='development-smoke' if state['smoke'] else 'primary',
                            architecture=step['architecture'], optimization_profile=profile,
                            rebuild_block=step['block'], repetition=sample)
                save_json(trial/'measurement/metadata.json', meta)
                save_json(trial/'observation.json', dict(status='passed', workload=workload,
                          sample=sample, block=step['block'], architecture=step['architecture'], profile=profile))
    finally:
        # Never replace vhost tasks if restoring their affinity failed.
        if pinned:
            restore_affinity(work)
        queues.configure(['worker-1', 'worker-2'], queues=1, output=work/'queues-restoration.json')


def execute(directory, state):
    state.update(status='running', pid=os.getpid())
    state.pop('error', None)
    try:
        # Resuming never reuses a possibly half-tuned previous boot.
        preflight = directory/('preflight-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ'))
        preflight.mkdir()
        restore_affinity(preflight)
        for step in state['plan']:
            if step['status'] == 'passed':
                continue
            work = directory/(step['name']+f'-attempt-{len(step["attempts"])+1}')
            work.mkdir()
            attempt = dict(directory=work.name, status='running', started_utc=now())
            attempt['source_revision'] = len(state.get('source_revisions', []))-1 if state.get('source_revisions') else 0
            step['attempts'].append(attempt)
            step['status'] = 'running'
            state['phase'] = step['name']
            write_manifest(directory, state)
            try:
                execute_case(work, step, state, directory)
                step['status'] = attempt['status'] = 'passed'
            except BaseException as error:
                step['status'] = attempt['status'] = 'failed'
                attempt['error'] = str(error)
                raise
            finally:
                attempt['finished_utc'] = now()
                write_manifest(directory, state)
        state['status'] = 'passed'
    except BaseException as error:
        state.update(status='failed', error=str(error))
    finally:
        # Always restore the original architecture's baseline, including on failure.
        restoration = directory/('restoration-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ'))
        restoration.mkdir()
        outcome = state['status']
        state.update(status='running', phase='restoring-baseline', measurement_status=outcome)
        write_manifest(directory, state)
        try:
            restore_affinity(restoration)
            run_logged([sys.executable, str(ROOT/'experiments/setup.py'), state['restore_mode'], '--reset'], restoration/'setup.log')
            queues.configure(['worker-1', 'worker-2'], queues=1, output=restoration/'queues.json')
            env = environment(include_policy=True)
            audit = host_audit()
            validate_audit(state['restore_mode'], 'baseline', audit, env)
            save_json(restoration/'environment.json', env)
            save_json(restoration/'attachment-audit.json', audit)
            holders = requests(-1, restoration/'cpu-latency')
            try:
                validate_policy(holders, 'baseline')
            finally:
                close_requests(holders)
            state['restoration'] = dict(status='passed', directory=restoration.name, mode=state['restore_mode'])
            state['status'] = outcome
        except BaseException as error:
            state['restoration'] = dict(status='failed', directory=restoration.name, error=str(error))
            state.update(status='failed', error=state.get('error', '')+'; restoration failed: '+str(error))
        state.update(phase='complete' if state['status'] == 'passed' else 'failed', finished_utc=now())
        write_manifest(directory, state)
    if state['status'] != 'passed':
        raise RuntimeError(state['error'])


def now():
    return datetime.now(timezone.utc).isoformat()


def source_files():
    files = set((ROOT/'experiments').glob('*.py'))
    for folder in ('infrastructure/ansible', 'infrastructure/terraform'):
        for path in (ROOT/folder).rglob('*'):
            if path.is_file() and not any(p.startswith('.') for p in path.relative_to(ROOT/folder).parts):
                if path.suffix in ('.py','.sh','.tf','.j2','.yml','.yaml','.tpl','.cfg'):
                    files.add(path)
    files.update(ROOT/name for name in ('infrastructure/cluster.sh', 'infrastructure/deploy.sh',
                                        'infrastructure/kubernetes/cluster-config.yml'))
    return sorted(files)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--blocks', type=int, default=3)
    parser.add_argument('--samples', type=int, default=5)
    parser.add_argument('--duration', type=int, default=30)
    parser.add_argument('--seed', type=int, default=20261125)
    parser.add_argument('--smoke', action='store_true', help='Development only: one block, one repetition, 3 seconds; never primary data')
    parser.add_argument('--output', type=Path)
    parser.add_argument('--resume', type=Path)
    parser.add_argument('--background', action='store_true')
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--accept-smoke-source-change', action='store_true',
                        help='Resume development smoke only after a bug fix; retain immutable source revisions. Never allowed for primary data')
    args = parser.parse_args()
    if args.smoke:
        args.blocks, args.samples, args.duration = 1, 1, 3
    elif args.blocks < 3 or args.samples < 3 or not 15 <= args.duration <= 120:
        parser.error('Primary collection requires >=3 blocks, >=3 repetitions and 15–120 seconds; use --smoke for development')
    if args.resume and args.output:
        parser.error('--resume and --output are mutually exclusive')
    if args.accept_smoke_source_change and not args.resume:
        parser.error('--accept-smoke-source-change requires --resume')
    digest = hashlib.sha256((ROOT/'infrastructure/config.json').read_bytes()).hexdigest()
    hashes = {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in source_files()}
    if args.resume:
        directory = args.resume.resolve()
        state = json.loads((directory/'collection.json').read_text())
        if state['study_type'] != 'vxlan-head-to-head' or state['config_sha256'] != digest:
            raise ValueError('Study type or deployment configuration changed; start a new collection')
        if args.accept_smoke_source_change and not state['smoke']:
            raise ValueError('Primary measurements may never resume with a changed source revision')
        if state['source_sha256'] != hashes:
            if not (state['smoke'] and args.accept_smoke_source_change):
                raise ValueError('Source changed; start a new collection, or explicitly accept a development-only smoke revision')
            revisions = state.setdefault('source_revisions', [dict(revision=0, directory='source',
                source_sha256=state['source_sha256'], git_snapshot=state['git_snapshot'])])
            for step in state['plan']:
                for attempt in step['attempts']:
                    attempt.setdefault('source_revision', len(revisions)-1)
            revision = len(revisions)
            revisions.append(dict(revision=revision, directory=f'source-revision-{revision}', source_sha256=hashes,
                                  git_snapshot=subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip(),
                                  reason='Explicitly accepted development-only bug fix', accepted_utc=now()))
            state.update(source_sha256=hashes, git_snapshot=revisions[-1]['git_snapshot'])
    else:
        directory = (args.output or ROOT/'results'/('vxlan-head-to-head-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ'))).resolve()
        active = json.loads((GENERATED/'active-experiment.json').read_text())
        restore = next((m['baseline'] for m in ARCHITECTURES.values() if active['mode'] in m.values()), None)
        if restore is None:
            raise ValueError('Start from one of the three head-to-head architectures; cannot infer a safe baseline restoration')
        state = dict(kind='extended-study', study_type='vxlan-head-to-head', status='pending', phase='pending',
                     smoke=args.smoke, quality='development-smoke' if args.smoke else 'primary',
                     blocks=args.blocks, samples=args.samples, duration=args.duration, seed=args.seed,
                     plan=plan(args.blocks, args.seed), restore_mode=restore,
                     config_sha256=digest, source_sha256=hashes, created_utc=now(),
                     git_snapshot=subprocess.check_output(['git','rev-parse','HEAD'], text=True).strip())
    print('Study:', directory, flush=True)
    if args.dry_run:
        print(json.dumps(state, indent=2))
        return
    with (GENERATED/'collection.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX|fcntl.LOCK_NB)
        if any((GENERATED/n).exists() for n in ('path-control.json','forwarding-control.json')):
            raise ValueError('Restore existing path/forwarding controls first')
        hosts = guests(GENERATED/'guest_inventory.json')
        from run import select_pair
        select_pair(hosts, 'vxlan', 'worker-1', 'worker-2')
        if not args.resume:
            directory.mkdir(parents=True, exist_ok=False)
            for name in hashes:
                dest = directory/'source'/name
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes((ROOT/name).read_bytes())
        elif state.get('source_revisions'):
            revision = state['source_revisions'][-1]
            snapshot = directory/revision['directory']
            if not snapshot.exists():
                snapshot.mkdir()
                for name in hashes:
                    dest = snapshot/name
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    dest.write_bytes((ROOT/name).read_bytes())
        if args.background:
            write_manifest(directory, state)
            sys.stdout.flush()
            pid = os.fork()
            if pid:
                (directory/'pid').write_text(str(pid)+'\n')
                print('Started PID', pid, flush=True)
                return
            os.setsid()
            with (directory/'collection.log').open('a') as log:
                os.dup2(log.fileno(), 1)
                os.dup2(log.fileno(), 2)
                with open(os.devnull) as null:
                    os.dup2(null.fileno(), 0)
                signal.signal(signal.SIGTERM, lambda *_: sys.exit('Study terminated'))
                execute(directory, state)
        else:
            execute(directory, state)


if __name__ == '__main__':
    main()
