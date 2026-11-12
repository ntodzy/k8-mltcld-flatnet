output "virtual_machines" {
  description = "Names, roles, disk layout, MAC addresses, and configured IPv4 addresses of the VMs."
  value       = module.qemu_vms.virtual_machines
}
