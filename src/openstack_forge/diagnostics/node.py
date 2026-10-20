"""Local node identification for ``openstack-forge doctor``."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from ipaddress import IPv4Address
from typing import Any

from openstack_forge.diagnostics.errors import NodeIdentificationError


@dataclass(frozen=True)
class IdentifiedNode:
    """A node from the cluster configuration matched to the local machine."""

    node: Mapping[str, Any]
    identified_by: str


def identify_node(
    nodes: Sequence[Mapping[str, Any]],
    *,
    requested_node: str | None = None,
    local_hostname: str | None = None,
    local_ipv4s: Iterable[str] = (),
) -> IdentifiedNode:
    """Identify the local node by ``--node``, local hostname, or local IPv4 address.

    Priority order:
    1. Explicit ``--node HOSTNAME`` (``requested_node``).
    2. Local hostname matching ``nodes[].hostname`` (case-insensitive).
    3. Any local IPv4 address matching ``nodes[].management_ip``.

    Raises :class:`NodeIdentificationError` with an actionable message when no node
    matches or when multiple nodes match.
    """
    if requested_node is not None:
        target = requested_node.strip()
        matches = [node for node in nodes if node["hostname"].lower() == target.lower()]
        if len(matches) == 1:
            return IdentifiedNode(node=matches[0], identified_by="cli")
        if len(matches) > 1:
            matched_names = ", ".join(node["hostname"] for node in matches)
            raise NodeIdentificationError(
                f"Node name {target!r} is ambiguous; matched multiple nodes: {matched_names}."
            )
        declared = ", ".join(node["hostname"] for node in nodes)
        raise NodeIdentificationError(
            f"Node {target!r} was not found in configuration (declared nodes: {declared})."
        )

    if local_hostname is not None and local_hostname.strip():
        candidate = local_hostname.strip().lower()
        short_candidate = candidate.split(".", 1)[0]
        matches = [
            node for node in nodes if node["hostname"].lower() in {candidate, short_candidate}
        ]
        if len(matches) == 1:
            return IdentifiedNode(node=matches[0], identified_by="hostname")
        if len(matches) > 1:
            matched_names = ", ".join(node["hostname"] for node in matches)
            raise NodeIdentificationError(
                f"Local hostname {local_hostname.strip()!r} is ambiguous; matched multiple "
                f"nodes ({matched_names}). Pass --node HOSTNAME to select a node explicitly."
            )

    local_ip_set = _normalise_ipv4_set(local_ipv4s)
    ip_matches = [node for node in nodes if IPv4Address(node["management_ip"]) in local_ip_set]
    if len(ip_matches) == 1:
        return IdentifiedNode(node=ip_matches[0], identified_by="management_ip")
    if len(ip_matches) > 1:
        matched_summary = ", ".join(
            f"{node['hostname']} ({node['management_ip']})" for node in ip_matches
        )
        raise NodeIdentificationError(
            "Local node identification is ambiguous: local IPv4 addresses match multiple "
            f"nodes ({matched_summary}). Pass --node HOSTNAME to select the node explicitly."
        )

    declared_summary = ", ".join(f"{node['hostname']} ({node['management_ip']})" for node in nodes)
    observed_host = local_hostname.strip() if local_hostname and local_hostname.strip() else "none"
    observed_ips = ", ".join(sorted((str(ip) for ip in local_ip_set), key=IPv4Address)) or "none"
    raise NodeIdentificationError(
        f"Could not identify local node: hostname {observed_host!r} and local IPv4 "
        f"addresses ({observed_ips}) do not match any declared node ({declared_summary}). "
        "Pass --node HOSTNAME to select a node explicitly."
    )


def _normalise_ipv4_set(addresses: Iterable[str]) -> set[IPv4Address]:
    result: set[IPv4Address] = set()
    for raw in addresses:
        try:
            result.add(IPv4Address(raw.strip()))
        except ValueError:
            continue
    return result
