# CS740 networking research

Compare Cilium VXLAN, native routing, and custom VXLAN tunnels across Kubernetes
VMs on two CloudLab hypervisors. The example topology has one controller and
one worker on host 1, and two workers on host 2.

## Configure the CloudLab hosts

```bash
./experiments/configure.py
```

Enter the hostnames and experiment NICs when prompted; press Enter to keep
existing values. The wizard updates `infrastructure/config.json` and preserves
SSH keys and VM settings. No Ansible or Terraform edits are needed.
See [configuration options](docs/configuration.md) for command-line flags.

## Experiments

Each guide includes the reset, setup, and measurement commands. Run commands
from the repository root. Existing hypervisor access, guest keys, and generated
SSH jump-host configuration are reused; no SSH key setup is needed.

| Experiment | Purpose |
| --- | --- |
| [Cilium VXLAN](docs/experiments/vxlan.md) | Cilium-managed overlay baseline, Pod MTU 1450 |
| [Native routing](docs/experiments/native.md) | Direct Pod forwarding at MTU 1500 |
| [Same-host VM–VM](docs/experiments/vm-local.md) | Two VMs communicating without crossing the physical link |
| [Direct MTU 1450](docs/experiments/direct-1450.md) | Isolate reduced-MTU cost without encapsulation |
| [Per-VM VXLAN](docs/experiments/vm-vxlan.md) | Linux VXLAN inside each VM, inner MTU 1500 |
| [Hypervisor VXLAN](docs/experiments/host-vxlan.md) | Shared Linux VXLAN on each physical host, inner MTU 1500 |
| [Per-VM VXLAN MTU 1450](docs/experiments/vm-vxlan-1450.md) | Custom guest tunnel at the same inner MTU as Cilium VXLAN |
| [Hypervisor VXLAN MTU 1450](docs/experiments/host-vxlan-1450.md) | Shared host tunnel at the same inner MTU as Cilium VXLAN |

Optional [BPF forwarding experiments](docs/experiments/bpf-forwarding.md) add
separate host bridge pass-through/redirect modes and guest legacy/BPF host-routing
modes. They have individual setup guides and are excluded from the default eight.
The [host latency follow-up](docs/experiments/host-latency-optimization.md) tests
guest BPF forwarding while retaining hypervisor VXLAN, with paired idle-policy controls.
The [six-case VXLAN head-to-head](docs/experiments/vxlan-head-to-head.md) compares
Cilium over macvtap, Cilium over a bridge, and host VXLAN with baseline and
optimized profiles, including a short development smoke option.

For example:

```bash
# Removes the current cluster workloads/state, reboots guests, and rebuilds native routing.
./experiments/setup.py native --reset
./experiments/run.py native --client worker-1 --server worker-2
```

The runner uses `cilium connectivity perf` on the controller and saves JSON
reports, console output, placement/configuration, and whole-run CPU snapshots
under `results/`. See [runner options and result interpretation](docs/experiments/runner.md).

For final data collection across every mode:

```bash
./experiments/collect.py --background
```

For a comparison of the three VXLAN implementations at inner MTU 1450, see
[the matched comparison](docs/experiments/matched-vxlan.md).

See [collection options, progress, and resume](docs/experiments/collection.md).
This resets the cluster between modes and saves all results together.

## References

- [Completed study results and methodology](report.md)
- [Which result folders to use: baseline, BPF, idle and throughput](docs/results-guide.md)
- [Collated charts and measured impact of each experiment](docs/experiment-impact-report.md)
- [VXLAN packet paths and bypass options](docs/vxlan-datapath-and-bypass.md)
- [Collect CPU, queue, offload, and TCP diagnostics](docs/experiments/diagnostics.md)
- [Aggregate attempts into tables and graphs](docs/experiments/aggregation.md)
- [Host VXLAN bottleneck study and complete rerun](docs/experiments/bottleneck-study.md)
- [Paired tunnel latency isolation and kernel timing](docs/experiments/latency-isolation.md)
- [CPU idle effects and optional VXLAN BPF prototype](docs/vxlan-latency-and-optimization.md)
- [Host and guest BPF forwarding experiment comparison](docs/experiments/bpf-forwarding.md)
- [Hypervisor setup and NIC tuning](docs/hypervisors.md)
- [Infrastructure deployment and networking](docs/infrastructure.md)
- [Research abstract and schedule](docs/project-plan.md)
- [Troubleshooting](TROUBLESHOOTING.md)

The deployment controller needs Python 3.10+, Bash, OpenSSH, jq, Ansible Core,
and Terraform 1.5+ (1.7+ for mock-provider tests). The scripts assume the VMs
have already been provisioned with `infrastructure/deploy.sh apply` and that
`infrastructure/config.json` retains the original direct experiment NIC
attachments. The original custom VXLAN modes with inner MTU 1500 require a CloudLab LAN
supporting MTU 1550; the new inner-MTU-1450 modes use a 1500-byte underlay.
