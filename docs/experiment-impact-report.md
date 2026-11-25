# Measured impact of each experiment

The clearest latency improvements came from supported BPF forwarding inside the
VMs and physical-host CPU idle tuning. The clearest host multistream throughput
improvements came from increasing guest NIC queues and then pinning vhost tasks.
Moving VXLAN to the hypervisor by itself did not improve every metric. Our custom
hypervisor TC bridge shortcut has not demonstrated a meaningful speedup.

This report uses **existing measurements only**. The already-running throughput
collection completed all 34 steps, including baseline restoration; no additional
benchmark suite, test run or software development was started for this analysis.

## Collated charts and complete numbers

The collation contains 43 primary conditions from 364 completed workload invocations,
plus five short correctness cases kept outside the efficacy figures. It covers
the original head-to-head, monitored mode baselines, queue/affinity interventions
and reversals, paired tunnel/bypass controls, idle screening, full BPF/idle
latency comparisons and the completed throughput follow-up. Earlier superseded
debugging runs and instrumented profiles/traces are not additional performance
repetitions.

- [Combined throughput/latency chart — all 43 primary conditions](../results/impact-collation-20261125T230019.309151Z/all-tests-overview.svg)
- [All throughput metrics](../results/impact-collation-20261125T230019.309151Z/all-throughput.svg)
- [All latency metrics, including min/mean/P50/P90/P99/max and transaction rate](../results/impact-collation-20261125T230019.309151Z/all-latency.svg)
- [Complete numerical table, sample counts and exact source attempts](../results/impact-collation-20261125T230019.309151Z/all-metrics.md)
- [CSV of all metrics](../results/impact-collation-20261125T230019.309151Z/all-metrics.csv)
- [Individual observations](../results/impact-collation-20261125T230019.309151Z/raw-observations.csv)
- [Separated A/B/C/D figures and accepted source selection](../results/host-followup-workflow-20261125T194314.961685Z/paper-analysis-complete/report.md)

![Collated primary conditions](../results/impact-collation-20261125T230019.309151Z/all-tests-overview.svg)

Missing cells say **not measured**, rather than zero. Most primary comparisons
use five 30-second invocations; reversals, screening and interaction blocks have
their actual sample counts recorded in the complete table. Error bars are sample
standard deviations across invocations, not confidence intervals. P50/P90/P99
columns average each invocation's quantile; min/max columns average each
invocation's minimum/maximum. They are not pooled transaction distributions. In the CSV, the min/max summary
fields describe the range of that per-invocation metric; separate min_us/max_us
metrics represent invocation-level transaction extrema.

“Meaningful” below describes effect size, variation, control quality and reversal
evidence. No new statistical tests were run. A small change between separate
collection/boot blocks is treated cautiously. Different profiles were not tested
over independent randomized cluster rebuilds. Latency and throughput in the
current BPF configuration summary come from separate measurement collections;
their source paths remain separate and no individual observations are pooled.

## Objective: which changes helped?

