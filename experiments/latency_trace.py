#!/usr/bin/env python3
"""Bounded, separate bpftrace function/queue/scheduler latency observations.

No cross-machine timestamp subtraction is performed. Function durations are
inclusive and instrumentation-inclusive; device counters are not flow-filtered.
"""
import ctypes
import json
import os
from pathlib import Path
import select
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time


def select_functions(available):
    names = {line.split()[0] for line in available.splitlines() if line.strip()}
    wanted = ['vxlan_xmit', 'vxlan_rcv']
    wanted += sorted(name for name in names if name.startswith('vxlan_build_skb'))[:1]
    wanted += ['__iptunnel_pull_header', 'udp_tunnel_xmit_skb', 'ip_forward',
               'br_forward', 'handle_tx', 'tun_get_user', 'tap_get_user']
    return [name for name in wanted if name in names]


def build_program(spec, functions, tracked, marker_pid):
    duration = int(spec.get('duration', 15))
    if not 1 <= duration <= 60:
        raise ValueError('Trace duration must be 1..60 seconds')
    tunnel = spec['tunnel']
    devices = spec['devices']
    if any(not name or len(name) > 15 or not all(c.isalnum() or c in '_.-' for c in name)
           for name in [tunnel, *devices]):
        raise ValueError('Invalid interface name')
    initial = [f'@role[{int(tid)}]={int(role)};' for tid, role in tracked.items()]
    initial += [f'@device[{json.dumps(device)}]={index + 1};' for index, device in enumerate(devices)]
    text = ['BEGIN { @created=nsecs; ' + ''.join(initial) + 'printf("CS740_READY\\n");}',
            f'tracepoint:syscalls:sys_enter_prctl /pid=={int(marker_pid)} && comm=="cs740-mark" && !@armed/ '
            '{ @armed=nsecs; printf("CS740_STARTED\\n"); }',
            f'interval:s:1 /(@armed && nsecs-@armed>={duration}000000000) || nsecs-@created>600000000000/ {{ exit(); }}']
    active = f'@armed && nsecs-@armed < {duration}000000000'
    key = 'tid, (tid==0 ? cpu+1 : 0)'
    # Each stack is keyed by task identity, with CPU disambiguation only for
    # idle tasks. Ordinary functions can migrate between entry and return.
    for index, name in enumerate(functions):
        if name == 'vxlan_xmit':
            condition = f'((struct net_device *)arg1)->name=={json.dumps(tunnel)}'
        elif name.startswith('vxlan_build_skb') or name == 'udp_tunnel_xmit_skb':
            parent = functions.index('vxlan_xmit') if 'vxlan_xmit' in functions else None
            condition = f'@begin[{parent},{key},@depth[{parent},{key}]]>0' if parent is not None else '0'
        elif name == '__iptunnel_pull_header':
            condition = f'@depth[{functions.index("vxlan_rcv")},{key}]>0' if 'vxlan_rcv' in functions else '0'
        elif name in ('handle_tx', 'tun_get_user', 'tap_get_user'):
            condition = '@role[tid]==2'
        elif name in ('ip_forward', 'br_forward'):
            # Inner Pod TCP only; unlike tunnel helpers this does not select
            # outer UDP traffic or management SSH. The configured Pod /16 is
            # checked by the study before constructing this specification.
            arg = 'arg0' if name == 'ip_forward' else 'arg1'
            condition = ('$iph->protocol==6 && (bswap((uint32)$iph->saddr)>>16)==0x0af4 '
                         '&& (bswap((uint32)$iph->daddr)>>16)==0x0af4')
        else:
            condition = '1'
        preamble = ''
        if name in ('ip_forward', 'br_forward'):
            preamble = f'$skb=(struct sk_buff *){arg}; $iph=(struct iphdr *)($skb->head+$skb->network_header);'
        if name == 'vxlan_rcv':
            preamble += f'@rx_stack[{key},$depth]=(uint64)arg1; @rx_packet[(uint64)arg1]=nsecs;'
        receive_return = (f'$packet=@rx_stack[{key},$depth]; '
                          f'if (@rx_packet[$packet]) {{ @rx_return[$packet]=nsecs; }} '
                          f'delete(@rx_stack[{key},$depth]);') if name == 'vxlan_rcv' else ''
        text += [f'kprobe:{name} /{active}/ {{ '
                 f'$depth=@depth[{index},{key}]+1; @depth[{index},{key}]=$depth; '
                 + preamble + f'if ({condition}) {{ @begin[{index},{key},$depth]=nsecs; }} }}',
                 f'kretprobe:{name} /@depth[{index},{key}]>0/ {{ '
                 f'$depth=@depth[{index},{key}]; $start=@begin[{index},{key},$depth]; '
                 f'if ($start) {{ $elapsed=nsecs-$start; @function_ns[{index}]=stats($elapsed); '
                 f'@function_max_ns[{index}]=max($elapsed); @function_hist_ns[{index}]=hist($elapsed); '
                 f'delete(@begin[{index},{key},$depth]); }} '
                 + receive_return +
                 f'if ($depth>1) {{ @depth[{index},{key}]=$depth-1; }} else {{ delete(@depth[{index},{key}]); }} }}']
    if 'vxlan_rcv' in functions:
        for name, argument in spec.get('handoff_functions', {}).items():
            text.append(f'kprobe:{name} /@rx_packet[(uint64)arg{argument}] '
                        f'&& ((struct sk_buff *)arg{argument})->dev->name=={json.dumps(tunnel)}/ {{ '
                        f'$packet=(uint64)arg{argument}; $elapsed=nsecs-@rx_packet[$packet]; '
                        f'@receive_handoff_ns["{name}"]=stats($elapsed); '
                        f'if (@rx_return[$packet]) {{ $gap=nsecs-@rx_return[$packet]; '
                        f'@receive_deferred_ns["{name}"]=stats($gap); '
                        f'@receive_deferred_hist_ns["{name}"]=hist($gap); }} '
                        'delete(@rx_packet[$packet]); delete(@rx_return[$packet]); }')
    if spec.get('kvm_wakeup_available'):
        text.append(f'tracepoint:kvm:kvm_vcpu_wakeup /{active} && @role[tid]==1/ {{ '
                    '@halt_ns[args.waited]=stats(args.ns); @halt_valid_count[args.waited,args.valid]=count(); }')
    text += [
        f'tracepoint:sched:sched_wakeup /{active}/ {{ '
        'if (args.comm=="netperf") { @role[args.pid]=3; } '
        'if (args.comm=="netserver") { @role[args.pid]=4; } '
        'if (@role[args.pid] && !@wake[args.pid]) { @wake[args.pid]=nsecs; } }',
        f'tracepoint:sched:sched_switch /{active}/ {{ '
        'if (@role[args.next_pid] && @wake[args.next_pid]) { '
        '$elapsed=nsecs-@wake[args.next_pid]; @wakeup_ns[@role[args.next_pid]]=stats($elapsed); '
        '@wakeup_hist_ns[@role[args.next_pid]]=hist($elapsed); '
        '@wakeup_max_ns[@role[args.next_pid]]=max($elapsed); delete(@wake[args.next_pid]); } '
        'if (@role[args.next_pid] && @offcpu[args.next_pid]) { '
        '$elapsed=nsecs-@offcpu[args.next_pid]; '
        '@offcpu_ns[@role[args.next_pid],@blocked[args.next_pid]]=stats($elapsed); '
        'delete(@offcpu[args.next_pid]); delete(@blocked[args.next_pid]); } '
        'if (@role[args.prev_pid]) { @offcpu[args.prev_pid]=nsecs; '
        '@blocked[args.prev_pid]=(args.prev_state!=0); } }',
        f'tracepoint:net:net_dev_queue /{active} && @device[str(args.name)]/ {{ '
        '@queued[(uint64)args.skbaddr]=nsecs; @qdevice[(uint64)args.skbaddr]=@device[str(args.name)]; }',
        f'tracepoint:net:net_dev_start_xmit /{active} && @queued[(uint64)args.skbaddr]/ {{ '
        '$key=(uint64)args.skbaddr; $device=@device[str(args.name)]; '
        'if ($device && $device==@qdevice[$key]) { '
        '$elapsed=nsecs-@queued[$key]; @queue_ns[$device]=stats($elapsed); '
        '@queue_hist_ns[$device]=hist($elapsed); @queue_max_ns[$device]=max($elapsed); '
        'delete(@queued[$key]); delete(@qdevice[$key]); } }',
        'END { clear(@depth); clear(@begin); clear(@wake); clear(@offcpu); clear(@blocked); '
        'clear(@queued); clear(@qdevice); clear(@device); clear(@armed); clear(@created); '
        + ('clear(@rx_stack); clear(@rx_packet); clear(@rx_return); ' if 'vxlan_rcv' in functions else '') + '}',
    ]
    return '\n'.join(text)


