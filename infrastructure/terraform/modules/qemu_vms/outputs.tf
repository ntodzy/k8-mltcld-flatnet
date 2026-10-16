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
      network_mode   = var.network.mode
      network_source = var.network.source
    }
  }
}
