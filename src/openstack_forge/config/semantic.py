"""Semantic checks that JSON Schema cannot express.

These functions assume the configuration already passed schema validation, so
required fields exist and have the right types. Rules that the schema can express
(roles, required fields, IPv4 address syntax) are intentionally not repeated here.
"""

from __future__ import annotations

from collections.abc import Callable, Hashable, Mapping, Sequence
from dataclasses import dataclass
from ipaddress import IPv4Address, IPv4Network
from itertools import combinations
from typing import Any

from openstack_forge.config.errors import ValidationIssue

MANAGEMENT_NETWORK = "management"

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


def validate_semantics(config: Mapping[str, Any]) -> list[ValidationIssue]:
    """Return cross-field problems in a schema-valid configuration."""
    networks, issues = _parse_networks(config["networks"])
    issues += _check_overlapping_networks(networks)
    issues += _check_duplicate_nodes(config["nodes"])
    issues += _check_fqdn_matches_hostname(config["nodes"])
    issues += _check_node_interfaces(config["nodes"], config["networks"])
    if MANAGEMENT_NETWORK in networks:
        issues += _check_management_ips(config["nodes"], networks[MANAGEMENT_NETWORK])
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
        if first.subnet.overlaps(second.subnet)
    ]


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
