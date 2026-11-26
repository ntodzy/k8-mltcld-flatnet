# Cilium VXLAN over a bridge with the BPF guest profile

Combine the [Cilium bridge attachment](vxlan-bridge.md) with kube-proxy
replacement, BPF masquerading and supported BPF guest forwarding. Encapsulation
remains inside each VM. `cs740br` connects TAPs directly to the physical NIC;
there is no host VXLAN. Guest/bridge/physical MTUs are 1500 and Pod MTU is 1450.

Run from the repository root after any active collection has finished:

```bash
# Removes Kubernetes workloads/state and reboots guests; preserves disks/SSH keys.
python3 experiments/setup.py vxlan-bridge-bpf --reset
python3 experiments/run.py vxlan-bridge-bpf --client worker-1 --server worker-2 \
  --pod-only --diagnostics --samples 5 --duration 30
```

This alias configures the guest BPF profile and bridge attachment. For the complete
two-queue, vhost-affinity and temporary idle-QoS package, use the
[six-cell head-to-head coordinator](vxlan-head-to-head.md). The coordinator also
provides independent rebuilds, exact packet-path checks, cleanup and aggregation.

To keep the bridge while restoring the baseline guest services:

```bash
python3 experiments/setup.py vxlan-bridge --reset
```

To restore Cilium's original macvtap attachment and guest profile:

```bash
python3 experiments/setup.py vxlan --reset
```
