# Work handoff

Recorded November 24, 2026. Read `AGENTS.md` and `README.md` before continuing.

## Research reference

The Cilium/custom VXLAN packet paths, TC/eBPF prototype proposal, XDP and DPDK
alternatives, and scope/effort estimates are saved in
[vxlan-datapath-and-bypass.md](vxlan-datapath-and-bypass.md).
No custom TC/eBPF, XDP, or DPDK prototype has been implemented.

## Existing experiment tooling

- `experiments/configure.py`: interactive hostnames/NIC settings, preserving
  existing key paths and VM settings in the local base config.
- `experiments/setup.py MODE --reset`: mode-specific infrastructure and fresh
  cluster setup. Direct/macvtap MTU must not be specified in libvirt XML.
- `experiments/run.py`: pinned Cilium CLI performance measurements. `--pod-only`
  excludes host-network controls for matched Pod workloads.
- `experiments/collect.py --background`: all eight modes, logs, raw reports,
  metrics CSV, consistent endpoint pairs, shared lock, and resumable attempts.
- `experiments/status.py --watch`: latest comparison/collection progress; exits
  with `DONE` or a failure status. Ctrl+C stops only the watcher.

The two new custom profiles, `vm-vxlan-1450` and `host-vxlan-1450`, match Cilium
VXLAN's inner MTU 1450 and physical MTU 1500. They passed local Python/template
checks but have not been measured live. Use the command in
[matched-vxlan.md](experiments/matched-vxlan.md) after the active job finishes.

## Active matched comparison

A previously authorized native-1450 versus Cilium VXLAN comparison is running
in the background using worker-1 to worker-2, five 30-second samples per test,
and four streams. Its directory is:

```text
results/matched-vxlan-native1450-20261125T005229Z/
```

At the last status check, native-1450 had completed successfully and the VXLAN
performance measurement was running after the cluster rebuilt successfully.
All four Kubernetes nodes were Ready. This is a status snapshot, not a claim
that the background job has now completed.

Inspect `comparison.json`, `collection.log`, and `pid` in that directory, or:

```bash
./experiments/status.py --watch
```

The temporary background wrapper is `/tmp/cs740-matched-comparison.py`, and
`/tmp/cs740-active-comparison` contains the output directory. The wrapper holds
`infrastructure/generated/collection.lock`; do not launch a conflicting
collection/reset. Retain successful native data if another phase fails.
`run.py` refuses existing output directories, so a retried failed measurement
must use a new output directory rather than overwrite its partial artifacts.

## Reset and rebuild fixes

The comparison exposed two lifecycle bugs, now fixed:

1. Reset recursively deleted `/run/cilium`, traversing mounted cgroupfs and
   failing on `io.prio.class`. Reset now leaves runtime mounts to reboot.
2. Reset removed `/etc/kubernetes`, while installed packages did not recreate
   it. The shared Kubernetes node role now explicitly creates that directory
   for both controller and workers before kubeadm templates are written.

After the second fix, cluster apply succeeded on the live VMs and the VXLAN
measurement began. The local regression playbook
`infrastructure/tests/reset-reapply.yml` passes. The Python suite has 51 passing
tests; profile-template checks cover both custom tunnel MTUs. Use local checks
appropriate to subsequent changes, and distinguish them from live validation.

## Working tree

There are uncommitted changes from the ongoing session, including the suite
collector/status tools, SSH policy fix, MTU-matched modes, reset/reapply fixes,
and documentation. Preserve them; no commit was requested. Local deployment
config, generated SSH/inventory files, Terraform state, and results are ignored
and must remain untracked. SSH keys are already configured; reuse them.
