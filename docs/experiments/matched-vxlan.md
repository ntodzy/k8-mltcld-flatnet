# Compare VXLAN implementations at inner MTU 1450

To compare Cilium's tunnel with custom VM and hypervisor tunnels, run:

```bash
./experiments/collect.py --modes vxlan vm-vxlan-1450 host-vxlan-1450 \
  --pod-only --background
```

Wait for any existing comparison/collection to finish first; the shared lock
rejects overlapping collections. Check progress with:

```bash
./experiments/status.py --watch
```

The collector resets/rebuilds the cluster between modes, preserves SSH keys
and VM disks, and saves original reports plus `metrics.csv`. Defaults are five
samples, 30 seconds per test/sample, four streams, the same cross-hypervisor
worker pair, and one host tuning profile for the whole collection. Use
`--rounds 3` to repeat with reversed mode order on alternating rounds.

| Layer | Cilium `vxlan` | `vm-vxlan-1450` | `host-vxlan-1450` |
| --- | ---: | ---: | ---: |
| Pod IP MTU | 1450 | 1450 | 1450 |
| VXLAN inner IP MTU | 1450 | 1450 | 1450 |
| Guest experiment NIC MTU | 1500 | 1500 | 1450 |
| Physical transport MTU | 1500 | 1500 | 1500 |
| Tunnel owner | Cilium in each VM | Linux VXLAN in each VM | Linux VXLAN on each hypervisor |

The guest transport MTU differs because guest-based encapsulation must fit
1450 bytes of inner IP packet plus 50 bytes of IPv4 VXLAN/Ethernet/UDP overhead
inside the guest's outgoing 1500-byte IP packet. With hypervisor encapsulation,
the guest sends the 1450-byte packet and the physical host adds those headers.
Setting every physical/transport NIC to 1450 would not preserve a 1450-byte
inner MTU without fragmentation or reducing the inner MTU again.

`--pod-only` ensures all three measurements exercise Pod traffic. Host-network
controls would bypass guest tunnels but traverse the shared hypervisor tunnel,
so combining those controls would change the workload used for CPU averages.
The runner records the expected MTU profile and live Cilium configuration;
host audits record NIC offloads and queue settings.

Compare request/response latency and single/multistream throughput at matching
parameters. Guest and hypervisor CPU snapshots cover the whole invocation,
including setup; they are not per-test or isolated tunnel CPU attribution.
Use both CPU locations when interpreting a shift in processing from VM to host.

Equal MTU removes the packet-size difference. These remain end-to-end system
comparisons: Cilium forwarding differs from manually installed Linux routes,
and host VXLAN uses a different bridge/NIC attachment and tunnels all cross-host
experiment traffic. UDP ports also remain 8472 for Cilium and 4789 for custom
VXLAN, which can influence flow hashing. Do not attribute every measured
difference exclusively to encapsulation code or management location.

The original `vm-vxlan` and `host-vxlan` modes retain inner MTU 1500 and underlay
MTU 1550 and remain in the default eight-mode collection. `direct-1450` remains
available as a no-tunnel control with the same Pod MTU.
