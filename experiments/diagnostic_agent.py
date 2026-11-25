#!/usr/bin/env python3
"""Read-only remote sampler, sent over SSH; requires only Python and sudo.

One JSON object per line. stdin EOF or any input requests shutdown. Optional
perf writes only to this invocation's temporary directory, removed on exit.
"""
import base64
from collections import Counter
from datetime import datetime, timezone
import json
import os
import re
from pathlib import Path
import select
import shutil
import signal
import subprocess
import sys
import tempfile
import time

EXPECTED_PODS = {}
REQUIRE_IDENTITY = False


def pod_for_cgroup(cgroup, pods):
    normalized = cgroup.replace('_', '-').lower()
    return next((pod for uid, pod in pods.items() if uid.lower() in normalized), None)


def task_stat(raw):
    """Parse proc stat without splitting a parenthesized task name on spaces."""
    end = raw.rindex(')')
    fields = raw[end + 2:].split()
    return {'comm': raw[raw.index('(') + 1:end], 'state': fields[0],
            'utime': int(fields[11]), 'stime': int(fields[12]),
            'starttime': int(fields[19]), 'processor': int(fields[36])}


def process_observations():
    netperf, pods, tasks = [], {}, []
    for entry in Path('/proc').iterdir():
        if not entry.name.isdigit():
            continue
        try:
            comm = (entry / 'comm').read_text().strip()
            cgroup = (entry / 'cgroup').read_text() if EXPECTED_PODS else ''
            pod = pod_for_cgroup(cgroup, EXPECTED_PODS)
            if pod and pod['uid'] not in pods:
                namespace = os.readlink(entry / 'ns/net')
                pods[pod['uid']] = {**pod, 'pid': int(entry.name), 'netns': namespace,
                    'snmp': read(entry / 'net/snmp'), 'netstat': read(entry / 'net/netstat'),
                    'tcp': command(['nsenter', '-t', entry.name, '-n', 'ss', '-t', '-i', '-n', '-p'])}
            selected = pod is not None or comm.startswith(('qemu-system', 'vhost-', 'ksoftirqd/', 'cilium-agent')) or comm in ('netperf', 'netserver')
            if not selected:
                continue
            cmdline = (entry / 'cmdline').read_bytes().replace(b'\0', b' ').decode(errors='replace')
            if comm == 'netperf' and (pod is not None or not REQUIRE_IDENTITY):
                netperf.append({'pid': int(entry.name), 'cmdline': cmdline,
                                'pod_uid': pod['uid'] if pod else None, 'cgroup': cgroup})
            for task in (entry / 'task').iterdir():
                try:
                    stat = task_stat((task / 'stat').read_text())
                    sched = [int(v) for v in (task / 'schedstat').read_text().split()]
                    tasks.append({'pid': int(entry.name), 'tid': int(task.name), **stat,
                                  'run_ns': sched[0], 'wait_ns': sched[1],
                                  'timeslices': sched[2],
                                  'domain': (re.search(r'guest=([^,\s]+)', cmdline).group(1)
                                             if re.search(r'guest=([^,\s]+)', cmdline) else None),
                                  'cmdline': cmdline if task.name == entry.name else '',
                                  'pod_uid': pod['uid'] if pod else None})
                except (OSError, ValueError, IndexError):
                    pass
        except (OSError, ValueError):
            pass
    domains = {task['pid']: task['domain'] for task in tasks if task.get('domain')}
    for task in tasks:
        match = re.fullmatch(r'vhost-(\d+)', task['comm'])
        if match:
            task['domain'] = domains.get(int(match[1]))
    return netperf, list(pods.values()), tasks


def read(path):
    try:
        return Path(path).read_text()
    except OSError as error:
        return {'error': str(error)}


def command(argv):
    try:
        result = subprocess.run(argv, text=True, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, timeout=5)
        return {'exit_code': result.returncode, 'output': result.stdout, 'stderr': result.stderr}
    except (OSError, subprocess.TimeoutExpired) as error:
        return {'error': str(error)}


