# Host VXLAN with BPF forwarding inside the guests

The VXLAN tunnel remains on each physical hypervisor. This profile shortens the
Kubernetes VM's forwarding path using supported Cilium BPF host routing. Cilium's
"host" here means the guest node; encapsulation still runs outside that VM.
The physical host's Linux bridge and Linux VXLAN remain unchanged. This does not
enable our experimental TC bridge shortcut or install the isolated lab program.

From the repository root, reuse the installed SSH keys:

```bash
# Removes Kubernetes state, reboots guests, configures TAP/bridge + host VXLAN,
# and enables the explicit guest BPF forwarding prerequisites/profile.
python3 experiments/setup.py host-vxlan-routing-bpf-1450 --reset
python3 experiments/run.py host-vxlan-routing-bpf-1450 \
  --client worker-1 --server worker-2 --pod-only --diagnostics \
  --samples 5 --duration 30 --streams 4
```

Setup enables kube-proxy replacement, BPF masquerading and
`bpf.hostLegacyRouting=false`; fresh kubeadm initialization omits kube-proxy.
The runner requires actual Native/BPF status on both agents and validates the
physical tunnel path and both receive counters. Compare against
[the prerequisite-matched legacy profile](host-vxlan-routing-legacy-1450.md),
which differs only in `bpf.hostLegacyRouting=true`. Neither setup changes CPU
idle policy, guest queues or vhost affinity.

For a separate instrumented execution check:

```bash
python3 experiments/forwarding_trace.py host-vxlan-routing-bpf-1450
```

Tracing is excluded from primary performance estimates. It checks whether
`ip_forward` executes in the guests while `vxlan_xmit` executes on both hosts.
Its inclusive durations overlap and cannot be added to reconstruct an RTT.

Restore the original setup with:

```bash
python3 experiments/setup.py host-vxlan-1450 --reset
```

The normal `--reuse-base` label shortcut cannot restore this changed forwarding
profile; a reset is required. See [the latency study](host-latency-optimization.md)
for repeated policy controls and analysis.
