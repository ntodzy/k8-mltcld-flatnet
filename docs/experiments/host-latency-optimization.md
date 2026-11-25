# Investigate and reduce host VXLAN latency

The earlier bridge shortcut removed the observed bridge forwarding stage but
had no convincing latency benefit in a short smoke. This follow-up tests two
other mechanisms: legacy forwarding inside the Kubernetes VMs and hypervisor
CPU idle exits. Linux VXLAN remains the encapsulator in every custom profile.

Run a repeated, latency-only comparison:

```bash
python3 experiments/host_latency_study.py \
  --modes host-vxlan-1450 host-vxlan-routing-legacy-1450 \
          host-vxlan-routing-bpf-1450 vm-vxlan-bpf-1450 vxlan \
  --samples 5 --duration 30 --trace --background
```

The script reuses a matching first prepared mode or resets/configures it, resets
between modes, and restores the original host-vxlan-1450 setup at the end. SSH
keys and the base deployment configuration are preserved. A shared collection
lock prevents competing orchestrators; do not launch manual resets or benchmarks
while it is active. To measure RR, single-stream and four-stream workloads:

```bash
python3 experiments/host_latency_study.py \
  --modes host-vxlan-routing-bpf-1450 vxlan \
  --samples 5 --duration 30 --workloads rr single multi --background
```

Each sample is a pair of separate Cilium CLI invocations at default hypervisor
CPU idle policy and a temporary 10 µs latency-QoS request on both physical hosts.
Pair order alternates AB/BA. VM boots, queues, NIC identities and worker CPU
masks are checked against the unchanged block environment. Each invocation has
Pod identity, guest and hypervisor monitoring. Both policy conditions hold the
same descriptor/SSH helper. Closing the descriptor restores the previous policy,
which is verified; helpers also expire after ten minutes. Each benchmark is
bounded to 15–120 seconds and an expired holder invalidates the observation.

CPU QoS limits idle-state eligibility; it does not guarantee a 10 µs network
latency, set CPU frequency or enable busy polling. Disallowing deep idle can
increase power consumption. No physical power or energy measurement is made.
Idle-state counters cover the whole invocation, including Pod startup, while
ordinary CPU/TCP diagnostics use the observed active benchmark window.

The prerequisite-matched host profiles differ only in Cilium guest
`hostLegacyRouting`. Comparing those profiles still involves fresh cluster/VM
boots. Host versus guest custom VXLAN also changes bridge versus macvtap
attachment. A tuned host versus default Cilium win would demonstrate an
optimization, not isolate tunnel placement. Compare the same idle policy and
supported guest-forwarding prerequisites when discussing relative placement.

Optional `--trace` adds a separate instrumented 30-second RR case per mode. The
existing four-machine tracer requires actual VXLAN transmit execution at both
expected tunnel endpoints. These observations are marked instrumented and omitted
from primary estimates. Tracing does not run under the temporary idle constraint;
it establishes executed stages, not a precise cost allocation.

The manifest retains every attempt, source hashes/copies, base-config hash,
Git checkpoint, live environments and timestamps. On failure, successful phases
are retained; CPU requests are closed before failure is reported. The cluster
remains in its last prepared mode if a step fails; resume repairs that attempt:

```bash
python3 experiments/status.py --directory RESULTS_DIRECTORY --watch
python3 experiments/host_latency_study.py --resume RESULTS_DIRECTORY --background
```

The watcher checks every 300 seconds. Orchestration polling does not change
scientific sampling. After success, generate tables, paired effects, per-run
points and standard-deviation plots using the existing
[Matplotlib environment](aggregation.md):

```bash
python3 experiments/analyze_host_latency.py RESULTS_DIRECTORY \
  --output RESULTS_DIRECTORY/analysis
```

Multiple study directories may be supplied; they remain distinct groups. Output
includes mean, P50/P90/P99 and RR transactions/s, throughput in Gb/s where
selected, host busy-core equivalents and CPU/application-work proxies, plus raw
CSV/JSON and PNG/SVG plots. Mean sample P99 is not a pooled packet percentile.
Failed attempts and instrumented phases are excluded. `--partial` explicitly
labels an unfinished analysis. Detailed per-core, thread, queue, offload and Pod
TCP diagnostics remain available through the ordinary aggregation utility.
