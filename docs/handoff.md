# Work handoff

## Active overnight work (November 24–25)

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
