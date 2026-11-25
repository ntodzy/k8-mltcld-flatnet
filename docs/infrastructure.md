# Infrastructure reference

Run commands from the repository root. Existing SSH keys can be reused.

Two physical CloudLab hosts are provisioned separately. The supplied example
places one Kubernetes controller and one worker on the first host, and two
workers on the second host.

### Prerequisites

The deployment controller needs Bash, `jq`, Ansible Core, Terraform 1.5 or
newer, and SSH access with passwordless sudo to each physical host. The target
hosts currently need to run Debian or Ubuntu and expose CPU virtualization
extensions.

### Deploying QEMU VMs

Enter CloudLab hostnames and experiment interfaces through the configuration
wizard. It creates the local deployment JSON from the example if needed, or
updates the existing file while preserving SSH key paths and VM settings:

```bash
./experiments/configure.py
```

Use `--edit-guests` to also enter guest experiment IP addresses. See
[configuration options](configuration.md) for noninteractive flags.

The JSON file is the single input for the pipeline. Its format is documented by
[`infrastructure/config.schema.json`](../infrastructure/config.schema.json), and
relative key paths are resolved from the directory containing the input file.
The local `config.json`, Terraform state, and provider cache are ignored by Git.
For reproducible experiments, replace the example image's `current` URL with a
dated or otherwise immutable cloud-image URL.

Validate the local configuration without contacting the hypervisors:

```bash
infrastructure/deploy.sh --config infrastructure/config.json validate
```

Configure QEMU/KVM and create the guests:

```bash
infrastructure/deploy.sh --config infrastructure/config.json apply
```

Other supported operations are:

```bash
# Show the Terraform changes (the hypervisors must already have libvirt).
infrastructure/deploy.sh -c infrastructure/config.json plan

# Reconcile only the VMs, without running the host playbook.
infrastructure/deploy.sh -c infrastructure/config.json --skip-ansible apply

# Remove the VMs and their managed volumes.
infrastructure/deploy.sh -c infrastructure/config.json destroy
```

`--auto-approve` disables the deployment-level confirmation for automation.
Terraform state is kept separately for every hypervisor under
`infrastructure/terraform/state/`, allowing the same pipeline to support more
than two hosts without hard-coded provider aliases.

### Networking

All guest disks and NICs use virtio. Each VM has two NICs: a management NIC on
libvirt's NAT network and an experiment NIC on the CloudLab LAN. On the current
CloudLab hosts, `eno1` is the host-management interface, `eno1d1` is the
experiment LAN interface, and `virbr0` backs libvirt's `default` management
network. The Ansible role installs QEMU/libvirt, loads KVM and `vhost_net`,
starts libvirt, grants the SSH user access to the `kvm` and `libvirt` groups,
and creates/starts the default network and storage pool when absent.

The management NIC obtains an address from the libvirt `default` network. It is
reachable from its hypervisor, so the hypervisor acts as the SSH jump host. The
experiment NIC uses the hypervisor's `network` object, which supports:

- `direct`: macvtap bridge mode over an existing physical interface, such as a
  CloudLab experiment LAN NIC. This is the example default and avoids changing
  host network configuration. The example connects it to `eno1d1`.
- `bridge`: attach to an existing Linux bridge. The bridge must be configured
  out of band because moving a live management NIC into a bridge can sever SSH.
- `network`: attach to a named libvirt network, such as `default`. The default
  network is NATed separately on each host, so it does not by itself provide a
  shared layer-2 network between hosts.

After a successful apply, the script discovers every management DHCP address
and writes local SSH, inventory, and host-key artifacts under
`infrastructure/generated/`:

```bash
# Connect to a guest through its assigned hypervisor.
ssh -F infrastructure/generated/ssh_config control-1

# Run a playbook against all guests through their assigned hypervisors.
ansible-playbook -i infrastructure/generated/guest_inventory.json PLAYBOOK.yml
```

Guest and jump-host keys are kept in
`infrastructure/generated/known_hosts`, rather than the user's global SSH
trust store. Each guest receives a host-key alias containing its hypervisor and
VM name, which prevents identical private DHCP addresses on different libvirt
networks from colliding. A successful infrastructure apply resets this local
trust store because the managed VMs may have been recreated.

These generated files are ignored by Git. If the hypervisor SSH connection
itself requires a jump host or other advanced SSH options, set
that hypervisor's optional `libvirt_uri` to a `qemu+sshcmd://` URI that refers
to an entry in `~/.ssh/config`. Otherwise, the deploy script builds a
`qemu+ssh://` URI using the configured user, hostname, and private key.

Set a guest `ipv4_address` in CIDR form for the experiment NIC, or omit/null it
to use DHCP. The management NIC always uses DHCP. `gateway_ipv4` and
`dns_servers` apply to the experiment NIC and can be specified globally under
`defaults` and overridden per VM. Both NIC MAC addresses and VM names must be
unique.

The Terraform root manages one hypervisor per state and calls the reusable
`infrastructure/terraform/modules/qemu_vms` module for all guests on that host.
The module imports the cloud image once as a read-only backing volume and gives
each guest a separately sized copy-on-write system disk. This is required
because libvirt preserves an imported cloud image's original virtual capacity
instead of expanding it to the requested guest disk size. The Kubernetes node
role grows the root partition and filesystem to fill that provisioned disk.
Cloud-init creates the requested VM user, installs its SSH key and the QEMU
guest agent, applies the network configuration, and records the Kubernetes role
in `/etc/cs740-role`.

On Ubuntu hypervisors, `disable_apparmor` defaults to `true`. This adds an
AppArmor-only `type=none` security label to each managed domain because disks
uploaded through a remote libvirt connection may not be included in libvirt's
generated AppArmor profile. The host's AppArmor service stays enabled, and
libvirt's DAC file ownership checks remain active. Set it to `false` globally
under `defaults`, or per hypervisor, when the host's libvirt AppArmor profiles
can access the storage pool normally.


## Hypervisor setup and tuning

Use `./experiments/hypervisors.py` to prepare QEMU/KVM and apply supported
experiment-NIC offloads with the existing config and SSH credentials. Use
`--audit` for inspection or `--profile baseline` to restore saved NIC settings.
See [hypervisor instructions](hypervisors.md).

## Kubernetes

Versions and cluster CIDRs are pinned in
[`cluster-config.yml`](../infrastructure/kubernetes/cluster-config.yml).
Kubernetes assigns each VM a `/24` PodCIDR from `10.244.0.0/16`. Experiment
traffic uses the `experiment` NIC; management addresses are used for SSH.
Kube-proxy remains enabled across all profiles.

```bash
# Local syntax validation; no CloudLab access.
infrastructure/cluster.sh validate

# Reset Kubernetes/Cilium inside all guests, preserving VM disks and SSH keys.
infrastructure/cluster.sh reset
```

Reset removes cluster workloads, certificates, etcd membership, CNI configuration,
and guest tunnel state, then reboots the guests to clear kernel networking state.
It leaves the physical hypervisors and VM management networking intact.
For complete experiment reset/setup commands, use the [experiment guides](../README.md).
