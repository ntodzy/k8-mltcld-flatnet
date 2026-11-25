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
    source = "eno1d1"
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

  assert {
    condition     = module.qemu_vms.virtual_machines.controller.libvirt_mtu == null
    error_message = "Direct/macvtap attachments must omit the unsupported libvirt MTU element."
  }

  assert {
    condition     = module.qemu_vms.virtual_machines.controller.disk_gib == 20
    error_message = "The configured system disk capacity must reach the VM module."
  }

  assert {
    condition     = module.qemu_vms.virtual_machines.controller.disk_volume == "test-controller-disk.qcow2"
    error_message = "Each VM must receive a separately sized copy-on-write disk."
  }

  assert {
    condition     = module.qemu_vms.virtual_machines.controller.base_volume == "test-base.qcow2"
    error_message = "VM disks must use the imported cloud image as their backing volume."
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

  assert {
    condition     = module.qemu_vms.virtual_machines.controller.libvirt_mtu == 1500
    error_message = "Bridge attachments must retain their supported libvirt MTU element."
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

run "plans_native_mtu_vxlan_underlay" {
  command = plan

  variables {
    network = {
      mode   = "direct"
      source = "eno1d1"
      mtu    = 1550
    }
  }

  assert {
    condition     = module.qemu_vms.virtual_machines.controller.network_mtu == 1550
    error_message = "The requested 1550-byte underlay MTU must remain available to experiment setup."
  }

  assert {
    condition     = module.qemu_vms.virtual_machines.controller.libvirt_mtu == null
    error_message = "Jumbo direct interfaces must also omit the unsupported libvirt MTU element."
  }
}
