"""Pure parsers for local host inspection outputs."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from ipaddress import IPv4Address
from typing import Any

from openstack_forge.diagnostics.errors import ParseError

_MEMINFO_UNITS: dict[str, int] = {
    "": 1,
    "b": 1,
    "kb": 1024,
    "mb": 1024**2,
    "gb": 1024**3,
}
_ESCAPED_CHAR_RE = re.compile(r"\\(.)")


@dataclass(frozen=True)
class OsRelease:
    """Parsed fields from ``/etc/os-release``."""

    id: str
    version_id: str
    pretty_name: str = ""
    name: str = ""


@dataclass(frozen=True)
class BlockDevice:
    """A block device entry parsed from ``lsblk -J -b``."""

    name: str
    path: str
    type: str
    size: int
    fstype: str | None = None
    pttype: str | None = None
    mountpoints: tuple[str, ...] = ()
    wwn: str | None = None
    symlinks: tuple[str, ...] = ()
    children: tuple[BlockDevice, ...] = ()


@dataclass(frozen=True)
class NetworkInterface:
    """A network interface entry parsed from ``ip -j addr``."""

    name: str
    operstate: str
    ipv4_addresses: tuple[str, ...] = ()


def parse_os_release(text: str) -> OsRelease:
    """Parse ``/etc/os-release`` key-value lines into an :class:`OsRelease`."""
    fields: dict[str, str] = {}
    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        key, sep, raw_value = line.partition("=")
        if not sep or not key.strip():
            raise ParseError(f"Invalid line {line_number} in /etc/os-release")
        fields[key.strip()] = _unquote_os_release_value(raw_value.strip())

    os_id = fields.get("ID", "").strip().lower()
    version_id = fields.get("VERSION_ID", "").strip()
    if not os_id and not version_id:
        raise ParseError("/etc/os-release is empty or missing ID and VERSION_ID")

    return OsRelease(
        id=os_id,
        version_id=version_id,
        pretty_name=fields.get("PRETTY_NAME", "").strip(),
        name=fields.get("NAME", "").strip(),
    )


def _unquote_os_release_value(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        inner = value[1:-1]
        if value[0] == '"':
            return _ESCAPED_CHAR_RE.sub(r"\1", inner)
        return inner
    return value


def parse_nproc(text: str) -> int:
    """Parse ``nproc`` output into a positive integer CPU count."""
    stripped = text.strip()
    try:
        count = int(stripped)
    except ValueError as exc:
        raise ParseError(f"Invalid nproc output: {stripped!r}") from exc
    if count < 1:
        raise ParseError(f"nproc must be at least 1, got {count}")
    return count


def parse_meminfo(text: str) -> int:
    """Parse ``/proc/meminfo`` and return ``MemTotal`` in bytes."""
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line.startswith("MemTotal:"):
            continue
        _, _, rest = line.partition(":")
        parts = rest.split()
        if not parts or len(parts) > 2:
            raise ParseError(f"Malformed MemTotal line in /proc/meminfo: {line!r}")
        try:
            amount = int(parts[0])
        except ValueError as exc:
            raise ParseError(f"Invalid MemTotal value in /proc/meminfo: {parts[0]!r}") from exc
        unit = parts[1].lower() if len(parts) == 2 else ""
        multiplier = _MEMINFO_UNITS.get(unit)
        if multiplier is None:
            raise ParseError(f"Unsupported MemTotal unit in /proc/meminfo: {parts[1]!r}")
        if amount <= 0:
            raise ParseError(f"MemTotal must be positive, got {amount}")
        return amount * multiplier

    raise ParseError("MemTotal not found in /proc/meminfo")


def parse_cpuinfo_flags(text: str) -> frozenset[str]:
    """Extract the set of CPU feature flags from ``/proc/cpuinfo``."""
    flags: set[str] = set()
    for raw_line in text.splitlines():
        key, sep, value = raw_line.partition(":")
        if not sep:
            continue
        if key.strip().lower() in {"flags", "features"}:
            flags.update(value.split())
    return frozenset(flags)


def parse_virt(text: str, returncode: int = 0) -> str:
    """Parse ``systemd-detect-virt`` output into ``'none'`` or the hypervisor name."""
    stripped = text.strip().lower()
    if not stripped:
        if returncode != 0:
            raise ParseError("systemd-detect-virt failed with no output")
        return "none"
    return stripped.splitlines()[0].strip()


def parse_lsblk(text: str) -> tuple[BlockDevice, ...]:
    """Parse ``lsblk -J -b`` JSON output into a tuple of :class:`BlockDevice` trees."""
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ParseError(f"Invalid JSON from lsblk: {exc.msg}") from exc

    if not isinstance(payload, Mapping):
        raise ParseError("Expected lsblk JSON root to be an object")
    raw_devices = payload.get("blockdevices")
    if not isinstance(raw_devices, list):
        raise ParseError("lsblk JSON is missing the 'blockdevices' list")

    return tuple(_parse_block_device(item) for item in raw_devices)


def _parse_block_device(item: Any) -> BlockDevice:
    if not isinstance(item, Mapping):
        raise ParseError("lsblk blockdevice entry must be an object")

    raw_name = item.get("name")
    if not isinstance(raw_name, str) or not raw_name.strip():
        raise ParseError("lsblk blockdevice entry is missing a valid 'name'")
    name = raw_name.strip()

    raw_path = item.get("path")
    path = raw_path.strip() if isinstance(raw_path, str) and raw_path.strip() else f"/dev/{name}"

    raw_type = item.get("type")
    if not isinstance(raw_type, str) or not raw_type.strip():
        raise ParseError(f"lsblk blockdevice {name!r} is missing a valid 'type'")
    dev_type = raw_type.strip().lower()

    raw_size = item.get("size", 0)
    if isinstance(raw_size, bool) or not isinstance(raw_size, (int, str)):
        raise ParseError(f"lsblk blockdevice {name!r} has invalid 'size': {raw_size!r}")
    try:
        size = int(raw_size)
    except ValueError as exc:
        raise ParseError(
            f"lsblk blockdevice {name!r} has non-integer 'size': {raw_size!r}"
        ) from exc
    if size < 0:
        raise ParseError(f"lsblk blockdevice {name!r} has negative 'size': {size}")

    raw_fstype = item.get("fstype")
    fstype = raw_fstype.strip() if isinstance(raw_fstype, str) and raw_fstype.strip() else None

    raw_pttype = item.get("pttype")
    pttype = raw_pttype.strip() if isinstance(raw_pttype, str) and raw_pttype.strip() else None

    mountpoints = _extract_mountpoints(item)

    raw_wwn = item.get("wwn")
    wwn = raw_wwn.strip() if isinstance(raw_wwn, str) and raw_wwn.strip() else None

    symlinks = _extract_symlinks(item)

    raw_children = item.get("children") or []
    if not isinstance(raw_children, list):
        raise ParseError(f"lsblk blockdevice {name!r} has invalid 'children' field")
    children = tuple(_parse_block_device(child) for child in raw_children)

    return BlockDevice(
        name=name,
        path=path,
        type=dev_type,
        size=size,
        fstype=fstype,
        pttype=pttype,
        mountpoints=mountpoints,
        wwn=wwn,
        symlinks=symlinks,
        children=children,
    )


def _extract_mountpoints(item: Mapping[str, Any]) -> tuple[str, ...]:
    collected: list[str] = []
    raw_list = item.get("mountpoints")
    if isinstance(raw_list, list):
        for entry in raw_list:
            if isinstance(entry, str) and entry.strip():
                collected.append(entry.strip())
    raw_single = item.get("mountpoint")
    if isinstance(raw_single, str) and raw_single.strip():
        collected.append(raw_single.strip())
    return tuple(dict.fromkeys(collected))


def _extract_symlinks(item: Mapping[str, Any]) -> tuple[str, ...]:
    collected: list[str] = []
    for key in ("symlinks", "id-link"):
        raw = item.get(key)
        if isinstance(raw, list):
            for entry in raw:
                if isinstance(entry, str) and entry.strip():
                    collected.append(entry.strip())
        elif isinstance(raw, str) and raw.strip():
            collected.append(raw.strip())
    return tuple(dict.fromkeys(collected))


def parse_ip_addr(text: str) -> tuple[NetworkInterface, ...]:
    """Parse ``ip -j addr`` JSON output into a tuple of :class:`NetworkInterface` objects."""
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ParseError(f"Invalid JSON from 'ip -j addr': {exc.msg}") from exc

    if not isinstance(payload, list):
        raise ParseError("Expected 'ip -j addr' JSON root to be a list")

    interfaces: list[NetworkInterface] = []
    for item in payload:
        if not isinstance(item, Mapping):
            raise ParseError("Each 'ip -j addr' entry must be an object")
        raw_name = item.get("ifname")
        if not isinstance(raw_name, str) or not raw_name.strip():
            raise ParseError("Network interface entry is missing 'ifname'")
        operstate_raw = item.get("operstate")
        operstate = (
            operstate_raw.strip()
            if isinstance(operstate_raw, str) and operstate_raw.strip()
            else "UNKNOWN"
        )
        addr_info = item.get("addr_info") or []
        if not isinstance(addr_info, list):
            raise ParseError(f"Interface {raw_name!r} has invalid 'addr_info'")

        ipv4s: list[str] = []
        for addr in addr_info:
            if not isinstance(addr, Mapping):
                continue
            if addr.get("family") != "inet":
                continue
            local = addr.get("local")
            if isinstance(local, str) and local.strip():
                try:
                    normalised = str(IPv4Address(local.strip()))
                except ValueError as exc:
                    raise ParseError(
                        f"Interface {raw_name!r} has invalid IPv4 address {local!r}"
                    ) from exc
                ipv4s.append(normalised)

        interfaces.append(
            NetworkInterface(
                name=raw_name.strip(),
                operstate=operstate,
                ipv4_addresses=tuple(dict.fromkeys(ipv4s)),
            )
        )

    return tuple(interfaces)


def parse_readlink(text: str) -> str:
    """Parse ``readlink --canonicalize-existing`` output into an absolute path."""
    for line in text.splitlines():
        stripped = line.strip()
        if stripped:
            if not stripped.startswith("/"):
                raise ParseError(f"readlink returned a non-absolute path: {stripped!r}")
            return stripped
    raise ParseError("readlink output is empty")


def parse_hostname(text: str) -> str:
    """Parse ``hostname`` output into a non-empty hostname string."""
    for line in text.splitlines():
        stripped = line.strip()
        if stripped:
            return stripped
    raise ParseError("Hostname output is empty")
