# Isolate custom VXLAN latency

```bash
./experiments/latency_study.py --background
./experiments/status.py --directory results/latency-isolation-TIMESTAMP --watch
# Resume a failed step, keeping successful pairs and failed attempts:
./experiments/latency_study.py --resume results/latency-isolation-TIMESTAMP --background
```

The study reuses an already prepared host-vxlan-1450 cluster initially. Otherwise
it prepares that mode, then switches to per-VM VXLAN and Cilium as needed using
the normal reset/setup scripts. Existing SSH keys and the base config are reused.
It holds the collection lock, records every attempt, and finally restores
host-vxlan-1450. Each custom-mode block contains five tunnel/bypass pairs with
balanced AB/BA order, plus separate instrumented cases. The default duration is
30 seconds. There are no contention, VM-count scaling, Hubble or Cilium tuning
profiles in this study.

## What remains constant within a pair

The same cluster, VM boot identities, worker CPU masks, guest NIC attachments,
queues, MTUs and Cilium configuration remain in place. Environment guards reject
unexpected kernel, boot, queue, affinity or experiment-NIC changes within a block.
The per-VM tunnel control changes remote PodCIDR routes between the existing
VXLAN interface and direct experiment-network next hops. It preserves the tunnel
device, socket, FDB and neighbors. The host control switches the existing bridge
between its VXLAN port and the physical uplink, preserving the same TAP-backed
VM interfaces. Neither condition reboots VMs. Device counters and routes are
retained, and the runner validates the actual path before benchmarking.

The differences estimate the marginal complete tunnel path. They still include
the outer route, packet format, receive processing, batching, interrupts and
scheduling changes caused by adding that path. They do not isolate header writes
alone. The Cilium run is a separate reference; its BPF forwarding and metadata
cannot be replaced by manually changing a route in the same manner.

## Separate kernel timings

`latency_trace.py` sends a bounded bpftrace observer to both endpoint guests and
their hypervisors. It requires bpftrace and accessible kernel BTF/tracing files;
Ubuntu 24.04's bpftrace 0.20.2 is supported. Tracers attach before the benchmark,
then arm only after the diagnostic sampler identifies client netperf activity
inside the actual benchmark Pod cgroup. Each captures approximately 15 seconds.
Their latency results are excluded from the primary pairs.

Observations include the VXLAN build-SKB helper, header pull scoped to VXLAN
receive, full transmit/receive callbacks, Pod-TCP Linux forwarding, selected
worker vhost backend functions, device queue-to-driver observations, and
wakeup-to-run scheduling latency. The same skb is followed from VXLAN receive
to the next receive/GRO stage when possible. All durations use one kernel's
clock; guest, host and remote-host timestamps are never subtracted.
Available KVM halt-wakeup observations distinguish the reported waited flag;
their duration includes legitimate event waiting. CPU idle/clocksource/halt-poll
configuration and TC/BPF link attachments (including TCX where bpftool supports
it) are captured before the instrumented benchmark. Missing tools remain explicit.

Function durations include nested work and instrumentation overhead. Device
observations include other traffic on that device. Helper routines and inclusive
callbacks overlap, so their averages cannot be summed into an RTT breakdown.
Histogram percentiles are reported as upper bin boundaries, not exact values.
Blocked off-CPU time includes legitimate network waiting and is retained
separately from runnable scheduler delay. [Linux VXLAN source](https://github.com/torvalds/linux/blob/v6.8/drivers/net/vxlan/vxlan_core.c)
shows the distinction between building the skb, outer transmission, header pull,
and receive/GRO handoff.

## Reports and recovery

```bash
python3 experiments/analyze_latency.py results/latency-isolation-TIMESTAMP \
  --output results/latency-isolation-TIMESTAMP/analysis
```

Use the [Matplotlib environment](aggregation.md) for graphs. The report separates
paired end-to-end differences, tracer perturbation, kernel helpers, scheduling,
and receive handoff. CSV/JSON, generated BPF programs, raw observations and path
audits remain available. `--partial` explicitly labels an interim analysis.

The temporary path state lives in `infrastructure/generated/path-control.json`.
Ordinary benchmark commands refuse to run under an unacknowledged override.
The coordinator restores the tunnel path even on failure; if restoration itself
fails, the marker remains and the failure is recorded. To explicitly restore
the matching prepared custom mode when no collection is running:

```bash
./experiments/path_control.py host-vxlan-1450 tunnel --output results/path-restored.json
# Or, when the per-VM mode is the current prepared mode:
./experiments/path_control.py vm-vxlan-1450 tunnel --output results/path-restored.json
```

Use a new audit filename for each operation. These controls require the matching
prepared mode and reject an experiment uplink carrying a default route.
