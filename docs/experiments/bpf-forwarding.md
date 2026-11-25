# Separate BPF forwarding experiments

These optional experiments retain custom Linux VXLAN and inner MTU 1450. They
are excluded from the original eight-mode default suite and stored under their
own mode names. This is forwarding bypass, not removing VXLAN encapsulation,
changing the physical MTU, DPDK, or hardware offload.

| Mode | Tunnel location | Forwarding change | Primary control |
| --- | --- | --- | --- |
| `host-vxlan-1450` | Hypervisor | Existing Linux bridge | Original host baseline |
| `host-vxlan-bpf-pass-1450` | Hypervisor | Flow lookup/counters, then ordinary bridge | BPF attachment/lookup cost |
| `host-vxlan-bpf-1450` | Hypervisor | Flow-specific TAP ↔ VXLAN TC redirect | Host BPF pass-through mode |
| `vm-vxlan-1450` | Guest | Original Cilium/native + Linux VXLAN path | Original guest baseline |
| `vm-vxlan-bpf-legacy-1450` | Guest | BPF service/masquerade prerequisites, legacy host routing | Guest prerequisite control |
| `vm-vxlan-bpf-1450` | Guest | Same prerequisites, Cilium BPF host routing | Guest BPF legacy mode |

The **host pair** changes only the action of an identical BPF program. Each
attachment matches exactly one direction between the two current benchmark Pod
IPs and only untagged, nonfragmented IPv4 TCP without IP options. The transmit
hook redirects from the selected VM's managed experiment TAP to `cs740hvx`.
The receive hook redirects from `cs740hvx` to the managed TAP. Ethernet and TTL
remain unchanged, preserving the L2 forwarding semantics. Linux still handles
the tunnel, FDB and outer UDP/IP transmission. Other traffic follows the existing
bridge. Guest Cilium programs and policies are not replaced.

The **guest pair uses supported Cilium host routing**, not the raw lab program.
Here Cilium's "host" is the Kubernetes VM, not the physical hypervisor.
Cilium's TCX programs can terminate a hook before legacy TC executes; simply
appending our lab program would not establish acceleration. Placing it before
Cilium could skip policy processing. Instead both guest profiles enable BPF
masquerading and kube-proxy replacement, and skip kube-proxy installation during
the fresh kubeadm initialization. Their only Helm-value difference is
`bpf.hostLegacyRouting: true` versus `false`. Guest interfaces remain direct/
macvtap with the existing static `cs740vx` routes and peers. No netkit, BIG TCP,
CPU policy, queues or affinity changes are bundled into these modes.

