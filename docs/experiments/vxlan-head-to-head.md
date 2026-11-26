# VXLAN architecture head-to-head

Compare three architectures, each with baseline and optimized settings:

| Architecture | Baseline mode | Optimized mode |
|---|---|---|
| Cilium VXLAN over macvtap | [`vxlan`](vxlan.md) | [`vxlan-bpf`](vxlan-bpf.md) |
| Cilium VXLAN over TAP/Linux bridge | [`vxlan-bridge`](vxlan-bridge.md) | [`vxlan-bridge-bpf`](vxlan-bridge-bpf.md) |
| Native Cilium over hypervisor VXLAN | [`host-vxlan-1450`](host-vxlan-1450.md) | [`host-vxlan-routing-bpf-1450`](host-vxlan-routing-bpf-1450.md) |

The Cilium bridge connects the physical experiment NIC to `cs740br`, without
a host tunnel. The provider bridge connects TAPs to `cs740hvx`; its physical
NIC stays outside that bridge and transports VXLAN. There is one tunnel layer
in either architecture. Management networking and existing SSH keys are preserved.
Base `infrastructure/config.json` remains unchanged; bridge configs are generated.
The provider tunnel also carries VM node/control-plane traffic. Cilium's guest
tunnel carries the Pod overlay, with node traffic using the VM underlay. This
is part of the complete architecture comparison: CPU monitoring includes that
background work, while path-validation filters select benchmark Pod traffic.

## Profiles and controls

Baseline uses one experiment NIC queue, original broad vhost masks, default
physical-host CPU idle policy, normal offloads, and the existing Cilium/kube-proxy
baseline. It does **not** disable Cilium's ordinary BPF datapath. Optimized uses
two queues, the existing NUMA-aware vhost affinity policy, temporary 10 µs idle QoS
requests on both physical hosts, kube-proxy replacement, BPF masquerading, and
supported BPF guest forwarding. The Cilium variants retain tunnel routing; the
provider variant retains native routing in the guest. Actual routing status is
checked. The custom physical-host TC shortcut, frequency changes, busy polling,
NIC coalescing changes, and experimental offload changes are excluded.

Pod MTU is 1450 and physical MTU 1500 throughout. Cilium tunnel guests and their
direct bridges need transport MTU 1500; host-tunnel guests/bridge use MTU 1450.
This transport difference is intentional and is recorded. `worker-1` is the
client and `worker-2` the server, on different hypervisors. VM allocations,
physical NIC settings, versions, TCP sizes and four-stream count are held fixed.

The three comparisons are attachment (Cilium bridge vs macvtap), placement with
matching attachment (host VXLAN vs Cilium bridge), and complete architecture
(host VXLAN vs Cilium macvtap). This is a fixed-cluster performance study, not a
tenant isolation, migration, failure-recovery, contention or scaling evaluation.

## Development validation

From the repository root:

```bash
# Print the schedule without making changes or connecting to the hosts.
python3 experiments/head_to_head.py --smoke --dry-run

# Resets/rebuilds the cluster for all six cells. One block, one repetition,
# three seconds for each of RR, single-stream and four-stream throughput.
python3 experiments/head_to_head.py --smoke --background
```

Every smoke collection and workload metadata is labeled `development-smoke`.
Short measurements validate execution and reporting; they are not performance
evidence and must not enter the paper's primary results.

## Primary collection

```bash
# Three rebuild blocks × six cells × five repetitions × three workloads.
python3 experiments/head_to_head.py --background

# Optional shorter primary schedule: retain independent rebuilds, reduce trials.
python3 experiments/head_to_head.py --samples 3 --background
```

No manual reset/setup is required: the coordinator calls `setup.py MODE --reset`
for every cell, sets and verifies queues, rediscovers vhost tasks, applies
affinity, and validates the packet path before collecting primary observations.
It requires a starting mode from the table and restores that architecture's
**baseline**, rather than preserving an arbitrary pre-existing optimized state.
Defaults are three independent rebuild blocks and five 30-second repetitions
of each workload per cell (270 primary invocations). Configuration order uses
a saved seeded permutation rotated across blocks; six blocks cover all order
positions. Workload order also rotates. Three blocks reduce ordering bias but
do not fully balance all six positions. The default schedule is estimated at
12–14 hours before retries; captures and queue restarts can add time.