def tracked_tasks(domain):
    if not domain:
        return {}
    qemu = int(Path(f'/run/libvirt/qemu/{domain}.pid').read_text())
    command = Path(f'/proc/{qemu}/cmdline').read_bytes().replace(b'\0', b' ').decode()
    if f'guest={domain},' not in command:
        raise ValueError('QEMU identity mismatch')
    result = {}
    for task in Path(f'/proc/{qemu}/task').iterdir():
        name = (task / 'comm').read_text().strip()
        result[int(task.name)] = 1 if name.startswith('CPU ') else 2 if name.startswith('vhost-') else 5
    # Compatibility with kernels using separate vhost kernel processes.
    for path in Path('/proc').iterdir():
        if path.name.isdigit():
            try:
                if (path / 'comm').read_text().strip() == f'vhost-{qemu}':
                    result[int(path.name)] = 2
            except OSError:
                pass
    return result


def agent(spec):
    def terminate(signum, frame):
        raise SystemExit(128 + signum)
    signal.signal(signal.SIGTERM, terminate)
    signal.signal(signal.SIGHUP, terminate)
    def emit(kind, **data):
        print(json.dumps({'kind': kind, 'epoch': time.time(), **data}), flush=True)
    available = Path('/sys/kernel/tracing/available_filter_functions').read_text()
    functions = select_functions(available)
    symbols = {line.split()[0] for line in available.splitlines() if line.strip()}
    spec['handoff_functions'] = {name: arg for name, arg in
        [('napi_gro_receive', 1), ('__netif_receive_skb_one_core', 0)] if name in symbols}
    wakeup_format = Path('/sys/kernel/tracing/events/kvm/kvm_vcpu_wakeup/format')
    spec['kvm_wakeup_available'] = bool(spec.get('domain') and wakeup_format.exists()
                                       and all(field in wakeup_format.read_text() for field in (' ns;', ' waited;', ' valid;')))
    tracked = tracked_tasks(spec.get('domain'))
    timing = {}
    for name, path in {
        'clocksource': '/sys/devices/system/clocksource/clocksource0/current_clocksource',
        'cpuidle_driver': '/sys/devices/system/cpu/cpuidle/current_driver',
        'cpuidle_governor': '/sys/devices/system/cpu/cpuidle/current_governor_ro',
        'cpu0_governor': '/sys/devices/system/cpu/cpu0/cpufreq/scaling_governor',
        'halt_poll_ns': '/sys/module/kvm/parameters/halt_poll_ns',
    }.items():
        value = Path(path)
        timing[name] = value.read_text().strip() if value.exists() else None
    timing['cpu0_idle_states'] = [{key: (state / key).read_text().strip()
                                  for key in ('name', 'latency', 'disable') if (state / key).exists()}
                                 for state in Path('/sys/devices/system/cpu/cpu0/cpuidle').glob('state*')]
    def inspect(arguments):
        try:
            result = subprocess.run(arguments, text=True, capture_output=True, timeout=10)
            return dict(exit_code=result.returncode, output=result.stdout, stderr=result.stderr)
        except (OSError, subprocess.TimeoutExpired) as error:
            return {'error': str(error)}
    hooks = {'tc': {}}
    for device in spec['devices']:
        if Path('/sys/class/net', device).exists():
            hooks['tc'][device] = {direction: inspect(['tc', '-j', 'filter', 'show', 'dev', device, direction])
                                    for direction in ('ingress', 'egress')}
    hooks['bpf_links'] = inspect(['bpftool', '-j', 'link', 'show']) if shutil.which('bpftool') else {'error': 'bpftool unavailable'}
    hooks['bpf_net'] = inspect(['bpftool', '-j', 'net', 'show']) if shutil.which('bpftool') else {'error': 'bpftool unavailable'}
    program = build_program(spec, functions, tracked, os.getpid())
    records, ready = [], threading.Event()
    with tempfile.TemporaryFile(mode='w+') as errors:
        process = subprocess.Popen(['bpftrace', '-f', 'json', '-B', 'line', '-e', program],
                                   stdout=subprocess.PIPE, stderr=errors, text=True,
                                   stdin=subprocess.DEVNULL, start_new_session=True)
        def read():
            for line in process.stdout:
                try:
                    record = json.loads(line)
                    records.append(record)
                    if record.get('type') == 'printf' and 'CS740_READY' in record.get('data', ''):
                        ready.set()
                except ValueError:
                    records.append({'unparsed': line.strip()})
        reader = threading.Thread(target=read, daemon=True)
        reader.start()
        try:
            deadline = time.monotonic() + 90
            while not ready.wait(.1):
                if process.poll() is not None or time.monotonic() >= deadline:
                    errors.seek(0)
                    raise RuntimeError('bpftrace did not attach: ' + errors.read()[-6000:])
            emit('ready', functions=functions, tracked=tracked, specification=spec,
                 kernel=os.uname().release, program=program, timing_configuration=timing, kernel_hooks=hooks)
            deadline = time.monotonic() + 600
            while process.poll() is None and time.monotonic() < deadline:
                readable, _, _ = select.select([sys.stdin], [], [], .2)
                if readable:
                    control = sys.stdin.readline().strip()
                    if control == 'start':
                        libc = ctypes.CDLL(None)
                        libc.prctl(15, b'cs740-mark', 0, 0, 0)
                        libc.prctl(3, 0, 0, 0, 0)
                        libc.prctl(15, b'python3', 0, 0, 0)
                        emit('started')
                    else:
                        break
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGINT)
                try:
                    process.wait(timeout=20)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait()
            reader.join(timeout=10)
            errors.seek(0)
            emit('result', exit_code=process.returncode, records=records, stderr=errors.read())


