"""Pure diagnostic check functions over parsed system data."""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from ipaddress import IPv4Address
from typing import Any

from openstack_forge.diagnostics.parsers import BlockDevice, NetworkInterface, OsRelease

BYTES_PER_GIB: int = 1024**3

# Linux /proc/meminfo reports MemTotal after firmware/BIOS and kernel memory reservation
# (kernel image, page tables, crashkernel, reserved regions). Allow up to 10% below the
# nominal GiB equivalent of the node's declared ram_gb.
RAM_TOLERANCE: float = 0.10

VIRT_CPU_FLAGS: tuple[str, ...] = ("vmx", "svm")

_LVM_RAID_FSTYPES: frozenset[str] = frozenset(
    {
        "lvm2_member",
        "linux_raid_member",
        "ddf_raid_member",
        "isw_raid_member",
    }
)


class CheckStatus(str, Enum):
    """Outcome of a single diagnostic check."""

    PASS = "PASS"
    WARN = "WARN"
    FAIL = "FAIL"


@dataclass(frozen=True)
class CheckResult:
    """Result of a single diagnostic check."""

    name: str
    status: CheckStatus
    declared: str
    observed: str
    message: str

    def to_dict(self) -> dict[str, str]:
        return {
            "name": self.name,
            "status": self.status.value,
            "declared": self.declared,
            "observed": self.observed,
            "message": self.message,
        }


def check_os(os_release: OsRelease, declared_ubuntu_version: str) -> CheckResult:
    """Check that ``/etc/os-release`` matches ``cluster.ubuntu_version``."""
    declared = f"Ubuntu {declared_ubuntu_version}"
    if os_release.id == "ubuntu":
        observed = (
            f"Ubuntu {os_release.version_id}"
            if os_release.version_id
            else "Ubuntu (unknown version)"
        )
    else:
        distro = os_release.name or os_release.id or "unknown"
        observed = f"{distro} {os_release.version_id}".strip()

    if os_release.id == "ubuntu" and os_release.version_id == declared_ubuntu_version:
        return CheckResult(
            name="os",
            status=CheckStatus.PASS,
            declared=declared,
            observed=observed,
            message=f"Operating system matches declared {declared}.",
        )
    return CheckResult(
        name="os",
        status=CheckStatus.FAIL,
        declared=declared,
        observed=observed,
        message=f"Operating system is {observed}; expected {declared}.",
    )


def check_vcpus(observed_vcpus: int, declared_vcpus: int) -> CheckResult:
    """Check that available vCPUs (``nproc``) meet or exceed ``node.vcpus``."""
    declared = f"{declared_vcpus} vCPU(s)"
    observed = f"{observed_vcpus} vCPU(s)"
    if observed_vcpus >= declared_vcpus:
        return CheckResult(
            name="vcpus",
            status=CheckStatus.PASS,
            declared=declared,
            observed=observed,
            message=f"Detected {observed_vcpus} vCPU(s), meeting declared {declared_vcpus}.",
        )
    return CheckResult(
        name="vcpus",
        status=CheckStatus.FAIL,
        declared=declared,
        observed=observed,
        message=f"Detected {observed_vcpus} vCPU(s), below declared {declared_vcpus} vCPU(s).",
    )


def check_ram(
    memtotal_bytes: int,
    declared_ram_gb: int,
    *,
    tolerance: float = RAM_TOLERANCE,
) -> CheckResult:
    """Check that usable RAM (``MemTotal``) meets ``node.ram_gb`` within tolerance."""
    observed_gib = memtotal_bytes / BYTES_PER_GIB
    min_required_bytes = int(declared_ram_gb * BYTES_PER_GIB * (1.0 - tolerance))
    min_required_gib = declared_ram_gb * (1.0 - tolerance)
    tolerance_pct = int(round(tolerance * 100))

    declared = f"{declared_ram_gb} GB"
    observed = f"{observed_gib:.1f} GiB"

    if memtotal_bytes >= min_required_bytes:
        return CheckResult(
            name="ram",
            status=CheckStatus.PASS,
            declared=declared,
            observed=observed,
            message=(
                f"Usable memory {observed_gib:.1f} GiB meets declared {declared_ram_gb} GB "
                f"(within {tolerance_pct}% kernel-reserved tolerance)."
            ),
        )
    return CheckResult(
        name="ram",
        status=CheckStatus.FAIL,
        declared=declared,
        observed=observed,
        message=(
            f"Usable memory {observed_gib:.1f} GiB is below declared {declared_ram_gb} GB "
            f"(minimum {min_required_gib:.1f} GiB with {tolerance_pct}% tolerance)."
        ),
    )


