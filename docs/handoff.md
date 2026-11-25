# Work handoff

## Completed latency-isolation follow-up (November 25)

The requested isolation study is complete. Do not restart or append controls.
All 44 steps passed without failed attempts, producing 42 TCP_RR phase reports
and 20 successful machine traces. All 95 unit tests passed after restoration.
Full results and graphs: `results/latency-isolation-20261125T144819Z/analysis/report.md`.
The first section of `report.md` explains the findings and limitations.

Five same-boot, same-attachment tunnel/bypass pairs measured marginal transaction
latency of 224.454 ±46.783 µs for custom VM VXLAN and 256.764 ±117.637 µs for
custom host VXLAN. All ten pair differences were positive. These are complete
path costs, not exclusive header CPU costs. Shared Linux header helpers averaged
roughly 3–4 µs under instrumentation. Custom VM traffic traversed Linux
`ip_forward`; the available probe had zero hits for the Cilium reference.
Custom `cs740vx` DOES have generic `cil_from_netdev` TCX ingress; Cilium's tunnel
has dedicated `cil_from_overlay` ingress and `cil_to_overlay` egress. Never claim
that the custom configuration has no BPF or that Cilium bypasses the kernel.
Host worker `handle_tx` means rose from ~53 to ~109 µs with the tunnel.
Inclusive, probe-perturbed timings overlap and must not be added into an RTT budget.

A separate three-round host idle-policy block used temporary descriptor-scoped
10 µs CPU latency QoS requests on both hypervisors. Default paired tunnel penalty
574.497 ±214.569 µs fell to 298.313 ±22.989 µs, a mean interaction of
276.183 ±205.015 µs. All three interactions were positive, but the small sample
and broad default variation prevent a universal allocation to idle exits. Do not
pool this fresh block with the original five-pair baseline. Deep-idle usage was
zero under the constraint. No power measurements or permanent tuning were made.

Final audit: `results/latency-isolation-20261125T144819Z/final-state-audit.json`.
Host-vxlan-1450 is restored; all nodes Ready, guest queues one, original CPU masks,
physical MTU 1500 and inner MTU 1450. No path override, benchmark namespace,
tracer/benchmark process or CPU latency request remains. Base config and keys
are preserved. bpftrace and dependencies were installed on hypervisors.

Initial local snapshot: 732a76d. Implementation commits: 2a87e38, 424f2ac,
9aceae5, 715c568. Final documentation snapshot follows these in local Git.
Nothing pushed. Live work is finished; reproduction guide is
`docs/experiments/latency-isolation.md`. Retain raw data and prior studies.

## Completed overnight study (November 25)

The authorized work is complete. `results/extended-study-20261125T042706Z/collection.json`
reports **passed, 48/48 steps**. There are 25 successful measurement steps and
210 phase reports, including four separate profiles with all 16 target profiles
successful. All eight existing modes plus bridge1450 were rerun with guest and
hypervisor monitoring. Four failed attempts remain preserved/excluded; repairs
included optional JSON parsing, capture-direction interpretation, vhost task-group
discovery, and stable custom-tunnel MAC initialization across reboot.

Read `report.md` for the written findings and the study's `analysis/report.md`
for detailed tables, profiles, CSV/JSON, and 62 PNG plus 62 SVG figures.
The final state audit is `final-state-audit.json`. It verifies host-vxlan-1450,
physical MTU1500, guest/bridge/tunnel MTU1450, one queue on every guest, broad
original vhost masks, four Ready nodes, no benchmark namespaces or perf/tcpdump/
netperf processes. Both physical NICs negotiated10Gb/s. No push was performed.
The base deployment config and SSH credentials were preserved.

Key outcomes: host two queues4.013→5.601Gb/s (+39.6%), reversal3.854; separate
vhost affinity A/B/A5.664→6.427→5.665Gb/s (+13.5%). Combined versus fresh host
one-queue baseline3.835 is +67.6%, with lower physical-host CPU cost/GB. The
queue intervention helped Cilium only~1%, at higher CPU cost, and per-VM VXLAN
~12.5%, also at higher CPU cost and retransmissions. Baseline matched-mode
ranking depends on workload: Cilium lowest RR, per-VM highest multi, host highest
single. Bridge no-tunnel multi8.858Gb/s rules out the bridge alone as the full
explanation for host overlay throughput. No contention, multi-VM scaling, Cilium
tuning profiles, or Hubble toggles were run. Do not mix tuned controls into the
main equal-settings mode ranking. No required work remains.

