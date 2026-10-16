variable "project_name" {
  type = string
}

variable "storage_pool" {
  type = string
}

variable "image" {
  type = object({
    url    = string
    format = optional(string, "qcow2")
  })
}

variable "vm_user" {
  type = string
}

variable "ssh_public_key" {
  type      = string
  sensitive = true
}

variable "disable_apparmor" {
  description = "Disable AppArmor labeling for these domains while leaving host-wide AppArmor enabled."
  type        = bool
}

variable "network" {
  type = object({
    mode   = string
    source = string
  })
}

variable "management_network" {
  type = object({
    source = string
    cidr   = string
  })
}

variable "vms" {
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
}
