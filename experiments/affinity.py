#!/usr/bin/env python3
"""Apply or restore the explicit worker vhost CPU-affinity experiment."""
import argparse
import fcntl
import json
from pathlib import Path
import subprocess

from common import GENERATED, ROOT, guests, remote, save_json


def configure(action, output):
    hosts = guests(GENERATED / 'guest_inventory.json')
    config = json.loads((ROOT / 'infrastructure/config.json').read_text())
    parents = {host['name']: host['network']['source'] for host in config['hypervisors']}
    agent = Path(__file__).with_name('affinity_agent.py').read_text()
    result = []
    for vm in ('worker-1', 'worker-2'):
        host = hosts[vm]['hypervisor']
        try:
            result.append(json.loads(remote(GENERATED / 'ssh_config', 'cs740-' + host + '-jump',
                         ['sudo', '-n', 'python3', '-c', agent, vm, parents[host], action])))
        except subprocess.CalledProcessError as error:
            raise RuntimeError(f'{host}/{vm}: affinity {action} failed with status {error.returncode}; see remote traceback in collection.log') from None
    save_json(output, result)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('pin', 'restore'))
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise ValueError('Output already exists')
    with (GENERATED / 'collection.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        configure(args.action, args.output)


if __name__ == '__main__':
    main()
