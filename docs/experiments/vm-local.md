# Same-host VM–VM baseline

Two distinct worker VMs communicate through their experiment IPs on the same
hypervisor. The cluster uses native routing at MTU 1500, but the measurement
uses only Cilium's **host-to-host** performance cases. This bypasses the Pod
datapath and measures the local VM–VM path.

## Reset and set up

Run from the repository root with the existing CloudLab reservation, deployment
config, and generated SSH/inventory files. This removes the current Kubernetes
workloads and cluster state, reboots the guests, reconciles the VM experiment
NICs, and builds a fresh cluster. VM disks and existing SSH credentials are
preserved. No manual uninstall or SSH-key installation is needed.

```bash
./experiments/setup.py vm-local --reset
```

Setup restores direct macvtap bridge-mode attachment from the base deployment
config, stops previous custom tunnels, and restores host and guest NIC MTU
1500. Any old shared VXLAN bridge is removed after its VMs are detached.

## Run

```bash
./experiments/run.py vm-local --client worker-2 --server worker-3 \
  --duration 30 --samples 5 --streams 4
```

Both workers must have the same `hypervisor` in the generated inventory. The
runner rejects identical VMs or VMs on different hypervisors. It passes
`--same-node=false --other-node=true --host-net=true --pod-net=false` to Cilium:
**other-node means another VM**, even when the physical host is the same.

Verify the local path on the hypervisor hosting both workers. In a second
terminal, run this capture while the experiment runs (example placement: node2):

```bash
ssh -F infrastructure/generated/ssh_config cs740-node2-jump \
  'sudo tcpdump -ni eno1d1 "ip and host 10.74.0.21 and host 10.74.0.22"'
```

Use the real interface and worker experiment IPs from your config. The benchmark
unicast flow should not appear on the physical NIC. Broadcast/ARP traffic is
separate from the measured unicast path. Placement checks alone cannot prove
that an externally configured network keeps the flow off the physical link.

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