def snapshot():
    interfaces = {}
    for device in sorted(Path('/sys/class/net').iterdir()):
        if device.name == 'lo':
            continue
        interfaces[device.name] = {
            flag: command(['ethtool', flag, device.name])
            for flag in ('-k', '-l', '-x', '-S', '-g', '-i')}
    domains = {}
    if shutil.which('virsh'):
        listed = command(['virsh', 'list', '--name'])
        if listed.get('exit_code') == 0:
            domains = {name: command(['virsh', 'dumpxml', name]) for name in listed['output'].splitlines() if name.strip()}
    return {
        'interfaces': interfaces,
        'links': command(['ip', '-j', '-s', '-d', 'link', 'show']),
        'routes': command(['ip', '-j', '-4', 'route', 'show']),
        'nstat': command(['nstat', '-a', '-s', '-z']),
        'qdiscs': command(['tc', '-s', '-j', 'qdisc', 'show']),
        'network_sysctls': command(['sysctl', 'net.core.netdev_budget', 'net.core.netdev_budget_usecs',
                                    'net.core.netdev_max_backlog', 'net.core.rmem_max', 'net.core.wmem_max']),
        'cpu_topology': command(['lscpu', '-J']),
        'nic_numa_nodes': {device.name: read(device / 'device/numa_node') for device in Path('/sys/class/net').iterdir()
                           if (device / 'device/numa_node').exists()},
        'memory': read('/proc/meminfo'),
        'kernel_command_line': read('/proc/cmdline'),
        'nic_iommu_types': {device.name: read(device / 'device/iommu_group/type') for device in Path('/sys/class/net').iterdir()
                            if (device / 'device/iommu_group/type').exists()},
        'domain_xml': domains,
        'bridge_netfilter': command(['sysctl', 'net.bridge.bridge-nf-call-iptables', 'net.bridge.bridge-nf-call-ip6tables']),
        'iptables_counters': command(['iptables-save', '-c']),
        'modules': read('/proc/modules'),
        'interrupts': read('/proc/interrupts'),
        'affinity': {str(p): read(p) for p in Path('/proc/irq').glob('*/smp_affinity_list')},
        'rps': {str(p): read(p) for p in Path('/sys/class/net').glob('*/queues/rx-*/rps_cpus')},
        'xps': {str(p): read(p) for p in Path('/sys/class/net').glob('*/queues/tx-*/xps_cpus')},
    }


def sample():
    netperf, pods, tasks = process_observations()
    return {
        'stat': read('/proc/stat'), 'softirqs': read('/proc/softirqs'),
        'softnet': read('/proc/net/softnet_stat'),
        'snmp': read('/proc/net/snmp'), 'netstat': read('/proc/net/netstat'),
        'interrupts': read('/proc/interrupts'), 'netdev': read('/proc/net/dev'),
        'tcp': command(['ss', '-t', '-i', '-n', '-p']), 'netperf': netperf,
        'pods': pods, 'tasks': tasks, 'clock_ticks': os.sysconf('SC_CLK_TCK'),
        'scheduler_stats_enabled': read('/proc/sys/kernel/sched_schedstats'),
        'pressure': {kind: read('/proc/pressure/' + kind) for kind in ('cpu', 'memory', 'io')},
        'vmstat': read('/proc/vmstat'),
    }


def emit(kind, payload):
    print(json.dumps({'kind': kind, 'utc': datetime.now(timezone.utc).isoformat(),
                      'monotonic_seconds': time.monotonic(), **payload}), flush=True)


def drop_report(path):
    reasons, locations = Counter(), Counter()
    examples, messages = [], []
    with subprocess.Popen(['perf', 'script', '-i', str(path)], text=True,
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT) as process:
        for line in process.stdout:
            if 'skb:kfree_skb:' not in line:
                if len(messages) < 25:
                    messages.append(line.strip())
                continue
            reason = re.search(r'reason:\s*(\S+)', line)
            location = re.search(r'location=(\S+)', line)
            reasons[reason[1] if reason else 'unknown'] += 1
            locations[location[1] if location else 'unknown'] += 1
            if len(examples) < 25:
                examples.append(line.strip())
        code = process.wait()
    return {'exit_code': code, 'reasons': dict(reasons), 'locations': dict(locations),
            'examples': examples, 'messages': messages,
            'scope': 'system-wide skb frees with drop reasons during the profile; not filtered to the benchmark flow'}


