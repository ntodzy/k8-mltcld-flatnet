# Hypervisor setup and NIC tuning

To **enter CloudLab hostnames and experiment interfaces**, run
`./experiments/configure.py`; see [configuration instructions](configuration.md).
The commands below provision/tune hosts using that saved configuration.

Run from the repository root using the existing deployment config and SSH keys:

```bash
./experiments/hypervisors.py
```

The script prepares QEMU/KVM/libvirt on each configured physical host, enables
`vhost_net`, and applies the `optimized` profile to each host's dedicated
experiment NIC. It reads the base `infrastructure/config.json` and reuses its
SSH user, key, and connection options. Generated guest inventories and guest
keys are not required or modified. A different config can be selected with
`--config PATH`.

The base config must use `network.mode: direct`, with `network.source` naming
the physical experiment NIC, such as `eno1d1`. The playbook refuses to tune the
default-route interface. It does not create VMs, reset the cluster, change
MTUs, rewire NICs, or change firewall rules. Experiment-specific MTU and VXLAN
setup remains in [the experiment setup helper](experiments/runner.md).

## Profiles

`optimized` enables supported receive/checksum, GRO/GSO, TCP segmentation, and
UDP tunnel segmentation offloads. Unsupported or fixed features are skipped;
mutable features that fail to enable cause the service to fail. Many may
already be enabled, so this profile is a reproducible configuration rather
than a guarantee of improved performance.

A `cs740-host-network` systemd service reapplies the settings after reboot.
Original mutable feature settings are saved privately under
`/var/lib/cs740-network/` on each host before the first change. Repeated runs
preserve the original baseline. To restore the saved NIC settings and disable
tuning at boot:

```bash
./experiments/hypervisors.py --profile baseline
```

Combined physical NIC queue counts are preserved by default. To explicitly
request a count supported by your NIC:

```bash
./experiments/hypervisors.py --queues 2
```

The script checks the hardware's combined-queue limit before changing it and
saves the previous count for baseline restoration. This adjusts the physical
NIC; virtio queue counts inside VM definitions are a separate setting. Change
queues between experiment runs because changing channels can interrupt traffic.

## Audit or preview

```bash
# Inspect current host settings without installing packages or tuning hosts.
./experiments/hypervisors.py --audit

# Print host targets and planned invocation without contacting hosts.
./experiments/hypervisors.py --dry-run
```

Audits save per-host JSON under
`infrastructure/generated/hypervisor-reports/`, including kernel, driver,
interface/MTU, offload and queue settings, bridge netfilter settings, and VM
list. If an inspection tool is absent, its failed check is recorded in the
report; audit mode does not install it. Reports are also collected after setup.

This script configures the **hypervisors**. Cilium kube-proxy replacement,
eBPF masquerading/host routing, and netkit require guest-cluster configuration;
they are not enabled by this host script. Host firewall and bridge-netfilter
rules remain intact. Keep the same host profile across modes when comparing
tunnel placement, and treat host tuning as an independent experimental variable.

The selected offloads are described in the Linux kernel's
[segmentation offload documentation](https://www.kernel.org/doc/html/latest/networking/segmentation-offloads.html).
