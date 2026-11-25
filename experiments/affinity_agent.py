#!/usr/bin/env python3
"""Pin only one managed worker's vhost threads, retaining their original masks."""
import json
import os
from pathlib import Path
import re
import sys


def cpulist(text):
    result = set()
    for group in text.strip().split(','):
        if '-' in group:
            first, last = map(int, group.split('-'))
            result.update(range(first, last + 1))
        elif group:
            result.add(int(group))
    return result


def starttime(pid):
    raw = Path(f'/proc/{pid}/stat').read_text()
    return int(raw[raw.rindex(')') + 2:].split()[19])


def choose_cpus(allowed, topology_root=Path('/sys/devices/system/cpu')):
    selected, seen = [], set()
    for cpu in sorted(allowed, key=lambda value: (value == 0, value)):
        root = topology_root / f'cpu{cpu}/topology'
        identity = ((root / 'physical_package_id').read_text().strip(),
                    (root / 'core_id').read_text().strip())
        if identity not in seen:
            selected.append(cpu)
            seen.add(identity)
        if len(selected) == 2:
            return selected
    raise ValueError('Two distinct physical cores are required for the affinity control')


def main():
    domain, interface, action = sys.argv[1:]
    if not re.fullmatch(r'worker-[A-Za-z0-9.-]+', domain) or action not in ('pin', 'restore'):
        raise ValueError('Only worker vhost affinity controls are accepted')
    qemu = int(Path(f'/run/libvirt/qemu/{domain}.pid').read_text())
    command = Path(f'/proc/{qemu}/cmdline').read_bytes().replace(b'\0', b' ').decode()
    if f'guest={domain},' not in command:
        raise ValueError('QEMU PID does not identify the requested worker domain')
    backup_dir = Path('/var/lib/cs740-network/affinity')
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup = backup_dir / f'{domain}-{qemu}.json'
    if action == 'pin':
        tids = []
        for entry in Path('/proc').iterdir():
            if entry.name.isdigit():
                try:
                    if (entry / 'comm').read_text().strip() == f'vhost-{qemu}':
                        tids.append(int(entry.name))
                except OSError:
                    pass
        if not tids:
            raise ValueError('No kernel vhost threads found for this worker')
        existing = json.loads(backup.read_text()) if backup.exists() else None
        if existing is not None and existing['qemu_starttime'] != starttime(qemu):
            raise ValueError('Affinity backup belongs to a different QEMU process lifetime')
        if existing is not None and {thread['tid'] for thread in existing['threads']} != set(tids):
            raise ValueError('Vhost task set changed since the affinity baseline was saved')
        allowed = set.intersection(*(set(thread['cpus']) for thread in existing['threads'])) if existing else set.intersection(*(set(os.sched_getaffinity(tid)) for tid in tids))
        numa_path = Path(f'/sys/class/net/{interface}/device/numa_node')
        numa = int(numa_path.read_text()) if numa_path.exists() else -1
        if numa >= 0:
            allowed &= cpulist(Path(f'/sys/devices/system/node/node{numa}/cpulist').read_text())
        selected = choose_cpus(allowed)
        if not backup.exists():
            backup.write_text(json.dumps({'domain': domain, 'qemu': qemu, 'qemu_starttime': starttime(qemu),
                'threads': [{'tid': tid, 'starttime': starttime(tid), 'cpus': sorted(os.sched_getaffinity(tid))} for tid in sorted(tids)]}))
            backup.chmod(0o600)
        saved = json.loads(backup.read_text())
        if saved['qemu_starttime'] != starttime(qemu):
            raise ValueError('Affinity backup belongs to a different QEMU process lifetime')
        for index, tid in enumerate(sorted(tids)):
            os.sched_setaffinity(tid, {selected[index % len(selected)]})
    else:
        saved = json.loads(backup.read_text())
        if saved['qemu_starttime'] != starttime(qemu):
            raise ValueError('Affinity backup belongs to a different QEMU process lifetime')
        selected = None
        for thread in saved['threads']:
            if starttime(thread['tid']) != thread['starttime']:
                raise ValueError('Vhost task lifetime changed; refusing to change a reused PID')
            os.sched_setaffinity(thread['tid'], set(thread['cpus']))
    print(json.dumps({'domain': domain, 'action': action, 'selected_cpus': selected,
                      'baseline': saved, 'current': [{'tid': thread['tid'], 'cpus': sorted(os.sched_getaffinity(thread['tid']))}
                                                    for thread in saved['threads']]}))


if __name__ == '__main__':
    main()
