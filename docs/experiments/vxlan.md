# Cilium VXLAN baseline

Cilium encapsulates inter-VM Pod traffic in VXLAN (UDP 8472). The physical
underlay and guest experiment NICs use MTU 1500, giving Pods an MTU of 1450.
Use this as the Cilium-managed overlay baseline.

## Reset and set up

Run from the repository root with the existing CloudLab reservation, deployment
config, and generated SSH/inventory files. This removes the current Kubernetes
workloads and cluster state, reboots the guests, reconciles the VM experiment
NICs, and builds a fresh cluster. VM disks and existing SSH credentials are
preserved. No manual uninstall or SSH-key installation is needed.

```bash
./experiments/setup.py vxlan --reset
```

Setup restores direct macvtap bridge-mode attachment from the base deployment
config, stops previous custom tunnels, and restores host and guest NIC MTU
1500. Any old shared VXLAN bridge is removed after its VMs are detached.

## Run

```bash
./experiments/run.py vxlan --client worker-1 --server worker-2 \
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
