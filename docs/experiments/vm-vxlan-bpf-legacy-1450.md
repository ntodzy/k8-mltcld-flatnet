# Guest VXLAN with BPF prerequisites and legacy routing

Enables BPF masquerading and kube-proxy replacement but forces legacy host routing; the matched prerequisite control for vm-vxlan-bpf-1450.

Inner MTU is 1450, physical MTU 1500. Existing SSH credentials are reused.
The reset removes cluster workloads/state and reboots guests; VM disks and keys
are retained. Run from the repository root:

```bash
./experiments/setup.py vm-vxlan-bpf-legacy-1450 --reset
./experiments/run.py vm-vxlan-bpf-legacy-1450 --client worker-1 --server worker-2 \
  --pod-only --diagnostics --duration 30 --samples 5 --streams 4
```

A fresh reset/setup is required when changing the guest forwarding profile.
Both guest profiles omit kube-proxy and enable BPF service handling/masquerading;
only their hostLegacyRouting value differs. Runtime status must agree with the
requested mode. This is a Cilium-supported forwarding profile, not installation
of the raw isolated lab program on Pod interfaces.

After the experiment, restore the original baseline:

```bash
./experiments/setup.py host-vxlan-1450 --reset
```

See [comparison, counters, aggregation and cleanup](bpf-forwarding.md) and
[latency/idle-policy background](../vxlan-latency-and-optimization.md).
These modes are optional and excluded from the default eight-mode collection.