class TraceSession:
    def __init__(self, target, spec, directory):
        from common import GENERATED, ssh_command
        self.directory = Path(directory)
        self.directory.mkdir(parents=True)
        self.ready = threading.Event()
        self.records, self.error = [], None
        self.stderr = (self.directory / 'ssh-stderr.log').open('w')
        self.process = subprocess.Popen(ssh_command(GENERATED / 'ssh_config', target,
             ['sudo', '-n', 'python3', '-u', '-c', Path(__file__).read_text(), '--agent', json.dumps(spec)]),
             stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self.stderr, text=True)
        def read():
            try:
                with (self.directory / 'trace.jsonl').open('w') as output:
                    for line in self.process.stdout:
                        record = json.loads(line)
                        output.write(line); output.flush(); self.records.append(record)
                        if record['kind'] == 'ready':
                            (self.directory / 'program.bt').write_text(record['program'])
                            self.ready.set()
            except (OSError, ValueError) as error:
                self.error = str(error)
            finally:
                self.ready.set()
        self.reader = threading.Thread(target=read, daemon=True)
        self.reader.start()

    def start(self):
        self.process.stdin.write('start\n'); self.process.stdin.flush()

    def stop(self):
        if self.process.poll() is None:
            try:
                self.process.stdin.write('stop\n'); self.process.stdin.flush()
                self.process.wait(timeout=40)
            except (OSError, subprocess.TimeoutExpired):
                self.process.kill(); self.process.wait()
        self.reader.join(timeout=10)
        self.stderr.close(); self.process.stdin.close(); self.process.stdout.close()


if __name__ == '__main__' and '--agent' in sys.argv:
    agent(json.loads(sys.argv[-1]))
