#!/usr/bin/env python3
"""Read-only remote sampler, sent over SSH; requires only Python and sudo.

One JSON object per line. stdin EOF or any input requests shutdown. Optional
perf writes only to this invocation's temporary directory, removed on exit.
"""
import base64
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import select
import shutil
import signal
import subprocess
import sys
import tempfile
import time


def read(path):
    try:
        return Path(path).read_text()
    except OSError as error:
        return {'error': str(error)}


def command(argv):
    try:
        result = subprocess.run(argv, text=True, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, timeout=5)
        return {'exit_code': result.returncode, 'output': result.stdout}
    except (OSError, subprocess.TimeoutExpired) as error:
        return {'error': str(error)}


def snapshot():
    interfaces = {}
    for device in sorted(Path('/sys/class/net').iterdir()):
        if device.name == 'lo':
            continue
        interfaces[device.name] = {
            flag: command(['ethtool', flag, device.name])
            for flag in ('-k', '-l', '-x', '-S')}
    return {
        'interfaces': interfaces,
        'links': command(['ip', '-j', '-s', '-d', 'link', 'show']),
        'routes': command(['ip', '-j', '-4', 'route', 'show']),
        'nstat': command(['nstat', '-a', '-s', '-z']),
        'interrupts': read('/proc/interrupts'),
        'affinity': {str(p): read(p) for p in Path('/proc/irq').glob('*/smp_affinity_list')},
        'rps': {str(p): read(p) for p in Path('/sys/class/net').glob('*/queues/rx-*/rps_cpus')},
        'xps': {str(p): read(p) for p in Path('/sys/class/net').glob('*/queues/tx-*/xps_cpus')},
    }


def sample():
    netperf = []
    for entry in Path('/proc').iterdir():
        if entry.name.isdigit():
            try:
                if (entry / 'comm').read_text().strip() == 'netperf':
                    netperf.append({'pid': int(entry.name),
                                    'cmdline': (entry / 'cmdline').read_bytes().replace(b'\0', b' ').decode(errors='replace')})
            except OSError:
                pass
    return {
        'stat': read('/proc/stat'), 'softirqs': read('/proc/softirqs'),
        'softnet': read('/proc/net/softnet_stat'),
        'snmp': read('/proc/net/snmp'), 'netstat': read('/proc/net/netstat'),
        'interrupts': read('/proc/interrupts'), 'netdev': read('/proc/net/dev'),
        'tcp': command(['ss', '-t', '-i', '-n', '-p']), 'netperf': netperf,
    }


def emit(kind, payload):
    print(json.dumps({'kind': kind, 'utc': datetime.now(timezone.utc).isoformat(),
                      'monotonic_seconds': time.monotonic(), **payload}), flush=True)


def main():
    profile = '--profile' in sys.argv
    workspace = Path(tempfile.mkdtemp(prefix='cs740-diagnostics.'))
    profiler = None
    profile_log = None
    try:
        emit('before', snapshot())
        emit('ready', {'hostname': os.uname().nodename, 'profile_requested': profile})
        # Hard limit also bounds orphan collectors if SSH loses its control channel.
        deadline = time.monotonic() + 7200
        while time.monotonic() < deadline:
            readable, _, _ = select.select([sys.stdin], [], [], 0)
            if readable:
                control = sys.stdin.readline().strip()
                if control.startswith('profile ') and profile and profiler is None:
                    duration = int(control.split()[1])
                    if not 1 <= duration <= 3600:
                        raise ValueError('Profile duration must be between 1 and 3600 seconds')
                    profile_log = (workspace / 'perf.log').open('w+')
                    try:
                        profiler = subprocess.Popen(
                            ['perf', 'record', '-a', '-F', '49', '-g', '-o', str(workspace / 'perf.data'),
                             '--', 'sleep', str(duration)],
                            stdin=subprocess.DEVNULL, stdout=profile_log, stderr=profile_log,
                            start_new_session=True)
                        emit('profile_started', {})
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
                'report': command(['perf', 'report', '--stdio', '-i', str(workspace / 'perf.data')]),
            })
            if (workspace / 'perf.data').exists():
                # Chunk binary output to keep memory and JSON line sizes bounded.
                with (workspace / 'perf.data').open('rb') as stream:
                    while chunk := stream.read(49152):
                        emit('perf_data', {'base64': base64.b64encode(chunk).decode()})
        if profile_log is not None:
            profile_log.close()
        shutil.rmtree(workspace)


if __name__ == '__main__':
    main()
