#!/usr/bin/env python3
"""Enter CloudLab hostnames and experiment interfaces without editing Ansible/Terraform."""
import argparse
import copy
import ipaddress
import json
import os
from pathlib import Path
import re
import shlex
import tempfile

from common import ROOT


def assignments(values, label):
    result = {}
    for value in values:
        name, separator, setting = value.partition('=')
        if not separator or not name or not setting:
            raise ValueError(f'{label} must be NAME=VALUE: {value}')
        if name in result:
            raise ValueError(f'Duplicate {label} for {name}')
        result[name] = setting
    return result


def update_config(source, hostnames, interfaces, guest_addresses, ssh_user=None, libvirt_uris=None):
    config = copy.deepcopy(source)
    hosts = {host['name']: host for host in config['hypervisors']}
    vms = {vm['name']: vm for host in hosts.values() for vm in host['vms']}
    for values, known, label in ((hostnames, hosts, 'hypervisor'), (interfaces, hosts, 'hypervisor'),
                                  (libvirt_uris or {}, hosts, 'hypervisor'), (guest_addresses, vms, 'VM')):
        unknown = values.keys() - known.keys()
        if unknown:
            raise ValueError(f'Unknown {label}: {", ".join(sorted(unknown))}')
    for name, address in hostnames.items():
        old = hosts[name]['address']
        uri = hosts[name].get('libvirt_uri', '')
        if address != old and old in uri and name not in (libvirt_uris or {}):
            raise ValueError(f'{name} has a custom libvirt URI pointing to {old}; '
                             f'also set --libvirt-uri {name}=URI')
        hosts[name]['address'] = address
    for name, interface in interfaces.items():
        hosts[name]['network']['source'] = interface
    for name, address in guest_addresses.items():
        vms[name]['ipv4_address'] = address
    for name, uri in (libvirt_uris or {}).items():
        hosts[name]['libvirt_uri'] = uri
    if ssh_user is not None:
        config['ssh']['user'] = ssh_user
    validate(config)
    return config


def validate(config):
    user = config['ssh']['user']
    if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_.-]*', user) or user == 'your-cloudlab-user':
        raise ValueError('Enter your existing CloudLab SSH username (--ssh-user USER)')
    endpoints = []
    addresses = []
    for host in config['hypervisors']:
        address = host['address']
        try:
            ipaddress.ip_address(address)
        except ValueError:
            labels = address.rstrip('.').split('.')
            if len(address) > 253 or any(not re.fullmatch(r'[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?', label) for label in labels):
                raise ValueError(f'Invalid hostname or IP for {host["name"]}: {address}')
        if address.endswith('.example.cloudlab.us'):
            raise ValueError(f'Replace the example hostname for {host["name"]}')
        endpoints.append(address.rstrip('.').lower())
        network = host['network']
        if network['mode'] != 'direct':
            raise ValueError('Use the original base config with direct experiment NIC attachments')
        if not re.fullmatch(r'[A-Za-z0-9_.:-]{1,15}', network['source']):
            raise ValueError(f'Invalid experiment interface for {host["name"]}')
        for vm in host['vms']:
            address = vm.get('ipv4_address')
            if address is None:
                continue
            if '/' not in address:
                raise ValueError(f'{vm["name"]} requires an IPv4 address with prefix, e.g. 10.74.0.11/24')
            addresses.append(ipaddress.IPv4Interface(address).ip)
    if not endpoints or len(endpoints) != len(set(endpoints)):
        raise ValueError('Each hypervisor requires a distinct hostname/address')
    if len(addresses) != len(set(addresses)):
        raise ValueError('VM experiment IPv4 addresses must be unique')


def prompt(label, current):
    return input(f'{label} [{current}]: ').strip() or current


def interactive_values(source, edit_guests):
    print('Press Enter to keep the existing value. Existing SSH keys are reused.')
    user = prompt('CloudLab SSH username', source['ssh']['user'])
    hosts, interfaces, addresses = {}, {}, {}
    for host in source['hypervisors']:
        name = host['name']
        hosts[name] = prompt(f'{name} CloudLab hostname', host['address'])
        interfaces[name] = prompt(f'{name} experiment NIC (not management)', host['network']['source'])
        if edit_guests:
            for vm in host['vms']:
                addresses[vm['name']] = prompt(f'{vm["name"]} experiment IPv4/prefix', vm.get('ipv4_address') or '')
    return hosts, interfaces, addresses, user


def atomic_write(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as output:
        temporary = Path(output.name)
        output.write(content)
    try:
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, default=ROOT / 'infrastructure/config.json')
    parser.add_argument('--host', action='append', default=[], metavar='NAME=HOSTNAME')
    parser.add_argument('--interface', action='append', default=[], metavar='NAME=NIC')
    parser.add_argument('--guest-address', action='append', default=[], metavar='NAME=IP/PREFIX')
    parser.add_argument('--ssh-user', help='Existing CloudLab account; SSH key paths are preserved')
    parser.add_argument('--libvirt-uri', action='append', default=[], metavar='NAME=URI')
    parser.add_argument('--interactive', action='store_true', help='Prompt for settings; automatic when no settings are supplied')
    parser.add_argument('--edit-guests', action='store_true', help='Also prompt for guest experiment addresses')
    parser.add_argument('--dry-run', action='store_true', help='Show proposed settings without writing files or contacting hosts')
    args = parser.parse_args()
    destination = args.config.expanduser().resolve()
    template = ROOT / 'infrastructure/config.example.json'
    source_path = destination if destination.exists() else template
    original = source_path.read_bytes()
    source = json.loads(original)
    hostnames = assignments(args.host, 'host')
    interfaces = assignments(args.interface, 'interface')
    addresses = assignments(args.guest_address, 'guest address')
    uris = assignments(args.libvirt_uri, 'libvirt URI')
    user = args.ssh_user
    if args.interactive or args.edit_guests or (not args.dry_run and not (hostnames or interfaces or addresses or uris or user)):
        prompt_source = copy.deepcopy(source)
        # Supplied flags are the defaults shown in the wizard.
        for host in prompt_source['hypervisors']:
            host['address'] = hostnames.get(host['name'], host['address'])
            host['network']['source'] = interfaces.get(host['name'], host['network']['source'])
            for vm in host['vms']:
                vm['ipv4_address'] = addresses.get(vm['name'], vm.get('ipv4_address'))
        if user:
            prompt_source['ssh']['user'] = user
        prompted = interactive_values(prompt_source, args.edit_guests)
        hostnames.update(prompted[0])
        interfaces.update(prompted[1])
        addresses.update(prompted[2])
        user = prompted[3]
    config = update_config(source, hostnames, interfaces, addresses, user, uris)
    if source_path != destination and destination.parent != template.parent:
        config['$schema'] = str(ROOT / 'infrastructure/config.schema.json')
    for host in config['hypervisors']:
        print(f"{host['name']}: {host['address']}  experiment NIC: {host['network']['source']}")
    print(f'SSH user: {config["ssh"]["user"]}; existing SSH keys reused')
    if args.dry_run:
        print(f'Preview only: {destination}')
        return
    if destination.exists():
        backup = destination.with_name(destination.name + '.bak')
        atomic_write(backup, original)
        print(f'Previous config: {backup}')
    atomic_write(destination, (json.dumps(config, indent=2) + '\n').encode())
    print(f'Saved {destination}')
    print('Next: ' + shlex.join(['infrastructure/deploy.sh', '--config', str(destination), 'apply']))


if __name__ == '__main__':
    try:
        main()
    except (ValueError, KeyError, OSError, EOFError) as error:
        raise SystemExit(f'Configuration failed: {error}')
