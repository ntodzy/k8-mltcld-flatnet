#!/usr/bin/env bash

set -Eeuo pipefail

readonly SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
readonly ANSIBLE_DIR="${SCRIPT_DIR}/ansible"
readonly TERRAFORM_DIR="${SCRIPT_DIR}/terraform"

config_file="${SCRIPT_DIR}/config.json"
action="apply"
skip_ansible=false
auto_approve=false
work_dir=""

usage() {
  cat <<'EOF'
Usage: ./deploy.sh [-c FILE] [--skip-ansible] [--auto-approve] [plan|apply|destroy|validate]

Reads the complete deployment from a JSON file. Relative SSH key paths are
resolved relative to that file.

Actions:
  plan       Check the Ansible playbook and create Terraform plans.
  apply      Configure hypervisors and create/update VMs (default).
  destroy    Destroy VMs without changing the hypervisor configuration.
  validate   Validate inputs, Ansible syntax, and Terraform configuration.

Options:
  -c, --config FILE   Deployment input file (default: infrastructure/config.json)
  --skip-ansible      Do not run the QEMU host playbook
  --auto-approve      Do not ask before apply or destroy
  -h, --help          Show this help
EOF
}

die() {
  printf 'error: %s\n' "$*" >&2
  exit 1
}

log() {
  printf '\n==> %s\n' "$*"
}

cleanup() {
  if [[ -n "${work_dir}" && "${work_dir}" == /tmp/cs740-deploy.* && -d "${work_dir}" ]]; then
    rm -rf -- "${work_dir}"
  fi
}
trap cleanup EXIT

while (($# > 0)); do
  case "$1" in
    -c|--config)
      (($# >= 2)) || die "$1 requires a file"
      config_file="$2"
      shift 2
      ;;
    --skip-ansible)
      skip_ansible=true
      shift
      ;;
    --auto-approve)
      auto_approve=true
      shift
      ;;
    plan|apply|destroy|validate)
      action="$1"
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      die "unknown argument: $1 (try --help)"
      ;;
  esac
done

required_commands=(jq terraform)
if [[ "${skip_ansible}" == false && "${action}" != "destroy" ]]; then
  required_commands+=(ansible-playbook)
fi
for command in "${required_commands[@]}"; do
  command -v "${command}" >/dev/null 2>&1 || die "required command not found: ${command}"
done

[[ -f "${config_file}" ]] || die "configuration file not found: ${config_file} (copy config.example.json to config.json)"
config_file="$(cd -- "$(dirname -- "${config_file}")" && pwd)/$(basename -- "${config_file}")"
config_dir="$(dirname -- "${config_file}")"