Initial user-requested Git snapshot is `f589e34`; subsequent local commits
contain implementation and repairs. The final documentation/analysis checkpoint
can be found with `git log`. Results/generated configuration remain ignored.

## Overnight execution notes (historical)


11:31 UTC update: VM queue2 control passed (multi5.245 ±0.504 vs one4.662
±0.136 Gb/s), but its restored-one-queue run failed: reboot-created cs740vx
devices had persistent/random MACs instead of static02:74 peers. Ubuntu's
default99-default.link applies MACAddressPolicy=persistent. Fixed startup to
wait for udev settling before applying the intended MAC; added fail-closed
MAC/MTU/state preflight using existing snapshots. Snapshot2ecb9ee,88 tests.
Repaired all4 existing guest scripts with backups, restarted services, rebooted
onlyworker1/2, verified static MACs afterward and full-size1450 tunnel pings.
Audit is under vm-tunnel-startup-repair/. All86 saved endpoint snapshots from
successful prior VM modes had correct static MACs, so retain those results.
Resumed PID1293370 from vm-vxlan-1450-restored-queue-control-attempt-2. Remaining:
that3-sample reversal, VM profile, direct1450 profile setup+run, finalhost setup.
Four failed attempts total so far; all are preserved/excluded from aggregates.

Cilium queue control completed too: one queue4.148 ±0.128 Gb/s (n3), two
queues4.190 ±0.276 (n5), restored3.996 ±0.107 (n3). CPU-cost proxy
12.566→14.201→12.572 CPU-s/GB: more CPU without material throughput gain.
Two queues were verified live; receiver significant RX queues1.6±0.548 means
some invocations concentrated traffic in one queue despite two configured.
Cilium profile passed on all4 targets. Remaining plan is currently setting up
vm-vxlan-1450 for its queue controls, then direct1450 profile/final host restore.

Host affinity A/B/A has now completed successfully: unpinned two queues
5.664 ±0.035 Gb/s (n3), pinned6.427 ±0.147 (n5), restored masks5.665 ±0.094
(n3). Pinned CPU-cost proxy9.825 ±0.277 CPU-s/GB versus10.647 ±0.223 before
and10.616 ±0.073 after. Sender retrans/1000OutSegs4.264→3.428→4.101.
All broad [0..15] masks and original one-queue drivers restored. Study is now
at vxlan-queue-control-setup (31/48 passed); follow the remaining existing plan.
Pinning gain~13.5% is repeatably reversible in this workload. Compared to the
fresh host one-queue baseline3.835 Gb/s, combined queues+pinning gain~67.6%,
but do not compare this tuned result as an equal-settings implementation ranking.

Latest: initial22 passed and the full48-step plan is running. Final host baseline
passed (single3.603 ±0.157, multi3.835 ±0.109 Gb/s, meanRR1001.836 ±12.392µs).
Final two-queue repeat passed. First pin step failed before any change because
vhost workers are QEMU task-group threads on this kernel, not top-level /proc
processes. Fixed discovery to search QEMU/task plus legacy /proc leaders, tested
87 unit checks, local snapshot997a22a, resumed PID1244315. Pin attempt2 PASSED:
both workers' three vhost tasks have verified masks [1],[2],[1]. The five pinned
samples are running. Original broad masks are saved and will be restored next;
then three unpinned samples, queue restore, Cilium and VM queue controls/profiles,
direct1450 profile, and final host1450 setup. No more --append-host-controls:
the full plan has already been appended; use --resume only on failures.

09:24 UTC update: ALL INITIAL 22 STEPS PASSED. Host VXLAN inner1500 multi
4.002 ±0.188 Gb/s, mean RR1007.790 ±16.334 µs; VM VXLAN inner1500 multi
4.486 ±0.404, mean RR982.320 ±43.512. Bridge1450 control multi8.858 ±0.258,
single5.861 ±0.507, mean RR789.898 ±109.999. Thus the bridge alone does not
explain the overlay's throughput deficit, though attachment affects latency.
Next action is the already implemented append-host-controls command below,
which appends the remaining 26 steps. Scope is frozen at these 48 total steps
unless an execution failure requires a repair. Complete them, generate final
analysis/report and local Git snapshot, verify original host1450 one-queue state.

