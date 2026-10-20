"""Semantic checks that JSON Schema cannot express.

These functions assume the configuration already passed schema validation, so
required fields exist and have the right types. Rules that the schema can express
(allowed roles, required fields, IPv4 address syntax, which roles take disks) are
intentionally not repeated here.
"""

from __future__ import annotations

from collections.abc import Callable, Hashable, Mapping, Sequence
from dataclasses import dataclass
from ipaddress import IPv4Address, IPv4Network
from itertools import combinations
from typing import Any

from openstack_forge.config import releases
from openstack_forge.config.errors import ValidationIssue

MANAGEMENT_NETWORK = "management"
PROVIDER_NETWORK = "provider"
PROVIDER_RANGES = ("floating_ip_range", "dhcp_range")

# Allowed number of nodes per role as (minimum, maximum); None means no maximum.
# The keys must match the role enum in the schema, which a test enforces.
ROLE_LIMITS: dict[str, tuple[int, int | None]] = {
    "controller": (1, 1),
    "compute": (1, None),
    "block": (0, 1),
    "object": (0, 1),
}

# Node fields that must be unique across the cluster, with their normalisation.
# Host names are compared case-insensitively because DNS is case-insensitive.
_UNIQUE_NODE_FIELDS: dict[str, Callable[[str], Hashable]] = {
    "hostname": str.lower,
    "fqdn": str.lower,
    "management_ip": IPv4Address,
}


@dataclass(frozen=True)
class _Network:
    name: str
    subnet: IPv4Network
    gateway: IPv4Address


@dataclass(frozen=True)
class _AddressRange:
    field: str
    start: IPv4Address
    end: IPv4Address

    def __contains__(self, address: IPv4Address) -> bool:
        return self.start <= address <= self.end

    def __str__(self) -> str:
        return f"{self.start}-{self.end}"

    def overlaps(self, other: _AddressRange) -> bool:
        return self.start <= other.end and other.start <= self.end


def validate_semantics(config: Mapping[str, Any]) -> list[ValidationIssue]:
    """Return cross-field problems in a schema-valid configuration."""
    nodes = config["nodes"]
    issues = _check_release(config["cluster"])
    networks, network_issues = _parse_networks(config["networks"])
    issues += network_issues
    issues += _check_overlapping_networks(networks)
    if PROVIDER_NETWORK in networks:
        issues += _check_provider_ranges(
            config["networks"][PROVIDER_NETWORK], networks[PROVIDER_NETWORK], nodes
        )
    issues += _check_role_counts(nodes)
    issues += _check_duplicate_nodes(nodes)
    issues += _check_fqdn_matches_hostname(nodes)
    issues += _check_node_interfaces(nodes, config["networks"])
    if MANAGEMENT_NETWORK in networks:
        issues += _check_management_ips(nodes, networks[MANAGEMENT_NETWORK])
    return issues


def _check_release(cluster: Mapping[str, Any]) -> list[ValidationIssue]:
    ubuntu, openstack = cluster["ubuntu_version"], cluster["openstack_release"]
    if releases.find_release(ubuntu, openstack):
        return []
    supported = releases.SUPPORTED_RELEASES
    ubuntu_versions = list(dict.fromkeys(r.ubuntu_version for r in supported))
    openstack_releases = list(dict.fromkeys(r.openstack_release for r in supported))
    issues: list[ValidationIssue] = []
    if ubuntu not in ubuntu_versions:
        issues.append(
            ValidationIssue(
                "cluster.ubuntu_version",
                f"unsupported Ubuntu release {ubuntu!r}; supported: {', '.join(ubuntu_versions)}",
            )
        )
    if openstack not in openstack_releases:
        issues.append(
            ValidationIssue(
                "cluster.openstack_release",
                f"unsupported OpenStack release {openstack!r}; "
                f"supported: {', '.join(openstack_releases)}",
            )
        )
    if not issues:
        pairs = ", ".join(f"{r.openstack_release} on Ubuntu {r.ubuntu_version}" for r in supported)
        issues.append(
            ValidationIssue(
                "cluster",
                f"OpenStack {openstack} is not supported on Ubuntu {ubuntu}; "
                f"supported combinations: {pairs}",
            )
        )
    return issues


def _parse_networks(
    networks: Mapping[str, Mapping[str, Any]],
) -> tuple[dict[str, _Network], list[ValidationIssue]]:
    parsed: dict[str, _Network] = {}
    issues: list[ValidationIssue] = []
    for name, network in networks.items():
        try:
            subnet = IPv4Network(network["subnet"], strict=True)
        except ValueError as exc:
            issues.append(
                ValidationIssue(f"networks.{name}.subnet", f"invalid IPv4 network: {exc}")
            )
            continue
        gateway = IPv4Address(network["gateway"])
        problem = _address_problem(gateway, subnet)
        if problem:
            issues.append(ValidationIssue(f"networks.{name}.gateway", f"{gateway} {problem}"))
        parsed[name] = _Network(name, subnet, gateway)
    return parsed, issues


def _check_overlapping_networks(networks: Mapping[str, _Network]) -> list[ValidationIssue]:
    return [
        ValidationIssue(
            f"networks.{second.name}.subnet",
            f"{second.subnet} overlaps network '{first.name}' ({first.subnet})",
        )
        for first, second in combinations(networks.values(), 2)
        if first.subnet.overlaps(second.subnet) and not _may_share_subnet(first, second)
    ]


def _may_share_subnet(first: _Network, second: _Network) -> bool:
    # Single-NIC labs often put the provider network on the management LAN.
    names = {first.name, second.name}
    return names == {PROVIDER_NETWORK, MANAGEMENT_NETWORK} and first.subnet == second.subnet


