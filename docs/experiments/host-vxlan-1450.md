# Custom hypervisor VXLAN with inner MTU 1450

Cilium uses native routing with Pod MTU 1450. Guest experiment NICs, the
hypervisor bridge (`cs740br`), and its VXLAN interface (`cs740hvx`) all use MTU
1450. The physical underlay uses MTU 1500. Encapsulation is shared by the VMs
on each hypervisor.

Compare this mode with `vxlan` and the other custom `*-vxlan-1450` mode to
hold inner packet size and physical underlay MTU equal. Unlike the original
custom VXLAN profiles, this mode does not require a 1550-byte physical LAN.
It uses VNI 740 and UDP 4789; permit that traffic and ICMP underlay probes.

## Reset and set up

Run from the repository root with the existing deployment JSON, VM inventory,
and SSH access. This removes Kubernetes workloads and cluster state, reboots
the guests, configures the selected attachments/MTUs, and rebuilds the cluster.
Existing VM disks and SSH keys are preserved.

```bash
./experiments/setup.py host-vxlan-1450 --reset
```

## Measure

Use workers on different physical hypervisors. For matching Pod traffic across
the implementations, exclude host-network controls:

```bash
./experiments/run.py host-vxlan-1450 --client worker-1 --server worker-2 \
  --duration 30 --samples 5 --streams 4 --pod-only
```

Without `--pod-only`, the runner also measures host-to-host traffic. Those
controls use different paths: VM VXLAN and Cilium VXLAN host controls bypass
the Pod tunnel, while hypervisor VXLAN also encapsulates host traffic. Their
NIC MTUs are 1500 in the guest-tunnel modes and 1450 in this host-tunnel mode.
Use Pod-to-Pod results for the comparison at equal inner MTU.

See [matched implementation comparison](matched-vxlan.md) for the single
collection command, controls, and interpretation. Results use new directories
under `results/` and retain original Cilium JSON, logs, config, and CPU snapshots.

## Reset or switch

Use this setup command again with `--reset`, or run the setup command from
another [experiment guide](../../README.md). To reset only:

```bash
infrastructure/cluster.sh reset
```
