# Troubleshooting

## Cluster setup cannot write `/etc/kubernetes/kubeadm-init.yml`

After reset, `/etc/kubernetes` is absent even though kubeadm/kubelet packages
remain installed. An idempotent package-install task does not restore that
directory. The shared Kubernetes node role must explicitly create it before
the controller writes its init configuration and workers write their join
configurations.

After fixing directory creation, retry cluster installation without another
reset or VM deployment:

```bash
infrastructure/cluster.sh --network-mode vxlan apply
```

Use the requested mode in place of `vxlan`. The local regression test
`infrastructure/tests/reset-reapply.yml` verifies configuration writes after a
simulated reset, without reinstalling packages or contacting guests.

## SSH asks for a host key during unattended setup

`ssh_askpass: exec(): No such file or directory` followed by
`Host key verification failed` can mean SSH is trying to confirm a new host key
without an interactive terminal. It does not mean an SSH login key needs to be
generated again or that an askpass program needs to be installed.

Ansible connects to guest management IPs. Settings under `Host control-1`, for
example, do not apply when its target is an IP, even with a `HostKeyAlias`.
The deployment generator therefore sets `StrictHostKeyChecking accept-new` and
`BatchMode yes` under `Host *` and explicitly in Ansible's connection options.

Inspect the effective configuration locally, substituting the guest's actual
management IP and aliases from the generated inventory:

```bash
ssh -G -T -F infrastructure/generated/ssh_config \
  -o ProxyJump=cs740-node1-jump \
  -o HostKeyAlias=cs740-node1-control-1 192.168.122.183
```

Expect `stricthostkeychecking accept-new` and `batchmode yes`. After correcting
the local generated policy, retry the original setup command; this error occurs
before the reset tasks can run. Do not disable host-key verification or clear
the trust store to work around a prompt. A changed-key warning is a different
case and requires checking whether the intended VM/hypervisor was replaced.

See the [OpenSSH configuration reference](https://man.openbsd.org/ssh_config)
for host matching and `StrictHostKeyChecking` behavior.


## Useful Commands

`ssh -F infrastructure/generated/ssh_config <vm-name>` will ssh into a particular VM

`sudo virsh list --all` to list all VM's that are available.



## QEMU cannot open a VM disk

If a domain is defined but QEMU exits with `Could not open ... Permission
denied`, first check both Unix permissions and AppArmor denials on the affected
hypervisor:

```bash
sudo ls -l /var/lib/libvirt/images
sudo -u libvirt-qemu test -r /var/lib/libvirt/images/cs740-control-1.qcow2 \
  && echo "DAC access is OK"
sudo journalctl -k --since "10 minutes ago" | grep -i apparmor
```

The deployment defaults `disable_apparmor` to `true`, which disables only the
AppArmor label on its VM domains. Host-wide AppArmor remains enabled, and
libvirt's Unix ownership checks still apply. If the disk is not readable by
`libvirt-qemu`, repair the storage-pool ownership separately instead of relying
on the AppArmor setting.

After a failed Terraform domain creation, libvirt can retain a shut-off domain
that Terraform never recorded in state. Undefine only the failed domains before
retrying:

```bash
sudo virsh undefine control-1
sudo virsh undefine worker-1
infrastructure/deploy.sh -c infrastructure/config.json --skip-ansible apply
```

The managed disk volumes remain in Terraform state and do not need to be
deleted for this domain-only retry.