def main():
    global EXPECTED_PODS, REQUIRE_IDENTITY
    profile = '--profile' in sys.argv
    workspace = Path(tempfile.mkdtemp(prefix='cs740-diagnostics.'))
    profiler = None
    profile_log = None
    drop_profiler = None
    drop_log = None
    capture = None
    capture_log = None
    try:
        emit('before', snapshot())
        emit('ready', {'hostname': os.uname().nodename, 'profile_requested': profile})
        # Hard limit also bounds orphan collectors if SSH loses its control channel.
        deadline = time.monotonic() + 7200
        while time.monotonic() < deadline:
            readable, _, _ = select.select([sys.stdin], [], [], 0)
            if readable:
                control = sys.stdin.readline().strip()
                if control.startswith('{'):
                    settings = json.loads(control)
                    EXPECTED_PODS = {pod['uid']: pod for pod in settings.get('pods', [])}
                    REQUIRE_IDENTITY = settings.get('require_identity', False)
                    emit('pod_identity', {'pods': list(EXPECTED_PODS.values()), 'required': REQUIRE_IDENTITY})
                    if settings.get('capture') and capture is None:
                        spec = settings['capture']
                        capture_log = (workspace / 'capture.log').open('w+')
                        capture = subprocess.Popen(['tcpdump', '-n', '-U', '-i', spec['interface'],
                            '-s', '128', '-w', str(workspace / 'capture.pcap'), spec['filter']],
                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=capture_log,
                            start_new_session=True)
                        ready_by = time.monotonic() + 10
                        while 'listening on' not in (workspace / 'capture.log').read_text():
                            if capture.poll() is not None or time.monotonic() >= ready_by:
                                raise RuntimeError('Physical-interface capture failed to start')
                            time.sleep(.1)
                        emit('capture_started', spec)
                elif control.startswith('profile ') and profile and profiler is None:
                    duration = int(control.split()[1])
                    if not 1 <= duration <= 3600:
                        raise ValueError('Profile duration must be between 1 and 3600 seconds')
                    profile_log = (workspace / 'perf.log').open('w+')
                    try:
                        profiler = subprocess.Popen(
                            ['perf', 'record', '-e', 'cpu-clock', '-a', '-F', '49', '-g', '-o', str(workspace / 'perf.data'),
                             '--', 'sleep', str(duration)],
                            stdin=subprocess.DEVNULL, stdout=profile_log, stderr=profile_log,
                            start_new_session=True)
                        emit('profile_started', {})
                        drop_log = (workspace / 'drops.log').open('w+')
                        drop_profiler = subprocess.Popen(
                            ['perf', 'record', '-a', '-e', 'skb:kfree_skb', '-o', str(workspace / 'drops.data'),
                             '--', 'sleep', str(min(duration, 15))],
                            stdin=subprocess.DEVNULL, stdout=drop_log, stderr=drop_log, start_new_session=True)
                    except OSError as error:
                        emit('profile_error', {'error': str(error)})
                else:
                    break
            emit('sample', sample())
            # Wait remains interruptible by the SSH control channel.
            select.select([sys.stdin], [], [], 1)
        emit('after', snapshot())
    finally:
        if profiler is not None:
            if profiler.poll() is None:
                os.killpg(profiler.pid, signal.SIGINT)
                try:
                    profiler.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    os.killpg(profiler.pid, signal.SIGKILL)
                    profiler.wait()
            profile_log.flush()
            emit('profile_result', {
                'exit_code': profiler.returncode,
                'log': (workspace / 'perf.log').read_text(),
                'report': command(['perf', 'report', '--stdio', '--no-children', '--call-graph', 'none', '-i', str(workspace / 'perf.data')]),
            })
            if (workspace / 'perf.data').exists():
                # Chunk binary output to keep memory and JSON line sizes bounded.
                with (workspace / 'perf.data').open('rb') as stream:
                    while chunk := stream.read(49152):
                        emit('perf_data', {'base64': base64.b64encode(chunk).decode()})
        if profile_log is not None:
            profile_log.close()
        if drop_profiler is not None:
            if drop_profiler.poll() is None:
                os.killpg(drop_profiler.pid, signal.SIGINT)
                try:
                    drop_profiler.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    os.killpg(drop_profiler.pid, signal.SIGKILL)
                    drop_profiler.wait()
            drop_log.flush()
            emit('drop_profile_result', {'exit_code': drop_profiler.returncode,
                'log': (workspace / 'drops.log').read_text(),
                'report': drop_report(workspace / 'drops.data') if (workspace / 'drops.data').exists() else None})
            if (workspace / 'drops.data').exists():
                with (workspace / 'drops.data').open('rb') as stream:
                    while chunk := stream.read(49152):
                        emit('drop_data', {'base64': base64.b64encode(chunk).decode()})
            drop_log.close()
        if capture is not None:
            if capture.poll() is None:
                os.killpg(capture.pid, signal.SIGINT)
                capture.wait(timeout=15)
            capture_log.flush()
            emit('capture_result', {'exit_code': capture.returncode, 'log': (workspace / 'capture.log').read_text()})
            if (workspace / 'capture.pcap').exists():
                with (workspace / 'capture.pcap').open('rb') as stream:
                    while chunk := stream.read(49152):
                        emit('capture_data', {'base64': base64.b64encode(chunk).decode()})
            capture_log.close()
        shutil.rmtree(workspace)


if __name__ == '__main__':
    main()
