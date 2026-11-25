# AGENTS

## Project

This repository contains the configuration, automation, and measurements for the
CS740 networking research project. It compares Cilium VXLAN, native routing,
and custom VXLAN implementations using Kubernetes VMs across CloudLab hosts.
Read `README.md` first, then the relevant guide under `docs/`. The latest
continuation notes are in `docs/handoff.md`; packet-path and bypass research
is in `docs/vxlan-datapath-and-bypass.md`.

## User workflow

Expose routine configuration through scripts and flags. The user should not
need to edit Ansible or Terraform to change CloudLab endpoints, experiment
interfaces, or select an experiment.

- `experiments/configure.py`: enter hostnames and networking settings; updates
  the local deployment JSON and preserves existing SSH keys and VM settings.
- `infrastructure/deploy.sh`: provision/reconcile hypervisors and VMs through
  Ansible and Terraform; refresh generated guest inventory and SSH aliases.
- `experiments/setup.py MODE --reset`: reset the guest cluster and configure
  the selected experiment, including NIC attachments and MTUs.
- `experiments/run.py MODE --client VM --server VM`: run Cilium CLI performance
  tests and collect reports.
- Add `--diagnostics` to isolate every sample/path/test and monitor both endpoint
  VMs and hypervisors. `experiments/diagnostics.py` coordinates remote
  `diagnostic_agent.py` samplers. `--profile` adds perf only in a separate
  diagnostic run. See `docs/experiments/diagnostics.md` for timing/scope limits.
- `experiments/collect.py --background`: collect all eight modes with consistent
  parameters and endpoint placement, cluster resets, host NIC audits, a metrics
  CSV, and resumable per-mode attempts. Respect its shared collection lock.
  `--resume DIR --use-current-setup` reuses a matching successful current setup
  for only the first unfinished mode; later modes rebuild normally. It creates
  a new measurement attempt and records the reused setup without rerunning reset.
- `experiments/hypervisors.py`: optional QEMU/KVM preparation, host NIC tuning,
  audits, and restoration of saved NIC settings. This is separate from the
  hostname configuration wizard.
- `experiments/aggregate.py ATTEMPT [ATTEMPT ...]`: read-only aggregation of
  saved measurements into Markdown/CSV/JSON and Matplotlib SVG/PNG figures.
  Preserve phase/sample identity; do not double-count mirrored reports or
  host-wide counters repeated in per-core CSV rows. See `docs/experiments/aggregation.md`.
- `experiments/study.py --background`: complete rerun and targeted host-VXLAN
  queue/profile/bridge controls. Reuse its collection lock, preserve retry
  history, and restore queue baselines. `experiments/queues.py` changes only
  worker experiment NIC drivers and gracefully restarts those workers.
  `experiments/affinity.py` pins/restores only managed worker vhost tasks; discover
  both QEMU task-group members and legacy separate vhost processes. Restore
  affinity before queue changes replace task IDs. Status watching defaults to
  300 seconds; keep scientific sampling independent of orchestration polling.
- `experiments/latency_study.py`: paired tunnel/bypass latency controls with
  unchanged VM boots/attachments within each custom mode. Temporary state is
  `generated/path-control.json`; the runner requires explicit acknowledgement
  and validates live routes/bridge ports. Route protocol 243 is reserved for
  temporary bypass routes; 242 remains the custom tunnel route protocol.
  Restore the tunnel path before changing modes. Keep instrumented timing runs
  separate from primary latency pairs; inclusive probe durations are not additive.

Run commands from the repository root. Keep the README short; each experiment
belongs in its own `docs/experiments/MODE.md` with exact reset, setup, and run
commands. Shared runner behavior belongs in `docs/experiments/runner.md`.

## Configuration and SSH

`infrastructure/config.json` is the local source of deployment settings;
`config.example.json` and `config.schema.json` document its shape. Preserve the
base config's direct experiment NIC attachments. Experiment-specific bridge
attachments belong in generated configs rather than changes to that base file.

Hypervisor SSH access and guest credentials are already configured. Reuse the
existing key paths and generated SSH configuration; do not regenerate keys or
ask the user to reinstall them as a routine setup step. Guests are reached
through their hypervisor jump hosts. Preserve management-network connectivity.
Ansible targets management IPs, so noninteractive SSH host-key policy must cover
IP connections as well as named hosts. `HostKeyAlias` does not select a `Host`
block. Keep `accept-new` and batch mode in generated defaults/connection options;
retain rejection of changed keys and do not clear trust to fix prompt failures.

Local configs, backups, generated inventories/SSH files, Terraform state, and
measurement output must remain outside version control. Do not overwrite
unrelated local changes or earlier measurements.

## Networking constraints

Management and experiment NICs are separate. On the current hosts, `eno1` is
management and `eno1d1` is the experiment NIC; obtain actual names from the
deployment config rather than hard-coding them. Guest experiment NICs are named
`experiment`. Hypervisor service changes and guest Cilium changes are distinct.

