locals {
  experiment_interfaces = {
    for name, vm in var.vms : name => merge(
      {
        type  = var.network.mode
        model = { type = "virtio" }
        mac   = { address = lower(vm.mac_address) }
      },
      var.network.mode == "direct" ? {
        source = { direct = { dev = var.network.source, mode = "bridge" } }
        } : var.network.mode == "bridge" ? {
        source = { bridge = { bridge = var.network.source } }
        } : {
        source = { network = { network = var.network.source } }
      }
    )
  }

  network_configs = {
    for name, vm in var.vms : name => {
      version = 2
      ethernets = {
        management = {
          match    = { macaddress = lower(vm.management_mac_address) }
          set-name = "management"
          dhcp4    = true
          dhcp6    = false
        }
        experiment = merge(
          {
            match    = { macaddress = lower(vm.mac_address) }
            set-name = "experiment"
            dhcp4    = vm.ipv4_address == null
            dhcp6    = false
          },
          vm.ipv4_address != null ? {
            addresses = [vm.ipv4_address]
          } : {},
          vm.gateway_ipv4 != null ? {
            routes = [{ to = "0.0.0.0/0", via = vm.gateway_ipv4 }]
          } : {},
          length(vm.dns_servers) > 0 ? {
            nameservers = { addresses = vm.dns_servers }
          } : {}
        )
      }
    }
  }
}

resource "libvirt_volume" "system" {
  for_each = var.vms

  name     = "${var.project_name}-${each.key}.${var.image.format}"
  pool     = var.storage_pool
  capacity = each.value.disk_gib * 1024 * 1024 * 1024

  target = {
    format = { type = var.image.format }
  }

  create = {
    content = { url = var.image.url }
  }
}

resource "libvirt_cloudinit_disk" "seed" {
  for_each = var.vms

  name = "${var.project_name}-${each.key}-seed"

  meta_data = yamlencode({
    instance-id    = "${var.project_name}-${each.key}"
    local-hostname = each.key
  })

  user_data = "#cloud-config\n${yamlencode({
    hostname         = each.key
    manage_etc_hosts = true
    package_update   = true
    packages         = ["qemu-guest-agent"]
    users = [
      {
        name                = var.vm_user
        groups              = ["adm", "sudo"]
        lock_passwd         = true
        shell               = "/bin/bash"
        sudo                = "ALL=(ALL) NOPASSWD:ALL"
        ssh_authorized_keys = [trimspace(var.ssh_public_key)]
      }
    ]
    ssh_pwauth = false
    write_files = [{
      path        = "/etc/cs740-role"
      owner       = "root:root"
      permissions = "0644"
      content     = "${each.value.role}\n"
    }]
    runcmd = [["systemctl", "enable", "--now", "qemu-guest-agent"]]
  })}"

  network_config = yamlencode(local.network_configs[each.key])
}

resource "libvirt_volume" "seed" {
  for_each = var.vms

  name = "${var.project_name}-${each.key}-seed.iso"
  pool = var.storage_pool

  create = {
    content = { url = libvirt_cloudinit_disk.seed[each.key].path }
  }
}

resource "libvirt_domain" "vm" {
  for_each = var.vms

  name        = each.key
  type        = "kvm"
  memory      = each.value.memory_mib
  memory_unit = "MiB"
  vcpu        = each.value.vcpu
  running     = true

  # The remote libvirt provider uploads disks outside libvirt's normal
  # AppArmor profile-generation path on Ubuntu. Disable only the AppArmor
  # security driver for these domains; libvirt's DAC ownership remains active.
  sec_label = var.disable_apparmor ? [{
    type  = "none"
    model = "apparmor"
  }] : []

  cpu = {
    mode = "host-passthrough"
  }

  os = {
    type         = "hvm"
    type_arch    = "x86_64"
    type_machine = "q35"
  }

  features = {
    acpi = true
    apic = {}
  }

  devices = {
    disks = [
      {
        source = {
          volume = {
            pool   = libvirt_volume.system[each.key].pool
            volume = libvirt_volume.system[each.key].name
          }
        }
        target = { dev = "vda", bus = "virtio" }
        driver = { type = var.image.format }
      },
      {
        device = "cdrom"
        source = {
          volume = {
            pool   = libvirt_volume.seed[each.key].pool
            volume = libvirt_volume.seed[each.key].name
          }
        }
        target = { dev = "sda", bus = "sata" }
      }
    ]

    interfaces = [
      {
        type  = "network"
        model = { type = "virtio" }
        mac   = { address = lower(each.value.management_mac_address) }
        source = {
          network = { network = var.management_network.source }
        }
        wait_for_ip = {
          source  = "lease"
          network = var.management_network.cidr
          timeout = 300
        }
      },
      local.experiment_interfaces[each.key]
    ]

    channels = [{
      source = {
        unix = { mode = "bind" }
      }
      target = {
        virt_io = { name = "org.qemu.guest_agent.0" }
      }
    }]

    consoles = [{
      type = "pty"
      target = {
        type = "serial"
        port = 0
      }
    }]
  }
}

data "libvirt_domain_interface_addresses" "vm" {
  for_each = var.vms

  domain = libvirt_domain.vm[each.key].name
  source = "lease"
}