07:27 UTC update: VM-local attempt 2 passed its full measurements (multi
24.080 ±0.510 Gb/s, mean RR109.706 ±1.830 µs). Cilium VXLAN passed (multi
4.015 ±0.181 Gb/s, mean RR821.668 ±53.065 µs). Thus its multistream result
matches the initial host baseline closely; do not repeat the prior assumption
that host VXLAN alone has poor stream scaling. Initial study is now setting up
vm-vxlan-1450. Based on this result, `final_controls()` now adds matching queue
controls for Cilium and per-VM VXLAN too, after the host affinity/reversal series,
then restores the initial host mode. The same `--append-host-controls` command
now appends 26 steps for a total of 48, not the earlier nine/31. Separate
one-sample profiles on restored one-queue Cilium/per-VM VXLAN and native routing
provide comparisons to the host kernel profile; they add four steps including
native setup and are excluded from ordinary performance averages. Each extra mode
uses 3xmulti one-queue, 5xsingle+multi two-queue, 3xmulti restored, plus setup and
reversible queue changes. This tests whether the queue benefit is generic across
the tunnel paths. No contention/multi-VM scaling or Cilium tuning/Hubble profiles.

Latest update (06:30 UTC): native and direct-1450 baselines passed. Native
multi 8.858 ±0.287 Gb/s; direct-1450 8.312 ±0.583. Native RR sample means
236.63,317.75,635.75,788.77,181.15 µs show substantial run-to-run variation.
The first VM-local attempt stopped on a validation error before its main samples:
ordinary tcpdump sees local macvtap traffic at the physical lower device's taps.
Linux `macvlan_queue_xmit` explicitly forwards locally through those taps.
Hardware physical TX increased only 201516 bytes across the invocation while
the 5-second application run transferred roughly 15 GB (23.948 Gb/s). The corrected
validation uses egress-only capture, checks live same-parent bridge-mode MACs,
and compares hardware TX bytes against application bytes (<1% including background).
It preserves the failed attempt and will resume VM-local in a new attempt.
Local Git checkpoints now include `ab8b3ec` (affinity), `07889a7` (compact figures),
and `817ac04` (macvtap validation correction). Resume PID 1124699 is running
VM-local attempt 2. Its new validation PASSED: zero captured egress TCP packets,
zero capture drops, 204567 physical TX bytes versus 15196312500 estimated
application bytes (0.001346%). libpcap's pre-direction-filter received count
was 381708, demonstrating why it cannot be used as a zero-egress condition.

The user explicitly authorized remote changes, cluster resets, all eight mode
reruns, host/Pod diagnostics, queue and attachment controls, and a final analysis
report. They excluded contention/multi-VM scaling and Cilium/Hubble tuning.
They are asleep and asked for no permission questions, a local Git snapshot,
no push, and reduced orchestration polling. Snapshot commit: `f589e34`.

The durable active study is `results/extended-study-20261125T042706Z/`.
Inspect `collection.json` and `collection.log`; use `experiments/status.py`.
It holds the shared collection lock. Do not run competing tests/resets.
Resume failed work with `./experiments/study.py --resume DIR --background`.
The first host baseline attempt failed only in optional interface-counter
parsing: Ubuntu iproute2 emits a fan-map message on stderr, which had been merged
into JSON. Capture now separates stdout/stderr and parsing errors become explicit
coverage warnings. Attempt 2 is running; preserve all old artifacts.

Update: host baseline attempt 2 and the separate CPU/drop profile have passed;
the study has configured and verified two queues on both worker experiment NICs
and is measuring `host-1450-two-queues`. Initial baseline multistream mean is
4.013 ± 0.130 Gb/s, sender vhost 83.821 ± 1.958% of one CPU, sender Pod retrans
0.848 ± 0.544 per 1000 TCP OutSegs. Physical host CPU cost proxy is about
11.933 CPU-s/application-GB. These are not the old host-namespace counters.
Two receiver-host TAP tx/drop deltas were nonzero (22 and 439) in whole-invocation
windows. Avoid asserting exact flow attribution or packet/segment equivalence.

The profile succeeded on all four targets. Host sender flat self symbols include
memory copies, checksum work, DMA/IOMMU mapping and cache flushes. Most aggregate
CPU samples are idle, so inclusive child percentages are misleading; sort by
self overhead and inspect worker vhost threads. Receiver host skb-free trace
has about 204k NOT_SPECIFIED frees at skb_release_data. Linux consume/free paths
can free fragment lists this way, so these are NOT 204k lost benchmark packets.
Raw profile data is saved, including CPU perf.data and drops.data.

