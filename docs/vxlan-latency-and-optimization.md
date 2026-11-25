# VXLAN latency, CPU idle policy, and an optional forwarding prototype

Recorded November 25, 2026. This follow-up supports the completed paper dataset;
it does not change its baseline or require a new full study.

## What was isolated

Five same-boot, same-attachment tunnel/bypass pairs measured additional mean
TCP request/response latency of **224.454 ±46.783 µs for custom VM VXLAN** and
**256.764 ±117.637 µs for custom host VXLAN**. Values are mean ± sample SD of
paired transaction differences, not one-way packet delays. VM controls changed
remote PodCIDR routes while retaining the tunnel device/socket/FDB/neighbors.
Host controls switched the same bridge between its tunnel and physical uplink,
retaining guest TAP interfaces. All ten contrasts were positive.

Our scripts configure Linux VXLAN; they are not userspace per-packet encoders.
Cilium and both custom configurations share the kernel's VXLAN implementation.
Separate instrumented runs observed build/header-pull helper means of roughly
3–4 µs per call. Those observations include probe overhead and are not precise
uninstrumented costs. They do not support attributing hundreds of microseconds
to a special custom header-building algorithm.

The custom VM path traversed ordinary Linux `ip_forward`; the same available
Pod-TCP probe had zero hits in the Cilium reference. Custom `cs740vx` **does**
have generic `cil_from_netdev` TCX ingress. Cilium's tunnel instead has dedicated
`cil_from_overlay` ingress and `cil_to_overlay` egress. Both use BPF, with
different forwarding integration. For host VXLAN, instrumented worker
`handle_tx` means rose from about 53 to 109 µs with the tunnel. These are
inclusive, overlapping, perturbed timings; they cannot be added into an RTT
budget or used to allocate the complete Cilium/custom gap to one function.

See the [main report](../report.md),
[full isolation analysis](../results/latency-isolation-20261125T144819Z/analysis/report.md),
and [reproduction guide](experiments/latency-isolation.md).

## Why idle policy affects networking

A logical CPU with no runnable work can enter an idle C-state. Deeper states
save energy but take longer to leave. Linux's idle governor chooses among states
using predicted idle duration, target residency, and the allowed exit latency.
The physical hosts reported `intel_idle`, the `menu` idle governor, and an
advertised C6 exit latency of 133 µs. This is a state characteristic, not a
measured 133 µs charge on every packet. The frequency governor, `schedutil`, is
a separate mechanism governing operating frequency. KVM halt polling and
scheduler CPU placement are separate again.

Our sequential request/response test leaves gaps: the client waits for a reply,
and the server waits for another request. CPUs serving interrupts, guest vCPUs,
or vhost work can become idle between transactions. An extra processing stage
can change which CPUs wake and when, so its end-to-end effect includes those
interactions. High idle percentage therefore does not establish low latency.
A bulk stream keeps the path busier and need not exhibit the same effect.

As an illustrative path, not a measured event sequence:

```text
packet arrives -> physical CPU exits idle -> receive processing
               -> vhost/guest work becomes runnable -> application responds
```

Our scheduler wakeup-to-run means around 9–12 µs do not rule out idle effects:
they do not capture every delay preceding the wakeup tracepoint, including all
hardware and virtual-interrupt delivery. We did not establish one per-packet
idle-exit timeline or identify which host contributed each delay.

