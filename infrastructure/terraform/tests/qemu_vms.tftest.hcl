mock_provider "libvirt" {}

variables {
  project_name   = "test"
  libvirt_uri    = "qemu:///system"
  storage_pool   = "default"
  vm_user        = "ubuntu"
  ssh_public_key = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAITestKeyOnly"

  image = {
    url    = "https://example.invalid/ubuntu.qcow2"
    format = "qcow2"
  }

  network = {
    mode   = "direct"
    source = "enp130s0f0"
  }

  management_network = {
    source = "default"
    cidr   = "192.168.122.0/24"
  }

  vms = {
    controller = {
      role                   = "controller"
      vcpu                   = 2
      memory_mib             = 2048
      disk_gib               = 20
      management_mac_address = "52:54:00:74:10:11"
      mac_address            = "52:54:00:74:00:11"
      ipv4_address           = "10.74.0.11/24"
      gateway_ipv4           = null
      dns_servers            = ["1.1.1.1"]
    }
    worker = {
      role                   = "worker"
      vcpu                   = 2
      memory_mib             = 2048
      disk_gib               = 20
      management_mac_address = "52:54:00:74:10:12"
      mac_address            = "52:54:00:74:00:12"
      ipv4_address           = null
      gateway_ipv4           = null
      dns_servers            = []
    }
  }
}

run "plans_direct_virtio_guests" {
  command = plan

  assert {
    condition     = length(module.qemu_vms.virtual_machines) == 2
    error_message = "The module must plan both requested VMs."
  }

  assert {
    condition     = module.qemu_vms.virtual_machines.controller.network_mode == "direct"
    error_message = "The selected direct mode must reach the VM module."
  }
}

run "plans_linux_bridge_guests" {
  command = plan

  variables {
    network = {
      mode   = "bridge"
      source = "br0"
    }
  }

  assert {
    condition     = module.qemu_vms.virtual_machines.controller.network_source == "br0"
    error_message = "The selected Linux bridge must reach the VM module."
  }
}

run "plans_libvirt_network_guests" {
  command = plan

  variables {
    network = {
      mode   = "network"
      source = "default"
    }
  }

  assert {
    condition     = module.qemu_vms.virtual_machines.controller.network_mode == "network"
    error_message = "The selected libvirt network must reach the VM module."
  }
}
