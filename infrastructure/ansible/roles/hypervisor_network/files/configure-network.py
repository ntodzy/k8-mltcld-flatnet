#!/usr/bin/env python3
"""Apply experiment-NIC offloads and optional queues, or restore their baseline."""
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile

# Use individual TSO/checksum features so grouped toggles do not change siblings.
FEATURES = (
    'rx-checksumming', 'tx-checksum-ip-generic', 'tx-checksum-ipv4', 'tx-checksum-ipv6',
    'generic-segmentation-offload', 'generic-receive-offload',
    'tx-tcp-segmentation', 'tx-tcp6-segmentation',
    'tx-udp_tnl-segmentation', 'tx-udp_tnl-csum-segmentation',
)


def command(argv):
    return subprocess.run(argv, check=True, text=True, stdout=subprocess.PIPE,
                          env=dict(os.environ, LC_ALL='C')).stdout


def features(text):
    result = {}
    for line in text.splitlines():
        match = re.fullmatch(r'\s*([\w-]+):\s+(on|off)(\s+\[fixed\])?\s*', line)
        if match:
            result[match[1]] = {'enabled': match[2] == 'on', 'fixed': bool(match[3])}
    return result


def channels(text):
    sections = text.split('Current hardware settings:', 1)
    if len(sections) != 2:
        raise ValueError('NIC does not report combined queue settings')
    maximum = re.search(r'^Combined:\s+(\d+)\s*$', sections[0], re.MULTILINE)
    current = re.search(r'^Combined:\s+(\d+)\s*$', sections[1], re.MULTILINE)
    if not maximum or not current or int(maximum[1]) == 0 or int(current[1]) == 0:
        raise ValueError('NIC does not support a configurable combined queue count')
    return int(current[1]), int(maximum[1])


def save_state(path, state):
    with tempfile.NamedTemporaryFile(mode='w', dir=path.parent, delete=False) as output:
        json.dump(state, output, indent=2)
        temporary = output.name
    os.replace(temporary, path)


def configure(config, state_dir):
    interface = config['interface']
    if not re.fullmatch(r'[A-Za-z0-9_.:-]{1,15}', interface):
        raise ValueError('Invalid interface name')
    if config['profile'] not in ('baseline', 'optimized'):
        raise ValueError('Profile must be baseline or optimized')
    requested = config.get('queues')
    if requested is not None and (type(requested) is not int or requested < 1):
        raise ValueError('Queue count must be a positive integer')
    path = state_dir / (interface + '.json')
    state = json.loads(path.read_text()) if path.exists() else None
    if config['profile'] == 'baseline' and state is None:
        return {'changed': False, 'message': 'No saved changes to restore'}
    current = features(command(['ethtool', '-k', interface]))
    if not current:
        raise ValueError('Could not parse NIC feature settings')
    changes = []
    skipped = []
    if config['profile'] == 'optimized':
        queue_now = None
        if requested is not None:
            queue_now, maximum = channels(command(['ethtool', '-l', interface]))
            if requested > maximum:
                raise ValueError(f'Requested {requested} queues exceeds NIC maximum {maximum}')
        if state is None:
            state = {'interface': interface, 'features': {
                key: current[key]['enabled'] for key in FEATURES
                if key in current and not current[key]['fixed']}}
        if requested is not None and 'queues' not in state:
            state['queues'] = queue_now
        # Save the baseline before attempting any mutation, including partial failure.
        save_state(path, state)
        if requested is not None and requested != queue_now:
            command(['ethtool', '-L', interface, 'combined', str(requested)])
            changes.append({'queues': requested})
            current = features(command(['ethtool', '-k', interface]))
        for feature in FEATURES:
            setting = current.get(feature)
            if setting is None or setting['fixed']:
                skipped.append(feature)
                continue
            if feature not in state['features']:
                state['features'][feature] = setting['enabled']
                save_state(path, state)
            if not setting['enabled']:
                command(['ethtool', '-K', interface, feature, 'on'])
                changes.append({feature: True})
        effective = features(command(['ethtool', '-k', interface]))
        for feature in state['features']:
            if not effective.get(feature, {}).get('enabled'):
                raise ValueError(f'Driver did not enable requested feature {feature}')
        if requested is not None and channels(command(['ethtool', '-l', interface]))[0] != requested:
            raise ValueError('Driver did not apply requested queue count')
    else:
        if 'queues' in state:
            queue_now, _ = channels(command(['ethtool', '-l', interface]))
            if queue_now != state['queues']:
                command(['ethtool', '-L', interface, 'combined', str(state['queues'])])
                changes.append({'queues': state['queues']})
                current = features(command(['ethtool', '-k', interface]))
        for feature, enabled in reversed(list(state['features'].items())):
            setting = current.get(feature)
            if setting is None:
                raise ValueError(f'Cannot restore missing feature {feature}')
            if setting['enabled'] == enabled:
                continue
            if setting['fixed']:
                raise ValueError(f'Cannot restore fixed feature {feature}')
            command(['ethtool', '-K', interface, feature, 'on' if enabled else 'off'])
            changes.append({feature: enabled})
        effective = features(command(['ethtool', '-k', interface]))
        for feature, enabled in state['features'].items():
            if effective.get(feature, {}).get('enabled') != enabled:
                raise ValueError(f'Driver did not restore feature {feature}')
        if 'queues' in state and channels(command(['ethtool', '-l', interface]))[0] != state['queues']:
            raise ValueError('Driver did not restore original queue count')
        path.unlink()
    return {'changed': bool(changes), 'changes': changes, 'unsupported_or_fixed': skipped}


if __name__ == '__main__':
    try:
        directory = Path('/var/lib/cs740-network')
        result = configure(json.loads((directory / 'config.json').read_text()), directory)
        print(json.dumps(result))
    except (ValueError, KeyError, OSError, subprocess.CalledProcessError) as error:
        raise SystemExit(f'Host NIC configuration failed: {error}')