jq -e '
  def nonempty: type == "string" and length > 0;
  def integer_at_least($minimum): type == "number" and floor == . and . >= $minimum;
  def nullable_string: . == null or type == "string";
  def string_array: type == "array" and all(.[]; type == "string" and length > 0);
  def valid_mode: . == "direct" or . == "bridge" or . == "network";
  (.project_name | type == "string" and test("^[A-Za-z0-9][A-Za-z0-9_-]*$")) and
  (.ssh.user | nonempty) and
  (.ssh.private_key_file | nonempty) and
  (.ssh.public_key_file | nonempty) and
  (.ssh.common_args // "" | type == "string") and
  (.image.url | nonempty) and
  (.image.format // "qcow2" | nonempty) and
  (.defaults // {} |
    (.storage_pool // "default" | nonempty) and
    (.vm_user // "ubuntu" | nonempty) and
    (.vcpu // 2 | integer_at_least(1)) and
    (.memory_mib // 4096 | integer_at_least(512)) and
    (.disk_gib // 30 | integer_at_least(8)) and
    (.gateway_ipv4 // null | nullable_string) and
    (.dns_servers // [] | string_array)
  ) and
  (.hypervisors | type == "array" and length > 0) and
  (all(.hypervisors[];
    (.name | test("^[A-Za-z0-9][A-Za-z0-9_-]*$")) and
    (.address | nonempty) and
    (.libvirt_uri // "qemu" | nonempty) and
    (.storage_pool // "default" | nonempty) and
    (.management_network.source | nonempty) and
    (.management_network.cidr | nonempty) and
    (.network.mode | valid_mode) and
    (.network.source | nonempty) and
    (.vms | type == "array" and length > 0) and
    (all(.vms[];
      (.name | test("^[a-zA-Z0-9][a-zA-Z0-9.-]*$")) and
      (.role | nonempty) and
      (.management_mac_address | test("^([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$")) and
      (.mac_address | test("^([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$")) and
      (.vcpu // 2 | integer_at_least(1)) and
      (.memory_mib // 4096 | integer_at_least(512)) and
      (.disk_gib // 30 | integer_at_least(8)) and
      (.ipv4_address // null | nullable_string) and
      (.gateway_ipv4 // null | nullable_string) and
      (.dns_servers // [] | string_array)
    ))
  )) and
  (([.hypervisors[].name] | length) == ([.hypervisors[].name] | unique | length)) and
  (([.hypervisors[].vms[].name] | length) == ([.hypervisors[].vms[].name] | unique | length)) and
  (([.hypervisors[].vms[] | .mac_address, .management_mac_address | ascii_downcase] | length) ==
   ([.hypervisors[].vms[] | .mac_address, .management_mac_address | ascii_downcase] | unique | length))
' "${config_file}" >/dev/null || die "configuration does not satisfy the required shape or contains duplicate names/MAC addresses"

resolve_path() {
  local path="$1"
  if [[ "${path}" == /* ]]; then
    printf '%s\n' "${path}"
  elif [[ "${path}" == "~/"* ]]; then
    printf '%s/%s\n' "$(getent passwd "$(id -un)" | cut -d: -f6)" "${path:2}"
  else
    printf '%s\n' "${config_dir}/${path}"
  fi
}

private_key="$(resolve_path "$(jq -r '.ssh.private_key_file' "${config_file}")")"
public_key_file="$(resolve_path "$(jq -r '.ssh.public_key_file' "${config_file}")")"
[[ -r "${private_key}" ]] || die "SSH private key is not readable: ${private_key}"
[[ -r "${public_key_file}" ]] || die "SSH public key is not readable: ${public_key_file}"
public_key="$(<"${public_key_file}")"
if [[ "${public_key}" != ssh-*\ * && "${public_key}" != ecdsa-*\ * && "${public_key}" != sk-*\ * ]]; then
  die "SSH public key does not look like an OpenSSH public key: ${public_key_file}"
fi

work_dir="$(mktemp -d /tmp/cs740-deploy.XXXXXXXX)"
inventory_file="${work_dir}/inventory.json"
export ANSIBLE_LOCAL_TEMP="${work_dir}/ansible-local"
mkdir -p "${ANSIBLE_LOCAL_TEMP}"

jq --arg private_key "${private_key}" '
  . as $root |
  {
    all: {
      children: {
        hypervisors: {
          hosts: (reduce .hypervisors[] as $host ({};
            .[$host.name] = {
              ansible_host: $host.address,
              ansible_user: $root.ssh.user,
              ansible_ssh_private_key_file: $private_key,
              ansible_ssh_common_args: ($root.ssh.common_args // "-o StrictHostKeyChecking=accept-new"),
              qemu_admin_users: [$root.ssh.user]
            }
          ))
        }
      }
    }
  }
' "${config_file}" >"${inventory_file}"

export ANSIBLE_CONFIG="${ANSIBLE_DIR}/ansible.cfg"

if [[ "${skip_ansible}" == false ]] && [[ "${action}" == "validate" || "${action}" == "plan" ]]; then
  log "Validating Ansible syntax"
  ansible-playbook -i "${inventory_file}" "${ANSIBLE_DIR}/playbooks/install-qemu.yml" --syntax-check
fi

if [[ "${action}" == "apply" || "${action}" == "destroy" ]] && [[ "${auto_approve}" == false ]]; then
  read -r -p "Type 'yes' to ${action} the QEMU deployment: " confirmation
  [[ "${confirmation}" == "yes" ]] || die "operation cancelled"
fi

if [[ "${skip_ansible}" == false && "${action}" == "apply" ]]; then
  log "Configuring QEMU/KVM hypervisors with Ansible"
  ansible-playbook -i "${inventory_file}" "${ANSIBLE_DIR}/playbooks/install-qemu.yml"
fi

mkdir -p "${TERRAFORM_DIR}/state" "${TERRAFORM_DIR}/.terraform/plugin-cache"
export TF_PLUGIN_CACHE_DIR="${TERRAFORM_DIR}/.terraform/plugin-cache"
host_count="$(jq '.hypervisors | length' "${config_file}")"
guest_records="${work_dir}/guest-records.jsonl"
: >"${guest_records}"

for ((host_index = 0; host_index < host_count; host_index++)); do
  host_name="$(jq -r ".hypervisors[${host_index}].name" "${config_file}")"
  host_address="$(jq -r ".hypervisors[${host_index}].address" "${config_file}")"
  ssh_user="$(jq -r '.ssh.user' "${config_file}")"
  libvirt_uri="$(jq -r ".hypervisors[${host_index}].libvirt_uri // empty" "${config_file}")"
  if [[ -z "${libvirt_uri}" ]]; then
    encoded_key="$(jq -rn --arg key "${private_key}" '$key | @uri')"
    libvirt_uri="qemu+ssh://${ssh_user}@${host_address}/system?keyfile=${encoded_key}"
  fi
  tfvars_file="${work_dir}/${host_name}.tfvars.json"
  plan_file="${work_dir}/${host_name}.tfplan"
  state_file="${TERRAFORM_DIR}/state/${host_name}.tfstate"
  export TF_DATA_DIR="${TERRAFORM_DIR}/.terraform/${host_name}"

  jq --argjson host_index "${host_index}" \
     --arg public_key "${public_key}" \
     --arg libvirt_uri "${libvirt_uri}" '
    . as $root | .hypervisors[$host_index] as $host |
    {
      project_name: .project_name,
      libvirt_uri: $libvirt_uri,
      storage_pool: ($host.storage_pool // .defaults.storage_pool // "default"),
      image: .image,
      vm_user: (.defaults.vm_user // "ubuntu"),
      ssh_public_key: $public_key,
      disable_apparmor: ($host.disable_apparmor // .defaults.disable_apparmor // true),
      management_network: $host.management_network,
      network: $host.network,
      vms: (reduce $host.vms[] as $vm ({};
        .[$vm.name] = {
          role: $vm.role,
          vcpu: ($vm.vcpu // $root.defaults.vcpu // 2),
          memory_mib: ($vm.memory_mib // $root.defaults.memory_mib // 4096),
          disk_gib: ($vm.disk_gib // $root.defaults.disk_gib // 30),
          management_mac_address: ($vm.management_mac_address | ascii_downcase),
          mac_address: ($vm.mac_address | ascii_downcase),
          ipv4_address: ($vm.ipv4_address // null),
          gateway_ipv4: ($vm.gateway_ipv4 // $root.defaults.gateway_ipv4 // null),
          dns_servers: ($vm.dns_servers // $root.defaults.dns_servers // [])
        }
      ))
    }
  ' "${config_file}" >"${tfvars_file}"

  log "Initializing Terraform for ${host_name} (${host_address})"
  terraform -chdir="${TERRAFORM_DIR}" init -input=false -reconfigure \
    -backend-config="path=${state_file}"

  if [[ "${action}" == "validate" ]]; then
    terraform -chdir="${TERRAFORM_DIR}" validate
    continue
  fi

  if [[ "${action}" == "destroy" ]]; then
    log "Destroying QEMU VMs on ${host_name}"
    terraform -chdir="${TERRAFORM_DIR}" destroy -input=false -auto-approve \
      -var-file="${tfvars_file}"
    continue
  fi

  log "Planning QEMU VMs on ${host_name}"
  terraform -chdir="${TERRAFORM_DIR}" plan -input=false \
    -var-file="${tfvars_file}" -out="${plan_file}"

  if [[ "${action}" == "apply" ]]; then
    log "Applying QEMU VMs on ${host_name}"
    terraform -chdir="${TERRAFORM_DIR}" apply -input=false -auto-approve "${plan_file}"

    vm_output="$(terraform -chdir="${TERRAFORM_DIR}" output -json virtual_machines)"
    jq -e 'all(.[]; .management_ipv4 != null and .management_ipv4 != "")' <<<"${vm_output}" >/dev/null ||
      die "Terraform did not discover a management IPv4 address for every VM on ${host_name}"
    jq -c \
      --arg hypervisor "${host_name}" \
      --arg hypervisor_address "${host_address}" \
      --arg hypervisor_user "${ssh_user}" \
      --arg vm_user "$(jq -r '.defaults.vm_user // "ubuntu"' "${config_file}")" \
      '.[] + {
        hypervisor: $hypervisor,
        hypervisor_address: $hypervisor_address,
        hypervisor_user: $hypervisor_user,
        vm_user: $vm_user
      }' <<<"${vm_output}" >>"${guest_records}"
  fi
done

if [[ "${action}" == "apply" ]]; then
  generated_dir="${SCRIPT_DIR}/generated"
  guest_inventory="${generated_dir}/guest_inventory.json"
  guest_ssh_config="${generated_dir}/ssh_config"
  mkdir -p "${generated_dir}"

  jq -rs --arg key "${private_key}" '
    ([.[] | {
      key: ("cs740-" + .hypervisor + "-jump"),
      value: {
        hostname: .hypervisor_address,
        user: .hypervisor_user
      }
    }] | unique_by(.key)) as $jumps |
    ($jumps | map(
      "Host \(.key)\n  HostName \(.value.hostname)\n  User \(.value.user)\n  IdentityFile \"\($key)\"\n  StrictHostKeyChecking accept-new"
    )) +
    ([.[] | "Host \(.name)\n  HostName \(.management_ipv4)\n  User \(.vm_user)\n  IdentityFile \"\($key)\"\n  ProxyJump cs740-\(.hypervisor)-jump\n  StrictHostKeyChecking accept-new"])
    | join("\n\n") + "\n"
  ' "${guest_records}" >"${guest_ssh_config}"

  jq -s \
    --arg key "${private_key}" \
    --arg ssh_config "${guest_ssh_config}" '
    reduce .[] as $vm ({};
      .[$vm.name] = {
        ansible_host: $vm.management_ipv4,
        ansible_user: $vm.vm_user,
        ansible_ssh_private_key_file: $key,
        ansible_ssh_common_args: ("-F " + $ssh_config + " -o ProxyJump=cs740-" + $vm.hypervisor + "-jump"),
        cs740_role: $vm.role,
        experiment_ipv4: $vm.ipv4_address,
        hypervisor: $vm.hypervisor
      }
    ) |
    {all: {children: {virtual_machines: {hosts: .}}}}
  ' "${guest_records}" >"${guest_inventory}"
  chmod 0600 "${guest_inventory}" "${guest_ssh_config}"

  log "Guest SSH config: ${guest_ssh_config}"
  log "Guest Ansible inventory: ${guest_inventory}"
fi

log "Deployment ${action} completed"
