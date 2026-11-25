output "virtual_machines" {
  value = {
    for name, vm in libvirt_domain.vm : name => {
      name                   = vm.name
      role                   = var.vms[name].role
      management_mac_address = lower(var.vms[name].management_mac_address)
      management_ipv4 = try(one(flatten([
        for interface in data.libvirt_domain_interface_addresses.vm[name].interfaces : [
          for address in interface.addrs : address.addr
          if lower(interface.hwaddr) == lower(var.vms[name].management_mac_address) && address.type == "ipv4"
        ]
      ])), null)
      mac_address    = lower(var.vms[name].mac_address)
      ipv4_address   = var.vms[name].ipv4_address
      disk_gib       = var.vms[name].disk_gib
      disk_volume    = libvirt_volume.system[name].name
      base_volume    = libvirt_volume.base.name
      network_mode   = var.network.mode
      network_source = var.network.source
      # Requested MTU; direct interfaces intentionally omit libvirt's MTU XML.
      network_mtu = var.network.mtu
      libvirt_mtu = try(vm.devices.interfaces[1].mtu.size, null)
    }
  }
}
