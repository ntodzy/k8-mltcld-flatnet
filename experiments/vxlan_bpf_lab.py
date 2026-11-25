#!/usr/bin/env python3
"""Compile and exercise an optional TC/VXLAN prototype in disposable namespaces."""
import argparse
import base64
from datetime import datetime, timezone
import fcntl
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys

from common import GENERATED, ROOT, save_json, ssh_command


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host', required=True, help='Existing hypervisor SSH alias')
    parser.add_argument('--output', type=Path)
    parser.add_argument('--compile-only', action='store_true')
    parser.add_argument('--clang', default='clang')
    args = parser.parse_args()
    if args.output is None:
        args.output = ROOT / 'results' / ('vxlan-bpf-lab-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ'))
    if args.host not in {f"cs740-{h['name']}-jump" for h in json.loads((ROOT/'infrastructure/config.json').read_text())['hypervisors']}:
        parser.error('--host must identify a configured hypervisor')
    args.output.mkdir(parents=True, exist_ok=False)
    source = ROOT / 'experiments/prototypes/vxlan_redirect.bpf.c'
    agent = ROOT / 'experiments/prototypes/vxlan_lab_agent.py'
    metadata = {'host': args.host, 'source_sha256': hashlib.sha256(source.read_bytes()).hexdigest(),
                'agent_sha256': hashlib.sha256(agent.read_bytes()).hexdigest(),
                'git_commit': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(),
                'compile_only': args.compile_only}
    save_json(args.output/'metadata.json', metadata)
    shutil.copyfile(source, args.output/source.name)
    shutil.copyfile(agent, args.output/agent.name)
    obj = args.output/'redirect.o'
    command = [args.clang, '-target', 'bpfel', '-O2', '-g', '-Wall', '-Werror',
               '-Wno-unused-command-line-argument',
               '-c', str(source), '-o', str(obj)]
    result = subprocess.run(command, capture_output=True, text=True)
    save_json(args.output/'compile.json', {'argv': command, 'exit_code': result.returncode,
                                         'stdout': result.stdout, 'stderr': result.stderr})
    if result.returncode: raise RuntimeError(result.stderr)
    if args.compile_only:
        print(f'Compiled prototype: {obj}')
        return 0
    payload = json.dumps({'object': base64.b64encode(obj.read_bytes()).decode()})+'\n'
    # Same lock as the scientific collectors; no parallel study mutation.
    with (GENERATED/'collection.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = subprocess.run(ssh_command(GENERATED/'ssh_config', args.host,
                     ['sudo', '-n', 'timeout', '--signal=TERM', '--kill-after=15s', '180s',
                      'python3', '-u', '-c', agent.read_text()]),
                     input=payload, capture_output=True, text=True, timeout=220)
    (args.output/'remote.stdout').write_text(result.stdout)
    (args.output/'remote.stderr').write_text(result.stderr)
    if result.stdout:
        report = json.loads(result.stdout)
        save_json(args.output/'report.json', report)
        print(f"{report['status'].upper()}: {args.output}")
    else:
        print(result.stderr, file=sys.stderr)
    return result.returncode


if __name__ == '__main__':
    sys.exit(main())