def check_virt_environment(virt_type: str) -> CheckResult:
    """Report whether the host is a virtual machine or physical hardware."""
    declared = "physical or virtual"
    normalised = virt_type.strip().lower() or "none"
    if normalised == "none":
        return CheckResult(
            name="virt_environment",
            status=CheckStatus.PASS,
            declared=declared,
            observed="physical (none)",
            message="Host is running on physical hardware (systemd-detect-virt: none).",
        )
    return CheckResult(
        name="virt_environment",
        status=CheckStatus.PASS,
        declared=declared,
        observed=f"virtual ({normalised})",
        message=f"Host is running inside a virtual machine (systemd-detect-virt: {normalised}).",
    )


def check_cpu_virtualization(cpu_flags: Collection[str]) -> CheckResult:
    """Check for hardware virtualization CPU flags (``vmx`` or ``svm``) on compute nodes."""
    declared = "vmx or svm"
    found = [flag for flag in VIRT_CPU_FLAGS if flag in cpu_flags]
    if found:
        observed = ", ".join(found)
        return CheckResult(
            name="cpu_virtualization",
            status=CheckStatus.PASS,
            declared=declared,
            observed=observed,
            message=f"CPU hardware virtualization flag ({observed}) is present.",
        )
    return CheckResult(
        name="cpu_virtualization",
        status=CheckStatus.WARN,
        declared=declared,
        observed="none",
        message="CPU virtualization flags (vmx/svm) are absent; nova will need virt_type=qemu.",
    )


def check_disk(
    declared_disk: str,
    devices: Sequence[BlockDevice],
    *,
    check_name: str = "disk",
    resolved_disk: str | None = None,
) -> CheckResult:
    """Check that a declared storage disk exists, is a block disk, is not root, and is empty.

    ``resolved_disk`` is the declared path with symlinks resolved (for example a
    ``/dev/disk/by-id/...`` link resolved to ``/dev/sdb``); it is matched first.
    """
    declared = f"{declared_disk} (empty disk)"
    device = None
    if resolved_disk is not None:
        device = _find_block_device(resolved_disk, devices)
    if device is None:
        device = _find_block_device(declared_disk, devices)
    if device is None:
        resolution = (
            f" (resolves to {resolved_disk})"
            if resolved_disk is not None and resolved_disk != declared_disk
            else ""
        )
        return CheckResult(
            name=check_name,
            status=CheckStatus.FAIL,
            declared=declared,
            observed="missing",
            message=f"Declared disk {declared_disk}{resolution} does not exist in lsblk output.",
        )

    if device.type != "disk":
        return CheckResult(
            name=check_name,
            status=CheckStatus.FAIL,
            declared=declared,
            observed=f"{device.path} (type={device.type})",
            message=(
                f"Declared device {declared_disk} has type '{device.type}', "
                "expected a block 'disk'."
            ),
        )

    tags: list[str] = []
    reasons: list[str] = []

    all_mounts = _collect_mountpoints(device)
    if "/" in all_mounts:
        tags.append("root disk")
        reasons.append("is the root disk (mounted at /)")

    # A partition table (gpt, dos, ...) marks the disk as in use even with no partitions.
    if device.pttype:
        tags.append(f"partition table: {device.pttype}")
        reasons.append(f"has a partition table ({device.pttype})")

    partitions = [child.name for child in _iter_descendants(device) if child.type == "part"]
    if partitions:
        part_str = ", ".join(partitions)
        tags.append(f"partitions: {part_str}")
        reasons.append(f"has partition(s) ({part_str})")

    lvm_raid = _collect_lvm_raid_signatures(device)
    if lvm_raid:
        sig_str = ", ".join(lvm_raid)
        tags.append(f"LVM/RAID: {sig_str}")
        reasons.append(f"has LVM/RAID signature ({sig_str})")

    filesystems = _collect_filesystems(device)
    if filesystems:
        fs_str = ", ".join(filesystems)
        tags.append(f"fstype: {fs_str}")
        reasons.append(f"has filesystem ({fs_str})")

    non_root_mounts = [mnt for mnt in all_mounts if mnt != "/"]
    if non_root_mounts:
        mnt_str = ", ".join(non_root_mounts)
        tags.append(f"mounted: {mnt_str}")
        reasons.append(f"has mountpoint(s) ({mnt_str})")

    if reasons:
        return CheckResult(
            name=check_name,
            status=CheckStatus.FAIL,
            declared=declared,
            observed=f"{device.path} ({'; '.join(tags)})",
            message=f"Disk {declared_disk} is not empty/usable: {'; '.join(reasons)}.",
        )

    size_gib = device.size / BYTES_PER_GIB
    return CheckResult(
        name=check_name,
        status=CheckStatus.PASS,
        declared=declared,
        observed=f"{device.path} (empty disk, {size_gib:.1f} GiB)",
        message=(
            f"Disk {declared_disk} exists, is a block disk, is not the root disk, and is empty."
        ),
    )


