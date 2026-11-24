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

### Kubernetes and Cilium networking

After the VMs have been created, build the Kubernetes layer from the generated
guest inventory:

```bash
# This is entirely local and safe to run without an active CloudLab reservation.
infrastructure/cluster.sh validate

# This contacts the guests through their hypervisor jump hosts.
infrastructure/cluster.sh apply
```

The cluster consists of the single VM whose role is `controller` and every VM
whose role is `worker`. The example therefore creates one control-plane node
and three workers across the two physical hosts. All Kubernetes components use
the static `experiment_ipv4`; the DHCP management addresses are used only for
SSH through the hypervisors.

The versions and cluster CIDRs are pinned in
[`infrastructure/kubernetes/cluster-config.yml`](infrastructure/kubernetes/cluster-config.yml).
The playbook installs containerd and kubeadm, initializes the control plane,
joins the workers with short-lived bootstrap credentials, and installs Cilium.
Kubernetes allocates a `/24` PodCIDR to each node from `10.244.0.0/16`, and
Cilium always binds the datapath to the `experiment` interface. Kube-proxy
remains enabled in both profiles so the comparison changes Pod forwarding
without also changing Service handling.

VXLAN remains the default and can also be selected explicitly:

```bash
infrastructure/cluster.sh --network-mode vxlan apply
```

For the flat-IP experiment, select Cilium native routing (`flat` is accepted as
an alias for `native`):

```bash
infrastructure/cluster.sh --network-mode native apply
```

The native profile sets `ipv4NativeRoutingCIDR` to `10.244.0.0/16` and enables
`autoDirectNodeRoutes`. Cilium passes inter-node Pod traffic to the Linux
routing stack and installs a route for each remote PodCIDR through the owning
node's `experiment_ipv4`. Traffic within the Pod CIDR is not source-NATed, so
the flat Pod addresses remain visible end to end. This relies on the experiment
interfaces sharing one layer-2 segment, as they do in the designed CloudLab
topology. Traffic outside the native Pod CIDR retains Cilium's normal egress
masquerading in both profiles.

The experiment LAN must permit node-to-node Kubernetes API traffic (TCP 6443),
Cilium health checks (TCP 4240 and ICMP), and the normal kubeadm control-plane
traffic. The VXLAN profile additionally requires UDP 8472. The native profile
requires the LAN to forward the allocated Pod CIDRs through the node addresses.
The playbook does not manage host firewall rules; it assumes that the
experiment LAN is unfiltered or that these flows are allowed out of band.

The deployment is intentionally separate from `deploy.sh`: a new reservation
can update the hypervisor endpoints in `config.json`, recreate the same VM
topology, and then run `cluster.sh apply` without changing cluster automation.

### Additional experiment modes

| Mode | Forwarding | Inner MTU | Encapsulation location |
| --- | --- | --- | --- |
| `vm-local` benchmark | Direct VM IPs on one hypervisor | Guest experiment MTU | None |
| `direct-1450` | Cilium native routing | 1450 | None; MTU control for the VXLAN baseline |
| `vm-vxlan` | Cilium native routing over custom Linux VXLAN | 1500 | One VTEP per VM |
| `host-vxlan` | Cilium native routing over a shared VXLAN LAN | 1500 | One VTEP per hypervisor |

The existing `vxlan`, `native`, and `flat` selections remain available. Select
these cluster profiles with `cluster.sh --network-mode MODE apply`; replace
`apply` with `validate` for local syntax validation. The two custom VXLAN
variants use VNI 740 and UDP 4789, distinct from Cilium's UDP 8472 baseline.

#### Same-host VM baseline

Run the benchmark against two different VMs with the same inventory
`hypervisor`, using their experiment addresses directly:

```bash
export ANSIBLE_CONFIG="$PWD/infrastructure/ansible/ansible.cfg"
ansible-playbook -i infrastructure/generated/guest_inventory.json \
  infrastructure/ansible/playbooks/vm-local.yml \
  -e source_vm=worker-2 -e destination_vm=worker-3 \
  -e duration=30 -e output_file="$PWD/results-vm-local.json"
```

The playbook installs iperf3/ping on the two guests, checks placement and the
client route, and saves ping, iperf3 JSON (including endpoint CPU utilization),
and placement metadata on the deployment controller. It uses TCP 17400 and a
bounded, one-client server. Choose an unused output filename for each repeat.
On the supplied macvtap **bridge** configuration or a common Linux bridge,
local unicast stays on the hypervisor. Confirm this with a capture of the test
flow on the physical experiment NIC; inventory placement and route checks
alone cannot prove that an externally configured bridge never sends frames
onto the wire. Use this VM-IP benchmark separately from Pod-IP measurements.

