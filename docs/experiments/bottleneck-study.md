# Host VXLAN bottleneck study and complete rerun

This study reruns all eight existing modes with guest and hypervisor diagnostics,
and adds queue, profiling, and attachment controls. It does not add contention,
multi-VM scaling, Hubble toggles, or Cilium tuning profiles. Main measurements
use five 30-second samples, four streams, Pod traffic across worker-1/worker-2;
the local mode uses host traffic across worker-2/worker-3. All modes retain the
existing Cilium baseline and host offload configuration.
Before its quantitative samples, the local mode performs a separate 5-second
multistream validation with an egress-only physical-NIC TCP capture filtered to
the two VM experiment IPs. It checks the actual host-network Pod addresses and
live same-parent macvtap bridge attachments, requires zero captured egress packets
and zero capture drops, and saves the pcap and validation JSON. NIC transmit-byte
counters must remain below 1% of the estimated application bytes; these counters
also include unrelated traffic over the entire invocation. This bound supports
the path check and does not claim exactly zero background wire traffic.
An ordinary bidirectional capture is insufficient: Linux deliberately passes
locally forwarded macvtap frames to the lower device's packet taps before local
delivery, so tcpdump can see them without NIC transmission. See
[`macvlan_queue_xmit` in Linux 6.8](https://github.com/torvalds/linux/blob/v6.8/drivers/net/macvlan.c#L497).
The libpcap received-by-filter counter is retained but can precede directional
filtering; captured egress and kernel-drop counts determine capture validity.
That validation run is excluded from the five-sample performance averages.

## Running and watching

The initial successful mode must be `host-vxlan-1450`, with the baseline
single-queue worker NICs. Start or resume the durable sequential plan:

```bash
./experiments/study.py --background
./experiments/status.py --directory results/extended-study-TIMESTAMP --watch --interval 300
./experiments/study.py --resume results/extended-study-TIMESTAMP --background
```

After the initial plan completes, add the final stable host baseline and
reversible vhost-affinity comparison, then leave the host mode on one queue:

```bash
./experiments/study.py --resume results/extended-study-TIMESTAMP \
  --append-host-controls --background
```

This rebuilds the initial host-vxlan-1450 mode, repeats the full baseline with
the settled sampler, and compares unpinned/pinned/unpinned two-queue multistream
runs (three/five/three samples). Only vhost threads belonging to the two workers
are pinned, across two distinct physical cores on the NIC NUMA node if exposed.
Guest vCPU and IRQ affinities remain automatic. The worker management vhost thread
also receives a mask, so this is a worker-backend affinity control rather than
perfect isolation of one network device. Original masks are backed up, validated
against process lifetimes, restored before the reversal, and queues are finally
restored to their original configuration. No pinning persists across VM restarts.
The appended plan also repeats the queue intervention on Cilium VXLAN and
per-VM VXLAN at inner MTU 1450: three one-queue multistream samples, five
two-queue single/multistream samples, and three restored-queue multistream
samples per mode. This distinguishes a general virtio/backend improvement from
a benefit specific to the host tunnel. Finally it rebuilds host-vxlan-1450 with
the original one-queue configuration. These are queue controls for one worker
pair, not multi-VM contention/scaling tests.

The study shares the collection lock and rejects overlapping collections. It
records setup, queue transitions, measurements, failures and retry directories
in `collection.json`, writes `collection.log`, and never overwrites an earlier
attempt. Successful steps are skipped on resume. Changing the base deployment
configuration is rejected. A failed measurement restarts that measurement in a
new attempt; it does not silently reuse incomplete samples. Status checks now
default to 300 seconds, can be changed with `--interval`, and print only changed progress.
Packet/CPU sampling remains about once a second; orchestration status polling
and the scientific sampling interval serve different purposes.

After completion, generate the analysis, component comparison tables and graphs:

```bash
python3 experiments/analyze_study.py results/extended-study-TIMESTAMP \
  --output results/extended-study-TIMESTAMP/analysis
```

This requires the Matplotlib environment described in [aggregation](aggregation.md).
`--partial` labels interim reports and includes only successful measurement steps.
The output separates ordinary modes, matched VXLAN, queue controls, attachment
controls, affinity controls, and MTU comparisons. It also retains evidence tables and profiling
results, including estimated physical-host CPU-seconds per application GB;
guest CPU is not added again to host CPU accounting.

## Additional observations

The controller discovers the experiment namespace's actual Pod UIDs and nodes.
Guest collectors match those UIDs to process cgroups, accepting systemd's
underscore form of UUIDs. Only matched benchmark netperf processes establish
the active workload window. Each identified Pod namespace is sampled once per
poll using `/proc/PID/net/{snmp,netstat}` and `nsenter -t PID -n ss -tinp`.
This exposes benchmark namespace retransmissions, timeouts, socket congestion
windows and RTT observations. Host-network Pods still share the VM host network
namespace, so their namespace-wide counters include other host connections.
Raw observations preserve Pod UID, cgroup, namespace identity and command lines.

Hypervisor and guest observations include per-thread CPU ticks and scheduler
runtime/wait counters for QEMU, vhost, softirq workers, Cilium, and benchmark
Pod processes. QEMU thread records identify the VM domain and distinguish
individual task IDs. Thread CPU is normalized to one CPU; machine-wide CPU is
an average across that machine's cores. Host vCPU-thread time may already include
guest execution and must not be added to guest CPU percentages. Scheduler wait
counters are not reliable evidence of no waiting if kernel scheduler statistics
are disabled; the sampler records that setting and reports the limitation.
Snapshots also retain qdisc statistics, physical and virtual interface counters,
NIC queue counters/offloads, interrupt affinity, steering masks and relevant
network backlog/budget sysctls. stdout and stderr are kept separately so
iproute2 informational messages cannot corrupt JSON statistics.

## Queue and profiling controls

The baseline host mode is followed by a separate one-sample multistream perf
profile. The two-queue control changes only worker experiment NIC drivers to
`vhost` with two queues; management interfaces, guest vCPU counts, MTUs and tunnel
placement stay fixed. It gracefully stops/starts only the affected workers,
verifies their channels, waits for Kubernetes readiness, and records before/after
domain XML. It does not forcibly destroy a guest that fails to shut down.
The previous driver configuration is saved on the hypervisor under
`/var/lib/cs740-network/queues/` and restored before a three-sample multistream
reversal check. The full baseline and two-queue single/multistream runs both use
five 30-second samples. Subsequent setup reconciles the ordinary baseline.

The queue utility is also available separately when no collection is active:

```bash
./experiments/queues.py --queues 2 --vms worker-1 worker-2
./experiments/queues.py --restore --vms worker-1 worker-2
```

Profiling uses software CPU-clock samples at 49 Hz on both endpoint VMs and
their hypervisors, avoiding dependence on guest hardware performance counters.
Raw `perf.data` and text reports are retained. Profiles are diagnostic runs;
their throughput is not pooled with unprofiled results.
The same separate profile records `skb:kfree_skb` drop reasons and locations for
up to 15 seconds into `drops.data`, with summarized counts in the diagnostic
stream. These events are system-wide and not filtered to the benchmark flow;
reason counts alone do not establish where a particular TCP segment was lost.

## Bridge attachment control

`bridge-1450` is an additional control rather than a ninth default suite mode.
It uses Cilium native routing with Pod and guest MTU 1450, and TAP-backed guest
NICs attached to `cs740br`, whose physical uplink carries no VXLAN. Physical MTU
is 1500. This is closer to the host tunnel's guest attachment than the original
direct/macvtap native baseline. Setup refuses to bridge an experiment interface
carrying an IPv4 default route. It installs a reversible direct-bridge service;
later ordinary setup disables it and detaches only that experiment uplink.

```bash
./experiments/setup.py bridge-1450 --reset
./experiments/run.py bridge-1450 --client worker-1 --server worker-2 --pod-only --diagnostics
```

This control still does not yield a pure additive bridge cost: replacing a
tunnel with a physical bridge port changes batching and offload interactions.
Use it alongside matched TCP observations, queue distribution and profiles.
Increasing queues is an intervention, not a guaranteed optimization. If throughput
does not improve, retain that negative result and investigate TCP loss, backend
contention, and software segmentation rather than enabling unrelated settings.

## Lifecycle overhead

The runner batches read-only machine snapshots over one SSH connection per
target and collects independent targets concurrently. Once measurement/report
capture is complete, only its own benchmark namespace's Pods receive a shorter
termination grace before namespace deletion. This reduces SSH and cleanup time
without changing Cilium's benchmark duration or removing readiness validation.
Collectors continue to distinguish workload windows from invocation-wide NIC
counter windows. Terraform reconciliation now waits for guest SSH before applying
guest MTUs, covering the interface-change startup race observed earlier.