def check_object_disks(
    declared_disks: Sequence[str],
    devices: Sequence[BlockDevice],
    resolved_disks: Mapping[str, str | None] | None = None,
) -> list[CheckResult]:
    """Run :func:`check_disk` for each declared disk on an ``object`` storage node."""
    resolved = resolved_disks or {}
    return [
        check_disk(
            disk_path,
            devices,
            check_name=f"disk:{disk_path}",
            resolved_disk=resolved.get(disk_path),
        )
        for disk_path in declared_disks
    ]


def check_interfaces(
    node: Mapping[str, Any],
    networks: Mapping[str, Any],
    host_interfaces: Sequence[NetworkInterface],
) -> CheckResult:
    """Check that all declared interfaces exist and the management interface has management_ip."""
    declared_ifaces: Mapping[str, str] = node["interfaces"]
    management_ip = str(IPv4Address(node["management_ip"]))
    mgmt_iface = _resolve_management_interface(declared_ifaces, networks)

    unique_declared = list(dict.fromkeys(declared_ifaces.values()))
    by_name = {iface.name: iface for iface in host_interfaces}

    missing = [name for name in unique_declared if name not in by_name]
    mgmt_host_iface = by_name.get(mgmt_iface) if mgmt_iface else None
    mgmt_ips = mgmt_host_iface.ipv4_addresses if mgmt_host_iface else ()
    has_mgmt_ip = IPv4Address(management_ip) in {IPv4Address(ip) for ip in mgmt_ips}

    declared_desc = f"{', '.join(unique_declared)} ({mgmt_iface}={management_ip})"
    observed_parts: list[str] = []
    for name in unique_declared:
        if name in by_name:
            ips = ", ".join(by_name[name].ipv4_addresses) or "no IPv4"
            observed_parts.append(f"{name} ({ips})")
        else:
            observed_parts.append(f"{name} (missing)")
    observed_desc = ", ".join(observed_parts)

    if missing and not has_mgmt_ip:
        return CheckResult(
            name="interfaces",
            status=CheckStatus.FAIL,
            declared=declared_desc,
            observed=observed_desc,
            message=(
                f"Declared interface(s) missing: {', '.join(missing)}; "
                f"management interface '{mgmt_iface}' does not carry {management_ip}."
            ),
        )
    if missing:
        return CheckResult(
            name="interfaces",
            status=CheckStatus.FAIL,
            declared=declared_desc,
            observed=observed_desc,
            message=f"Declared interface(s) missing on host: {', '.join(missing)}.",
        )
    if not has_mgmt_ip:
        found_ips = ", ".join(mgmt_ips) or "none"
        return CheckResult(
            name="interfaces",
            status=CheckStatus.FAIL,
            declared=declared_desc,
            observed=observed_desc,
            message=(
                f"Management interface '{mgmt_iface}' does not carry management_ip "
                f"{management_ip} (found: {found_ips})."
            ),
        )
    return CheckResult(
        name="interfaces",
        status=CheckStatus.PASS,
        declared=declared_desc,
        observed=observed_desc,
        message=(
            f"All declared interfaces exist and management interface '{mgmt_iface}' "
            f"carries {management_ip}."
        ),
    )