| Change and reference | Single-stream throughput | Four-stream throughput | Mean RR latency | Assessment |
|---|---|---|---|---|
| Native routing versus Cilium VXLAN, A2 | 3.143 → 5.847 Gb/s, +86.0% | 4.015 → 8.858 Gb/s, +120.6% | 822 → 432 µs, −47.4%, but highly variable | Clear throughput gain; lower observed latency with weaker precision |
| Move original guest VXLAN to hypervisor, A2 | 2.608 → 3.603 Gb/s, +38.2% | 4.597 → 3.835 Gb/s, −16.6% | 964 → 1002 µs, +3.9% | Single-flow benefit; multistream penalty; no latency improvement |
| Hypervisor VXLAN: supported BPF guest forwarding, B2 | No fresh prerequisite-matched throughput control | No fresh prerequisite-matched throughput control | 1008 → 811 µs, −19.6% | Meaningful observed latency improvement; mechanism verified by trace |
| BPF host configuration versus old A2 host baseline, across blocks | 3.603 → 3.808 Gb/s, +5.7% | 3.835 → 4.090 Gb/s, +6.7% | See matched-prerequisite latency comparison above | Throughput gains are modest; no strong isolated BPF throughput claim |
| BPF guest configuration versus old A2 guest baseline, across blocks | 2.608 → 3.832 Gb/s, +46.9% | 4.597 → 4.698 Gb/s, +2.2% | 964 → 759 µs, −21.3%, variable | Large single-flow configuration gain; no clear multistream gain; lower observed latency |
| Physical CPU idle constraint, C | Not measured | Not measured | About 46–51% lower within each named profile | Clear repeatable latency gain; throughput effect unknown |
| Host: one → two guest queues, E | 3.587 → 3.326 Gb/s, −7.3% | 4.013 → 5.601 Gb/s, +39.6% | Not measured under two queues | Clear multistream improvement with a single-flow tradeoff |
| Host: pin vhost tasks with two queues, E | Not measured in this contrast | 5.664 → 6.427 Gb/s, +13.5% | Not measured | Clear multistream improvement, supported by reversal |
| Guest VXLAN: one → two queues, E | No immediate one-queue single-flow control | 4.662 → 5.245 Gb/s, +12.5% | Not measured | Moderate multistream improvement, less stable than host queue gain |
| Cilium: one → two queues, E | No immediate one-queue single-flow control | 4.148 → 4.190 Gb/s, +1.0% | Not measured | No meaningful multistream improvement demonstrated |
| Custom hypervisor TC bridge shortcut, B1 | Not measured | Not measured | 1024 → 1012 µs, −1.2%; single 5 s samples | No convincing speedup; correctness validated only |
| Larger custom inner MTU 1450 → 1500, A2 | Guest −3.5%; host −3.0% | Guest −2.4%; host +4.4% | Guest +1.9%; host +0.6% | No consistent performance improvement |

The “BPF guest forwarding” improvement uses Cilium-supported host routing inside
the Kubernetes VMs. It is different from our custom TC program on the physical
hypervisor. Both keep Linux VXLAN. Group labels and exact result directories are
defined in [the results guide](results-guide.md).

## A — No additional optimizations: networking mode only

These are the primary monitored A2 baselines. Original profiles, one guest queue,
broad CPU masks and default idle behavior were retained while changing each
mode's required network/MTU configuration. Normal Cilium BPF processing and
ordinary offloads were still present.

| Mode | Single Gb/s ± SD | Four-stream Gb/s ± SD | Mean RR µs ± SD | Average sample P99 µs |
|---|---:|---:|---:|---:|
| Cilium VXLAN | 3.143 ± 0.111 | 4.015 ± 0.181 | 821.668 ± 53.065 | 1517.2 |
| Native 1500 | 5.847 ± 0.569 | 8.858 ± 0.287 | 432.010 ± 265.964 | 1145.0 |
| Direct/native 1450 | 5.796 ± 0.182 | 8.312 ± 0.583 | 377.368 ± 186.399 | 1194.2 |
| VM-local, host-network traffic | 12.271 ± 0.674 | 24.080 ± 0.510 | 109.706 ± 1.830 | 195.8 |
| Guest VXLAN, inner 1450 | 2.608 ± 0.148 | 4.597 ± 0.174 | 964.236 ± 29.301 | 1661.8 |
| Guest VXLAN, inner 1500 | 2.517 ± 0.081 | 4.486 ± 0.404 | 982.320 ± 43.512 | 1671.8 |
| Hypervisor VXLAN, inner 1450 | 3.603 ± 0.157 | 3.835 ± 0.109 | 1001.836 ± 12.392 | 1661.6 |
| Hypervisor VXLAN, inner 1500 | 3.495 ± 0.263 | 4.002 ± 0.188 | 1007.790 ± 16.334 | 1719.0 |
| Native bridge 1450 | 5.861 ± 0.507 | 8.858 ± 0.258 | 789.898 ± 109.999 | 1439.4 |

