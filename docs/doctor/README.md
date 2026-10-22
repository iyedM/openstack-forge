# Host diagnostics (`openstack-forge doctor`)

`openstack-forge doctor` is a read-only, local preflight check command. It loads and
validates a cluster YAML configuration, identifies which node entry corresponds to the
local machine, and compares the host's actual operating system, CPU, memory,
virtualization support, storage disks, and network interfaces against what the
configuration declares for that node.

It never executes privileged commands (`sudo`), never modifies the system, and never
opens network connections.

## Usage

```sh
openstack-forge doctor --config cluster.yaml [--node HOSTNAME] [--json]
```

If `--config` is omitted, `cluster.yaml` in the current directory is used.

| Option | Meaning |
| ------ | ------- |
| `--config PATH` | Path to the cluster configuration file (default: `cluster.yaml`). Validated with the standard configuration validator before any check runs. |
| `--node HOSTNAME` | Explicitly select the node entry in `nodes[]` to check against. |
| `--json` | Emit the diagnostic report as machine-readable JSON instead of an ASCII table. |

### Exit codes

| Code | Meaning |
| ---- | ------- |
| 0    | Configuration is valid, the local node was identified, and no check returned `FAIL` (`PASS` and `WARN` exit with 0). |
| 1    | Configuration is invalid, the local node could not be uniquely identified, or at least one check returned `FAIL`. |
| 2    | Command-line argument parsing error. |

## Node identification

`doctor` determines which entry in `nodes[]` describes the local machine using three
methods in priority order:

1. **Explicit CLI flag (`--node HOSTNAME`).** Matches `nodes[].hostname`
   (case-insensitively). Fails if no declared node has that hostname.
2. **Local hostname (`hostname`).** Matches the local hostname (or its first DNS label
   when the host reports a dotted FQDN) against `nodes[].hostname`.
3. **Local IPv4 address (`ip -j addr`).** Matches any local IPv4 address assigned to the
   host's interfaces against `nodes[].management_ip`.

If none of these methods matches a node, or if multiple declared nodes match (for
example, a host carrying more than one declared `management_ip`), `doctor` exits with
code `1` and an actionable message listing the declared nodes and suggesting
`--node HOSTNAME`.

## Checks and why they exist

Every check returns `PASS`, `WARN`, or `FAIL`, a one-line explanation, and both the
`declared` and `observed` values. `doctor` never invents hardcoded minimum hardware
requirements: it compares the host strictly against what the cluster configuration
declares for the identified node.

| Check | Applies to | Source | Outcome | Why it exists |
| ----- | ---------- | ------ | ------- | ------------- |
| `os` | All nodes | `/etc/os-release` | `PASS` if `ID=ubuntu` and `VERSION_ID` equals `cluster.ubuntu_version`; `FAIL` otherwise. | OpenStack packages and Ubuntu Cloud Archive repositories (`cloud-archive:epoxy`) are tied to a specific Ubuntu LTS release. Running on another distribution or Ubuntu version causes package resolution and service failures. |
| `vcpus` | All nodes | `nproc` | `PASS` if observed logical CPUs `>= node.vcpus`; `FAIL` otherwise. | Verifies that the machine has at least the CPU capacity declared for this node in the cluster configuration. |
| `ram` | All nodes | `/proc/meminfo` (`MemTotal`) | `PASS` if `MemTotal` is within the 10% kernel-reserved tolerance of `node.ram_gb` (at least `0.90 * node.ram_gb` GiB); `FAIL` otherwise. | Verifies that usable memory matches the node's declared `ram_gb`. A 10% tolerance is allowed because `/proc/meminfo` `MemTotal` reports memory after BIOS/UEFI firmware and Linux kernel reservations (kernel image, page tables, crashkernel), so a machine provisioned with $N$ GB always reports slightly less than $N$ GiB of usable RAM. |
| `virt_environment` | All nodes | `systemd-detect-virt` | `PASS` (informational, reporting `physical (none)` or `virtual (<hypervisor>)`). | Records whether the node is running on bare metal or inside a virtual machine so operators can account for nested virtualization or hypervisor constraints when troubleshooting. |
| `cpu_virtualization` | `compute` nodes | `/proc/cpuinfo` (`flags`) | `PASS` if Intel VT-x (`vmx`) or AMD-V (`svm`) is present; `WARN` if absent. | Compute nodes run Nova instances with KVM hardware acceleration when `vmx` or `svm` is exposed. Without hardware virtualization flags (for example, in a VM without nested virtualization), Nova must fall back to software emulation (`virt_type=qemu`). |
| `disk` / `disk:<path>` | `block` (`node.disk`) and `object` (`node.disks`) nodes | `lsblk -J -b -o NAME,PATH,TYPE,SIZE,FSTYPE,MOUNTPOINTS,WWN,PTTYPE`, plus `readlink --canonicalize-existing -- <path>` per declared disk | `PASS` if the device exists, has `type == "disk"`, is not the root disk (`/`), and has no partition table (`PTTYPE` such as `gpt` or `dos`), partitions, filesystems, mountpoints, or LVM/RAID signatures; `FAIL` otherwise. A disk with a partition table fails as "has a partition table (<type>)" even when it has no partitions. | Cinder (block storage) and Swift (object storage) format or create volume groups on dedicated raw block devices. Pointing them at a missing device, a non-disk device, the OS root disk, or a disk that already contains a partition table, partitions, filesystems, active mounts, LVM physical volumes (`LVM2_member`), or RAID members (`linux_raid_member`) would either fail provisioning or destroy existing data. An empty partition table is still a sign that the disk was prepared for some other use. |
| `interfaces` | All nodes | `ip -j addr` | `PASS` if every interface in `node.interfaces` exists on the host and the management interface carries `node.management_ip`; `FAIL` otherwise. | OpenStack services bind to specific network interfaces and the node's `management_ip`. Missing interfaces or an unassigned management address prevent control-plane communication. |
| `provider_interface` | All nodes | Configuration (`node.interfaces` and `networks`) + `ip -j addr` | `WARN` if the management and provider networks use the same network interface or if the provider interface carries `node.management_ip`; `PASS` otherwise. | Neutron moves the provider interface into the Open vSwitch bridge `br-provider` (`ovs-vsctl add-port br-provider <iface>`) to provide external/floating IP connectivity. If the provider interface is the management interface or carries `node.management_ip`, adding it to `br-provider` can drop management connectivity; `neutron-controller` and `neutron-compute` refuse `apply` with `PreconditionError` in that case unless `allow_shared_provider_interface: true` is set on the node. |
| `provider_interface_link` | Nodes with a provider interface | `ip -j addr` (`flags`, `operstate`) | `PASS` if the provider interface link is operationally up (`LOWER_UP` in `flags` or `operstate` in `UP` / `UNKNOWN`); `WARN` if the interface is operationally down (`DOWN`, `NO-CARRIER`, etc.) or missing. | The official OpenStack installation guide requires the provider interface to be `UP` (active link state without an IPv4 address) so `br-provider` can forward external and floating-IP traffic. **OpenStack Forge never runs `netplan` or `ip link` commands; the operator must bring the provider interface link up on the host before deployment.** |

