"""Separate bounded captures proving the head-to-head VXLAN packet path."""
import ipaddress
import json
from pathlib import Path
import struct
import subprocess
import sys
import time

from common import GENERATED, ROOT, CILIUM_TUNNEL_MODES, guests, remote, save_json
from diagnostics import Monitor


def capture_filter(cidrs):
    """Match inner Pod TCP, excluding node/control-plane traffic from the cap."""
    networks = [ipaddress.IPv4Network(cidr) for cidr in cidrs]
    if len(networks) != 2 or networks[0].overlaps(networks[1]):
        raise ValueError('Two disjoint worker Pod CIDRs are required')
    def match(offset, network):
        return f'(udp[{offset}:4] & {int(network.netmask):#x}) = {int(network.network_address):#x}'
    a,b = networks
    return ('udp and (port 8472 or port 4789) and udp[39] = 6 and ('+
            f'({match(42,a)} and {match(46,b)}) or ({match(42,b)} and {match(46,a)}))')


def vxlan_packets(path, endpoint_ips):
    """Inspect Ethernet/IPv4 PCAP headers, including VLANs; no packet dependency."""
    def ipv4(frame, offset):
        while len(frame) >= offset + 4 and frame[offset-2:offset] in (b'\x81\x00', b'\x88\xa8'):
            offset += 4
        if len(frame) < offset + 20 or frame[offset-2:offset] != b'\x08\x00' or frame[offset] >> 4 != 4:
            return None
        size = (frame[offset] & 15)*4
        if size < 20 or len(frame) < offset + size:
            return None
        return dict(offset=offset, header=size, length=int.from_bytes(frame[offset+2:offset+4], 'big'),
                    fragment=int.from_bytes(frame[offset+6:offset+8], 'big') & 0x3fff,
                    protocol=frame[offset+9], src=str(ipaddress.IPv4Address(frame[offset+12:offset+16])),
                    dst=str(ipaddress.IPv4Address(frame[offset+16:offset+20])))
    packets = []
    with Path(path).open('rb') as stream:
        header = stream.read(24)
        if len(header) != 24:
            raise ValueError('Missing PCAP header')
        endian = {b'\xd4\xc3\xb2\xa1':'<', b'\xa1\xb2\xc3\xd4':'>',
                  b'\x4d\x3c\xb2\xa1':'<', b'\xa1\xb2\x3c\x4d':'>'}.get(header[:4])
        if endian is None or struct.unpack(endian+'I', header[20:24])[0] != 1:
            raise ValueError('Expected classic Ethernet PCAP')
        while raw := stream.read(16):
            if len(raw) != 16:
                raise ValueError('Truncated PCAP record')
            length = struct.unpack(endian+'IIII', raw)[2]
            if length > 65536:
                raise ValueError('Unexpected capture length')
            frame = stream.read(length)
            if len(frame) != length:
                raise ValueError('Truncated capture frame')
            outer = ipv4(frame, 14)
            if not outer or outer['protocol'] != 17:
                continue
            udp = outer['offset'] + outer['header']
            if len(frame) < udp+16:
                continue
            port = int.from_bytes(frame[udp+2:udp+4], 'big')
            if port not in (8472, 4789):
                continue
            # UDP(8), VXLAN(8), inner Ethernet(14).
            inner = ipv4(frame, udp+30)
            if inner and {inner['src'], inner['dst']} == set(endpoint_ips):
                packets.append(dict(port=port, outer=outer, inner=inner))
    return packets


def validate_packets(packets, port):
    if not packets:
        raise ValueError('No benchmark Pod packets found in physical VXLAN capture')
    if any(p['port'] != port or p['inner']['protocol'] != 6 or p['inner']['fragment'] or p['outer']['fragment'] for p in packets):
        raise ValueError('Wrong tunnel port, nested tunnel or fragmented benchmark traffic')
    return dict(matched_tcp_packets=len(packets), inner_max_length=max(p['inner']['length'] for p in packets),
                port=port, unfragmented=True,
                scope='Physical egress capture can contain GSO super-packets; lengths are not exact wire-frame measurements')


