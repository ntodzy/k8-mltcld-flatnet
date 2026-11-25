# Direct routing at MTU 1450

Cilium uses native routing with an explicit Pod MTU of 1450. The physical
underlay and guest experiment NICs remain at MTU 1500. Compare the **Pod-to-Pod**
results with native routing to isolate MTU effects, and with Cilium VXLAN to
compare forwarding at an equivalent inner MTU. Host-to-host controls still use
the guest NIC's MTU 1500.

## Reset and set up

Run from the repository root with the existing CloudLab reservation, deployment
config, and generated SSH/inventory files. This removes the current Kubernetes
workloads and cluster state, reboots the guests, reconciles the VM experiment
NICs, and builds a fresh cluster. VM disks and existing SSH credentials are
preserved. No manual uninstall or SSH-key installation is needed.

```bash
./experiments/setup.py direct-1450 --reset
```

Setup restores direct macvtap bridge-mode attachment from the base deployment
config, stops previous custom tunnels, and restores host and guest NIC MTU
1500. Any old shared VXLAN bridge is removed after its VMs are detached.

## Run

```bash
./experiments/run.py direct-1450 --client worker-1 --server worker-2 \
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