def _check_provider_ranges(
    provider_config: Mapping[str, Any],
    provider: _Network,
    nodes: Sequence[Mapping[str, Any]],
) -> list[ValidationIssue]:
    ranges = [
        _AddressRange(
            field,
            IPv4Address(provider_config[field]["start"]),
            IPv4Address(provider_config[field]["end"]),
        )
        for field in PROVIDER_RANGES
        if field in provider_config
    ]
    issues: list[ValidationIssue] = []
    for address_range in ranges:
        issues += _check_address_range(address_range, provider, nodes)
    ordered = [r for r in ranges if r.start <= r.end]
    for first, second in combinations(ordered, 2):
        if first.overlaps(second):
            issues.append(
                ValidationIssue(
                    f"networks.{PROVIDER_NETWORK}.{second.field}",
                    f"{second} overlaps {first.field} {first}",
                )
            )
    return issues


def _check_address_range(
    address_range: _AddressRange, network: _Network, nodes: Sequence[Mapping[str, Any]]
) -> list[ValidationIssue]:
    location = f"networks.{network.name}.{address_range.field}"
    if address_range.start > address_range.end:
        return [
            ValidationIssue(
                location, f"start {address_range.start} is after end {address_range.end}"
            )
        ]
    issues: list[ValidationIssue] = []
    for bound in ("start", "end"):
        address = getattr(address_range, bound)
        problem = _address_problem(address, network.subnet)
        if problem:
            issues.append(ValidationIssue(f"{location}.{bound}", f"{address} {problem}"))
    if network.gateway in address_range:
        issues.append(
            ValidationIssue(location, f"{address_range} includes the gateway {network.gateway}")
        )
    for index, node in enumerate(nodes):
        address = IPv4Address(node["management_ip"])
        if address in address_range:
            issues.append(
                ValidationIssue(
                    location, f"{address_range} includes nodes[{index}].management_ip {address}"
                )
            )
    return issues


def _check_role_counts(nodes: Sequence[Mapping[str, Any]]) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    for role, (minimum, maximum) in ROLE_LIMITS.items():
        indices = [index for index, node in enumerate(nodes) if node["role"] == role]
        limit = _describe_limit(role, minimum, maximum)
        if len(indices) < minimum:
            issues.append(ValidationIssue("nodes", f"requires {limit}; found {len(indices)}"))
        if maximum is not None and len(indices) > maximum:
            issues += [
                ValidationIssue(
                    f"nodes[{index}].role",
                    f"{limit} supported; found {len(indices)} (first at nodes[{indices[0]}])",
                )
                for index in indices[maximum:]
            ]
    return issues


def _describe_limit(role: str, minimum: int, maximum: int | None) -> str:
    if maximum is None:
        phrase, count = f"at least {minimum}", minimum
    elif minimum == maximum:
        phrase, count = f"exactly {maximum}", maximum
    elif minimum == 0:
        phrase, count = f"at most {maximum}", maximum
    else:
        phrase, count = f"between {minimum} and {maximum}", maximum
    return f"{phrase} '{role}' node" + ("" if count == 1 else "s")


def _check_duplicate_nodes(nodes: Sequence[Mapping[str, Any]]) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    for field, normalise in _UNIQUE_NODE_FIELDS.items():
        first_seen: dict[Hashable, int] = {}
        for index, node in enumerate(nodes):
            key = normalise(node[field])
            if key in first_seen:
                issues.append(
                    ValidationIssue(
                        f"nodes[{index}].{field}",
                        f"duplicate {field} {node[field]!r}; "
                        f"already used by nodes[{first_seen[key]}]",
                    )
                )
            else:
                first_seen[key] = index
    return issues


def _check_fqdn_matches_hostname(nodes: Sequence[Mapping[str, Any]]) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    for index, node in enumerate(nodes):
        short_name = node["fqdn"].split(".", 1)[0]
        if short_name.lower() != node["hostname"].lower():
            issues.append(
                ValidationIssue(
                    f"nodes[{index}].fqdn",
                    f"{node['fqdn']!r} does not start with hostname {node['hostname']!r}",
                )
            )
    return issues


def _check_node_interfaces(
    nodes: Sequence[Mapping[str, Any]], networks: Mapping[str, Any]
) -> list[ValidationIssue]:
    defined = ", ".join(networks)
    return [
        ValidationIssue(
            f"nodes[{index}].interfaces.{network_name}",
            f"refers to undefined network '{network_name}' (defined networks: {defined})",
        )
        for index, node in enumerate(nodes)
        for network_name in node["interfaces"]
        if network_name not in networks
    ]


def _check_management_ips(
    nodes: Sequence[Mapping[str, Any]], management: _Network
) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    for index, node in enumerate(nodes):
        address = IPv4Address(node["management_ip"])
        problem = _address_problem(address, management.subnet)
        if problem is None and address == management.gateway:
            problem = "is the gateway"
        if problem:
            issues.append(
                ValidationIssue(
                    f"nodes[{index}].management_ip",
                    f"{address} {problem} (the '{management.name}' network)",
                )
            )
    return issues


def _address_problem(address: IPv4Address, subnet: IPv4Network) -> str | None:
    """Explain why ``address`` cannot be used as a host address in ``subnet``, if it cannot."""
    if address not in subnet:
        return f"is outside subnet {subnet}"
    # /31 and /32 networks have no separate network or broadcast address (RFC 3021).
    if subnet.prefixlen < 31 and address == subnet.network_address:
        return f"is the network address of {subnet}"
    if subnet.prefixlen < 31 and address == subnet.broadcast_address:
        return f"is the broadcast address of {subnet}"
    return None
