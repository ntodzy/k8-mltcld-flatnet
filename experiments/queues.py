#!/usr/bin/env python3
"""Set experiment NIC queue counts without editing Terraform or management NICs."""
import argparse
from datetime import datetime, timezone
import fcntl
import json
from pathlib import Path
import subprocess
import time

from common import GENERATED, ROOT, guests, remote, save_json


def configure(vms, queues=None, restore=False, output=None):
    hosts = guests(GENERATED / 'guest_inventory.json')
    source = json.loads((ROOT / 'infrastructure/config.json').read_text())
    settings = {vm['name']: vm for host in source['hypervisors'] for vm in host['vms']}
    config = GENERATED / 'ssh_config'
    agent = Path(__file__).with_name('queue_agent.py').read_text()
    records = []
    for vm in vms:
        if vm not in hosts or hosts[vm]['cs740_role'] != 'worker':
            raise ValueError('Queue experiments accept worker VMs only')
        print(f'{vm}: configuring experiment NIC queues ({"restore" if restore else queues})', flush=True)
        records.append(json.loads(remote(config, 'cs740-' + hosts[vm]['hypervisor'] + '-jump',
                       ['sudo', '-n', 'python3', '-c', agent, vm, settings[vm]['mac_address'],
                        'restore' if restore else str(queues)])))
        deadline = time.monotonic() + 300
        while True:
            try:
                channels = remote(config, vm, ['sudo', '-n', 'ethtool', '-l', 'experiment'])
                records[-1]['guest_channels'] = channels
                if not restore:
                    current = channels.split('Current hardware settings:')[-1]
                    if f'Combined:\t{queues}' not in current and f'Combined:\t\t{queues}' not in current:
                        remote(config, vm, ['sudo', '-n', 'ethtool', '-L', 'experiment', 'combined', str(queues)])
                        records[-1]['guest_channels'] = remote(config, vm, ['sudo', '-n', 'ethtool', '-l', 'experiment'])
                break
            except (OSError, subprocess.CalledProcessError):
                if time.monotonic() >= deadline:
                    raise RuntimeError(f'{vm} did not return with usable queue settings')
                time.sleep(3)
    controller = next(name for name, host in hosts.items() if host['cs740_role'] == 'controller')
    remote(config, controller, ['sudo', '-n', 'kubectl', '--kubeconfig=/etc/kubernetes/admin.conf',
                                'wait', '--for=condition=Ready', 'nodes', *vms, '--timeout=300s'])
    if output:
        save_json(output, records)
    return records


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--vms', nargs='+', default=['worker-1', 'worker-2'])
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--queues', type=int, choices=(1, 2))
    group.add_argument('--restore', action='store_true')
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ')
    output = args.output or ROOT / 'results' / f'queues-{stamp}.json'
    if output.exists():
        raise ValueError('Output already exists')
    with (GENERATED / 'collection.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        configure(args.vms, args.queues, args.restore, output)
    print(f'Queue audit saved: {output}')


if __name__ == '__main__':
    main()
