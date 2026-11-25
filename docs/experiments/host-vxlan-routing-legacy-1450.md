# Host VXLAN with the guest-forwarding prerequisite control

This keeps Linux VXLAN on the physical hypervisors, TAP/bridge attachments and
inner MTU 1450. Kubernetes guests use BPF service handling and masquerading but
retain legacy forwarding. It controls the prerequisites needed by the separate
[guest BPF forwarding profile](host-vxlan-routing-bpf-1450.md). No host TC shortcut,
CPU idle-policy constraint, queue or affinity tuning is enabled by setup.

From the repository root, reuse the installed SSH keys:

```bash
# Resets Kubernetes, reboots guests, and configures the selected profile.
python3 experiments/setup.py host-vxlan-routing-legacy-1450 --reset
python3 experiments/run.py host-vxlan-routing-legacy-1450 \
  --client worker-1 --server worker-2 --pod-only --diagnostics \
  --samples 5 --duration 30 --streams 4
```

The runner verifies both agents actually report Native/Legacy, the expected
ConfigMap values, absent kube-proxy, the host bridge/tunnel attachment, and
receipts at both physical tunnel endpoints. All raw counters are retained;
device counters include other traffic and are not exact per-flow packet counts.

To restore the original setup:

```bash
python3 experiments/setup.py host-vxlan-1450 --reset
```

See [matched idle-policy tests](host-latency-optimization.md) for policy controls,
trace validation and aggregation. Profile comparisons involve separate resets;
do not call their entire difference isolated header-encapsulation overhead.
