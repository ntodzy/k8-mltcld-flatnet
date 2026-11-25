#!/usr/bin/env python3
"""Show collection/comparison progress, optionally waiting until it finishes."""
import argparse
import json
from pathlib import Path
import time

from common import ROOT

ACTIVE = {'pending', 'starting', 'running'}
SUCCESS = {'passed', 'complete', 'completed'}


def find_manifest(directory=None):
    if directory:
        directory = Path(directory).expanduser().resolve()
        candidates = [directory / name for name in ('collection.json', 'comparison.json')]
    else:
        results = ROOT / 'results'
        candidates = [*results.glob('*/collection.json'), *results.glob('*/comparison.json')]
    candidates = [path for path in candidates if path.is_file()]
    if not candidates:
        raise ValueError('No collection/comparison found; use --directory PATH to select one')
    return max(candidates, key=lambda path: path.stat().st_mtime)


def progress(state):
    if 'plan' in state:
        total = len(state['plan'])
        finished = sum(step['status'] == 'passed' for step in state['plan'])
    else:
        modes = state.get('modes', [])
        total = len(modes)
        finished = sum(step.get('phase') in modes and step.get('exit_code') == 0
                       for step in state.get('steps', []))
    status = state.get('status', 'unknown')
    label = 'DONE' if status in SUCCESS else status.upper()
    phase = state.get('phase', 'waiting to start')
    unit = 'steps' if state.get('kind') == 'extended-study' else 'measurements'
    text = f'{label}: {finished}/{total} {unit} complete | phase: {phase}'
    if state.get('error'):
        text += ' | ' + state['error']
    return text, status


def monitor(manifest, watch=False, interval=60, sleep=time.sleep):
    print(f'Results: {manifest.parent}', flush=True)
    print(f'Log: {manifest.parent / "collection.log"}', flush=True)
    previous = None
    while True:
        state = json.loads(manifest.read_text())
        text, status = progress(state)
        if text != previous:
            print(text, flush=True)
            previous = text
        if status in SUCCESS:
            return 0
        if status not in ACTIVE:
            return 1
        if not watch:
            return 0
        sleep(interval)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', type=Path, help='Select a result directory; default is the latest collection/comparison')
    parser.add_argument('--watch', action='store_true', help='Check periodically and print only changed progress')
    parser.add_argument('--interval', type=int, default=60, help='Seconds between watch checks (default: 60)')
    args = parser.parse_args()
    if args.interval < 1:
        parser.error('--interval must be positive')
    return monitor(find_manifest(args.directory), args.watch, args.interval)


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit('Stopped watching; the experiment continues in the background')
    except (OSError, ValueError, KeyError) as error:
        raise SystemExit(f'Cannot read experiment status: {error}')