Native routing is the strongest cross-host Pod baseline throughput result;
this configuration removes encapsulation. Its mean latency is lower too, but the large variation limits a
precise latency effect estimate. Reducing native Pod MTU from 1500 to 1450 barely
changed single-stream throughput (−0.9%) and reduced four-stream throughput by
6.2% in this sequence. The latency means changed by 12.6% in the favorable
direction, while P99 worsened 4.3% and both means were highly variable. Therefore
the reduced-MTU case does not establish a meaningful latency optimization.

The bridge 1450 control had approximately the same single-stream throughput as
direct 1450 and higher four-stream throughput in this block. It demonstrates that
the bridge-backed attachment alone does not explain the host overlay's weak
multistream result. Its latency was higher (790 versus 377 µs). This includes
different attachment/boot/configuration effects and should not be described as
an isolated 412 µs bridge-function cost. The VM-local case is a separate reference:
it uses host-network traffic between distinct VMs on one hypervisor and avoids
the physical link. Its 24.08 Gb/s and 110 µs do not represent a drop-in improvement
for cross-host Pod traffic.

Raising custom inner MTU from 1450 to 1500 produced no consistent benefit:
guest single/four-stream rates changed by −3.5%/−2.4%, host rates by −3.0%/+4.4%,
and mean RR latency by +1.9%/+0.6%. These modes also require a 1550-byte underlay;
they are configuration comparisons, not a matched isolated MTU intervention.

The original A1 comparison supports the same qualitative placement tradeoff:
guest versus host single-stream throughput was 2.497 versus 3.592 Gb/s, while
four-stream throughput was 4.719 versus 3.763 Gb/s. Mean latencies were 1009 and 991 µs,
respectively, compared with 768 µs for Cilium. Thus, the small guest/host latency
ordering changed between A1 and A2, whereas the single-versus-multistream
throughput tradeoff persisted. A1 and A2 remain separate rows in the collation.

## B1 — Our physical-host TC shortcut

The bridge pass-through smoke averaged 1023.53 µs and the redirect smoke 1011.54 µs,
a 1.17% reduction. P99 changed 1577 → 1614 µs, which is worse. Each was one 5-second
measurement. This is insufficient evidence for a meaningful latency benefit,
and throughput was not measured. Flow counters and a separate trace proved that
the selected traffic bypassed the observed bridge-forwarding stage while Linux
VXLAN remained active. Correctness success is not a performance success. The
isolated namespace prototype and early guest-routing smokes are also validation
evidence, not substitutes for the full runs analyzed below.

## B2 — Supported BPF forwarding and D throughput validation

The fresh prerequisite-matched host control averaged 1007.952 ± 23.758 µs. Enabling
BPF forwarding inside the VMs, while retaining the hypervisor tunnel and bridge,
reduced this to 810.660 ± 57.375 µs, a 19.6% decrease. Average sample P99 fell
1745.6 → 1435.4 µs (−17.8%). All five improved default-policy sample means were
below all five control sample means. Separate traces observed zero selected
guest Pod-TCP ip_forward calls while physical VXLAN and bridge forwarding
continued. This supports the intended forwarding-path improvement. The mode
setups used different VM boots, so the result is not an exclusive timing of one
function. Merely enabling the service/masquerading prerequisites while retaining
legacy routing did not give an obvious latency gain versus the original host
baseline:1008 versus 1002 µs across those blocks.

The completed D throughput collection uses **default idle policy**, one queue,
and the named forwarding configurations:

| Current configuration | Single Gb/s ± SD | Four-stream Gb/s ± SD | Default-policy RR µs ± SD, separate collection |
|---|---:|---:|---:|
| Hypervisor VXLAN + BPF guest forwarding | 3.808 ± 0.129 | 4.090 ± 0.195 | 810.660 ± 57.375 |
| Guest VXLAN + BPF forwarding | 3.832 ± 0.200 | 4.698 ± 0.231 | 758.658 ± 178.839 |
| Fresh Cilium reference | 3.242 ± 0.123 | 4.177 ± 0.100 | 759.718 ± 122.518 |

