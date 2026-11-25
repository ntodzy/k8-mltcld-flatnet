# Custom VXLAN inside each VM

Cilium uses native routing at MTU 1500. Each VM has a Linux VXLAN interface
(`cs740vx`, VNI 740, UDP 4789), and static routes send remote PodCIDRs through
that interface. Static neighbor/FDB entries identify each peer VM's VTEP.
Encapsulation CPU cost is paid inside the VMs.

## Reset and set up

Run from the repository root with the existing CloudLab reservation, deployment
config, and generated SSH/inventory files. This removes the current Kubernetes
workloads and cluster state, reboots the guests, reconciles the VM experiment
NICs, and builds a fresh cluster. VM disks and existing SSH credentials are
preserved. No manual uninstall or SSH-key installation is needed.

```bash
./experiments/setup.py vm-vxlan --reset
```

The physical experiment LAN must support MTU **1550** end to end; otherwise
the fragmentation-disabled probes fail. The setup helper configures host NIC
MTU 1550 and persists it in `cs740-experiment-underlay.service`. It does not
change the CloudLab switch configuration. Permit UDP 4789 and ICMP on the
underlay.

Guest experiment NICs and their host macvtap interfaces use MTU 1550;
setup reconciles the live macvtap devices after deployment because libvirt
rejects an explicit MTU element on direct interfaces. Newly created macvtap
interfaces inherit the parent NIC's MTU. The physical NIC setting is persistent;
rerun setup if a device is recreated with a different MTU. The custom
tunnel and Pods retain MTU 1500. Reserve `10.245.0.0/24`, route protocol 242,
and interface `cs740vx` for this experiment. The `cs740-vxlan` guest service
persists forwarding across reboots. The topology supports at most 254 VMs.

## Run

```bash
./experiments/run.py vm-vxlan --client worker-1 --server worker-2 \
  --duration 30 --samples 5 --streams 4
```

Use workers on **different physical hypervisors**. The runner checks inventory
placement and pins Cilium's server/client selectors to these VMs. It runs
Pod-to-Pod and host-to-host tests, excluding the single-VM cases by default.

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
