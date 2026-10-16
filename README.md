# k8-mltcld-flatnet

This project stores all of my configuration and code necessary to complete my CS740 Research Project.

## Abstract

Due to operational or resource constraints, adopters of Ku-
bernetes (k8) do not always have the ability to operate their
clusters on bare metal hardware and often rely on virtualized
solutions to enforce separation and high availability. Previous
work demonstrated that these networking abstractions can in-
troduce measurable overhead in both the networking stack by
introducing redundant packet-processing. This paper focuses
on the performance and operational differences between using
CNI-created virtual networks, or by leveraging the underlying
virtual private cloud (VPC) resources to tunnel packets be-
tween nodes. We will evaluate these alternatives using Kuber-
netes worker VMs deployed across CloudLab physical hosts,
measuring latency, throughput, CPU utilization, forwarding
state, and routing convergence while preserving equivalent
Kubernetes networking semantics.

## Plan

##### Phase 1
Oct. 18: CloudLab and VM provisioning 
- Reproducible artifacts for provisioning two CloudLab hosts, deploying KVM virtual machines, and verifying inter-VM connectivity.
- Nov. 1 Kubernetes and Cilium VXLAN Reproducible Kubernetes cluster deployment across worker VMs, with Cilium VXLAN providing inter-node Pod connectivity.
Nov. 8 Measurement infrastructure
- Automated latency, throughput, CPU utilization, and packet capture utilities,
with preliminary VXLAN baseline measurements.
Nov. 10 Midterm report
- Documented architecture, literature review, implementation progress, and
preliminary experimental results.

##### Phase 2
Nov. 15 VPC-native routing prototype 
- Implementation of Cilium native routing with hypervisor-managed Pod-
prefix forwarding and verification of inter-node connectivity.
Nov. 19 Exam 
- Reduced project activity to accommodate exam preparation.
Nov. 27 Routing validation and experiments
- Validation of equivalent forwarding semantics and initial comparative performance measurements for both architectures.
Nov. 30 Presentation and contingency buffer
- Prepared project presentation, preliminary analysis, and additional time for troubleshooting.
Dec. 4 Final experimental measurements
- Completed repeated benchmarks, collected performance data, and finalized experimental configurations.
Dec. 7 Paper draft 
- Complete draft containing methodology, experimental results, analysis, discussion, and limitations.
Dec. 9 Review and revision 
- Incorporation of feedback and final verification of figures, citations, and reproducibility artifacts.
Dec. 11 Final submission 
- Submission of the completed research paper and supporting experimental artifacts

## Infrastructure

Two physical CloudLab hosts are provisioned separately. The supplied example
places one Kubernetes controller and one worker on the first host, and two
workers on the second host.

### Prerequisites

The deployment controller needs Bash, `jq`, Ansible Core, Terraform 1.5 or
newer, and SSH access with passwordless sudo to each physical host. The target
hosts currently need to run Debian or Ubuntu and expose CPU virtualization
extensions.

### Deploying QEMU VMs

Copy the example deployment file and replace the CloudLab hostnames, SSH user,
key paths, parent network interfaces, and guest addresses:

```bash
cp infrastructure/config.example.json infrastructure/config.json
$EDITOR infrastructure/config.json
```

The JSON file is the single input for the pipeline. Its format is documented by
[`infrastructure/config.schema.json`](infrastructure/config.schema.json), and
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
libvirt's NAT network and an experiment NIC on the CloudLab LAN. The Ansible role installs QEMU/libvirt,
loads KVM and `vhost_net`, starts libvirt, grants the SSH user access to the
`kvm` and `libvirt` groups, and creates/starts the default network and storage
pool when absent.

The management NIC obtains an address from the libvirt `default` network. It is
reachable from its hypervisor, so the hypervisor acts as the SSH jump host. The
experiment NIC uses the hypervisor's `network` object, which supports:

- `direct`: macvtap bridge mode over an existing physical interface, such as a
  CloudLab experiment LAN NIC. This is the example default and avoids changing
  host network configuration. The example connects it to `enp130s0f0`.
- `bridge`: attach to an existing Linux bridge. The bridge must be configured
  out of band because moving a live management NIC into a bridge can sever SSH.
- `network`: attach to a named libvirt network, such as `default`. The default
  network is NATed separately on each host, so it does not by itself provide a
  shared layer-2 network between hosts.

After a successful apply, the script discovers every management DHCP address
and writes two local artifacts under `infrastructure/generated/`:

```bash
# Connect to a guest through its assigned hypervisor.
ssh -F infrastructure/generated/ssh_config control-1

# Run a playbook against all guests through their assigned hypervisors.
ansible-playbook -i infrastructure/generated/guest_inventory.json PLAYBOOK.yml
```

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