Host throughput is 5.7%/6.7% above the old A2 single/four-stream baseline. These
are modest changes across separate collections, not a fresh prerequisite-matched
throughput experiment. Cilium's repeated reference also changed by 3.1%/4.1%
without receiving a new forwarding optimization. Consequently there is no strong
basis to attribute the host's small throughput increase exclusively to BPF.
The host latency improvement is much clearer than its throughput improvement.

The guest BPF configuration's single-stream throughput increased 2.608 → 3.832 Gb/s
(+46.9%), substantially more than the reference drift and observed sample spread.
That is a large measured configuration improvement. Four-stream throughput
changed only 4.597 → 4.698 Gb/s (+2.2%), within the observed variation. Its mean
latency fell 964 → 759 µs across blocks, but the new result is variable and includes
one fast 452 µs invocation. Its median of per-run means was 811 µs; the host's was
820 µs. These support lower latency after improving the guest configuration,
without proving that either tunnel placement universally wins. No fresh guest
legacy-prerequisite throughput control separates service/masquerading changes
from the host-routing change itself.

The latest host and guest single-flow rates are effectively similar at 3.81 and
3.83 Gb/s. The guest still has higher four-stream throughput:4.70 versus 4.09 Gb/s,
about 15% more than the host. Relative to Cilium, the host has 17.5% higher
single-stream throughput but approximately 2.1% lower four-stream throughput.
Thus improving forwarding did not eliminate the host's remaining multistream
disadvantage against the guest tunnel with one queue.

## C — Physical CPU idle policy

These are same-boot default/10 µs comparisons within each profile. All 20 paired
mean-latency differences favored the idle constraint.

| Profile | Default RR µs ± SD | 10 µs policy RR µs ± SD | Mean reduction | Average P99 reduction |
|---|---:|---:|---:|---:|
| Host VXLAN, legacy prerequisite control | 1007.952 ± 23.758 | 514.108 ± 10.416 | 49.0% | 41.6% |
| Host VXLAN, BPF guest forwarding | 810.660 ± 57.375 | 400.762 ± 15.732 | 50.6% | 43.1% |
| Guest VXLAN, BPF forwarding | 758.658 ± 178.839 | 408.726 ± 52.509 | 46.1% | 38.8% |
| Cilium VXLAN reference | 759.718 ± 122.518 | 400.440 ± 25.327 | 47.3% | 39.7% |

This is the strongest broad latency intervention: it helped every profile, not
only hypervisor placement. The approximately 400 µs convergence requires the
specified forwarding profile and controlled idle behavior. CPU idle tuning
alone left the legacy host profile around 514 µs. Combining BPF forwarding and
the idle constraint reduced host mean latency by 60.2% versus its legacy/default
prerequisite control. This combined result does not isolate the cost of VXLAN
header construction and does not establish a physical-host placement advantage.
Throughput under the idle constraint was **not measured**. Keeping CPUs out of
deep idle can increase power consumption; power/energy was not measured either.

The supporting original-profile screening block reproduced a 960.677 → 502.360 µs
reduction (−47.7%) over three 20-second pairs. The earlier path×idle block measured
host tunnel 975.760 → 494.277 µs and bypass 401.263 → 195.963 µs. Its paired tunnel penalty
fell 574.497 → 298.313 µs; all three interactions favored the constraint, but the
interaction's variation was broad. These support the mechanism without being
pooled into the current five-sample comparison.

## E — Queues, affinity and restoration controls

The original host one-queue reference reached 4.013 ± 0.130 Gb/s with four streams.
Two queues reached 5.601 ± 0.156 Gb/s (+39.6%); restoring one queue returned it to
3.854 ± 0.076 Gb/s. Single-stream throughput fell 3.587 → 3.326 Gb/s (−7.3%). This is
a meaningful multistream improvement with a single-stream tradeoff, not an
improvement to every workload. The diagnostics found a heavily loaded sender
vhost worker despite machine-wide idle capacity, consistent with a concentrated
backend bottleneck. TCP retransmission/reordering indicators increased with two
queues, so the gain was not a clean reduction in all networking overheads.