Each invocation gets identical diagnostics on both endpoint guests and
hypervisors, plus an idle-policy holder (including a no-op default holder).
Boot, kernel, queue, affinity, interface, coalescing and offload snapshots guard
against drift within a configuration. Idle QoS is held only around the workload
invocation and verified on both application and restoration; it is not a CPU
frequency setting or a latency guarantee. Host/guest CPU utilization is observed;
power consumption is not measured.

Each cell also has a **separate excluded validation benchmark**: bounded physical
egress captures on both hypervisors prove benchmark Pod TCP inside the expected
VXLAN port, absence of nested UDP encapsulation, no observed IP fragmentation,
and full-size inner traffic. A DF ping tests the outer transport MTU. Guest
tunnel devices and host bridge membership establish placement. Egress captures
can contain GSO super-packets, so captured lengths are not exact wire lengths.
The custom host TC program and intrusive kernel tracing are not needed here.
Capture filters select inner IPv4 TCP between the two workers' live Pod CIDRs
before applying the 2,000-packet limit; node/control-plane traffic must not fill
that cap during pod startup. The parser then requires the exact benchmark Pod
IP pair. These offsets assume the untagged inner Ethernet/IPv4 VXLAN used here.

## Status, failures, and resume

The start command prints its result directory. Use that exact directory:

```bash
python3 experiments/status.py --directory results/vxlan-head-to-head-TIMESTAMP
python3 experiments/status.py --directory results/vxlan-head-to-head-TIMESTAMP --watch --interval 300

# New attempt for an unfinished cell; passed cells are retained.
python3 experiments/head_to_head.py --resume results/vxlan-head-to-head-TIMESTAMP --background
```

Each cell is an atomic attempt. A failure retries that entire cell, including a
fresh reset; partial workloads from the failed attempt are retained but excluded.
Primary resume rejects deployment/source changes; a bug fix requires a new
primary collection, so passed observations cannot silently mix implementations.
For development smoke only, a deliberate bug-fix resume is available:

```bash
python3 experiments/head_to_head.py --resume results/vxlan-head-to-head-TIMESTAMP \
  --accept-smoke-source-change --background
```

This retains earlier source snapshots and attempt identities, records a new
source revision, retries the failed cell, and keeps the entire output labeled
development-only. The override is rejected for primary data. Source
copies, hashes, the Git snapshot, settings, setup logs and retry history are saved.
Only one collection may own the shared lock. SIGTERM/SIGINT trigger cleanup;
hard kills cannot guarantee immediate affinity restoration, although idle leases
expire automatically. Do not start competing setup/collection commands.

Affinity is restored before queues replace vhost tasks. The workflow attempts
baseline architecture restoration on success, failure, or interruption and records
its verification separately. A restoration failure makes the collection failed;
inspect `collection.json` and the restoration directory before further work.

## Report and master chart

```bash
python3 experiments/analyze_head_to_head.py results/vxlan-head-to-head-TIMESTAMP \
  --output results/vxlan-head-to-head-TIMESTAMP/analysis
```

Install `experiments/requirements-analysis.txt` in your analysis environment for
SVG/PNG charts, or add `--no-plots` for tables/CSV/JSON only. Output directories
must be new. `--partial` labels an incomplete report explicitly and includes
only fully passed cells. Smoke reports carry a prominent development warning.

The master table groups baseline/optimized mean RR latency, P99, single-stream
and four-stream throughput. Figures show rebuild means and between-rebuild SD.
Raw observations and within-boot SD remain available, but repetitions are first
averaged within each rebuild; independent rebuild means receive equal weight.
P99 is an average of invocation P99 values, not a pooled transaction percentile.
Contrasts retain block identity and report absolute/percentage changes for
profile, attachment and placement comparisons. CPU busy-core observations for
guests and hosts retain workload identity in CSV/JSON. Physical-host CPU totals
already include VM execution; guest and host CPU values must not be added
together as independent costs.

Optimized-versus-baseline measures a package, not each setting's independent
effect. Equal attachment types reduce confounding but do not make all forwarding
steps identical. Conclusions must remain scoped to this hardware and workload.
RR and throughput are separate workloads in the same configuration; this study
does not measure request latency during concurrent throughput saturation.