**Libvirt does not support an explicit MTU element on `type=direct` interfaces.**
Omit that attribute in Terraform for direct/macvtap attachments. Experiment
setup reconciles their host-side MTU using the managed domain's experiment MAC
and live macvtap target, then configures the guest NIC separately. Retain the
supported libvirt MTU setting for bridge attachments. A provider mock test alone
does not prove that libvirt will accept a domain definition.

The experiment modes are:

| Mode | Network and MTU |
| --- | --- |
| `vxlan` | Cilium VXLAN, UDP 8472; 1500-byte underlay, 1450-byte Pod MTU |
| `native` (`flat` alias in `cluster.sh`) | Native routing, MTU 1500 |
| `vm-local` | Host-network tests between distinct VMs on one hypervisor |
| `direct-1450` | Native routing, Pod MTU 1450; guest NIC MTU 1500 |
| `vm-vxlan` | Per-VM Linux VXLAN; inner MTU 1500, underlay MTU 1550 |
| `host-vxlan` | Shared hypervisor VXLAN bridge; inner MTU 1500, underlay MTU 1550 |
| `vm-vxlan-1450` | Per-VM Linux VXLAN; inner MTU 1450, guest/physical transport MTU 1500 |
| `host-vxlan-1450` | Shared host VXLAN; guest/bridge/tunnel MTU 1450, physical MTU 1500 |

`bridge-1450` is an extra attachment control, excluded from the default eight:
native Cilium at Pod/guest MTU 1450, TAP/host bridge to physical LAN at MTU 1500.
Guest diagnostics identify benchmark Pod UIDs through cgroups and inspect their
network namespaces; host `/proc/net` counters do not cover ordinary Pod TCP.
Host-network Pod counters still include other connections in the host namespace.

Custom VXLAN uses VNI 740 and UDP 4789. Per-VM forwarding reserves
`10.245.0.0/24`, `cs740vx`, and route protocol 242. Shared host VXLAN uses
`cs740br`/`cs740hvx` and a separate configurable VTEP subnet. Verify full-size
underlay packets without fragmentation before measuring custom VXLAN.
Wait for udev initialization before applying custom VM tunnel MACs; static peers
depend on those addresses. Preserve the live MAC/MTU/state preflight rather than
assuming Kubernetes Ready proves that the custom tunnel is usable.

For comparisons of Cilium/custom VXLAN implementation overhead, use `vxlan`,
`vm-vxlan-1450`, and `host-vxlan-1450` with the same endpoints and `--pod-only`.
All three use inner MTU 1450 and physical MTU 1500. A VM performing
encapsulation still needs transport NIC MTU 1500; setting it to 1450 would
leave insufficient room for a full-size inner packet. Preserve the original
1500-inner-MTU experiments as separate modes. Reset must not recursively delete
`/run/cilium`, which may contain cgroupfs mounts; reboot clears runtime state.

Kube-proxy is intentionally retained in the current baseline. Do not silently
enable kube-proxy replacement, eBPF masquerading/host routing, netkit, or other
tuning as part of an unrelated network-mode change. Keep tuning profiles and
tunnel placement as independent experimental variables.

## Measurement and validation

Use the installed Cilium CLI on the controller through existing SSH access.
Pin client/server VMs and validate their hypervisor placement. In Cilium,
"same-node" means one Kubernetes VM; it does not mean two VMs on one physical
host. The local VM baseline uses host-to-host, other-node tests on colocated
VMs. Physical-NIC captures are needed to verify that measured unicast stays off
the link; placement checks alone cannot establish that.

Preserve Cilium's original JSON reports, console output, parameters, placement,
and active configuration. Whole-run `/proc/stat` deltas are aggregate CPU
measurements, not per-test or isolated encapsulation overhead. Record failures
and distinguish local validation from live CloudLab verification.

Relevant local checks:

```bash
python3 -m unittest discover -s experiments/tests -v
infrastructure/cluster.sh validate
terraform fmt -check -recursive infrastructure/terraform
git diff --check
```

Use Ansible syntax checks for changed playbooks. Terraform mock-provider tests
are in `infrastructure/terraform/tests/`; run them in an isolated working copy
when necessary to avoid changing existing per-hypervisor backend configuration
or state. Add regression coverage for networking failures, including the
unsupported direct-interface MTU case. Never describe mock tests as a successful
live deployment.

Cluster reset removes Kubernetes workloads and cluster state and reboots the
guests; it preserves VM disks and SSH keys. Creating or editing automation does
not itself require running a live reset or deployment. Clearly report any live
operations performed.
The shared node role must recreate `/etc/kubernetes` before controller/worker
templates are written after reset; already-installed packages will not restore
it. `infrastructure/tests/reset-reapply.yml` covers this lifecycle locally.

## Commit messages

Match the existing concise, lowercase style, such as `feat: add ...`,
`fix: ...`, or `documentation: ...`. Avoid long bodies for small changes.
