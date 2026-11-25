"""Shared paths and existing SSH access for the experiment scripts."""
import json
from pathlib import Path
import shlex
import subprocess

ROOT = Path(__file__).resolve().parents[1]
GENERATED = ROOT / 'infrastructure/generated'
MODES = ('vxlan', 'native', 'vm-local', 'direct-1450', 'vm-vxlan', 'host-vxlan')


def guests(path):
    return json.loads(Path(path).read_text())['all']['children']['virtual_machines']['hosts']


def ssh_command(ssh_config, host, argv):
    return ['ssh', '-F', str(Path(ssh_config).resolve()), '-o', 'BatchMode=yes',
            '-o', 'ConnectTimeout=15', host, shlex.join([str(arg) for arg in argv])]


def remote(ssh_config, host, argv):
    result = subprocess.run(ssh_command(ssh_config, host, argv), check=True,
                            text=True, stdout=subprocess.PIPE)
    return result.stdout


def save_json(path, data):
    Path(path).write_text(json.dumps(data, indent=2) + '\n')
