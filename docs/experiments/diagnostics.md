# Diagnose Pod networking throughput

To compare native routing and Cilium VXLAN at Pod MTU 1450, including host-network
controls and telemetry on both endpoint VMs and both hypervisors:

```bash
./experiments/collect.py --modes direct-1450 vxlan --diagnostics --background
```

This resets/rebuilds the cluster between modes using the existing deployment
configuration and SSH keys. It collects five 30-second samples per test, with
four streams for multistream tests. Watch completion using:

```bash
./experiments/status.py --watch
```

Each sample, network path (Pod or host), and test type (request/response,
single-stream, or multistream) runs in a separate Cilium CLI invocation. The
two-mode default therefore executes 60 invocations and 30 minutes of traffic.
Allow additional time for cluster rebuilds, Pod creation, readiness, snapshots,
and cleanup for every invocation; this is slower than the combined runner.
There is no concurrent benchmark traffic between phases.

To diagnose only throughput on an already prepared cluster:

```bash
./experiments/run.py vxlan --client worker-1 --server worker-2 \
  --diagnostics --test-type single --samples 5
./experiments/run.py vxlan --client worker-1 --server worker-2 \
  --diagnostics --test-type multi --samples 5
```

Use `--network pod` or `--network host` to select one path. `--pod-only` remains
available. Diagnostics excludes `--same-node`: each phase compares distinct VMs.
Use `--dry-run` to print the plan without SSH or output files.

## What is collected

Collectors start over the existing SSH aliases in parallel and all must report
ready before the benchmark starts. Each remote collector requires Python 3 and
noninteractive sudo, already used by the runner. No packages are installed and
no networking settings are changed.

Every approximately one second, collectors record UTC and monotonic timestamps,
per-core `/proc/stat`, softirq/interrupt counts, `/proc/net/softnet_stat`, TCP/IP
counters, interface counters, `ss -tinp` socket details, and visible `netperf`
process IDs and commands. `/proc/stat` supplies per-core CPU, softirq, and guest
steal time without requiring `mpstat`/sysstat. Socket details include the fields
available from the host kernel, such as retransmissions and congestion window.
Sampling intervals can exceed one second if a command is slow.

Before and after each invocation, collectors save interfaces/routes, `nstat`
absolute counters, NIC statistics (`ethtool -S`), offloads (`-k`), channel counts
(`-l`), receive indirection (`-x`), interrupt affinity, and RPS/XPS CPU masks.
These inspect all non-loopback interfaces, including tunnel and virtual devices.
Install `iproute2` and `ethtool` on the hosts if those tools are absent. Unsupported
NIC operations and missing commands are retained as explicit error records;
they are never interpreted as zero counters. Collection does not install them.

The coordinator uses the client's observed `netperf` activity to select an
interior workload window. Summaries calculate per-core CPU, steal, softirq,
TCP counter, and softnet deltas only within that window. Busy CPU excludes
idle, I/O wait, and steal; softirq time is a subset of busy time. Guest ticks
are not counted twice. Too few active observations produce a warning and no
CPU attribution. No observed client workload or a failed sampler fails the run.

Cross-host alignment uses local SSH receive timestamps, so clock skew does not
directly align the summary; SSH buffering and polling still introduce timing
uncertainty. This is approximate phase attribution, not hardware synchronization.
NIC/queue deltas cover the entire invocation, including setup and cleanup.
TCP counters and system-wide CPU include other workloads on the monitored host;
keep the testbed quiet. TCP retransmission counters are repeated on each core's
CSV row and must not be summed across cores.

## Output

The diagnostic result root contains `metadata.json`, a phase index and status;
`diagnostics.csv`, per-core CPU/softirq/steal and host-wide TCP/softnet deltas;
and `reports/`, the original Cilium reports under sample-specific directories.
Each `phases/sample-NN-NETWORK-TEST/` contains:

- `measurement/`: the ordinary runner's reports, metadata, console output,
  Pod placement, Cilium configuration, and snapshots.
- `summary.json`: active-window CPU/TCP/softnet summaries, whole-invocation
  NIC/queue counter deltas, and capability/coverage warnings for every target.
- One directory per endpoint VM or hypervisor with `samples.jsonl` and
  `ssh-stderr.log`.
- `console.log`: the child runner's output. The parent prints phase progress.

Using one sample per invocation also prevents Cilium's JSON report from hiding
earlier samples when repeated samples overwrite the same metric keys. The suite
collector exports all sample-specific reports through its existing `metrics.csv`.
The default runner remains unchanged unless `--diagnostics` is specified.

## Separate CPU profiling

Run a separate short diagnostic experiment to identify expensive kernel functions:

```bash
./experiments/run.py vxlan --client worker-1 --server worker-2 \
  --diagnostics --profile --test-type multi --network pod --samples 1 --duration 30
```

`perf` must already be installed and usable as root on all monitored VMs and
hypervisors. Missing/broken profiling fails this profiling run with details in
`profile_result`/`profile_error` records. The script does not enable Cilium's
`--unsafe-capture-kernel-profiles`, install packages, or change kernel settings.

Once client `netperf` activity is observed, the coordinator requests system-wide
`perf record -a -F 49 -g` on all targets, capped at the configured test duration
(maximum 3600 seconds). Trigger delay means the capture can miss the beginning
and include the tail of cleanup. Each target saves `perf.data` locally in its
result directory and a textual `perf report` in `samples.jsonl`. Temporary remote
files are removed on normal completion; collectors stop on SSH stdin EOF and
also have a two-hour safety deadline. Symbol visibility depends on the host's
kernel/perf configuration; guest and hypervisor profiles describe their own
respective kernels.

Profiling consumes resources and its throughput must be treated as a diagnostic
result, separately from final performance measurements. Ordinary diagnostics
also adds sampling/SSH overhead; compare matching monitoring settings and retain
an unmonitored control when quantifying that overhead.
