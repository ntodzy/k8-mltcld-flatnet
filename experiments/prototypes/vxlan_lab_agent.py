"""Root-only disposable namespace lab; receives the compiled BPF object on stdin."""
import base64
import json
import os
from pathlib import Path
import signal
import struct
import subprocess
import sys
import tempfile
import uuid


def program_id(filters):
    for entry in filters:
        options = entry.get('options', {})
        found = options.get('id') or options.get('prog', {}).get('id')
        if found:
            return found
    raise RuntimeError('tc did not report the owned BPF program ID')


def route_counters(result):
    raw = result.get('value')
    if isinstance(raw, list):
        value = bytes(int(v, 16) if isinstance(v, str) else v for v in raw)
        if len(value) != 56:
            raise RuntimeError('Unexpected forwarding map ABI')
        return dict(zip(('redirected', 'fallback', 'dropped'), struct.unpack_from('<QQQ', value, 32)))
    return result['formatted']['value']


def run_lab(payload):
    if os.geteuid() != 0:
        raise RuntimeError('The isolated lab requires root')
    prefix = 'c740b-' + uuid.uuid4().hex[:8]
    names = {key: prefix + '-' + key for key in ('a', 'r1', 'r2', 'b')}
    created, processes, commands, attachments = [], [], [], []
    report = {'status': 'running', 'namespaces': names, 'commands': commands,
              'attachments': attachments, 'kind': 'isolated-vxlan-bpf-lab',
              'kernel': os.uname().release,
              'scope': 'same-host synthetic namespaces; not Cilium or paper performance data'}

    def cmd(args, check=True):
        result = subprocess.run([str(a) for a in args], capture_output=True, text=True, timeout=30)
        commands.append({'argv': [str(a) for a in args], 'returncode': result.returncode,
                         'stdout': result.stdout, 'stderr': result.stderr})
        if check and result.returncode:
            raise RuntimeError(f'{args}: {result.stderr}')
        return result

    def ns(key, *args, check=True):
        return cmd(['ip', 'netns', 'exec', names[key], *args], check=check)

    def ip(key, *args):
        return ns(key, 'ip', *args)

    def links(key):
        return {x['ifname']: x for x in json.loads(ip(key, '-j', 'link', 'show').stdout)}

    def mac_bytes(s):
        return bytes.fromhex(s.replace(':', ''))

    def ping(key, destination, size=56, ttl=64, check=True):
        return ns(key, 'ping', '-n', '-c', '2', '-W', '1', '-M', 'do',
                  '-s', str(size), '-t', str(ttl), destination, check=check)

    try:
        with tempfile.TemporaryDirectory(prefix=prefix+'-') as temporary:
            obj = Path(temporary) / 'redirect.o'
            obj.write_bytes(base64.b64decode(payload['object']))
            for key in names:
                cmd(['ip', 'netns', 'add', names[key]])
                created.append(key)
                ip(key, 'link', 'set', 'lo', 'up')
            # All interfaces are created directly inside owned namespaces.
            # No physical interface, existing bridge, VM or root route is touched.
            for a, ai, b, bi in [('a', 'eth0', 'r1', 'pod'),
                                  ('r1', 'under', 'r2', 'under'),
                                  ('r2', 'pod', 'b', 'eth0')]:
                ip(a, 'link', 'add', ai, 'type', 'veth', 'peer', 'name', bi,
                   'netns', names[b])
            for key, dev, address, mtu in [
                ('a', 'eth0', '10.201.1.2/24', 1450),
                ('r1', 'pod', '10.201.1.1/24', 1450),
                ('r1', 'under', '192.0.2.1/24', 1500),
                ('r2', 'under', '192.0.2.2/24', 1500),
                ('r2', 'pod', '10.201.2.1/24', 1450),
                ('b', 'eth0', '10.201.2.2/24', 1450)]:
                ip(key, 'addr', 'add', address, 'dev', dev)
                ip(key, 'link', 'set', dev, 'mtu', str(mtu), 'up')
            for key, local, peer, mac, pmac in [
                ('r1', '192.0.2.1', '192.0.2.2', '02:74:00:00:00:01', '02:74:00:00:00:02'),
                ('r2', '192.0.2.2', '192.0.2.1', '02:74:00:00:00:02', '02:74:00:00:00:01')]:
                ip(key, 'link', 'add', 'vx', 'type', 'vxlan', 'id', '740',
                   'local', local, 'dev', 'under', 'dstport', '4789', 'nolearning')
                ip(key, 'link', 'set', 'vx', 'address', mac, 'mtu', '1450', 'up')
                ns(key, 'bridge', 'fdb', 'replace', pmac, 'dev', 'vx', 'dst', peer, 'self', 'permanent')
                ns(key, 'sysctl', '-qw', 'net.ipv4.ip_forward=1')
                ns(key, 'sysctl', '-qw', 'net.ipv4.conf.all.rp_filter=0')
                ns(key, 'sysctl', '-qw', 'net.ipv4.conf.vx.rp_filter=0')
            for key, address, peer, pmac, remote in [
                ('r1', '10.245.0.1/24', '10.245.0.2', '02:74:00:00:00:02', '10.201.2.0/24'),
                ('r2', '10.245.0.2/24', '10.245.0.1', '02:74:00:00:00:01', '10.201.1.0/24')]:
                ip(key, 'addr', 'add', address, 'dev', 'vx')
                ip(key, 'neigh', 'replace', peer, 'lladdr', pmac, 'nud', 'permanent', 'dev', 'vx')
                ip(key, 'route', 'add', remote, 'via', peer, 'dev', 'vx')
            ip('a', 'route', 'add', '10.201.2.0/24', 'via', '10.201.1.1')
            ip('b', 'route', 'add', '10.201.1.0/24', 'via', '10.201.2.1')
            report['baseline_ping'] = ping('a', '10.201.2.2', 1422).stdout
            # Negative control: baseline cannot deliver without IP forwarding.
            for key in ('r1', 'r2'):
                ns(key, 'sysctl', '-qw', 'net.ipv4.ip_forward=0')
            blocked = ping('a', '10.201.2.2', check=False)
            if blocked.returncode == 0:
                raise RuntimeError('Negative control unexpectedly bypasses IP forwarding')
            report['negative_control_blocked'] = True
            ll = {key: links(key) for key in names}
            for key, ingress, output, destination, next_mac in [
                ('r1', 'pod', 'vx', '10.201.2.0', ll['r2']['vx']['address']),
                ('r2', 'pod', 'vx', '10.201.1.0', ll['r1']['vx']['address']),
                ('r1', 'vx', 'pod', '10.201.1.0', ll['a']['eth0']['address']),
                ('r2', 'vx', 'pod', '10.201.2.0', ll['b']['eth0']['address'])]:
                ns(key, 'tc', 'qdisc', 'add', 'dev', ingress, 'clsact')
                ns(key, 'tc', 'filter', 'add', 'dev', ingress, 'ingress', 'pref', '10',
                   'bpf', 'da', 'obj', str(obj), 'sec', 'classifier')
                filters = json.loads(ns(key, 'tc', '-j', 'filter', 'show', 'dev', ingress, 'ingress').stdout)
                program = program_id(filters)
                info = json.loads(cmd(['bpftool', '-j', 'prog', 'show', 'id', str(program)]).stdout)
                if isinstance(info, list): info = info[0]
                maps = info['map_ids']
                if len(maps) != 1: raise RuntimeError('Unexpected program map ownership')
                value = struct.pack('<III6s6sI4xQQQ',
                    int.from_bytes(bytes(map(int, destination.split('.'))), 'big'),
                    0xffffff00, ll[key][output]['ifindex'],
                    mac_bytes(ll[key][output]['address']), mac_bytes(next_mac), 1450, 0, 0, 0)
                cmd(['bpftool', 'map', 'update', 'id', str(maps[0]), 'key', 'hex',
                     '00', '00', '00', '00', 'value', 'hex', *[f'{b:02x}' for b in value]])
                attachments.append({'namespace': key, 'ingress': ingress, 'output': output,
                                    'program_id': program, 'map_id': maps[0]})
            report['redirect_ping'] = ping('a', '10.201.2.2', 1422).stdout
            report['reverse_ping'] = ping('b', '10.201.1.2', 1422).stdout
            if 'ttl=62' not in report['redirect_ping']:
                raise RuntimeError('Two routing TTL decrements were not observed')
            expired = ping('a', '10.201.2.2', ttl=1, check=False)
            if expired.returncode == 0: raise RuntimeError('TTL exhaustion was not enforced')
            report['ttl_exhaustion_blocked'] = True
            # A brief TCP round-trip correctness check, not a latency study.
            server = '''import socket
s=socket.socket();s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)
s.bind(('10.201.2.2',18080));s.listen(1);print('ready',flush=True)
c,_=s.accept();c.settimeout(10)
while True:
 d=c.recv(4096)
 if not d:break
 c.sendall(d)
'''
            proc = subprocess.Popen(['ip', 'netns', 'exec', names['b'], 'python3', '-u', '-c', server],
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            processes.append(proc)
            import select
            if not select.select([proc.stdout], [], [], 5)[0] or proc.stdout.readline().strip() != 'ready':
                raise RuntimeError('TCP echo server did not start')
            client = '''import socket,json
s=socket.create_connection(('10.201.2.2',18080),5);s.settimeout(5)
for i in range(1000):
 data=i.to_bytes(4,'big');s.sendall(data);got=b''
 while len(got)<4:
  part=s.recv(4-len(got))
  if not part:raise RuntimeError('short echo')
  got+=part
 assert got==data
s.close();print(json.dumps({'completed_tcp_exchanges':1000}))
'''
            report['tcp'] = json.loads(ns('a', 'python3', '-c', client).stdout)
            for attachment in attachments:
                attachment['counters'] = json.loads(cmd(['bpftool', '-j', 'map', 'lookup', 'id',
                    str(attachment['map_id']), 'key', 'hex', '00', '00', '00', '00']).stdout)
                attachment['decoded_counters'] = route_counters(attachment['counters'])
                if attachment['decoded_counters']['redirected'] < 1000:
                    raise RuntimeError('An expected BPF path did not carry the TCP exchanges')
            if attachments[0]['decoded_counters']['dropped'] < 2:
                raise RuntimeError('TTL test did not exercise the owned drop branch')
            # Remove only our filters and re-enable the baseline: reversal check.
            for attachment in attachments:
                ns(attachment['namespace'], 'tc', 'qdisc', 'del', 'dev', attachment['ingress'], 'clsact')
            for key in ('r1', 'r2'):
                ns(key, 'sysctl', '-qw', 'net.ipv4.ip_forward=1')
            report['restored_baseline_ping'] = ping('a', '10.201.2.2', 1422).stdout
            report['status'] = 'passed'
    except BaseException as error:
        report['status'] = 'failed'
        report['error'] = f'{type(error).__name__}: {error}'
    finally:
        for process in processes:
            if process.poll() is None: process.terminate()
            try: process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill(); process.communicate()
        # Delete even partially configured owned namespaces; inspect cleanup.
        failures = []
        for key in reversed(created):
            result = cmd(['ip', 'netns', 'del', names[key]], check=False)
            if result.returncode: failures.append(names[key])
        remaining = cmd(['ip', 'netns', 'list'], check=False)
        if remaining.returncode:
            failures.append('namespace audit unavailable')
        else:
            listed = {line.split()[0] for line in remaining.stdout.splitlines() if line.strip()}
            failures.extend(n for n in names.values() if n in listed and n not in failures)
        report['owned_namespaces_remaining'] = [n for n in names.values() if n in failures]
        report['cleanup_failed'] = failures
        if failures: report['status'] = 'failed'
    return report


def main():
    def stop(signum, frame): raise RuntimeError(f'Interrupted by signal {signum}')
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGHUP, stop)
    report = run_lab(json.loads(sys.stdin.readline()))
    print(json.dumps(report), flush=True)
    return 0 if report['status'] == 'passed' else 1


if __name__ == '__main__':
    sys.exit(main())