## Declared disk paths and symlinks

Declared disks may be stable symlinks such as `/dev/disk/by-id/ata-...`,
`/dev/disk/by-id/nvme-...` or `/dev/disk/by-path/...`. Before matching, `doctor`
resolves each declared path to its real device node by running
`readlink --canonicalize-existing -- <path>` through the `CommandRunner`: the path is a
single argument (no shell), and the command needs no privileges. The resolved path (for
example `/dev/sdb`) is matched against the `PATH` and `NAME` columns of `lsblk` first; the
declared path itself is matched as a fallback, including the `/dev/disk/by-id/wwn-<WWN>`
form derived from the `WWN` column.

- If the path does not exist, `readlink` exits non-zero and the check fails with
  "does not exist in lsblk output".
- If `readlink` itself is missing or times out, the check fails and reports the command
  error, like any other check whose command fails.

`lsblk` reads `PTTYPE` from the udev database, so it is available without root. If udev
has no information about a disk, `PTTYPE` is empty and only the other emptiness checks
apply.

## RAM tolerance calculation

Linux reports `MemTotal` in `/proc/meminfo` in KiB ($1024\text{ bytes}$). `doctor`
converts `MemTotal` to bytes and compares it against the node's declared `ram_gb`:

$$\text{minimum\_bytes} = \lfloor \text{ram\_gb} \times 1024^3 \times (1 - 0.10) \rfloor$$

For example, a node declaring `ram_gb: 16` requires at least $14.4\text{ GiB}$ of
`MemTotal` in `/proc/meminfo`.

## Architecture and testability

Host inspection is separated into three layers inside `openstack_forge.diagnostics`:

1. **`CommandRunner` (`runner.py`).** A small protocol (`argv -> CommandResult`) with a
   default `SubprocessRunner` implementation that executes commands as argument lists
   (`shell=False`) with an explicit timeout and translates missing binaries or timeouts
   into `CommandNotFoundError` and `CommandTimeoutError`.
2. **Pure parsers (`parsers.py`).** Standalone functions (`parse_os_release`,
   `parse_nproc`, `parse_meminfo`, `parse_cpuinfo_flags`, `parse_virt`, `parse_lsblk`,
   `parse_ip_addr`, `parse_hostname`, `parse_readlink`) that convert raw command outputs into typed
   dataclasses.
3. **Pure checks (`checks.py` and `node.py`).** Deterministic functions that take the
   parsed dataclasses and the validated cluster configuration and return `CheckResult`
   values without performing any I/O.
