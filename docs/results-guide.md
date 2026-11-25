# Which results to use

This is the result-selection index for the paper and presentation. Use the
groups below separately. The word **baseline/no additional optimizations** means
we switched networking modes while keeping the existing experiment settings
fixed; it does not mean Cilium BPF, ordinary NIC offloads or virtualization were
disabled. All performance figures describe complete deployment paths.

The primary source is the successful attempt selected by `collection.json` and
its `measurement/metadata.json`. Raw Cilium JSON and console output reside in
`measurement/phases/sample-*/measurement/`; per-phase guest/hypervisor summaries
are in `measurement/phases/sample-*/summary.json`. Do not count mirrored reports
twice, or select a failed attempt because it contains a performance JSON file.

## A — No additional optimizations: mode-only VXLAN comparisons

Use **A1 for the original three-mode comparison**, or **A2 for the later monitored
rerun used by the main report's baseline table**. Keep their sample sets separate;
they are different collection blocks, not ten interchangeable samples per mode.
Both use the original routing profiles, one guest queue, broad CPU masks and
default physical CPU idle policy. No experimental host TC redirect, explicit
guest BPF host-routing profile, queue increase, affinity pin or idle request is
part of these selected baseline measurements.

### A1 — Original matched-MTU head-to-head

Root: [`results/collection-20261125T020122.055126Z/`](../results/collection-20261125T020122.055126Z/).
Five 30-second Pod-only runs each of RR, single-stream and four-stream throughput;
inner MTU 1450, physical MTU 1500, worker-1 → worker-2.

| Mode | Exact successful attempt under that root |
|---|---|
| Cilium VXLAN | `01-vxlan-attempt-1/measurement/` |
| Guest VXLAN | `02-vm-vxlan-1450-attempt-1/measurement/` |
| Hypervisor VXLAN | `03-host-vxlan-1450-attempt-2/measurement/` |

`03-host-vxlan-1450-attempt-1` failed setup and is excluded. The successful
attempt 2 was resumed in the original collection; do not discard it or restart
the collection when selecting the data.

A1 predates the later Pod-namespace and per-thread observability fixes. Its
Cilium CLI performance measurements remain useful as the original comparison;
use A2's later diagnostics for Pod TCP/retransmission and host-backend attribution.
Host-namespace TCP counters from an early run are not ordinary Pod TCP counters.

### A2 — Primary monitored rerun baseline

Root: [`results/extended-study-20261125T042706Z/`](../results/extended-study-20261125T042706Z/).
The whole directory also contains optimizations. **Only the attempts below form
the three-mode baseline table.** Five 30-second observations per workload.

| Mode | Exact selected attempt under that root |
|---|---|
| Cilium VXLAN | `vxlan-baseline-attempt-1/measurement/` |
| Guest VXLAN1450 | `vm-vxlan-1450-baseline-attempt-1/measurement/` |
| Hypervisor VXLAN1450 | `host-1450-final-baseline-attempt-1/measurement/` |

Other `*-baseline-*` steps provide the native, direct1450, bridge1450,
VM-local and inner-MTU 1500 controls. Their workloads/paths differ and should keep
their own labels. `host-1450-baseline-attempt-2` is the earlier host baseline for
the queue intervention; do not pool it into the selected final baseline above.
Read [the rerun analysis](../results/extended-study-20261125T042706Z/analysis/report.md)
for the selected baseline and separate optimization groups.

## B — BPF forwarding experiments, default idle policy

### B1 — Our custom hypervisor TC bridge shortcut: correctness evidence

Use [`results/bpf-integration-smoke-20261125T171045.529636Z/`](../results/bpf-integration-smoke-20261125T171045.529636Z/),
mode directories `host-vxlan-bpf-pass-1450/` and `host-vxlan-bpf-1450/`.
They compare the same exact-flow BPF lookup/counters with bridge pass-through
versus a TAP↔VXLAN redirect. Linux VXLAN remains. These are **single 5-second
functional smokes**, suitable for program/flow-counter and cleanup validation,
not a paper claim of a repeatable speedup. Mean 1024→1012 µs was a small change
with slightly higher P99; the study did not establish a convincing latency gain.

