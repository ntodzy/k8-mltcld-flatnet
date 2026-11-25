# Final data collection

Run all eight experiment modes with one command from the repository root:

```bash
./experiments/collect.py --background
```

The script prints a process ID and a results directory, then continues in the
background. It resets/rebuilds the guest cluster between modes, preserving VM
disks and existing SSH keys. Current Kubernetes workloads and cluster state are
removed. The CloudLab reservation, existing deployment config, generated SSH
files, controller machine, and network connectivity must remain available.
The original custom VXLAN modes require an experiment LAN supporting MTU 1550;
the `*-vxlan-1450` variants require only MTU 1500.

The default collection runs:

| Mode | Client → server in the example topology |
| --- | --- |
| `vxlan` | worker-1 → worker-2 |
| `native` | worker-1 → worker-2 |
| `vm-local` | worker-2 → worker-3 |
| `direct-1450` | worker-1 → worker-2 |
| `vm-vxlan` | worker-1 → worker-2 |
| `host-vxlan` | worker-1 → worker-2 |
| `vm-vxlan-1450` | worker-1 → worker-2 |
| `host-vxlan-1450` | worker-1 → worker-2 |

Cross-host modes use the same two workers. `vm-local` uses two different
workers on the same hypervisor and measures only host-network traffic.
The pairs are selected from the base config and checked by the runner.

Defaults are **five samples, 30 seconds per test/sample, four streams, one
round**. That is approximately **113 minutes of measured traffic**, plus host
preparation, eight cluster setups, image pulls, and diagnostic collection.
The default host profile is `baseline`, which restores NIC settings previously
changed by the host tuning script. That profile remains the same for every mode.
No Cilium eBPF tuning profile is silently enabled.

## Options

```bash
# Preview the plan without contacting hosts or writing result files.
./experiments/collect.py --dry-run

# Three complete rounds, reversing mode order on alternating rounds.
./experiments/collect.py --rounds 3 --background

# Explicit endpoint pairs, tuning profile, and measurement parameters.
./experiments/collect.py --client worker-1 --server worker-2 \
  --local-client worker-2 --local-server worker-3 \
  --host-profile optimized --samples 5 --duration 30 --streams 4 --background

# Collect only a matched subset.
./experiments/collect.py --modes vxlan direct-1450 --background

# Separate samples/tests and monitor both VMs and hypervisors.
./experiments/collect.py --modes direct-1450 vxlan --diagnostics --background

# Latency only: omit single/multistream throughput workloads.
./experiments/collect.py --modes direct-1450 vxlan --diagnostics --test-type rr --background
```

For the three implementations at equal inner MTU, use:

```bash
./experiments/collect.py --modes vxlan vm-vxlan-1450 host-vxlan-1450 \
  --pod-only --background
```

`--pod-only` excludes host-network controls and must be used with an explicit
mode list excluding `vm-local`. For these three modes, traffic measurement takes
approximately 23 minutes plus three cluster setups and diagnostic collection.
See [matched comparison](matched-vxlan.md) for what is held equal.

Other options are `--config PATH`, `--underlay-subnet CIDR`, and `--output PATH`.
`--test-type` accepts `all` (default), `rr`, `single`, `multi`, or `throughput`;
the workload selection is saved and retained on resume. Optional
[host/guest BPF forwarding experiments](bpf-forwarding.md) can be selected with
`--modes`; they are excluded from the default eight-mode plan.
See [diagnostic collection](diagnostics.md) for per-core CPU, queues, offloads,
TCP counters, and a separate perf profiling option. Diagnostic mode adds Pod
setup/cleanup per sample/test/path; allow substantially more time than the
combined measurement. Its setting is saved and retained on resume.
Omit `--background` to run in the foreground. New collections reject existing
output directories. A shared lock prevents another collection or the currently
launched matched comparison from running concurrently. Avoid manual
`setup.py`, `cluster.sh reset`, or deployments while collection is running.

## Results and progress

Results are stored in `results/collection-UTC_TIMESTAMP/`:

- `collection.json`: parameters, selected endpoints, current phase, per-mode
  attempts, status, timestamps, and a digest of the base deployment config.
- `collection.log`: background process output; `pid` identifies that process.
- `host-preparation.log`: initial QEMU/KVM preparation and NIC profile setup.
- `NN-MODE-attempt-N/`: setup and measurement logs, host offload/queue audits,
  and a `measurement/` directory containing the runner's original artifacts.
- `metrics.csv`: numeric Cilium report values from successful runs, including
  units, labels, mode, round, endpoints, and links to the original report files.
  It preserves reported values rather than calculating new sample statistics.

To see whether the latest collection or matched comparison has finished:

```bash
./experiments/status.py
```

To watch it and return to the shell automatically when it finishes:

```bash
./experiments/status.py --watch
```

It refreshes every 10 seconds, prints `DONE` on success or `FAILED` on failure,
and exits (zero on success, nonzero on failure). Watching does not control or
stop the experiment; Ctrl+C stops only the watcher. To select a specific run:

```bash
./experiments/status.py --directory results/collection-TIMESTAMP --watch
```

The background launcher prints that exact watch command. Detailed logs remain
available with `tail -f results/collection-TIMESTAMP/collection.log`.


A suite passes only when the measurement process succeeds and its metadata
reports successful JSON collection with the expected endpoints and parameters.
Failure stops the collection; previous results and failed-attempt logs remain.
To retry the failed mode and continue:

```bash
./experiments/collect.py --resume results/collection-TIMESTAMP --background
```

Resume uses the saved parameters and skips successful modes. It creates a new
attempt directory rather than overwriting a failed run. Changing the base
config requires a new collection so different reservations/topologies are not
silently combined. The cluster remains in the last successfully configured
mode, or in a partial setup after a failure; resume resets it before retrying.

If setup failed and you have already repaired it by successfully running
`experiments/setup.py MODE`, resume using that current setup:

```bash
./experiments/collect.py --resume results/collection-TIMESTAMP \
  --use-current-setup --background
```

This skips reset/setup for **only the first unfinished mode**. The mode and
inner/guest/physical MTUs in `generated/active-experiment.json` must match that
next mode, the base config digest must still match, and the original collection's
host preparation must have succeeded. The runner still checks live node readiness
and Cilium configuration before measurement. Host NIC audit runs again without
reapplying the host tuning profile. Later unfinished modes reset/setup normally.
The option does not resume halfway through diagnostic samples: it measures the
unfinished mode in a new attempt, preserving previous failures and all successful
modes, then updates the original collection manifest and metrics CSV.

The new attempt saves `reused-setup.json`, a setup log explaining the skipped
reset, and `setup_source` provenance in `collection.json`. The reuse request is
not saved as a default for future resumes. Preview without executing anything:

```bash
./experiments/collect.py --resume results/collection-TIMESTAMP \
  --use-current-setup --dry-run
```

Do not run a standalone measurement concurrently with the resumed collection.

See [runner documentation](runner.md) for metric definitions, whole-run CPU
limitations, and the distinction between Cilium's same-node and physical-host
placement. Check the physical-NIC capture described in [vm-local](vm-local.md)
when verifying that its unicast measurement stays off the physical link.