The later two-queue host reference was 5.664 ± 0.035 Gb/s. Pinning its vhost tasks
raised this to 6.427 ± 0.147 Gb/s (+13.5%); restoring the original masks returned it
to 5.665 ± 0.094 Gb/s. This reversal strengthens the evidence for the affinity gain.
The combined two-queue/pinned result is 67.6% above the later one-queue host
baseline of 3.835 Gb/s. This was the older forwarding profile. It was not measured
with the new BPF/idle combination, and no small-request latency measurement
accompanied the queue or affinity interventions.

For guest VXLAN, the queue control measured 4.662 ± 0.136 → 5.245 ± 0.504 Gb/s (+12.5%),
with restoration to 4.605 Gb/s. This is a moderate, less stable multistream gain.
For Cilium it measured 4.148 ± 0.128 → 4.190 ± 0.276 Gb/s (+1.0%), with restoration to
3.996 Gb/s: no meaningful multistream gain. CPU cost and TCP-processing indicators
also worsened in those two-queue comparisons. Their two-queue single-flow results
are retained in the full table, but the immediate one-queue controls measured
only multistream throughput, so no isolated single-flow queue effect is claimed
for those cases.

## F — Paired tunnel/bypass controls

These bypasses changed routes or bridge uplinks without replacing Linux VXLAN
with BPF. Holding VM boots/attachments fixed, guest tunnel latency was 946.164 µs
versus 721.710 µs in bypass, adding 224.454 ± 46.783 µs per request/response transaction.
Host tunnel latency was 1013.568 versus 756.804 µs, adding 256.764 ± 117.637 µs. All ten
paired tunnel penalties were positive. This is meaningful evidence for a cost
of the complete tunnel path under the original configuration. It includes
forwarding, interrupts, scheduling and offload effects, rather than just header
encoding. No corresponding paired throughput measurements exist. The separate
Cilium reference in that study averaged 762.086 µs and is retained as its own
reference row, not combined with other Cilium samples.

## Gaps that cannot be filled from the current data

| Missing combination | What we cannot claim |
|---|---|
| Throughput under 10 µs idle policy | That the roughly 400 µs configurations also achieved D's default-idle throughput |
| RR latency with two queues or pinned vhost tasks | That the 6.43 Gb/s host configuration also reduced small-request latency |
| Full repeated custom host TC shortcut measurements | A robust performance benefit from our bridge-bypass program |
| Fresh prerequisite-matched BPF throughput controls | A pure host-routing effect separated from other prerequisites and collection drift |
| BPF + idle constraint + two queues + affinity together | The combined best latency and best throughput numbers in one measured configuration |
| Paired tunnel/bypass throughput | An attachment-controlled throughput cost of encapsulation alone |

These cells remain unavailable. **No new suite was run to fill them.** The
existing data already supports a useful answer: supported guest forwarding and
CPU idle control improve latency; queues/affinity improve host multistream
throughput; placement alone has workload-dependent effects; and neither the
custom TC shortcut nor larger MTU has demonstrated a broad performance win.

## Where the accepted measurements live

Use [the detailed results index](results-guide.md) for exact attempts and
exclusions. The main roots are:

| Group | Raw result root |
|---|---|
| A1 original mode-only comparison | `results/collection-20261125T020122.055126Z/` |
| A2 baseline rerun and E queue/affinity controls | `results/extended-study-20261125T042706Z/` |
| F paired paths and C2 older idle interaction | `results/latency-isolation-20261125T144819Z/` |
| C original-profile screening | `results/host-latency-20261125T181639.324738Z/` |
| B2 default forwarding and C full idle controls | `results/host-latency-20261125T183320.140179Z/` |
| D completed default-idle throughput | `results/host-followup-workflow-20261125T194314.961685Z/throughput/` |
| B1 custom TC correctness only | `results/bpf-integration-smoke-20261125T171045.529636Z/` |

The collation's `cases.json` and numerical table preserve every selected source
attempt, including separate RR/throughput blocks for the current configurations.
Failed attempts and instrumented profiles/traces are excluded from primary
efficacy estimates. Short validation cases are identified as smokes and are
retained separately rather than silently promoted into performance evidence.