[Linux CPU idle documentation](https://docs.kernel.org/admin-guide/pm/cpuidle.html)
describes state selection and the meaning of the exit-latency attributes.

## The idle-policy control and its limits

A separate, same-boot three-round host block measured tunnel and bridge bypass
under both default policy and temporary **10 µs CPU latency QoS** requests on
both hypervisors. Each holder wrote a signed 32-bit value to
`/dev/cpu_dma_latency` and retained the descriptor. Linux aggregates requests by
taking the smallest limit; closing the descriptor removes that request. The
10 µs value limits eligible CPU idle-state exit latency. It is not an
application deadline, a packet latency promise, a CPU frequency setting, or
busy polling. The request is host-wide, not limited to the benchmark's CPUs.

| Fresh host block, three samples per condition | Bypass mean µs ± SD | Tunnel mean µs ± SD | Paired tunnel penalty µs ± SD |
| --- | ---: | ---: | ---: |
| Default policy | 401.263 ±211.585 | 975.760 ±34.013 | 574.497 ±214.569 |
| 10 µs constraint | 195.963 ±36.401 | 494.277 ±19.143 | 298.313 ±22.989 |

The paired penalty decreased in all three rounds: 292.95, 63.30, and 472.30 µs;
mean interaction 276.183 ±205.015 µs. Counter snapshots showed zero entries into
states advertising exit latency above 10 µs during constrained invocations.
The unconstrained effective target of 2,000,000,000 µs means there was no tight
request; it does not describe an actual wakeup delay. Each effective target was
verified as 10 during the request and restored to its original value afterward.

This is evidence that idle policy amplifies the measured tunnel penalty in this
block. It does not prove that a fixed proportion of every transaction was spent
in C6. There are only three rounds, default bypass variation is broad, counters
cover the whole invocation, and both hosts changed together. The substantial
remaining ~298 µs marginal penalty establishes that suppressing deeper idle did
not remove the tunnel-path cost. Do not pool this fresh block with the earlier
five-pair baseline. Power consumption was not measured; shallower idle generally
trades energy savings for responsiveness. No persistent policy was changed.

[Linux PM QoS documentation](https://docs.kernel.org/power/pm_qos_interface.html)
specifies the units, aggregation and descriptor lifetime. The implemented
holder and cleanup are in [`cpu_latency.py`](../experiments/cpu_latency.py).

## Investigation and optimization order

1. **Stabilize power policy.** Compare default, 50 µs and 10 µs requests with
   both tunnel and bypass, balanced order, identical monitoring, and repeated
   blocks. Include the Cilium reference under the same policy before comparing
   optimized custom latency with it. This is the most directly supported tuning
   candidate; its energy cost remains unmeasured.
2. **Test placement separately.** Coordinate NIC IRQs, vhost and vCPUs across
   suitable physical cores. Earlier placement/queue experiments improved
   throughput, not proven request/response latency. More queues can add wakeups
   and disrupt locality. Keep each intervention reversible and measure mean,
   tails, retransmissions, migrations and CPU cost.
3. **Evaluate supported Cilium host routing on custom VM VXLAN.** The baseline
   retains kube-proxy, does not request BPF masquerading, and does not explicitly
   select the optimized host-routing profile. Cilium documents eBPF host routing
   with BPF masquerading and kube-proxy replacement and says it avoids host
   namespace netfilter traversal. This is a multi-setting datapath change, not
   an iptables deletion command. A separate generated profile, agent/Pod restart
   plan, rollback and service/policy validation would be required. Its ability
   to optimize our route through `cs740vx` must be verified from actual hooks,
   routes and function probes; a Helm value alone does not establish it.
4. **Prototype explicit forwarding while retaining Linux VXLAN.** The isolated
   prototype below demonstrates this narrower mechanism before integrating
   anything with Cilium. The custom tunnel already has Cilium TCX ingress, so
   blindly attaching or replacing programs would be incorrect. Production
   integration needs a supported hook/order after required policy processing,
   or explicit cooperation with Cilium's datapath.
5. **Consider host bridge shortcuts later.** Directing TAP traffic to VXLAN and
   decoded traffic to the right TAP could remove bridge forwarding, but requires
   endpoint learning/mapping, broadcast/ARP and loop handling. It still retains
   virtio/vhost. The current prototype is an L3 VM-forwarding model, not a
   completed L2 bridge replacement.

[Cilium's tuning guide](https://docs.cilium.io/en/stable/operations/performance/tuning/)
is the supported-profile reference. Do not turn on every recommended tuning at
once or combine its resulting numbers with the original baseline. BIG TCP and
bulk batching are lower-priority explanations for a tiny sequential RR workload.
DPDK would require a substantially larger integration and is not needed to test
whether ordinary forwarding is avoidable.

## Optional TC/eBPF proof of concept

The implementation is [`vxlan_redirect.bpf.c`](../experiments/prototypes/vxlan_redirect.bpf.c),
with [`vxlan_bpf_lab.py`](../experiments/vxlan_bpf_lab.py) as the one-command runner.
It compiles locally and sends the object to a configured hypervisor over the
existing SSH connection. It requires a local clang with a BPF target and remote
`ip`, `tc`, `bpftool`, Python, and root access through existing sudo. It does not
install packages, reset the cluster or attach to a real experiment interface.

```bash
# Compile without contacting the hypervisor:
./experiments/vxlan_bpf_lab.py --host cs740-node1-jump --compile-only

# Run the disposable namespace correctness lab:
./experiments/vxlan_bpf_lab.py --host cs740-node1-jump
```

The runner shares the collection lock and creates a unique results directory.
Four randomly named network namespaces contain two endpoint veths, two routers,
and a veth underlay. VNI 740 / UDP 4789 and inner MTU 1450 model the custom tunnel.
All addresses, sysctls, routes, qdiscs and links are confined to these namespaces.
The root namespace's interfaces, routes, VM configuration and Cilium programs
are not changed.

```text
Baseline:
endpoint A -> router A IP forwarding -> Linux VXLAN -> isolated underlay
           -> Linux VXLAN -> router B IP forwarding -> endpoint B

Prototype:
endpoint A -> TC static route + MAC/TTL update -> Linux VXLAN -> same underlay
           -> Linux VXLAN -> TC static delivery + MAC/TTL update -> endpoint B
```

Each ingress program has one private array-map entry containing destination
prefix, output ifindex, source/destination MACs and MTU, plus atomic diagnostic
counters. It matches untagged, nonfragmented IPv4 without options, decrements TTL,
updates the IPv4 checksum, rewrites Ethernet addresses and uses `bpf_redirect`
to the selected device's egress. Linux still performs VXLAN encapsulation and
decapsulation. Unsupported traffic falls through to the normal stack; TTL
exhaustion is dropped. It does not build outer headers itself or bypass virtio,
the kernel, or the Linux VXLAN functions.

The correctness sequence checks baseline full-size DF ping, failure with IP
forwarding disabled, success with four BPF attachments while forwarding remains
disabled, bidirectional full-size DF ping, two routing TTL decrements, TTL
exhaustion, 1,000 TCP echo exchanges, private program/map identities and counters,
and baseline recovery after filter removal. Processes, namespaces and their
program/map references are removed in cleanup. Raw commands, verifier errors,
object/source hashes and cleanup failures are retained. SIGTERM/HUP trigger
cleanup; a remote timeout bounds ordinary failures. SIGKILL or host failure can
still require deleting the exact owned namespaces recorded in the result.

This is deliberately a **correctness prototype, not a Cilium-integrated datapath
or latency improvement claim**. Both ends run on one physical host, without
KVM or a physical link. It does not preserve Cilium identity, policy, services,
conntrack/NAT semantics or traffic accounting. It has static routing rather than
an endpoint controller, no IPv6/VLAN/fragment/options fast path, no ICMP error
synthesis, no complete validation of untrusted IP input, and no demonstrated
GSO/GRO/PMTU or multi-tenant behavior. It must not be attached to the cluster as
if it were a drop-in Cilium replacement.

The helper behavior is specified in the
[Linux 6.8 BPF UAPI](https://github.com/torvalds/linux/blob/v6.8/include/uapi/linux/bpf.h).
A subsequent integration experiment would first preserve policy/identity
semantics, then repeat matched untraced tunnel/bypass RR measurements and
separate bounded function traces under a fixed idle policy. Count skipped
`ip_forward` calls and verify correctness before attributing any latency change.

## Prototype validation result

The final lab passed on the physical hypervisor's Linux 6.8.0-149 kernel.
Its [raw report](../results/vxlan-bpf-lab-20261125T164717.298390Z/report.json)
records all four attachments with 1,007–1,009 redirects each, two intentional
TTL drops, full-size bidirectional delivery, all 1,000 TCP exchanges, and
successful recovery of ordinary forwarding after detaching the programs.
The [post-run audit](../results/vxlan-bpf-lab-20261125T164717.298390Z/post-run-audit.json)
verified no owned namespaces, programs or maps remained, the base deployment
config hash was unchanged, and host-vxlan-1450 remained the active experiment.
No packages were installed and no CPU idle settings were changed for this lab.

There were two local compiler compatibility fixes before live validation
(Nix wrapper warning handling and an ELF license-symbol name), followed by
one failed live attempt caused by a different `tc` JSON program-ID layout.
That attempt cleaned up successfully; a subsequent live run and the final
counter-asserting run passed. Each attempt has its own preserved result directory.
The complete repository suite now passes **98 unit tests**, including program-ID
parsing, map counter ABI and cleanup after partial namespace setup.

The available evidence is functional: it demonstrates a route around ordinary
IP forwarding while using Linux VXLAN. The incidental ping times are not a
controlled latency benchmark and are not incorporated into the paper results.