def check_provider_interface(
    node: Mapping[str, Any],
    networks: Mapping[str, Any],
    host_interfaces: Sequence[NetworkInterface] | None = None,
) -> CheckResult:
    """Warn when the provider interface is shared, carries management_ip, or is down."""
    declared_ifaces: Mapping[str, str] = node["interfaces"]
    mgmt_iface = _resolve_management_interface(declared_ifaces, networks)
    provider_iface = declared_ifaces.get("provider") or networks.get("provider", {}).get(
        "interface"
    )

    declared = "distinct management and provider interfaces (provider link UP)"
    observed = f"management={mgmt_iface or 'none'}, provider={provider_iface or 'none'}"

    if mgmt_iface and provider_iface and mgmt_iface == provider_iface:
        return CheckResult(
            name="provider_interface",
            status=CheckStatus.WARN,
            declared=declared,
            observed=observed,
            message=(
                f"Provider and management networks share interface '{mgmt_iface}'; "
                "bridge migration risks disrupting management connectivity."
            ),
        )

    uses_provider = "provider" in declared_ifaces or node.get("role") in {"controller", "compute"}
    if host_interfaces is not None and uses_provider and provider_iface:
        by_name = {iface.name: iface for iface in host_interfaces}
        host_provider = by_name.get(provider_iface)
        if host_provider is None:
            return CheckResult(
                name="provider_interface",
                status=CheckStatus.WARN,
                declared=declared,
                observed=f"{observed} (missing)",
                message=(
                    f"Provider interface '{provider_iface}' is missing on host; the operator "
                    "must configure and bring the link up (OpenStack Forge does not run netplan "
                    "or ip link)."
                ),
            )
        operstate = (host_provider.operstate or "UNKNOWN").upper()
        observed_with_state = f"{observed} (operstate={operstate})"
        mgmt_ip_raw = node.get("management_ip")
        if isinstance(mgmt_ip_raw, str):
            mgmt_ip = IPv4Address(mgmt_ip_raw)
            provider_ips = {IPv4Address(ip) for ip in host_provider.ipv4_addresses}
            if mgmt_ip in provider_ips:
                return CheckResult(
                    name="provider_interface",
                    status=CheckStatus.WARN,
                    declared=declared,
                    observed=observed_with_state,
                    message=(
                        f"Provider interface '{provider_iface}' carries management_ip "
                        f"{mgmt_ip}; attaching it to OVS bridge 'br-provider' would cut "
                        "management connectivity."
                    ),
                )
        if operstate not in {"UP", "UNKNOWN"}:
            return CheckResult(
                name="provider_interface",
                status=CheckStatus.WARN,
                declared=declared,
                observed=observed_with_state,
                message=(
                    f"Provider interface '{provider_iface}' link state is {operstate} "
                    "(expected UP); the operator must bring the link up before Neutron can "
                    "forward provider traffic (OpenStack Forge does not run netplan or ip link)."
                ),
            )
        return CheckResult(
            name="provider_interface",
            status=CheckStatus.PASS,
            declared=declared,
            observed=observed_with_state,
            message=(
                f"Management ({mgmt_iface}) and provider ({provider_iface}, link {operstate}) "
                "interfaces are separate and provider link is up."
            ),
        )

    return CheckResult(
        name="provider_interface",
        status=CheckStatus.PASS,
        declared=declared,
        observed=observed,
        message=(
            f"Management ({mgmt_iface}) and provider ({provider_iface}) interfaces are separate."
        ),
    )


def check_provider_interface_link(
    node: Mapping[str, Any],
    networks: Mapping[str, Any],
    host_interfaces: Sequence[NetworkInterface],
) -> CheckResult:
    """Check provider interface separation and host link ``operstate`` from ``ip -j addr``."""
    return check_provider_interface(node, networks, host_interfaces=host_interfaces)


def _resolve_management_interface(
    declared_ifaces: Mapping[str, str],
    networks: Mapping[str, Any],
) -> str:
    if "management" in declared_ifaces:
        return declared_ifaces["management"]
    mgmt_net = networks.get("management")
    if isinstance(mgmt_net, Mapping) and isinstance(mgmt_net.get("interface"), str):
        return mgmt_net["interface"]
    return next(iter(declared_ifaces.values()), "")


def _find_block_device(
    declared_disk: str,
    devices: Sequence[BlockDevice],
) -> BlockDevice | None:
    for device in devices:
        if _device_matches(declared_disk, device):
            return device
    for device in devices:
        for descendant in _iter_descendants(device):
            if _device_matches(declared_disk, descendant):
                return descendant
    return None


def _device_matches(declared_disk: str, device: BlockDevice) -> bool:
    candidates = {device.path, f"/dev/{device.name}", *device.symlinks}
    if device.wwn:
        candidates.add(f"/dev/disk/by-id/wwn-{device.wwn}")
    return declared_disk in candidates


def _iter_descendants(device: BlockDevice) -> list[BlockDevice]:
    descendants: list[BlockDevice] = []
    for child in device.children:
        descendants.append(child)
        descendants.extend(_iter_descendants(child))
    return descendants


def _collect_mountpoints(device: BlockDevice) -> list[str]:
    mounts: list[str] = list(device.mountpoints)
    for child in _iter_descendants(device):
        mounts.extend(child.mountpoints)
    return list(dict.fromkeys(mounts))


def _is_lvm_or_raid_fstype(fstype: str) -> bool:
    low = fstype.lower()
    return low in _LVM_RAID_FSTYPES or "lvm" in low or "raid" in low


def _collect_lvm_raid_signatures(device: BlockDevice) -> list[str]:
    found: list[str] = []
    for node in (device, *_iter_descendants(device)):
        if node.fstype and _is_lvm_or_raid_fstype(node.fstype):
            found.append(node.fstype)
        if node is not device and (node.type in {"lvm", "md"} or node.type.startswith("raid")):
            found.append(f"{node.type}:{node.name}")
    return list(dict.fromkeys(found))


def _collect_filesystems(device: BlockDevice) -> list[str]:
    found: list[str] = []
    for node in (device, *_iter_descendants(device)):
        if node.fstype and not _is_lvm_or_raid_fstype(node.fstype):
            found.append(node.fstype)
    return list(dict.fromkeys(found))
