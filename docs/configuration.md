# Enter CloudLab hostnames and networking settings

From the repository root:

```bash
./experiments/configure.py
```

The wizard asks for the existing CloudLab SSH username and, for each configured
hypervisor, its hostname and physical experiment NIC. Press Enter to keep a
value. On the current machines the experiment NIC is `eno1d1`; `eno1` is the
management NIC. Use the experiment NIC for this setting.

The script updates `infrastructure/config.json`. If it does not exist, the
script starts with `config.example.json`, including the example VM topology.
Existing SSH key paths, MAC addresses, VM sizes/placement, and other settings
are preserved. It never generates or installs keys and does not contact hosts.
There is no need to edit Ansible or Terraform.

The previous JSON is saved as `infrastructure/config.json.bak`. Both files are
ignored by Git and written with private permissions. A custom destination can
be selected with `--config PATH` (ignore custom files in Git separately).

## Command-line configuration

For a new reservation, update the endpoints directly:

```bash
./experiments/configure.py \
  --host node1=ms1234.utah.cloudlab.us \
  --host node2=ms1235.utah.cloudlab.us \
  --interface node1=eno1d1 \
  --interface node2=eno1d1
```

Only the supplied fields change. `node1`/`node2` are the existing hypervisor
names in the deployment config; these flags change their endpoints rather
than creating new hypervisors or moving VMs. For a first configuration, supply
`--ssh-user YOUR_EXISTING_USER` or use the wizard to replace the example user.
The existing key paths are reused.

To change VM experiment IPs, use the wizard's `--edit-guests` option, or:

```bash
./experiments/configure.py --guest-address worker-2=10.74.0.21/24
```

Guest IPs must include a prefix and remain unique. Management network settings,
gateways, and DNS values are preserved; the example experiment LAN uses no
gateway. `--interactive` prompts even when flags were supplied, showing those
flag values as defaults. `--dry-run` prints the proposed host settings without
writing files; by itself it previews the current configuration without prompts.

If a host has a custom `libvirt_uri` pointing at the old endpoint, update it
with `--libvirt-uri NAME=URI` in the same command. The script rejects that
stale endpoint instead of silently retaining it.

## Apply the saved configuration

```bash
infrastructure/deploy.sh apply
```

This runs the existing Ansible/Terraform pipeline and refreshes the generated
SSH aliases and guest inventory for the new endpoints. Saving JSON alone does
not update those generated files or change the running infrastructure. After
deployment, use the appropriate [experiment guide](../README.md) to reset,
configure, and run the cluster experiment.

The separate `experiments/hypervisors.py` script is for optional QEMU/KVM
preparation and host NIC tuning; it is not needed to enter hostnames.
