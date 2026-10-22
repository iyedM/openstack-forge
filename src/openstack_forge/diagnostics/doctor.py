"""Orchestrate local read-only host checks and format ``doctor`` reports."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from openstack_forge.diagnostics.checks import (
    CheckResult,
    CheckStatus,
    check_cpu_virtualization,
    check_disk,
    check_interfaces,
    check_os,
    check_provider_interface,
    check_ram,
    check_vcpus,
    check_virt_environment,
)
from openstack_forge.diagnostics.errors import (
    CommandError,
    CommandNotFoundError,
    CommandTimeoutError,
    ParseError,
)
from openstack_forge.diagnostics.node import IdentifiedNode, identify_node
from openstack_forge.diagnostics.parsers import (
    BlockDevice,
    NetworkInterface,
    parse_cpuinfo_flags,
    parse_hostname,
    parse_ip_addr,
    parse_lsblk,
    parse_meminfo,
    parse_nproc,
    parse_os_release,
    parse_readlink,
    parse_virt,
)
from openstack_forge.diagnostics.runner import CommandResult, CommandRunner, SubprocessRunner

HOSTNAME_CMD: tuple[str, ...] = ("hostname",)
IP_ADDR_CMD: tuple[str, ...] = ("ip", "-j", "addr")
OS_RELEASE_CMD: tuple[str, ...] = ("cat", "/etc/os-release")
NPROC_CMD: tuple[str, ...] = ("nproc",)
MEMINFO_CMD: tuple[str, ...] = ("cat", "/proc/meminfo")
VIRT_CMD: tuple[str, ...] = ("systemd-detect-virt",)
CPUINFO_CMD: tuple[str, ...] = ("cat", "/proc/cpuinfo")
LSBLK_CMD: tuple[str, ...] = (
    "lsblk",
    "-J",
    "-b",
    "-o",
    "NAME,PATH,TYPE,SIZE,FSTYPE,MOUNTPOINTS,WWN,PTTYPE",
)
# The declared device path is appended as a separate argv element (no shell).
READLINK_CMD: tuple[str, ...] = ("readlink", "--canonicalize-existing", "--")


@dataclass(frozen=True)
class DoctorReport:
    """Complete diagnostic report for a single cluster node."""

    cluster_name: str
    node: Mapping[str, Any]
    identified_by: str
    checks: tuple[CheckResult, ...]

    @property
    def status(self) -> CheckStatus:
        """Overall status: FAIL if any check failed, WARN if any warned, else PASS."""
        statuses = {check.status for check in self.checks}
        if CheckStatus.FAIL in statuses:
            return CheckStatus.FAIL
        if CheckStatus.WARN in statuses:
            return CheckStatus.WARN
        return CheckStatus.PASS

    @property
    def passed_count(self) -> int:
        return sum(1 for check in self.checks if check.status is CheckStatus.PASS)

    @property
    def warn_count(self) -> int:
        return sum(1 for check in self.checks if check.status is CheckStatus.WARN)

    @property
    def fail_count(self) -> int:
        return sum(1 for check in self.checks if check.status is CheckStatus.FAIL)

    @property
    def exit_code(self) -> int:
        """Return 0 if no check returned FAIL, 1 if any check returned FAIL."""
        return 1 if self.fail_count > 0 else 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "cluster": self.cluster_name,
            "node": {
                "hostname": self.node["hostname"],
                "fqdn": self.node["fqdn"],
                "role": self.node["role"],
                "management_ip": self.node["management_ip"],
                "identified_by": self.identified_by,
            },
            "status": self.status.value,
            "summary": {
                "pass": self.passed_count,
                "warn": self.warn_count,
                "fail": self.fail_count,
                "total": len(self.checks),
            },
            "checks": [check.to_dict() for check in self.checks],
        }


def run_doctor(
    config: Mapping[str, Any],
    *,
    node_hostname: str | None = None,
    runner: CommandRunner | None = None,
) -> DoctorReport:
    """Identify the local node and run all applicable read-only checks."""
    active_runner: CommandRunner = SubprocessRunner() if runner is None else runner
    identified, cached_interfaces = _resolve_node(
        config["nodes"],
        requested_node=node_hostname,
        runner=active_runner,
    )
    node = identified.node
    role = node["role"]
    ubuntu_version = config["cluster"]["ubuntu_version"]
    networks = config["networks"]

    results: list[CheckResult] = [
        _run_os_check(active_runner, ubuntu_version),
        _run_vcpus_check(active_runner, node["vcpus"]),
        _run_ram_check(active_runner, node["ram_gb"]),
        _run_virt_check(active_runner),
    ]

    if role == "compute":
        results.append(_run_cpu_virt_check(active_runner))
    elif role == "block":
        results.append(_run_block_disk_check(active_runner, node["disk"]))
    elif role == "object":
        results.extend(_run_object_disks_checks(active_runner, node["disks"]))

    ifaces_check, resolved_interfaces = _run_interfaces_check(
        active_runner, node, networks, cached_interfaces
    )
    results.append(ifaces_check)
    results.append(check_provider_interface(node, networks, host_interfaces=resolved_interfaces))

    return DoctorReport(
        cluster_name=config["cluster"]["name"],
        node=node,
        identified_by=identified.identified_by,
        checks=tuple(results),
    )


def _resolve_node(
    nodes: Sequence[Mapping[str, Any]],
    *,
    requested_node: str | None,
    runner: CommandRunner,
) -> tuple[IdentifiedNode, tuple[NetworkInterface, ...] | None]:
    if requested_node is not None:
        return identify_node(nodes, requested_node=requested_node), None

    result = _run_checked(runner, HOSTNAME_CMD)
    local_hostname = parse_hostname(result.stdout)

    candidate = local_hostname.lower()
    short_candidate = candidate.split(".", 1)[0]
    if any(node["hostname"].lower() in {candidate, short_candidate} for node in nodes):
        return identify_node(nodes, local_hostname=local_hostname), None

    ip_result = _run_checked(runner, IP_ADDR_CMD)
    interfaces = parse_ip_addr(ip_result.stdout)
    local_ips = [ip for iface in interfaces for ip in iface.ipv4_addresses]
    identified = identify_node(
        nodes,
        local_hostname=local_hostname,
        local_ipv4s=local_ips,
    )
    return identified, interfaces


def _run_checked(runner: CommandRunner, argv: Sequence[str]) -> CommandResult:
    result = runner.run(argv)
    if result.returncode != 0:
        detail = result.stderr.strip() or f"exit status {result.returncode}"
        cmd_str = " ".join(argv)
        raise CommandError(f"Command {cmd_str!r} failed: {detail}")
    return result


def _error_result(name: str, declared: str, exc: Exception) -> CheckResult:
    if isinstance(exc, CommandNotFoundError):
        observed = f"command missing ({exc.executable})"
    elif isinstance(exc, CommandTimeoutError):
        observed = f"command timed out ({exc.argv[0]})"
    elif isinstance(exc, CommandError):
        observed = "command failed"
    else:
        observed = "parse error"
    return CheckResult(
        name=name,
        status=CheckStatus.FAIL,
        declared=declared,
        observed=observed,
        message=str(exc),
    )


def _run_os_check(runner: CommandRunner, declared_ubuntu_version: str) -> CheckResult:
    declared = f"Ubuntu {declared_ubuntu_version}"
    try:
        result = _run_checked(runner, OS_RELEASE_CMD)
        os_release = parse_os_release(result.stdout)
    except (CommandError, ParseError) as exc:
        return _error_result("os", declared, exc)
    return check_os(os_release, declared_ubuntu_version)


def _run_vcpus_check(runner: CommandRunner, declared_vcpus: int) -> CheckResult:
    declared = f"{declared_vcpus} vCPU(s)"
    try:
        result = _run_checked(runner, NPROC_CMD)
        observed_vcpus = parse_nproc(result.stdout)
    except (CommandError, ParseError) as exc:
        return _error_result("vcpus", declared, exc)
    return check_vcpus(observed_vcpus, declared_vcpus)


def _run_ram_check(runner: CommandRunner, declared_ram_gb: int) -> CheckResult:
    declared = f"{declared_ram_gb} GB"
    try:
        result = _run_checked(runner, MEMINFO_CMD)
        memtotal_bytes = parse_meminfo(result.stdout)
    except (CommandError, ParseError) as exc:
        return _error_result("ram", declared, exc)
    return check_ram(memtotal_bytes, declared_ram_gb)


def _run_virt_check(runner: CommandRunner) -> CheckResult:
    declared = "physical or virtual"
    try:
        # systemd-detect-virt returns exit code 1 with stdout "none" on physical hosts.
        result = runner.run(VIRT_CMD)
        virt_type = parse_virt(result.stdout, result.returncode)
    except (CommandError, ParseError) as exc:
        return _error_result("virt_environment", declared, exc)
    return check_virt_environment(virt_type)


def _run_cpu_virt_check(runner: CommandRunner) -> CheckResult:
    declared = "vmx or svm"
    try:
        result = _run_checked(runner, CPUINFO_CMD)
        flags = parse_cpuinfo_flags(result.stdout)
    except (CommandError, ParseError) as exc:
        return _error_result("cpu_virtualization", declared, exc)
    return check_cpu_virtualization(flags)


def _load_block_devices(runner: CommandRunner) -> tuple[BlockDevice, ...]:
    result = _run_checked(runner, LSBLK_CMD)
    return parse_lsblk(result.stdout)


def _resolve_device_path(runner: CommandRunner, declared_disk: str) -> str | None:
    """Resolve symlinks in ``declared_disk``; return None if the path does not exist."""
    result = runner.run((*READLINK_CMD, declared_disk))
    if result.returncode != 0:
        return None
    return parse_readlink(result.stdout)


def _run_disk_check(
    runner: CommandRunner,
    declared_disk: str,
    devices: Sequence[BlockDevice],
    *,
    check_name: str,
) -> CheckResult:
    try:
        resolved_disk = _resolve_device_path(runner, declared_disk)
    except (CommandError, ParseError) as exc:
        return _error_result(check_name, f"{declared_disk} (empty disk)", exc)
    return check_disk(declared_disk, devices, check_name=check_name, resolved_disk=resolved_disk)


def _run_block_disk_check(runner: CommandRunner, declared_disk: str) -> CheckResult:
    declared = f"{declared_disk} (empty disk)"
    try:
        devices = _load_block_devices(runner)
    except (CommandError, ParseError) as exc:
        return _error_result("disk", declared, exc)
    return _run_disk_check(runner, declared_disk, devices, check_name="disk")


def _run_object_disks_checks(
    runner: CommandRunner,
    declared_disks: Sequence[str],
) -> list[CheckResult]:
    try:
        devices = _load_block_devices(runner)
    except (CommandError, ParseError) as exc:
        return [
            _error_result(f"disk:{disk_path}", f"{disk_path} (empty disk)", exc)
            for disk_path in declared_disks
        ]
    return [
        _run_disk_check(runner, disk_path, devices, check_name=f"disk:{disk_path}")
        for disk_path in declared_disks
    ]


def _run_interfaces_check(
    runner: CommandRunner,
    node: Mapping[str, Any],
    networks: Mapping[str, Any],
    cached_interfaces: tuple[NetworkInterface, ...] | None,
) -> tuple[CheckResult, tuple[NetworkInterface, ...] | None]:
    unique_declared = list(dict.fromkeys(node["interfaces"].values()))
    declared_desc = f"{', '.join(unique_declared)} (management_ip={node['management_ip']})"
    try:
        if cached_interfaces is not None:
            interfaces = cached_interfaces
        else:
            result = _run_checked(runner, IP_ADDR_CMD)
            interfaces = parse_ip_addr(result.stdout)
    except (CommandError, ParseError) as exc:
        return _error_result("interfaces", declared_desc, exc), None
    return check_interfaces(node, networks, interfaces), interfaces


def format_report_table(report: DoctorReport) -> str:
    """Format a :class:`DoctorReport` as a human-readable ASCII table."""
    node = report.node
    header = (
        f"Doctor report for node '{node['hostname']}' "
        f"(role: {node['role']}, management_ip: {node['management_ip']}, "
        f"cluster: {report.cluster_name}, identified by: {report.identified_by})"
    )

    columns = ("CHECK", "STATUS", "DECLARED", "OBSERVED", "MESSAGE")
    rows = [
        (
            check.name,
            check.status.value,
            check.declared,
            check.observed,
            check.message,
        )
        for check in report.checks
    ]

    widths = [
        max(len(columns[idx]), *(len(row[idx]) for row in rows)) if rows else len(columns[idx])
        for idx in range(len(columns))
    ]

    def format_row(cells: Sequence[str]) -> str:
        padded = [
            cells[idx].ljust(widths[idx]) if idx < len(cells) - 1 else cells[idx]
            for idx in range(len(cells))
        ]
        return "  ".join(padded).rstrip()

    separator = "  ".join("-" * width for width in widths)
    table_lines = [
        header,
        "",
        format_row(columns),
        separator,
        *(format_row(row) for row in rows),
        "",
        (
            f"Summary: {report.passed_count} passed, "
            f"{report.warn_count} warning(s), "
            f"{report.fail_count} failed (overall: {report.status.value})"
        ),
    ]
    return "\n".join(table_lines)


def format_report_json(report: DoctorReport) -> str:
    """Serialize a :class:`DoctorReport` to pretty-printed JSON."""
    return json.dumps(report.to_dict(), indent=2)
