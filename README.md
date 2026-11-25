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

For example:

```bash
# Removes the current cluster workloads/state, reboots guests, and rebuilds native routing.
./experiments/setup.py native --reset
./experiments/run.py native --client worker-1 --server worker-2
```

The runner uses `cilium connectivity perf` on the controller and saves JSON
reports, console output, placement/configuration, and whole-run CPU snapshots
under `results/`. See [runner options and result interpretation](docs/experiments/runner.md).

## References

- [Hypervisor setup and NIC tuning](docs/hypervisors.md)
- [Infrastructure deployment and networking](docs/infrastructure.md)
- [Research abstract and schedule](docs/project-plan.md)
- [Troubleshooting](TROUBLESHOOTING.md)

The deployment controller needs Python 3.10+, Bash, OpenSSH, jq, Ansible Core,
and Terraform 1.5+ (1.7+ for mock-provider tests). The scripts assume the VMs
have already been provisioned with `infrastructure/deploy.sh apply` and that
`infrastructure/config.json` retains the original direct experiment NIC
attachments. Custom VXLAN also requires a CloudLab LAN supporting MTU 1550.
