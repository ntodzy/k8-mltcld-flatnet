# Work handoff

## Active overnight work (November 24–25)

07:27 UTC update: VM-local attempt 2 passed its full measurements (multi
24.080 ±0.510 Gb/s, mean RR109.706 ±1.830 µs). Cilium VXLAN passed (multi
4.015 ±0.181 Gb/s, mean RR821.668 ±53.065 µs). Thus its multistream result
matches the initial host baseline closely; do not repeat the prior assumption
that host VXLAN alone has poor stream scaling. Initial study is now setting up
vm-vxlan-1450. Based on this result, `final_controls()` now adds matching queue
controls for Cilium and per-VM VXLAN too, after the host affinity/reversal series,
then restores the initial host mode. The same `--append-host-controls` command
now appends 22 steps for a total of 44, not the earlier nine/31. Each extra mode
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
