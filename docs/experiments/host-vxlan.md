# Custom VXLAN shared by each hypervisor

Each hypervisor has a Linux bridge (`cs740br`) connected to a VXLAN interface
(`cs740hvx`, VNI 740, UDP 4789). Guest experiment NICs attach to the bridge.
Cilium uses native routing at MTU 1500; the hypervisor encapsulates frames
between physical hosts, sharing the tunnel across its VMs.

For the same implementation at inner MTU 1450, use [host-vxlan-1450](host-vxlan-1450.md)
and the [matched comparison](matched-vxlan.md).

## Reset and set up

Run from the repository root with the existing CloudLab reservation, deployment
config, and generated SSH/inventory files. This removes the current Kubernetes
workloads and cluster state, reboots the guests, reconciles the VM experiment
NICs, and builds a fresh cluster. VM disks and existing SSH credentials are
preserved. No manual uninstall or SSH-key installation is needed.

```bash
./experiments/setup.py host-vxlan --reset
```

The physical experiment LAN must support MTU **1550** end to end; otherwise
the fragmentation-disabled probes fail. The setup helper configures host NIC
MTU 1550 and persists it in `cs740-experiment-underlay.service`. It does not
change the CloudLab switch configuration. Permit UDP 4789 and ICMP on the
underlay.

The helper assigns host VTEPs consecutive addresses in `10.75.0.0/24`, separate
from the VMs' experiment subnet. Reserve that subnet, or select another unused
subnet with `--underlay-subnet CIDR`. The generated deployment config attaches
VMs to `cs740br`; guest NICs remain at MTU 1500. The shared host tunnel is
persisted by `cs740-host-vxlan.service`.

## Run

```bash
./experiments/run.py host-vxlan --client worker-1 --server worker-2 \
  --duration 30 --samples 5 --streams 4
```

Use workers on **different physical hypervisors**. The runner checks inventory
placement and pins Cilium's server/client selectors to these VMs. It runs
Pod-to-Pod and host-to-host tests, excluding the single-VM cases by default.

All cross-host experiment traffic, including the host-to-host control, uses
the shared overlay. In `vm-vxlan`, only remote Pod traffic uses the custom
tunnel. Compare Pod-to-Pod results at matching MTU and inspect both guest and
hypervisor CPU snapshots when interpreting overhead.

Results go to a new directory under `results/`. See [runner options](runner.md)
for the saved metrics and CPU limitations.

## Reset or switch experiments

Rerun this guide's setup command with `--reset` for a clean repeat, or run the
setup command from another [experiment guide](../../README.md). The helper
stops custom guest tunnels, reconciles NIC attachments, and rebuilds the cluster
for the selected mode. To reset only and leave the cluster unconfigured:

```bash
infrastructure/cluster.sh reset
```
