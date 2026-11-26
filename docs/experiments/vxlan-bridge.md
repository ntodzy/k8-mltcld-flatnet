# Cilium VXLAN over a Linux bridge

Use Cilium's guest VXLAN with virtio/TAP interfaces attached to `cs740br`.
The physical experiment NIC is a bridge port; no hypervisor VXLAN device is
present. Guest, bridge and physical MTUs are 1500, yielding Pod MTU 1450.
This is the attachment control for Cilium's usual direct/macvtap mode.

Run from the repository root after any active collection has finished:

```bash
# Removes Kubernetes workloads/state and reboots guests; preserves disks/SSH keys.
python3 experiments/setup.py vxlan-bridge --reset
python3 experiments/run.py vxlan-bridge --client worker-1 --server worker-2 \
  --pod-only --diagnostics --samples 5 --duration 30
```

This mode retains the original Cilium/kube-proxy profile. It does not automatically
normalize queues or affinity left by a manual tuning session. Use the
[head-to-head coordinator](vxlan-head-to-head.md) to apply and audit a complete
baseline or optimized package, run independent rebuilds, and restore state.

To return to the original Cilium macvtap attachment:

```bash
python3 experiments/setup.py vxlan --reset
```
