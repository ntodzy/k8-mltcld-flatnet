# Cilium VXLAN over macvtap with the BPF guest profile

Retain virtio/direct/macvtap and Cilium's VXLAN encapsulation inside the VM.
Enable kube-proxy replacement, BPF masquerading and supported BPF host routing.
Physical/guest MTUs are 1500 and Pod MTU is 1450. The runner requires live
Tunnel/BPF routing status on both endpoint agents and no kube-proxy DaemonSet.

Run from the repository root after any active collection has finished:

```bash
# Fresh cluster is required for the changed service/forwarding prerequisites.
# Reset removes workloads/state and reboots guests, preserving disks and SSH keys.
python3 experiments/setup.py vxlan-bpf --reset
python3 experiments/run.py vxlan-bpf --client worker-1 --server worker-2 \
  --pod-only --diagnostics --samples 5 --duration 30
```

The alias changes guest forwarding/services only. Queue count, vhost affinity and
physical-host idle QoS are separate controls. Use the
[head-to-head coordinator](vxlan-head-to-head.md) for the complete optimized
package and its matched baseline; it normalizes and verifies these settings.

To restore the original guest profile and kube-proxy:

```bash
python3 experiments/setup.py vxlan --reset
```
