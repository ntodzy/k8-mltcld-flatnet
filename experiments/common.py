"""Shared paths and existing SSH access for the experiment scripts."""
import json
from pathlib import Path
import shlex
import subprocess

ROOT = Path(__file__).resolve().parents[1]
GENERATED = ROOT / 'infrastructure/generated'
VM_VXLAN_MODES = ('vm-vxlan', 'vm-vxlan-1450')
HOST_VXLAN_MODES = ('host-vxlan', 'host-vxlan-1450')
# Inner IP MTU, guest transport NIC MTU, physical MTU, Cilium MTU override.
# A guest doing encapsulation needs 50 extra bytes on its transport NIC.
MTU_PROFILES = {
    'vxlan': (1450, 1500, 1500, 0),
    'native': (1500, 1500, 1500, 0),
    'vm-local': (1500, 1500, 1500, 0),
    'direct-1450': (1450, 1500, 1500, 1450),
    'vm-vxlan': (1500, 1550, 1550, 1500),
    'host-vxlan': (1500, 1500, 1550, 1500),
    'vm-vxlan-1450': (1450, 1500, 1500, 1450),
    'host-vxlan-1450': (1450, 1450, 1500, 1450),
    'bridge-1450': (1450, 1450, 1500, 1450),
}
CONTROL_MODES = ('bridge-1450',)
ALL_MODES = tuple(MTU_PROFILES)
MODES = tuple(mode for mode in ALL_MODES if mode not in CONTROL_MODES)


def mtu_profile(mode):
    return dict(zip(('inner_mtu', 'guest_mtu', 'physical_mtu', 'cilium_mtu'), MTU_PROFILES[mode]))


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
