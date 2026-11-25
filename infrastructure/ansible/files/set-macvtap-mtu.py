#!/usr/bin/env python3
"""Set a managed VM's live macvtap MTU without unsupported libvirt MTU XML."""
import argparse
import json
import subprocess
import xml.etree.ElementTree as ET


def command(argv):
    return subprocess.run(argv, check=True, text=True, stdout=subprocess.PIPE).stdout


def reconcile(domain, mac, parent, mtu):
    if not 1280 <= mtu <= 9000:
        raise ValueError('MTU must be between 1280 and 9000')
    root = ET.fromstring(command(['virsh', '-c', 'qemu:///system', 'dumpxml', domain]))
    interfaces = [interface for interface in root.findall('./devices/interface')
                  if interface.get('type') == 'direct' and interface.find('mac') is not None
                  and interface.find('mac').get('address', '').lower() == mac.lower()]
    if len(interfaces) != 1:
        raise ValueError(f'{domain}: expected one direct experiment interface matching {mac}')
    source = interfaces[0].find('source')
    target = interfaces[0].find('target')
    if source is None or source.get('dev') != parent:
        raise ValueError(f'{domain}: experiment interface does not use parent {parent}')
    if target is None or not target.get('dev'):
        raise ValueError(f'{domain}: no live macvtap target; ensure the VM is running')
    name = target.get('dev')
    parent_link = json.loads(command(['ip', '-j', '-d', 'link', 'show', 'dev', parent]))[0]
    if parent_link['mtu'] < mtu:
        raise ValueError(f'{parent}: raise the physical interface MTU to at least {mtu} first')
    link = json.loads(command(['ip', '-j', '-d', 'link', 'show', 'dev', name]))[0]
    if link.get('linkinfo', {}).get('info_kind') != 'macvtap':
        raise ValueError(f'{domain}: {name} is not a macvtap device')
    changed = link['mtu'] != mtu
    if changed:
        command(['ip', 'link', 'set', 'dev', name, 'mtu', str(mtu)])
        actual = json.loads(command(['ip', '-j', '-d', 'link', 'show', 'dev', name]))[0]
        if actual['mtu'] != mtu:
            raise ValueError(f'{name}: requested MTU was not applied')
    return {'changed': changed, 'domain': domain, 'interface': name, 'mtu': mtu}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--domain', required=True)
    parser.add_argument('--mac', required=True)
    parser.add_argument('--parent', required=True)
    parser.add_argument('--mtu', type=int, required=True)
    args = parser.parse_args()
    try:
        print(json.dumps(reconcile(args.domain, args.mac, args.parent, args.mtu)))
    except (ValueError, KeyError, OSError, ET.ParseError, subprocess.CalledProcessError) as error:
        raise SystemExit(f'Macvtap MTU setup failed: {error}')