#### Reduced-MTU direct control

```bash
infrastructure/cluster.sh --network-mode direct-1450 apply
```

This uses native routing with Cilium's `MTU: 1450` to match the inner MTU of
VXLAN over a 1500-byte underlay. It does not add a tunnel or change the VM NIC
MTU. Recreate benchmark Pods after changing MTU so their existing interfaces
do not retain the previous setting. Explicitly selecting `native` restores
Cilium MTU autodetection.

#### Custom VXLAN in each VM

Before applying this mode, configure the complete physical/virtual underlay
path, including guest `experiment` NICs, to MTU **at least 1550**. Keep those
settings persistent in the host and guest network configuration. The playbook
checks guest interface MTU and sends 1550-byte IPv4 probes with fragmentation
disabled between every pair; it does not raise physical NIC MTUs itself.

```bash
infrastructure/cluster.sh --network-mode vm-vxlan apply
```

A persistent `cs740-vxlan` service creates `cs740vx` in every guest. Static
FDB and neighbor entries map each peer to its experiment address; explicit
routes carry remote Kubernetes PodCIDRs over that tunnel. Cilium runs native
routing at MTU 1500 with automatic direct-node routes disabled. The experiment
reserves `10.245.0.0/24`, interface `cs740vx`, and route protocol 242; ensure
these do not overlap existing networks or routing automation. At most 254 VMs
are supported. Reapply after changing node membership or allocated PodCIDRs.
Selecting another cluster mode stops/disables the VM service and removes its
interface and routes. Recreate benchmark Pods when changing profiles.

#### Shared VXLAN on the hypervisors

Use a separate underlay subnet (the example uses `10.75.0.0/24`) with persistent
addresses and MTU >=1550 on each physical experiment NIC. The guest experiment
subnet remains `10.74.0.0/24`. Copy
[`host-vxlan.inventory.example.yml`](infrastructure/experiments/host-vxlan.inventory.example.yml)
and replace SSH settings, underlay interfaces, and VTEP addresses, then run:

```bash
export ANSIBLE_CONFIG="$PWD/infrastructure/ansible/ansible.cfg"
ansible-playbook -i /path/to/host-vxlan.inventory.yml \
  infrastructure/ansible/playbooks/host-vxlan.yml
```

This checks interface and peer-path MTU, then creates persistent `cs740br` and
`cs740hvx` devices. Physical interfaces remain outside the bridge. In the
deployment JSON, set **each** hypervisor's `network` to
`{"mode": "bridge", "source": "cs740br"}`, then deploy the VMs and cluster:

```bash
infrastructure/deploy.sh --config infrastructure/config.json apply
infrastructure/cluster.sh --network-mode host-vxlan apply
```

The guest experiment NICs and Cilium use MTU 1500. Node traffic, including
native-routed Pod traffic, crosses the shared host tunnel; encapsulation CPU
cost is paid by the hypervisor. Unlike the VM variant, all guest experiment
traffic between hypervisors uses the overlay. Measure guest **and hypervisor**
CPU, and keep placement, stream counts, duration, offloads, and inner MTU the
same when comparing the variants. Permit UDP 4789 and the MTU probes on the
underlay. The playbooks assume an unfiltered experiment network.

Changing `cluster.sh` mode does not move VM NICs off `cs740br`. To return to a
physical direct-network experiment, restore the deployment JSON's direct
attachment and reconcile the VMs first. To remove shared VXLAN after detaching
all guests, disable `cs740-host-vxlan` on each host and delete `cs740hvx` and
`cs740br`; the shared bridge is deliberately not deleted during cluster-mode
switches. Reapply its host playbook after changing VTEP addresses or peers;
recreate `cs740hvx` first if its local address or underlay device changes.

Cilium's [Helm reference](https://docs.cilium.io/en/stable/helm-reference/)
documents the MTU override and native-routing controls used by these profiles.

Run the local profile regression checks without contacting any CloudLab host:

```bash
ANSIBLE_CONFIG="$PWD/infrastructure/ansible/ansible.cfg" \
ansible-playbook -i infrastructure/kubernetes/inventory.example.json \
  infrastructure/tests/network-profiles.yml
```