def validate_path(directory, mode, smoke=False):
    directory.mkdir()
    inventory = guests(GENERATED/'guest_inventory.json')
    source = json.loads((ROOT/'infrastructure/config.json').read_text())
    cilium = mode in CILIUM_TUNNEL_MODES
    port = 8472 if cilium else 4789
    from run import KUBECTL
    controller = next(name for name,h in inventory.items() if h['cs740_role']=='controller')
    nodes = json.loads(remote(GENERATED/'ssh_config', controller, KUBECTL+
                             ['get','nodes','worker-1','worker-2','-o','json']))
    cidrs = {n['metadata']['name']:n['spec']['podCIDR'] for n in nodes['items']}
    if set(cidrs) != {'worker-1','worker-2'}:
        raise ValueError('Cannot identify the two worker Pod CIDRs')
    packet_filter = capture_filter([cidrs['worker-1'],cidrs['worker-2']])
    save_json(directory/'capture-filter.json', dict(worker_pod_cidrs=cidrs, filter=packet_filter))
    monitors = {}
    try:
        for vm in ('worker-1', 'worker-2'):
            host = next(h for h in source['hypervisors'] if h['name'] == inventory[vm]['hypervisor'])
            monitor = Monitor(GENERATED/'ssh_config', 'cs740-'+host['name']+'-jump', directory/host['name'])
            monitors[host['name']] = monitor
            if not monitor.ready.wait(60) or monitor.error or monitor.process.poll() is not None:
                raise RuntimeError('Capture monitor failed: '+host['name'])
            # Fixed packet count bounds capture size; all capture data is excluded.
            monitor.control(json.dumps({'capture': dict(interface=host['network']['source'], direction='out',
                packet_limit=2000, filter=packet_filter)}))
            deadline = time.monotonic()+20
            while not any(r['kind'] == 'capture_started' for r in monitor.records):
                if monitor.error or monitor.process.poll() is not None or time.monotonic() > deadline:
                    raise RuntimeError('Capture failed to start: '+host['name'])
                time.sleep(.1)
        # Prove the outer transport can carry 1500-byte IPv4 packets with DF.
        if cilium:
            ping_target = inventory['worker-2']['experiment_ipv4'].split('/')[0]
            ping_from = 'worker-1'
        else:
            host_inventory = json.loads((GENERATED/'experiment_hypervisors.json').read_text())['all']['children']['hypervisors']['hosts']
            a, b = [inventory[v]['hypervisor'] for v in ('worker-1', 'worker-2')]
            ping_target = host_inventory[b]['vxlan_underlay_ipv4']
            ping_from = 'cs740-'+a+'-jump'
        ping = remote(GENERATED/'ssh_config', ping_from,
                      ['ping', '-n', '-I', 'experiment' if cilium else host_inventory[a]['vxlan_underlay_interface'],
                       '-M', 'do', '-s', '1472', '-c', '3', '-W', '3', ping_target])
        (directory/'outer-mtu-ping.txt').write_text(ping)
        with (directory/'benchmark.log').open('w') as log:
            subprocess.run([sys.executable, str(ROOT/'experiments/run.py'), mode,
                        '--client', 'worker-1', '--server', 'worker-2', '--pod-only',
                        '--test-type', 'single', '--duration', '3' if smoke else '5', '--samples', '1',
                        '--output', str(directory/'measurement')], check=True, stdout=log, stderr=subprocess.STDOUT)
    finally:
        errors = []
        for label, monitor in monitors.items():
            try:
                monitor.stop()
                result = next((r for r in monitor.records if r['kind'] == 'capture_result'), {})
                if monitor.error or result.get('exit_code') != 0:
                    raise RuntimeError('Capture did not finish cleanly: '+label)
            except Exception as error:
                errors.append(str(error))
        if errors:
            raise RuntimeError('; '.join(errors))
    measurement = directory/'measurement'
    meta = json.loads((measurement/'metadata.json').read_text())
    meta.update(instrumented=True, instrumentation='bounded-path-capture', quality='path-validation')
    save_json(measurement/'metadata.json', meta)
    pods = json.loads((measurement/'test-pods.json').read_text())['items']
    ips = {p['status']['podIP'] for p in pods if not p['spec'].get('hostNetwork') and
           p['spec'].get('nodeName') in ('worker-1', 'worker-2')}
    if len(ips) != 2:
        raise ValueError('Cannot identify unique benchmark Pod endpoints')
    proof = {}
    for label, monitor in monitors.items():
        packets = vxlan_packets(monitor.directory/'capture.pcap', ips)
        proof[label] = validate_packets(packets, port)
    if not any(p['inner_max_length'] >= 1450 for p in proof.values()):
        raise ValueError('Full-size inner TCP packets not observed on either transmission leg')
    for vm in ('worker-1', 'worker-2'):
        links = json.loads((measurement/(vm+'-links.json')).read_text())
        names = {l['ifname'] for l in links}
        if ('cilium_vxlan' in names) != cilium or 'cs740vx' in names:
            raise ValueError('Guest tunnel topology does not match placement: '+vm)
    save_json(directory/'validation.json', dict(status='passed', mode=mode, instrumented=True,
              excluded_from_primary=True, endpoint_ips=sorted(ips), physical_captures=proof))
