# Host VXLAN latency follow-up

This investigation is in progress. It extends the earlier completed studies;
the paper's original mode measurements remain unchanged. The question is whether
we can make hypervisor encapsulation competitive without conflating tunnel
placement, guest forwarding, host bridge forwarding and CPU idle policy.

## A repeatable initial improvement

The first follow-up block retained the existing host-vxlan-1450 cluster, VM
boots, guest queues, CPU masks, bridge/TAP attachments, MTUs and Cilium values.
Three AB/BA pairs compared the default idle policy against a temporary 10 µs
CPU-latency QoS request on both physical hypervisors. Each observation was a
20-second Cilium CLI Pod TCP_RR run with guest and physical-host diagnostics.
Both conditions held the same descriptor/SSH helper. Requests were removed and
restoration verified after each invocation.

| Hypervisor idle policy | n | Mean RR latency, µs | Average per-run P99, µs | Transactions/s |
|---|---:|---:|---:|---:|
| Default | 3 | 960.677 ± 13.309 | 1600.000 ± 53.357 | 1038.745 |
| Temporary 10 µs limit | 3 | 502.360 ± 7.688 | 978.667 ± 22.368 | 1985.315 |

Values following ± are sample standard deviations across invocations. The
paired mean reductions were 466.680, 473.410 and 434.860 µs. Every pair improved;
the average reduction was 47.7%. P99 improved by 38.8%. This is a repeatable
improvement to the complete host-overlay path in this block, with no changes
to bridge lookup or encapsulation code. It does not yet establish an advantage
over Cilium under the same policy.

![Matched host idle-policy observations](../results/host-latency-20261125T181639.324738Z/analysis/rr-mean_us.svg)

The physical idle-state counters recorded zero entries to states advertising
exit latency above 10 µs under the constraint. Across the default invocation
windows they recorded approximately 1.81 million such entries. Those windows
include benchmark startup and all physical CPUs, so these counts are not an
exclusive count of benchmark wakeups. The intervention constrains which idle
states may be entered; it does not promise 10 µs network latency, force a CPU
frequency, or bypass Linux VXLAN. Additional electrical power may be consumed
even when software busy-time measurements do not increase. Energy was not measured.

Both-host active-window busy-core equivalents averaged approximately 2.40 under
each policy. Dividing by completed application work gave about 2310 versus 1209
host CPU µs/transaction. This whole-host proxy includes background activity and
amortization; it is not an isolated encapsulation CPU cost or energy metric.
The original reports, paired observations, CPU policy/restoration evidence and
plots are retained under
[`results/host-latency-20261125T181639.324738Z/`](../results/host-latency-20261125T181639.324738Z/).

## What the next controls distinguish

Moving VXLAN to a physical hypervisor retains the Pod namespace, guest Cilium
processing and virtio transport. The previous bridge shortcut removed an
observed host bridge stage, while leaving legacy forwarding in the guests.
The new host-vxlan-routing-bpf-1450 profile keeps the physical bridge and VXLAN
and instead enables Cilium-supported BPF forwarding inside each Kubernetes VM.
Its prerequisite-matched legacy control has the same BPF service handling and
masquerading settings and differs only in hostLegacyRouting. These changes
have their own experiment labels and are excluded from the original default suite.

The repeated comparison measures both profiles, custom per-VM VXLAN with BPF
forwarding, and Cilium VXLAN. Every mode receives default and constrained idle
policy cases. This distinguishes a general host idle-policy benefit from a
relative advantage for a particular placement. The guest and physical custom
tunnels also use different macvtap versus TAP/bridge attachments, and mode
setups involve different VM boots. Those cross-mode costs remain part of a
placement configuration comparison; they cannot all be attributed to header
encapsulation alone. Native routing removes the overlay entirely, so its earlier
advantage motivates the study without proving that relocating an unchanged
overlay will automatically reproduce that advantage.

Separate instrumented checks will verify the requested guest forwarding mode
and executed Linux functions, including physical versus guest VXLAN transmit.
Their timings are inclusive and probe-perturbed, and are excluded from primary
latency estimates. A configured BPF flag alone is insufficient evidence that
the intended path executed. The supported forwarding requirements are described
in [Cilium's host-routing documentation](https://docs.cilium.io/en/stable/operations/performance/tuning/#ebpf-host-routing).

## Other potential limits

The live physical NICs use mlx4_en. Their reported fixed capabilities do not
include UDP tunnel segmentation or tunnel-port offload. Interrupt moderation
uses adaptive RX with a configured 16 µs value and a high-rate 128 µs value; these
settings are unchanged during the current paired study. The audit is retained
as nic-coalescing-audit.json in the first result directory. A later fixed-low
coalescing control could test interrupt batching independently if needed. These
configuration values do not directly establish how long an individual packet
waited, and TX completion interrupt moderation is not a packet transmit timer.

Reproduction and analysis commands are in
[the experiment guide](experiments/host-latency-optimization.md). The larger
comparison is still running/queued; no ranking or combined optimization benefit
is claimed before those measurements pass.
