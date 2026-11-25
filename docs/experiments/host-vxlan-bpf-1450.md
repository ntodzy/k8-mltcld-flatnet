# Host VXLAN with BPF bridge bypass

Redirects only the current benchmark TCP flow between managed TAP and VXLAN ports; compare against host-vxlan-bpf-pass-1450.

Inner MTU is 1450, physical MTU 1500. Existing SSH credentials are reused.
The reset removes cluster workloads/state and reboots guests; VM disks and keys
are retained. Run from the repository root:

```bash
./experiments/setup.py host-vxlan-bpf-1450 --reset
./experiments/run.py host-vxlan-bpf-1450 --client worker-1 --server worker-2 \
  --pod-only --diagnostics --duration 30 --samples 5 --streams 4
```

To reuse a successful matching host-vxlan-1450 topology without another reset:

```bash
./experiments/setup.py host-vxlan-bpf-1450 --reuse-base
./experiments/run.py host-vxlan-bpf-1450 --client worker-1 --server worker-2 \
  --pod-only --test-type rr --duration 30 --samples 1
```

Switch between the two host BPF modes with `--reuse-base` to retain VM boots,
attachments and queue settings. The runner still validates live ports and scopes
attachments to each invocation's Pod IPs. Only explicit `--pod-only` or
`--network pod` runs are accepted. Multiple samples require diagnostics so each
phase has its own path proof.

After the experiment, restore the original baseline:

```bash
./experiments/setup.py host-vxlan-1450 --reset
```

See [comparison, counters, aggregation and cleanup](bpf-forwarding.md) and
[latency/idle-policy background](../vxlan-latency-and-optimization.md).
These modes are optional and excluded from the default eight-mode collection.
