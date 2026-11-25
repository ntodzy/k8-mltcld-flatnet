# Experiment runner

For all modes in one command, use [final data collection](collection.md).

Run from the repository root. The scripts reuse
`infrastructure/generated/ssh_config` and `guest_inventory.json`; the Cilium
CLI and kubeconfig already installed on the controller execute the tests.
The deployment controller does not need a local Cilium CLI or kubeconfig.

## Prepare

```bash
./experiments/setup.py native --reset
```

`--reset` runs `kubeadm reset`, removes persistent CNI/cluster state and guest
custom-tunnel configuration, and reboots every guest. Existing VMs, disks,
management networking, and SSH keys remain in place. Current Kubernetes
workloads and cluster data are removed.

Setup prepares physical interface MTUs, creates shared VXLAN when selected,
reconciles VM experiment NIC attachments through Terraform, sets live host
macvtap MTUs for direct attachments, persists guest MTUs,
and reinstalls Kubernetes/Cilium. It writes an isolated deployment config to
`infrastructure/generated/experiment_config.json`; your base
`infrastructure/config.json` is unchanged. Its networks must describe the
original **direct** attachments to dedicated physical experiment interfaces,
not a previous experiment's `cs740br`. Never point these at management NICs.

The supplied base config's key paths continue to be used by deployment. No key
is generated or replaced. Keep the generated files and base config available.
For a different base config use `--config PATH`. Custom host VXLAN also accepts
`--underlay-subnet CIDR` (default `10.75.0.0/24`). Use an unused, reasonably sized
IPv4 subnet that does not overlap existing host or VM networks.

To preview setup without changes or SSH:

```bash
./experiments/setup.py native --reset --dry-run
```

Omit `--reset` only when the guest cluster has already been reset. Setup checks
for existing cluster membership before changing networking in that case.
It auto-approves the VM reconciliation described by the generated config.
Physical switches/firewalls remain externally configured. The original custom VXLAN modes need MTU 1550 end to end. Their new
`*-vxlan-1450` variants use inner MTU 1450 and physical MTU 1500.

## Measure

```bash
./experiments/run.py native --client worker-1 --server worker-2 \
  --duration 30 --samples 5 --streams 4
```

The runner validates placement, successful setup mode, node readiness, and live
Cilium routing/MTU configuration. It pins both endpoints with hostname
selectors, creates a unique test namespace, and requests TCP request/response,
single-stream throughput, and multistream throughput. `vm-local` measures only
host-network traffic between distinct VMs on one physical host. Other profiles
measure both Pod and host networks across physical hosts.

`--duration` applies to **each test/sample**, not the whole run. With defaults,
three test types × two network cases × five samples × 30 seconds take roughly
15 minutes plus image pulls/setup. `vm-local` has one network case and takes
roughly half as long. Run one experiment at a time; a controller-side lock
prevents overlapping performance commands from this runner.

Additional options:

- `--diagnostics`: isolate each sample, network path, and test type and monitor
  endpoint VMs/hypervisors. See [diagnostic collection](diagnostics.md).
- `--test-type all|rr|single|multi`, `--network both|pod|host`: select workloads.
- `--profile`: optional system-wide perf capture in a separate diagnostic run;
  requires `--diagnostics` and existing usable perf on each monitored machine.
- `--pod-only`: exclude host-network controls for matching Pod workloads
  across tunnel implementations; unavailable for the `vm-local` host baseline.
- `--same-node`: also measure clients on the server VM. Cilium's “same-node”
  means the same Kubernetes VM, not two VMs sharing a hypervisor.
- `--output PATH`: use a new directory instead of the timestamped default.
  Existing directories are rejected to preserve earlier results.
- `--dry-run`: check inventory placement and print the SSH/Cilium command
  without contacting the cluster or creating output files.

## Results

Each `results/MODE-UTC_TIMESTAMP/` directory contains:

| File | Contents |
| --- | --- |
| `reports/` | Cilium's original JSON performance reports |
| `console.log` | Complete Cilium performance output and summary tables |
| `metadata.json` | Mode, endpoint placement, parameters, command, status, timestamps |
| `cilium-config.json` | Live Cilium configuration |
| `cilium-version.txt`, `cilium-status.txt` | Cilium version and health |
| `nodes-before.json`, `test-pods.json` | Kubernetes node state and actual test Pod placement |
| `*-links.json`, `*-routes.json` | Guest/hypervisor interfaces, MTUs, and routes |
| `cpu-whole-run.json` | Guest and hypervisor aggregate CPU deltas from `/proc/stat` |
| `reports.tar.gz` | Archive of the original reports |

TCP request/response results include latency distributions and transaction
rates; stream tests report throughput. These are Cilium CLI network performance
metrics, not Prometheus counters. CPU samples cover the entire test invocation,
including setup and all test cases; they are average whole-machine busy
percentages, not isolated encapsulation CPU or per-test measurements. Keep
other workloads quiet and compare matching placement, MTU, samples, and streams.
For approximate per-test attribution, use the opt-in diagnostic mode. It records
an observed workload window and its alignment limits; heavy profiling remains
a separate diagnostic run.

The runner removes its own namespace and remote report directory after saving
results. On failure it keeps available local logs and failure metadata. If report collection
fails, it also leaves the remote report directory identified in `metadata.json`. Do not use a
failed run as a valid measurement.

Official flag reference:
[Cilium connectivity perf](https://docs.cilium.io/en/latest/cmdref/cilium_connectivity_perf/).
The runner's flags were checked against the pinned
[Cilium CLI v0.20.1 source](https://github.com/cilium/cilium-cli/blob/v0.20.1/vendor/github.com/cilium/cilium/cilium-cli/cli/connectivity.go).

## Local checks

```bash
python3 -m unittest discover -s experiments/tests -v
infrastructure/cluster.sh validate
ANSIBLE_CONFIG="$PWD/infrastructure/ansible/ansible.cfg" \
  ansible-playbook -i infrastructure/kubernetes/inventory.example.json \
  infrastructure/tests/network-profiles.yml
```
