variable "project_name" {
  description = "Prefix used for volumes and other per-project resources."
  type        = string
}

variable "libvirt_uri" {
  description = "Libvirt connection URI for the one hypervisor managed by this state."
  type        = string
}

variable "storage_pool" {
  description = "Existing libvirt storage pool on the target hypervisor."
  type        = string
  default     = "default"
}

variable "image" {
  description = "Cloud image imported into the target storage pool."
  type = object({
    url    = string
    format = optional(string, "qcow2")
  })
}

variable "vm_user" {
  description = "Administrative user created in each guest by cloud-init."
  type        = string
  default     = "ubuntu"
}

variable "ssh_public_key" {
  description = "OpenSSH public key installed in each guest."
  type        = string
  sensitive   = true
}

variable "disable_apparmor" {
  description = "Disable AppArmor labeling only for the managed VM domains."
  type        = bool
  default     = true
}

variable "network" {
  description = "Libvirt attachment: direct parent NIC, Linux bridge, or libvirt network."
  type = object({
    mode   = string
    source = string
  })

  validation {
    condition     = contains(["direct", "bridge", "network"], var.network.mode)
    error_message = "network.mode must be direct, bridge, or network."
  }
}

variable "management_network" {
  description = "NAT-backed libvirt network used for SSH access through the hypervisor."
  type = object({
    source = string
    cidr   = string
  })

  validation {
    condition     = can(cidrhost(var.management_network.cidr, 0))
    error_message = "management_network.cidr must be a valid CIDR prefix."
  }
}

variable "vms" {
  description = "VMs placed on this hypervisor, keyed by guest hostname."
  type = map(object({
    role                   = string
    vcpu                   = number
    memory_mib             = number
    disk_gib               = number
    management_mac_address = string
    mac_address            = string
    ipv4_address           = optional(string)
    gateway_ipv4           = optional(string)
    dns_servers            = optional(list(string), [])
  }))

  validation {
    condition     = alltrue([for vm in values(var.vms) : vm.vcpu >= 1 && vm.memory_mib >= 512 && vm.disk_gib >= 8])
    error_message = "Every VM needs at least 1 vCPU, 512 MiB RAM, and an 8 GiB disk."
  }

  validation {
    condition = alltrue([
      for vm in values(var.vms) :
      can(regex("^([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$", vm.mac_address)) &&
      can(regex("^([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$", vm.management_mac_address))
    ])
    error_message = "Every VM must have a valid six-octet MAC address."
  }

  validation {
    condition = alltrue([
      for vm in values(var.vms) : vm.ipv4_address == null ? true : can(cidrhost(vm.ipv4_address, 0))
    ])
    error_message = "Each ipv4_address must be null or an IPv4 address in CIDR form."
  }

  validation {
    condition = alltrue([
      for vm in values(var.vms) : vm.gateway_ipv4 == null ? true : can(cidrhost("${vm.gateway_ipv4}/32", 0))
    ])
    error_message = "Each gateway_ipv4 must be null or a valid IPv4 address without a prefix."
  }
}