[`results/integrated-host-bpf-trace-20261125T175202.363427Z/`](../results/integrated-host-bpf-trace-20261125T175202.363427Z/)
is separate instrumented proof that this shortcut removed the observed
bridge-forwarding stage while Linux VXLAN remained active. Exclude its RR timing
from untraced performance tables.

### B2 — Supported BPF guest forwarding: primary optimization results

Root: [`results/host-latency-20261125T183320.140179Z/`](../results/host-latency-20261125T183320.140179Z/).
Select passed **measurement** steps with `cpu_latency_us: -1`, matching
`MODE-rr-pair-{1..5}-default-attempt-1/measurement/`. These are five 30-second
observations per mode at the **default idle policy**.

| Mode/prefix | Interpretation |
|---|---|
| `host-vxlan-routing-legacy-1450` | Prerequisite control: BPF service handling/masquerading, legacy guest forwarding |
| `host-vxlan-routing-bpf-1450` | Hypervisor VXLAN and bridge retained; Cilium BPF forwarding inside VMs |
| `vm-vxlan-bpf-1450` | Guest VXLAN with Cilium BPF guest forwarding |
| `vxlan` | Fresh original Cilium VXLAN reference; no added forwarding profile |

The legacy prerequisite control is **not A's unchanged baseline**: kube-proxy
replacement and BPF masquerading have already been enabled. Comparing it with
the host BPF profile isolates the requested hostLegacyRouting value more closely,
while still involving separate VM boots and fresh connections. The Cilium row
already uses its integrated overlay BPF path; it does not represent BPF-disabled
Cilium. No netkit or broader Cilium tuning profile is included.

The repeatable host latency improvement belongs to B2, **not B1**. Per-mode
`*-instrumented-trace-attempt-1/` directories prove executed stages but their
performance values must remain excluded. Runtime routing JSON, tunnel receipt
counters, routes and Pod identities are retained in each measurement.

## C — CPU idle-policy intervention, layered on a named forwarding profile

Use the **same root as B2**, but select `cpu_latency_us: 10`, matching
`MODE-rr-pair-{1..5}-10us-attempt-1/measurement/`. Pair each case against the
same mode and pair number's `default` observation. Requests constrain physical
hypervisor idle-state eligibility on both hosts; they do not change guest CPU
frequency, queue counts or affinity. The helpers verify effective policy and
restoration; per-state usage counters are in `cpu-latency/*/request.jsonl`.

**BPF + idle tuning is a combined configuration.** Approximately 400 µs is the
result for the BPF custom modes and Cilium reference under this constraint.
It is not the mode-only result in A. The legacy prerequisite control still
measured approximately 514 µs under the same idle constraint.

Supporting, separate blocks:

- [`results/host-latency-20261125T181639.324738Z/`](../results/host-latency-20261125T181639.324738Z/):
  three 20-second default/10µs pairs on the original host profile. Use for
  reproduction of the idle effect without the new forwarding prerequisites;
  do not pool with B2/C's five 30-second observations.
- [`results/latency-isolation-20261125T144819Z/`](../results/latency-isolation-20261125T144819Z/):
  `idle-pair-*` steps form a three-round path×idle interaction study. Earlier
  custom tunnel/bypass `*-pair-*` steps estimate the complete tunnel path cost.
  Neither is the three-mode baseline in A. Instrumented `*-trace-*` cases are
  execution evidence only.

Power consumption was not measured. Keeping CPUs out of deep idle can increase
power use even when software CPU-time counters do not increase.

## D — Throughput validation: BPF profiles, default idle policy only

