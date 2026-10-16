# Troubleshooting


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

