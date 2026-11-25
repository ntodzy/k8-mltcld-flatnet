#!/usr/bin/env python3
"""Reversible tunnel/bypass controls on an already prepared custom VXLAN mode."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import fcntl
import json
from pathlib import Path
import subprocess

from common import GENERATED, ROOT, guests, remote, save_json

MARKER = GENERATED / 'path-control.json'

# The controller supplies only validated deployment names and the allocated
# PodCIDRs. No management interface, default route, VM definition or Cilium
# configuration is changed by these controls.
AGENT = r'''
import ipaddress,json,subprocess,sys
from pathlib import Path
s=json.loads(sys.argv[1])
def run(a):return subprocess.check_output(a,text=True).strip()
def links():return json.loads(run(['ip','-j','-d','link','show']))
before=links()
if s['kind']=='host':
    interface=s['interface']; bridge='cs740br'; tunnel='cs740hvx'
    if json.loads(run(['ip','-j','-4','route','show','default','dev',interface])):
        raise ValueError('Experiment interface carries a default route; refusing to change it')
    byname={x['ifname']:x for x in before}
    if not all(name in byname for name in (interface,bridge,tunnel)):
        raise ValueError('Prepared host VXLAN devices are required')
    parent=byname[interface].get('master')
    if parent is not None and parent not in (bridge,byname[bridge]['ifindex']):
        raise ValueError('Experiment interface belongs to an unmanaged bridge')
    if s['condition']=='bypass':
        run(['ip','link','set','dev',tunnel,'nomaster'])
        run(['ip','link','set','dev',interface,'master',bridge])
    else:
        run(['ip','link','set','dev',interface,'nomaster'])
        run(['ip','link','set','dev',tunnel,'master',bridge,'up'])
    run(['ip','link','set','dev',bridge,'mtu','1450','up'])
else:
    routes=json.loads(run(['ip','-j','-4','route','show']))
    if s['condition']=='bypass':
        # Refuse an unrelated route before removing any experiment route.
        for peer in s['peers']:
            for route in routes:
                if route.get('dst')==peer['cidr'] and str(route.get('protocol')) not in ('242','243'):
                    raise ValueError('An unmanaged route overlaps the controlled PodCIDR')
        run(['ip','-4','route','flush','proto','242'])
        for peer in s['peers']:
            ipaddress.ip_network(peer['cidr']);ipaddress.ip_address(peer['ip'])
            run(['ip','-4','route','replace',peer['cidr'],'via',peer['ip'],'dev','experiment','proto','243'])
    else:
        run(['ip','-4','route','flush','proto','243'])
        # Keep the same tunnel device, socket, neighbors and FDB throughout
        # the pair. Recreating the device would introduce another variable.
        for peer in s['peers']:
            run(['ip','-4','route','replace',peer['cidr'],'via',peer['vtep'],'dev','cs740vx','proto','242'])
print(json.dumps({'before_links':before,'after_links':links(),
 'routes':json.loads(run(['ip','-j','-4','route','show'])),
 'bridge_fdb':json.loads(run(['bridge','-j','fdb','show'])),
 'kernel':run(['uname','-r']),'specification':s}))
'''


def specifications(mode, condition, config, inventory, nodes):
    if mode not in ('host-vxlan-1450', 'vm-vxlan-1450') or condition not in ('tunnel', 'bypass'):
        raise ValueError('Controls support custom 1450 modes and tunnel/bypass only')
    if mode == 'host-vxlan-1450':
        return {f"cs740-{host['name']}-jump": {'kind': 'host', 'condition': condition,
                    'interface': host['network']['source']} for host in config['hypervisors']}
    cidrs = {node['metadata']['name']: node['spec']['podCIDR'] for node in nodes['items']}
    return {name: {'kind': 'vm', 'condition': condition,
                  'peers': [{'name': peer, 'cidr': cidrs[peer],
                             'vtep': f'10.245.0.{sorted(inventory).index(peer) + 1}',
                             'ip': inventory[peer]['experiment_ipv4'].split('/')[0]}
                            for peer in sorted(inventory) if peer != name]}
            for name in inventory}


def switch(mode, condition, output):
    active = json.loads((GENERATED / 'active-experiment.json').read_text())
    if active['mode'] != mode:
        raise ValueError('Prepare the matching custom VXLAN mode first')
    config = json.loads((ROOT / 'infrastructure/config.json').read_text())
    inventory = guests(GENERATED / 'guest_inventory.json')
    controller = next(name for name, vm in inventory.items() if vm['cs740_role'] == 'controller')
    nodes = json.loads(remote(GENERATED / 'ssh_config', controller,
                       ['sudo', '-n', 'kubectl', '--kubeconfig=/etc/kubernetes/admin.conf', 'get', 'nodes', '-o', 'json']))
    specs = specifications(mode, condition, config, inventory, nodes)
    # A preparing marker prevents a normal benchmark from accidentally running
    # during or after a partially completed switch.
    save_json(MARKER, {'mode': mode, 'condition': condition, 'status': 'changing'})
    records, failures = {}, []
    def apply(item):
        name, spec = item
        try:
            return name, json.loads(remote(GENERATED / 'ssh_config', name,
                         ['sudo', '-n', 'python3', '-c', AGENT, json.dumps(spec)])), None
        except (ValueError, OSError, subprocess.CalledProcessError) as error:
            return name, None, f'{name}: {type(error).__name__}; see remote traceback'
    with ThreadPoolExecutor(max_workers=len(specs)) as pool:
        for name, record, failure in pool.map(apply, specs.items()):
            records[name] = record
            if failure:
                failures.append(failure)
    proof = {'mode': mode, 'condition': condition, 'status': 'failed' if failures else 'ready',
             'targets': records, 'errors': failures}
    save_json(output, proof)
    save_json(MARKER, proof)
    if failures:
        raise RuntimeError('Path switch incomplete: ' + '; '.join(failures))
    return proof


def validate(snapshot, label, hosts, mode, condition, nodes):
    """Validate the actual path, rather than trusting the local marker alone."""
    links = {link['ifname']: link for link in json.loads(snapshot['links'])}
    if mode == 'host-vxlan-1450':
        if not label.startswith('hypervisor-'):
            return
        config = json.loads((ROOT / 'infrastructure/config.json').read_text())
        host = next(h for h in config['hypervisors'] if label == 'hypervisor-' + h['name'])
        physical = links[host['network']['source']]
        bridge, tunnel = links['cs740br'], links['cs740hvx']
        def attached(link):
            return link.get('master') in ('cs740br', bridge['ifindex'])
        correct = (attached(tunnel) and not attached(physical)) if condition == 'tunnel' else (attached(physical) and not attached(tunnel))
        if not correct or bridge['mtu'] != 1450:
            raise ValueError(f'{label}: live bridge ports/MTU do not match {condition}')
    elif label in hosts:
        node_map = {node['metadata']['name']: node for node in nodes['items']}
        routes = json.loads(snapshot['routes'])
        for peer in hosts:
            if peer == label:
                continue
            cidr = node_map[peer]['spec']['podCIDR']
            matches = [route for route in routes if route.get('dst') == cidr]
            expected_device = 'cs740vx' if condition == 'tunnel' else 'experiment'
            if len(matches) != 1 or matches[0].get('dev') != expected_device:
                raise ValueError(f'{label}: route to {cidr} does not match {condition}')
            expected_protocol = '242' if condition == 'tunnel' else '243'
            if str(matches[0].get('protocol')) != expected_protocol:
                raise ValueError(f'{label}: controlled route has an unexpected protocol')
            if condition == 'bypass' and matches[0].get('gateway') != hosts[peer]['experiment_ipv4'].split('/')[0]:
                raise ValueError(f'{label}: bypass route has an unexpected next hop')
            if condition == 'tunnel' and matches[0].get('gateway') != f'10.245.0.{sorted(hosts).index(peer) + 1}':
                raise ValueError(f'{label}: tunnel route has an unexpected VTEP')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=('host-vxlan-1450', 'vm-vxlan-1450'))
    parser.add_argument('condition', choices=('tunnel', 'bypass'))
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise ValueError('Output already exists')
    with (GENERATED / 'collection.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        switch(args.mode, args.condition, args.output)
        if args.condition == 'tunnel':
            MARKER.unlink()


if __name__ == '__main__':
    main()