[Cilium documents host-routing requirements and operational status](https://docs.cilium.io/en/stable/operations/performance/tuning/#ebpf-host-routing).
The guest experiment therefore tests supported Cilium forwarding integration
around **our custom tunnel**, while the host experiment tests our custom bridge
shortcut. They are not identical BPF implementations moved between kernels.

## Run the complete optional comparison

Existing hypervisor and node SSH credentials are reused. From the repository root:
Host modes also require a local clang with a BPF target and existing remote
`tc`, `bpftool`, libvirt and passwordless sudo. The current testbed has these;
the runner records compiler/preflight failures rather than installing packages
during a performance sample. Keep host BPF result directories under `results/`
so interrupted-session recovery can validate their ownership records.

```bash
./experiments/collect.py \
  --modes host-vxlan-1450 host-vxlan-bpf-pass-1450 host-vxlan-bpf-1450 \
          vm-vxlan-1450 vm-vxlan-bpf-legacy-1450 vm-vxlan-bpf-1450 \
  --client worker-1 --server worker-2 --pod-only --diagnostics \
  --duration 30 --samples 5 --streams 4 --background
```

This resets/rebuilds the cluster between modes and collects the usual Cilium CLI
RR, single-stream and multistream workloads with endpoint/hypervisor diagnostics.
It preserves raw reports, mode labels, setup logs, retries and host NIC audits.
Resume with `./experiments/collect.py --resume RESULTS_DIRECTORY --background`.
Default `collect.py` still runs the original eight modes.

For a shorter exploratory collection use `--duration 15 --samples 3`. Do not
combine short and long trials without retaining their identities. Each host
BPF diagnostic phase adds a **60-second Cilium CLI setup delay** so Ready Pod
identities can be discovered and both host agents attached before traffic.
This is lifecycle time, not measured test duration. Both host BPF controls have
the same delay. The baseline host mode retains its usual startup behavior.
For a latency-only comparison, add `--test-type rr` to the collection command;
this omits the two throughput workloads and their repeated setup delays.

Use existing aggregation for completed attempts, preserving each mode:

```bash
./experiments/aggregate.py RESULTS_DIRECTORY/02-host-vxlan-bpf-pass-1450-attempt-1 \
                           RESULTS_DIRECTORY/03-host-vxlan-bpf-1450-attempt-1
```

Actual numeric prefixes follow the selected mode order and retries. The utility
exports tables and PNG/SVG figures. Compare each bypass against its stated
control; a host/guest cross-mode difference also includes attachment and execution
context differences. The collector does not promise same-boot paired estimates:
for that, repeat a host action contrast with `--reuse-base` as described below.
Guest profiles require fresh setups; do not call their cross-reset difference
an isolated per-packet encapsulation cost.

## Evidence and validity checks

Host results contain `host-bpf/`: exact Pod identities, copied source/object,
compiler output, agent source, preflight links/hooks/TAP, program/map IDs,
per-direction matched/redirected/passed counters, readiness and restoration.
Counters cover the attachment lifetime, including netperf control/setup traffic,
and use shared atomic updates; they are path evidence, not an exclusive count
of application transactions. Both host controls incur the counter operations.
Preflight also records bridge FDB and bridge-netfilter settings; the shortcut
can skip enabled bridge-netfilter work as well as the bridge lookup itself.
Selected mode actions must carry traffic on all four attachments. The runner
refuses a passed result without those counters. It refuses existing classic
filters or TCX on the selected host ports and never replaces Cilium programs.

Guest results save actual endpoint-agent `cilium-dbg status --output json` output.
Both agents must report the requested **routing.intra-host-routing-mode** as
**Legacy/BPF** and their inter-host mode as Native; the ConfigMap
must match all prerequisites. Static custom routes are validated, and before/
after `cs740vx` receive counters must advance on both endpoint VMs, proving
activity on both transmission legs. All TX counters are also retained. One live
control showed a non-advancing worker-2 TX packet counter despite about 5,000
receipts at each end and successful TCP transactions; it is recorded as an
anomaly rather than interpreted as zero actual transmission. Its cause has not
been isolated.
Device activity alone is not an exact per-flow trace. Use a separate bounded
kernel trace if claiming how many ordinary forwarding calls were eliminated.
A configured flag alone is insufficient to claim stack bypass or a speedup.
The pinned [Cilium 1.20 routing API](https://github.com/cilium/cilium/blob/v1.20.4/api/v1/models/routing.go)
defines these JSON fields. This avoids depending on the text status layout,
which now combines network and host routing on one line.

For a separate instrumented path check on the prepared mode:

```bash
./experiments/forwarding_trace.py vm-vxlan-bpf-1450
# Or, after preparing its matched control:
./experiments/forwarding_trace.py vm-vxlan-bpf-legacy-1450
```

This optional utility uses the existing four-machine tracer with worker-1 as
client and worker-2 as server. A 30-second RR case contains about 15 seconds of
active tracing, armed after the diagnostic sampler observes netperf. Raw traces
and CSV/JSON kernel observations report executed `ip_forward`, `br_forward`,
VXLAN and vhost stages, including available probes with zero hits. It also accepts
the host BPF pair and original custom MTU-1450 modes. It acquires the collection
lock and is not included in the untraced collector. Tracing adds substantial
overhead: its timings overlap and must not be pooled with primary measurements.

Idle policy matters substantially for RR. The experiments do not silently add
CPU latency QoS requests. Hold power policy, queues, masks, workload and endpoint
placement constant in comparisons, and retain their diagnostics. No performance
benefit is assumed; pass-through BPF may add cost, and the shortcut may fail to
improve the actual bottleneck. Policy/service regression testing is needed
before treating any research profile as a general deployment recommendation.

## Cleanup and restore the original cluster

Host filters use classic TC and require explicit removal; closing a program
descriptor does not automatically detach them. A lifetime-scoped agent handles
this: the runner closes its input after measurement; the agents collect counters, delete only their own
filters, and remove only empty qdiscs they created. EOF, SIGTERM and a bounded
lifetime also trigger cleanup. A failed cleanup retains
`infrastructure/generated/forwarding-control.json`, and normal runs/setup refuse
to continue. Inspect the corresponding `host-bpf/*/session.jsonl` and restoration
proof before deleting anything; never flush all filters or remove Cilium links.
A host failure/SIGKILL can require ownership-checked manual recovery using the
recorded exact device, preference 49174, handle 0xc740 and program ID.

After the owning runner has exited, first try the ownership-checked recovery:

```bash
python3 experiments/host_bypass.py --restore
```

It takes the collection lock, refuses a still-existing owner PID, uses recorded
program IDs, and retains the marker if cleanup cannot be proven. Recovery
artifacts must be under the repository's `results/` directory. Missing ownership
proof requires inspection rather than flushing unrelated filters.

After successful cleanup, return to the baseline with:

```bash
./experiments/setup.py host-vxlan-1450 --reset
```

This resets Kubernetes and reboots guests, recreates the original host overlay,
and restores the baseline Cilium profile with kube-proxy. It preserves VM disks,
SSH keys and base configuration. The collector otherwise leaves its final mode
prepared, as with existing collections.

If only the two host BPF modes were used on the same prepared topology, a
lighter label restoration is available after verified filter cleanup:
`./experiments/setup.py host-vxlan-1450 --reuse-base`. It changes no VM or Cilium
settings. It cannot restore a cluster that used either guest forwarding profile.

Individual mode setup/run commands are in the four linked experiment guides:
[host pass-through](host-vxlan-bpf-pass-1450.md),
[host redirect](host-vxlan-bpf-1450.md),
[guest prerequisite control](vm-vxlan-bpf-legacy-1450.md), and
[guest BPF host routing](vm-vxlan-bpf-1450.md).
