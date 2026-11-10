#!/usr/bin/env bash

set -Eeuo pipefail

readonly SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
readonly ANSIBLE_DIR="${SCRIPT_DIR}/ansible"

inventory_file="${SCRIPT_DIR}/generated/guest_inventory.json"
inventory_was_selected=false
network_mode=""
action="apply"

usage() {
  cat <<'EOF'
Usage: ./cluster.sh [-i FILE] [--network-mode MODE] [apply|validate]

Builds the kubeadm cluster described by the generated guest inventory. The
validate action performs local Ansible syntax validation and does not contact
any VM or hypervisor.

Options:
  -i, --inventory FILE  Guest inventory (default: generated/guest_inventory.json)
  --network-mode MODE   Cilium datapath: vxlan (default) or native (flat routing)
  -h, --help            Show this help
EOF
}

die() {
  printf 'error: %s\n' "$*" >&2
  exit 1
}

while (($# > 0)); do
  case "$1" in
    -i|--inventory)
      (($# >= 2)) || die "$1 requires a file"
      inventory_file="$2"
      inventory_was_selected=true
      shift 2
      ;;
    --network-mode)
      (($# >= 2)) || die "$1 requires vxlan or native"
      network_mode="$2"
      shift 2
      ;;
    apply|validate)
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

if [[ "${network_mode}" == "flat" ]]; then
  network_mode="native"
fi
if [[ -n "${network_mode}" && "${network_mode}" != "vxlan" && "${network_mode}" != "native" ]]; then
  die "network mode must be vxlan or native (flat is accepted as an alias)"
fi

command -v ansible-playbook >/dev/null 2>&1 || die "required command not found: ansible-playbook"
command -v jq >/dev/null 2>&1 || die "required command not found: jq"
if [[ "${action}" == "validate" && "${inventory_was_selected}" == false && ! -r "${inventory_file}" ]]; then
  inventory_file="${SCRIPT_DIR}/kubernetes/inventory.example.json"
fi
[[ -r "${inventory_file}" ]] || die "guest inventory not found: ${inventory_file} (run deploy.sh apply first)"

inventory_file="$(cd -- "$(dirname -- "${inventory_file}")" && pwd)/$(basename -- "${inventory_file}")"
jq -e '
  .all.children.virtual_machines.hosts as $hosts |
  ($hosts | type == "object" and length >= 2) and
  (all($hosts[];
    (.cs740_role == "controller" or .cs740_role == "worker") and
    (.experiment_ipv4 | type == "string" and test("^[0-9.]+/[0-9]+$"))
  )) and
  ([$hosts[] | select(.cs740_role == "controller")] | length == 1) and
  ([$hosts[] | select(.cs740_role == "worker")] | length >= 1) and
  (([$hosts[].experiment_ipv4 | split("/")[0]] | length) ==
   ([$hosts[].experiment_ipv4 | split("/")[0]] | unique | length))
' "${inventory_file}" >/dev/null ||
  die "inventory must contain one controller, at least one worker, and unique static experiment IPv4 CIDRs"

export ANSIBLE_CONFIG="${ANSIBLE_DIR}/ansible.cfg"
export ANSIBLE_LOCAL_TEMP="$(mktemp -d /tmp/cs740-cluster.XXXXXXXX)"
trap 'rm -rf -- "${ANSIBLE_LOCAL_TEMP}"' EXIT

playbook_args=(-i "${inventory_file}" "${ANSIBLE_DIR}/playbooks/install-kubernetes.yml")
if [[ -n "${network_mode}" ]]; then
  playbook_args+=(--extra-vars "cilium_network_mode=${network_mode}")
fi
if [[ "${action}" == "validate" ]]; then
  playbook_args+=(--syntax-check)
  export ANSIBLE_HOST_PATTERN_MISMATCH=ignore
fi

ansible-playbook "${playbook_args[@]}"

