#!/usr/bin/env python3
"""Reset/setup a reproducible experiment using the existing VM SSH credentials."""
import argparse
import copy
import ipaddress
from itertools import islice
import json
import os
from pathlib import Path
import shlex
import subprocess

from common import GENERATED, MODES, ALL_MODES, ROOT, HOST_VXLAN_MODES, guests, remote, save_json, mtu_profile


def deployment_config(source, mode, config_dir):
    """Make an isolated deployment config; never edit the user's base config."""
    config = copy.deepcopy(source)
    for key in ('private_key_file', 'public_key_file'):
        path = Path(config['ssh'][key]).expanduser()
        config['ssh'][key] = str((config_dir / path).resolve())
    for host in config['hypervisors']:
        if host['network']['mode'] != 'direct':
            raise ValueError('Base config must use direct experiment NIC attachments; '
                             'keep bridge changes in the generated experiment config.')
        host['network']['mtu'] = mtu_profile(mode)['guest_mtu']
        if mode in HOST_VXLAN_MODES or mode == 'bridge-1450':
            host['network'] = {'mode': 'bridge', 'source': 'cs740br', 'mtu': mtu_profile(mode)['guest_mtu']}
    return config


def hypervisor_inventory(config, mode, ssh_config, subnet):
    subnet = ipaddress.IPv4Network(subnet)
    addresses = list(islice(subnet.hosts(), len(config['hypervisors'])))
    if len(config['hypervisors']) > len(addresses):
        raise ValueError('Underlay subnet does not have enough VTEP addresses')
    for host in config['hypervisors']:
        for vm in host['vms']:
            if vm.get('ipv4_address') and subnet.overlaps(ipaddress.IPv4Interface(vm['ipv4_address']).network):
                raise ValueError('The VXLAN underlay subnet overlaps the VM experiment subnet')
    hosts = {}
    for index, host in enumerate(config['hypervisors']):
        hosts[host['name']] = {
            'ansible_host': f"cs740-{host['name']}-jump",
            'ansible_ssh_common_args': '-F ' + shlex.quote(str(ssh_config)),
            'vxlan_underlay_interface': host['network']['source'],
            'vxlan_underlay_ipv4': str(addresses[index]),
            'experiment_vms': [{'name': vm['name'], 'mac_address': vm['mac_address']}
                               for vm in host['vms']],
        }
    return {'all': {'children': {'hypervisors': {'hosts': hosts}}, 'vars': {
        'experiment_mode': mode,
        'experiment_underlay_mtu': mtu_profile(mode)['physical_mtu'],
        'experiment_inner_mtu': mtu_profile(mode)['inner_mtu'],
        'experiment_guest_mtu': mtu_profile(mode)['guest_mtu'],
        'experiment_underlay_prefix': subnet.prefixlen,
    }}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=ALL_MODES)
    parser.add_argument('--config', type=Path, default=ROOT / 'infrastructure/config.json')
    parser.add_argument('--reset', action='store_true',
                        help='Remove Kubernetes/Cilium state and reboot all guests first; preserves disks and SSH keys')
    parser.add_argument('--underlay-subnet', default='10.75.0.0/24')
    parser.add_argument('--dry-run', action='store_true', help='Print the plan without SSH or file changes')
    args = parser.parse_args()
    config_path = args.config.resolve()
    source = json.loads(config_path.read_text())
    config = deployment_config(source, args.mode, config_path.parent)
    ssh_config = GENERATED / 'ssh_config'
    inventory = GENERATED / 'guest_inventory.json'
    host_inventory = hypervisor_inventory(source, args.mode, ssh_config, args.underlay_subnet)
    if not ssh_config.is_file() or not inventory.is_file():
        parser.error('Deploy the VMs first; existing generated SSH config and guest inventory are required')
    if args.mode in HOST_VXLAN_MODES and len(source['hypervisors']) < 2:
        parser.error('host-vxlan requires at least two hypervisors')
    host_file = GENERATED / 'experiment_hypervisors.json'
    config_file = GENERATED / 'experiment_config.json'
    cluster_mode = 'native' if args.mode == 'vm-local' else 'direct-1450' if args.mode == 'bridge-1450' else args.mode
    playbooks = ROOT / 'infrastructure/ansible/playbooks'
    commands = []
    if args.reset:
        commands.append([str(ROOT / 'infrastructure/cluster.sh'), 'reset'])
    commands.append(['ansible-playbook', '-i', str(host_file), str(playbooks / 'experiment-underlay.yml')])
    if args.mode in HOST_VXLAN_MODES:
        commands.append(['ansible-playbook', '-i', str(host_file), str(playbooks / 'host-vxlan.yml')])
    if args.mode == 'bridge-1450':
        commands.append(['ansible-playbook', '-i', str(host_file), str(playbooks / 'direct-bridge.yml')])
    commands += [
        [str(ROOT / 'infrastructure/deploy.sh'), '--config', str(config_file), '--auto-approve', 'apply'],
        ['ansible-playbook', '-i', str(inventory), '-i', str(host_file), str(playbooks / 'experiment-guests.yml')],
        [str(ROOT / 'infrastructure/cluster.sh'), '--network-mode', cluster_mode, 'apply'],
    ]
    if args.dry_run:
        print(f'Generated deployment: {config_file} (base config and keys preserved)')
        print(f'Physical experiment MTU: {host_inventory["all"]["vars"]["experiment_underlay_mtu"]}')
        for command in commands:
            print(shlex.join(command))
        return
    # Invalidate any previous successful setup before changing the cluster.
    (GENERATED / 'active-experiment.json').unlink(missing_ok=True)
    if not args.reset:
        for name in guests(inventory):
            remote(ssh_config, name, ['sudo', '-n', 'test', '!', '-e', '/etc/kubernetes/kubelet.conf'])
    save_json(config_file, config)
    config_file.chmod(0o600)
    save_json(host_file, host_inventory)
    env = dict(os.environ, ANSIBLE_CONFIG=str(ROOT / 'infrastructure/ansible/ansible.cfg'))
    for command in commands:
        print('+ ' + shlex.join(command), flush=True)
        subprocess.run(command, env=env, check=True, cwd=ROOT)
    save_json(GENERATED / 'active-experiment.json', {
        'mode': args.mode, 'cluster_mode': cluster_mode,
        'physical_mtu': host_inventory['all']['vars']['experiment_underlay_mtu'],
        'guest_mtu': host_inventory['all']['vars']['experiment_guest_mtu'],
        'inner_mtu': host_inventory['all']['vars']['experiment_inner_mtu'],
    })
    print(f'Ready: run experiments/run.py {args.mode} --client VM --server VM')


if __name__ == '__main__':
    try:
        main()
    except (ValueError, KeyError, OSError, subprocess.CalledProcessError) as error:
        raise SystemExit(f'Setup failed: {error}')