The supervised workflow is
[`results/host-followup-workflow-20261125T194314.961685Z/`](../results/host-followup-workflow-20261125T194314.961685Z/).
The completed throughput collection is its **`throughput/`** subdirectory.
All 34 steps passed, including restoration, and the parent workflow passed all
three stages. The final [cohort figures and selections](../results/host-followup-workflow-20261125T194314.961685Z/paper-analysis-complete/report.md)
and [separate impact report](experiment-impact-report.md) contain the accepted results.

The accepted selection is five 30-second single-stream and four-stream
runs for `host-vxlan-routing-bpf-1450`, `vm-vxlan-bpf-1450` and `vxlan`, at
default idle policy and one guest queue. Successful attempts match
`MODE-{single,multi}-pair-{1..5}-default-attempt-N/measurement/`. Select the
successful attempt from the manifest; retain any failures separately.

**This is BPF-profile throughput validation, not an idle-tuned throughput test.**
It preserves the physical NIC configuration and records guest/hypervisor CPU,
vhost-thread load, TCP namespace counters, drops and queue activity. Do not pair
its throughput with the 400 µs idle-tuned RR values as though both were measured
under one configuration. No throughput estimate is accepted until the relevant
measurement passes; final collection success also requires baseline restoration.

D has no fresh legacy-prerequisite throughput control. Comparing D against A2
also crosses collection/boot blocks and prerequisite settings; describe it as a
configuration comparison rather than an isolated causal BPF throughput saving.

## E — Other optimizations and diagnostics: keep separate

Within A2's `extended-study` root, `host-1450-two-queues*`,
`host-1450-two-queues-repeat*`, `host-1450-pinned*`, `host-1450-unpinned*`,
`vxlan-*-queue-control*` and `vm-vxlan-1450-*-queue-control*` are queue/affinity
interventions or reversals. The approximately 6.43 Gb/s host result uses two
queues and pinned vhost tasks with the original guest-forwarding profile. It is
not an A baseline, B2 forwarding-only result, or a measured combination with C.

Use `*-profile-*`, `*-trace-*`, lab, capture and audit directories to explain or
validate paths. Do not use them as ordinary performance repetitions. Ignore
failed attempts, short `*-smoke-*` datasets and generated `analysis-interim-*`
views when building primary paper performance estimates. Retain them as evidence
of validation, failures and debugging rather than deleting them.

The newest directory is not automatically the appropriate dataset. Choose the
question, group and exact successful attempt first; then use its original
reports and diagnostics. Tables and figures should say **A mode-only**, **B BPF
forwarding**, **C idle-policy control**, **D forwarding-only throughput**, or
**E queue/affinity intervention** explicitly.

## Complete collation and regeneration

The [full collation](../results/impact-collation-20261125T230019.309151Z/all-metrics.md)
covers 43 primary conditions and five separate correctness cases, including
queue reversals and paired path controls beyond the A/B/C/D paper figures.
The [impact report](experiment-impact-report.md) evaluates each change and lists
missing measurements. No additional experiments were started to fill them.

To regenerate the separated paper figures into a **new** directory:

```bash
python3 experiments/analyze_followup.py \
  --baseline results/extended-study-20261125T042706Z \
  --latency results/host-latency-20261125T183320.140179Z \
  --throughput results/host-followup-workflow-20261125T194314.961685Z/throughput \
  --output results/host-followup-workflow-20261125T194314.961685Z/paper-analysis-REPRO
```

Use the [Matplotlib environment](experiments/aggregation.md). The exporter
creates separate A2 mode-only, B2 default-idle forwarding, C idle-control and D
default-idle throughput figures, plus `selection.json`, `metrics.json`, CSV and
a report. C's selection records its exact same-mode/default control source.
Before throughput passes, `--partial` omits D estimates entirely and labels the
report interim; choose a new output directory for each export. B1 short smokes,
E queue tuning and instrumented timings are excluded from these figures.
