provider "libvirt" {
  uri = var.libvirt_uri
}

module "qemu_vms" {
  source = "./modules/qemu_vms"

  project_name       = var.project_name
  storage_pool       = var.storage_pool
  image              = var.image
  vm_user            = var.vm_user
  ssh_public_key     = var.ssh_public_key
  disable_apparmor   = var.disable_apparmor
  management_network = var.management_network
  network            = var.network
  vms                = var.vms
}