`experiments/analyze_study.py DIR --output NEWDIR` produces the combined report
and five comparison groups with SVG/PNG figures and evidence.json; `--partial`
labels interim output. An interim report exists at the study's `analysis-interim-1/`.
It includes only the completed baseline. Main report still needs final results,
verification, and interpretation after the complete rerun.

The VM-local diagnostic runner now performs a separate 5-second physical NIC
capture validation before its ordinary samples, saves a pcap and validation.json,
checks actual host-network Pod addresses, and requires zero matched TCP packets
and zero capture drops. tcpdump is installed on node2. This validation is not
part of its five-sample averages.

After the 22-step study completes, restore the initial host-vxlan-1450 mode and
repeat a full 5x30s host baseline using the now-stable sampler, then use that
latest baseline for the main mode comparison. This also leaves the cluster in
its initial mode. Early sampler additions were made during the first host
baseline; the queue/reversal and later mode runs use the settled implementation.
One clean way is to append setup-host-vxlan-1450 + host-1450-final-baseline steps
to the completed manifest and resume the same study (preserve all history).
Use the implemented command:
`./experiments/study.py --resume results/extended-study-20261125T042706Z --append-host-controls --background`.
It adds nine steps: host setup, full final baseline, two queues, 3-sample unpinned
multi, vhost pinning, 5-sample pinned multi, affinity restore, 3-sample unpinned
reversal, and final queue restore. This explores the observed heavy migration of
vhost threads, using two distinct physical cores near the NIC where available.
The queue intervention itself completed: single 3.326 ±0.130 Gb/s (baseline
3.587 ±0.071), multi 5.601 ±0.156 (baseline 4.013 ±0.130), about +40% multi.
TCP retrans/1000 OutSegs increased to 4.467 ±0.374, with much larger DSACK and
SACK reordering counters. Two vhost threads each carry substantial load. Do not
claim retransmissions disappeared: no TAP drops were observed in the two-queue
samples, but retransmission/reordering indicators rose. Queue-restore reversal
was running at last check; remaining ordinary modes then run automatically.

The analysis selects the last baseline per mode, so the final host repeat will
replace the initial host baseline only in the main comparison; queue-control
tables retain the initial A/B/A sequence. Do not stop work at detached status.

The study first measures current host-vxlan-1450 (5x30s, Pod-only), profiles it
separately, enables two virtio queues on the two workers, measures throughput,
restores original drivers, and performs a three-sample reversal check. It then
resets/measures native, direct-1450, vm-local, vxlan, vm-vxlan-1450, vm-vxlan,
host-vxlan, plus the new bridge-1450 attachment control. Every ordinary mode is
rerun with host monitoring. The study's main process uses its loaded plan;
measurement/setup commands read current source for each invocation.

Implemented new diagnostics: Pod UID/cgroup identification, namespace-specific
TCP counters and socket state, QEMU/vhost/softirq/benchmark per-thread CPU and
scheduler wait, virtual-interface counters, qdisc/sysctl snapshots. `perf` works
on all four monitored machines and uses software cpu-clock. A separate profile
also records skb:kfree_skb drop reasons/location for up to 15 seconds; capture
is system-wide, so do not assert every event belongs to the benchmark. Host-net
Pods share the host namespace. The new data-socket parser excludes port 12865
control connections; earlier naive cwnd/RTT summaries include them.

A 15-second smoke test completed successfully at
`results/host-vxlan-1450-diagnostics-20261125T042125.395833Z/`: 3.73845 Gb/s and
8855 retransmitted segments in the identified sender Pod namespace. This is a
short diagnostic observation, not a five-sample conclusion. It exposed the
previous host-namespace observability hole. Inspect new repeated data before
claiming a bottleneck. Original three-mode results remain unchanged.

`experiments/queues.py` gracefully restarts workers and changes only the
experiment driver; remote baseline driver backups are under
`/var/lib/cs740-network/queues/`. No forced destroy. Queue restore changes only
the saved driver, preserving current management/attachment settings. Bridge
control uses a reversible direct-bridge systemd service and protects default
management routes. Ordinary setup disables the service before switching modes.
All eight defaults remain unchanged; bridge-1450 is an additional control.

