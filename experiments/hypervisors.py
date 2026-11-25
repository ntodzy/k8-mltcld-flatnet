#!/usr/bin/env python3
"""Prepare QEMU/KVM hosts and configure or audit their experiment NICs."""
import argparse
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import tempfile

from common import GENERATED, ROOT, save_json


def build_inventory(config, config_dir, profile, queues, audit):
    private_key = Path(config['ssh']['private_key_file']).expanduser()
    if not private_key.is_absolute():
        private_key = config_dir / private_key
    hosts = {}
    for host in config['hypervisors']:
        if host['network']['mode'] != 'direct':
            raise ValueError('Use the base config with direct experiment NIC attachments')
        interface = host['network']['source']
        if not re.fullmatch(r'[A-Za-z0-9_.:-]{1,15}', interface):
            raise ValueError(f'Invalid physical experiment interface: {interface}')
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]*', host['name']):
            raise ValueError('Invalid hypervisor name')
        if host['name'] in hosts:
            raise ValueError('Duplicate hypervisor name')
        hosts[host['name']] = {
            'ansible_host': host['address'],
            'ansible_user': config['ssh']['user'],
            'ansible_ssh_private_key_file': str(private_key.resolve()),
            'ansible_ssh_common_args': config['ssh'].get('common_args', '-o StrictHostKeyChecking=accept-new'),
            'qemu_admin_users': [config['ssh']['user']],
            'hypervisor_experiment_interface': interface,
        }
    if not hosts:
        raise ValueError('At least one hypervisor is required')
    return {'all': {'children': {'hypervisors': {'hosts': hosts}}, 'vars': {
        'hypervisor_network_profile': profile,
        'hypervisor_network_queues': queues,
        'hypervisor_audit_only': audit,
        'hypervisor_report_dir': str(GENERATED / 'hypervisor-reports'),
    }}}


def positive(value):
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError('must be a positive integer')
    return number


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, default=ROOT / 'infrastructure/config.json')
    parser.add_argument('--profile', choices=('baseline', 'optimized'), default='optimized',
                        help='optimized enables supported offloads; baseline restores saved NIC settings')
    parser.add_argument('--queues', type=positive,
                        help='Explicit combined physical NIC queue count; default preserves current count')
    parser.add_argument('--audit', action='store_true', help='Collect capabilities and settings without modifying the hosts')
    parser.add_argument('--dry-run', action='store_true', help='Print the host targets and playbook command without SSH or changes')
    args = parser.parse_args()
    if args.queues and args.profile != 'optimized':
        parser.error('--queues requires --profile optimized')
    config_path = args.config.resolve()
    inventory = build_inventory(json.loads(config_path.read_text()), config_path.parent,
                                args.profile, args.queues, args.audit)
    env = dict(os.environ, ANSIBLE_CONFIG=str(ROOT / 'infrastructure/ansible/ansible.cfg'))
    with tempfile.TemporaryDirectory(prefix='cs740-hypervisors-') as temp:
        inventory_file = Path(temp) / 'inventory.json'
        command = ['ansible-playbook', '-i', str(inventory_file),
                   str(ROOT / 'infrastructure/ansible/playbooks/setup-hypervisors.yml')]
        if args.dry_run:
            for name, host in inventory['all']['children']['hypervisors']['hosts'].items():
                print(f"{name}: {host['ansible_host']} experiment NIC={host['hypervisor_experiment_interface']}")
            print(f'Profile: {args.profile}; audit only: {args.audit}; queues: {args.queues or "unchanged"}')
            print(shlex.join(command))
            return
        save_json(inventory_file, inventory)
        inventory_file.chmod(0o600)
        env['ANSIBLE_LOCAL_TEMP'] = str(Path(temp) / 'ansible')
        subprocess.run(command, env=env, check=True, cwd=ROOT)
    print(f'Host network reports: {GENERATED / "hypervisor-reports"}')


if __name__ == '__main__':
    try:
        main()
    except (ValueError, KeyError, OSError, subprocess.CalledProcessError) as error:
        raise SystemExit(f'Hypervisor setup failed: {error}')