Run local tests after edits. Plotting dependencies are in `/tmp/cs740-plot-deps`;
on this NixOS session use `PYTHONPATH=/tmp/cs740-plot-deps`,
`MPLCONFIGDIR=/tmp/cs740-matplotlib`, and
`LD_LIBRARY_PATH=/nix/store/dqj57l1prad38vjrad4bs9d977i4q5zm-gcc-15.3.0-lib/lib`.
The aggregation utility now includes Pod TCP/thread/interface tables. Produce
separate main-mode, queue, MTU and attachment comparisons and a final research
analysis explaining improvements/regressions and evidence limits. This work
is not complete merely because the study is detached: monitor, fix failures,
finish the measurements, generate graphs/report, and preserve changes locally.

Recorded November 24, 2026. Read `AGENTS.md` and `README.md` before continuing.

## Research reference

The Cilium/custom VXLAN packet paths, TC/eBPF prototype proposal, XDP and DPDK
alternatives, and scope/effort estimates are saved in
[vxlan-datapath-and-bypass.md](vxlan-datapath-and-bypass.md).
No custom TC/eBPF, XDP, or DPDK prototype has been implemented.

## Existing experiment tooling

- `experiments/configure.py`: interactive hostnames/NIC settings, preserving
  existing key paths and VM settings in the local base config.
- `experiments/setup.py MODE --reset`: mode-specific infrastructure and fresh
  cluster setup. Direct/macvtap MTU must not be specified in libvirt XML.
- `experiments/run.py`: pinned Cilium CLI performance measurements. `--pod-only`
  excludes host-network controls for matched Pod workloads.
- `experiments/collect.py --background`: all eight modes, logs, raw reports,
  metrics CSV, consistent endpoint pairs, shared lock, and resumable attempts.
- `experiments/status.py --watch`: latest comparison/collection progress; exits
  with `DONE` or a failure status. Ctrl+C stops only the watcher.

The two new custom profiles, `vm-vxlan-1450` and `host-vxlan-1450`, match Cilium
VXLAN's inner MTU 1450 and physical MTU 1500. They passed local Python/template
checks but have not been measured live. Use the command in
[matched-vxlan.md](experiments/matched-vxlan.md) after the active job finishes.

## Active matched comparison

A previously authorized native-1450 versus Cilium VXLAN comparison is running
in the background using worker-1 to worker-2, five 30-second samples per test,
and four streams. Its directory is:

```text
results/matched-vxlan-native1450-20261125T005229Z/
```

At the last status check, native-1450 had completed successfully and the VXLAN
performance measurement was running after the cluster rebuilt successfully.
All four Kubernetes nodes were Ready. This is a status snapshot, not a claim
that the background job has now completed.

Inspect `comparison.json`, `collection.log`, and `pid` in that directory, or:

```bash
./experiments/status.py --watch
```

The temporary background wrapper is `/tmp/cs740-matched-comparison.py`, and
`/tmp/cs740-active-comparison` contains the output directory. The wrapper holds
`infrastructure/generated/collection.lock`; do not launch a conflicting
collection/reset. Retain successful native data if another phase fails.
`run.py` refuses existing output directories, so a retried failed measurement
must use a new output directory rather than overwrite its partial artifacts.

## Reset and rebuild fixes

The comparison exposed two lifecycle bugs, now fixed:

1. Reset recursively deleted `/run/cilium`, traversing mounted cgroupfs and
   failing on `io.prio.class`. Reset now leaves runtime mounts to reboot.
2. Reset removed `/etc/kubernetes`, while installed packages did not recreate
   it. The shared Kubernetes node role now explicitly creates that directory
   for both controller and workers before kubeadm templates are written.

After the second fix, cluster apply succeeded on the live VMs and the VXLAN
measurement began. The local regression playbook
`infrastructure/tests/reset-reapply.yml` passes. The Python suite has 51 passing
tests; profile-template checks cover both custom tunnel MTUs. Use local checks
appropriate to subsequent changes, and distinguish them from live validation.

## Working tree

There are uncommitted changes from the ongoing session, including the suite
collector/status tools, SSH policy fix, MTU-matched modes, reset/reapply fixes,
and documentation. Preserve them; no commit was requested. Local deployment
config, generated SSH/inventory files, Terraform state, and results are ignored
and must remain untracked. SSH keys are already configured; reuse them.
