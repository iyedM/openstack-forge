"""Tests for semantic (cross-field) validation."""

from __future__ import annotations

import copy
from typing import Any

import pytest

from openstack_forge.config import (
    SUPPORTED_RELEASES,
    ValidationIssue,
    find_release,
    load_config_file,
    releases,
    validate_schema,
    validate_semantics,
)
from openstack_forge.config import semantic as semantic_module


def _messages(issues: list[ValidationIssue]) -> list[str]:
    return [str(issue) for issue in issues]


def _semantic_messages(fixture_path, schema, name: str) -> list[str]:
    """Load a fixture that must be schema-valid and return its semantic errors."""
    data = load_config_file(fixture_path(name))
    assert validate_schema(data, schema) == []
    return _messages(validate_semantics(data))


def _add_node(config: dict[str, Any], template_index: int, name: str, address: str) -> None:
    node = copy.deepcopy(config["nodes"][template_index])
    node.update(hostname=name, fqdn=f"{name}.cluster.test", management_ip=address)
    config["nodes"].append(node)


def test_valid_fixture_has_no_semantic_issues(valid_config) -> None:
    assert validate_semantics(valid_config) == []


# --- releases -----------------------------------------------------------------


def test_unsupported_release(fixture_path, schema) -> None:
    assert _semantic_messages(fixture_path, schema, "unsupported_release.yaml") == [
        "cluster.ubuntu_version: unsupported Ubuntu release '22.04'; supported: 24.04",
        "cluster.openstack_release: unsupported OpenStack release '2024.2'; supported: 2025.1",
    ]


def test_only_the_unsupported_half_of_the_pair_is_reported(valid_config) -> None:
    valid_config["cluster"]["openstack_release"] = "2024.1"
    assert _messages(validate_semantics(valid_config)) == [
        "cluster.openstack_release: unsupported OpenStack release '2024.1'; supported: 2025.1"
    ]


def test_known_versions_in_an_unsupported_combination(
    valid_config, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Simulates the one-line table change that adds a future release.
    future = releases.SupportedRelease("26.04", "2026.1", "future")
    monkeypatch.setattr(releases, "SUPPORTED_RELEASES", (*SUPPORTED_RELEASES, future))
    valid_config["cluster"]["openstack_release"] = "2026.1"
    assert _messages(validate_semantics(valid_config)) == [
        "cluster: OpenStack 2026.1 is not supported on Ubuntu 24.04; supported combinations: "
        "2025.1 on Ubuntu 24.04, 2026.1 on Ubuntu 26.04"
    ]


def test_epoxy_maps_to_cloud_archive() -> None:
    release = find_release("24.04", "2025.1")
    assert release is not None
    assert release.codename == "epoxy"
    assert release.cloud_archive == "cloud-archive:epoxy"
    assert find_release("24.04", "2024.2") is None


# --- roles ----------------------------------------------------------------------


def test_role_limits_cover_exactly_the_schema_roles(schema) -> None:
    roles = schema["$defs"]["node"]["properties"]["role"]["enum"]
    assert set(semantic_module.ROLE_LIMITS) == set(roles)


def test_invalid_role_counts(fixture_path, schema) -> None:
    assert _semantic_messages(fixture_path, schema, "invalid_role_counts.yaml") == [
        "nodes[1].role: exactly 1 'controller' node supported; found 2 (first at nodes[0])",
        "nodes: requires at least 1 'compute' node; found 0",
        "nodes[3].role: at most 1 'block' node supported; found 2 (first at nodes[2])",
    ]


def test_controller_is_required(valid_config) -> None:
    valid_config["nodes"][0]["role"] = "compute"
    assert _messages(validate_semantics(valid_config)) == [
        "nodes: requires exactly 1 'controller' node; found 0"
    ]


def test_many_compute_nodes_are_allowed(valid_config) -> None:
    for offset in range(5):
        _add_node(valid_config, 1, f"cmp-{offset + 2:02d}", f"192.0.2.{22 + offset}")
    assert validate_semantics(valid_config) == []


def test_second_object_node_is_rejected(valid_config) -> None:
    _add_node(valid_config, 3, "obj-02", "192.0.2.33")
    assert _messages(validate_semantics(valid_config)) == [
        "nodes[4].role: at most 1 'object' node supported; found 2 (first at nodes[3])"
    ]


def test_block_and_object_nodes_are_optional(valid_config) -> None:
    del valid_config["nodes"][2:]
    assert validate_semantics(valid_config) == []


# --- duplicate and inconsistent nodes ---------------------------------------------


def test_duplicate_nodes(fixture_path, schema) -> None:
    assert _semantic_messages(fixture_path, schema, "duplicate_nodes.yaml") == [
        "nodes[2].hostname: duplicate hostname 'CMP-01'; already used by nodes[1]",
        "nodes[2].fqdn: duplicate fqdn 'cmp-01.cluster.test'; already used by nodes[1]",
        "nodes[2].management_ip: duplicate management_ip '192.0.2.21'; already used by nodes[1]",
    ]


def test_duplicate_reports_point_at_first_occurrence(valid_config) -> None:
    valid_config["nodes"][2]["management_ip"] = valid_config["nodes"][0]["management_ip"]
    assert _messages(validate_semantics(valid_config)) == [
        "nodes[2].management_ip: duplicate management_ip '192.0.2.11'; already used by nodes[0]"
    ]


def test_fqdn_comparison_is_case_insensitive(valid_config) -> None:
    valid_config["nodes"][0]["fqdn"] = "CTL-01.Cluster.Test"
    assert validate_semantics(valid_config) == []


# --- networks -------------------------------------------------------------------


def test_inconsistent_network_configuration(fixture_path, schema) -> None:
    assert _semantic_messages(fixture_path, schema, "inconsistent_network.yaml") == [
        "networks.management.gateway: 198.51.100.1 is outside subnet 192.0.2.0/24",
        "networks.tenant.subnet: invalid IPv4 network: 198.51.100.5/24 has host bits set",
        "networks.storage.subnet: 192.0.2.128/25 overlaps network 'management' (192.0.2.0/24)",
        "nodes[0].fqdn: 'ctl-02.cluster.test' does not start with hostname 'ctl-01'",
        "nodes[0].interfaces.external: refers to undefined network 'external' "
        "(defined networks: management, storage, tenant, provider)",
        "nodes[0].management_ip: 203.0.113.10 is outside subnet 192.0.2.0/24 "
        "(the 'management' network)",
    ]


def test_subnet_with_invalid_octet(valid_config) -> None:
    valid_config["networks"]["storage"]["subnet"] = "198.51.300.0/24"
    (issue,) = validate_semantics(valid_config)
    assert issue.location == "networks.storage.subnet"
    assert "invalid IPv4 network" in issue.message


def test_gateway_cannot_be_network_or_broadcast_address(valid_config) -> None:
    valid_config["networks"]["management"]["gateway"] = "192.0.2.0"
    valid_config["networks"]["storage"]["gateway"] = "198.51.100.255"
    assert _messages(validate_semantics(valid_config)) == [
        "networks.management.gateway: 192.0.2.0 is the network address of 192.0.2.0/24",
        "networks.storage.gateway: 198.51.100.255 is the broadcast address of 198.51.100.0/24",
    ]


def test_point_to_point_subnet_allows_every_address(valid_config) -> None:
    valid_config["networks"]["storage"].update(subnet="198.51.100.0/31", gateway="198.51.100.0")
    assert validate_semantics(valid_config) == []


def test_management_ip_cannot_be_the_gateway(valid_config) -> None:
    valid_config["nodes"][1]["management_ip"] = "192.0.2.1"
    assert _messages(validate_semantics(valid_config)) == [
        "nodes[1].management_ip: 192.0.2.1 is the gateway (the 'management' network)"
    ]


def test_management_ip_is_not_checked_without_management_network(valid_config) -> None:
    valid_config["networks"]["mgmt"] = valid_config["networks"].pop("management")
    for index, node in enumerate(valid_config["nodes"]):
        node["interfaces"]["mgmt"] = node["interfaces"].pop("management")
        node["management_ip"] = f"203.0.113.{50 + index}"
    assert validate_semantics(valid_config) == []


# --- provider network ------------------------------------------------------------


def test_provider_may_share_the_management_subnet(valid_config) -> None:
    valid_config["networks"]["provider"].update(
        subnet="192.0.2.0/24",
        gateway="192.0.2.1",
        floating_ip_range={"start": "192.0.2.100", "end": "192.0.2.199"},
        dhcp_range={"start": "192.0.2.200", "end": "192.0.2.250"},
    )
    assert validate_semantics(valid_config) == []


def test_provider_may_not_partially_overlap_management(valid_config) -> None:
    valid_config["networks"]["provider"].update(
        subnet="192.0.2.128/25",
        gateway="192.0.2.129",
        floating_ip_range={"start": "192.0.2.150", "end": "192.0.2.199"},
    )
    del valid_config["networks"]["provider"]["dhcp_range"]
    assert _messages(validate_semantics(valid_config)) == [
        "networks.provider.subnet: 192.0.2.128/25 overlaps network 'management' (192.0.2.0/24)"
    ]


def test_provider_range_conflicts(fixture_path, schema) -> None:
    assert _semantic_messages(fixture_path, schema, "provider_range_conflicts.yaml") == [
        "networks.provider.floating_ip_range: 192.0.2.1-192.0.2.30 includes the gateway 192.0.2.1",
        "networks.provider.floating_ip_range: 192.0.2.1-192.0.2.30 "
        "includes nodes[0].management_ip 192.0.2.11",
        "networks.provider.floating_ip_range: 192.0.2.1-192.0.2.30 "
        "includes nodes[1].management_ip 192.0.2.21",
        "networks.provider.dhcp_range: 192.0.2.25-192.0.2.60 "
        "overlaps floating_ip_range 192.0.2.1-192.0.2.30",
    ]


def test_provider_range_bounds(fixture_path, schema) -> None:
    assert _semantic_messages(fixture_path, schema, "provider_range_invalid.yaml") == [
        "networks.provider.floating_ip_range: start 203.0.113.199 is after end 203.0.113.100",
        "networks.provider.dhcp_range.end: 203.0.114.10 is outside subnet 203.0.113.0/24",
    ]


def test_floating_range_cannot_include_network_or_broadcast_address(valid_config) -> None:
    del valid_config["networks"]["provider"]["dhcp_range"]
    valid_config["networks"]["provider"]["floating_ip_range"] = {
        "start": "203.0.113.0",
        "end": "203.0.113.255",
    }
    assert _messages(validate_semantics(valid_config)) == [
        "networks.provider.floating_ip_range.start: 203.0.113.0 is the network address of "
        "203.0.113.0/24",
        "networks.provider.floating_ip_range.end: 203.0.113.255 is the broadcast address of "
        "203.0.113.0/24",
        "networks.provider.floating_ip_range: 203.0.113.0-203.0.113.255 includes the gateway "
        "203.0.113.1",
    ]


def test_single_address_floating_range_is_allowed(valid_config) -> None:
    valid_config["networks"]["provider"]["floating_ip_range"] = {
        "start": "203.0.113.150",
        "end": "203.0.113.150",
    }
    assert validate_semantics(valid_config) == []


def test_adjacent_ranges_do_not_overlap(valid_config) -> None:
    valid_config["networks"]["provider"]["dhcp_range"]["start"] = "203.0.113.200"
    valid_config["networks"]["provider"]["floating_ip_range"]["end"] = "203.0.113.199"
    assert validate_semantics(valid_config) == []


def test_dhcp_range_is_optional(valid_config) -> None:
    del valid_config["networks"]["provider"]["dhcp_range"]
    assert validate_semantics(valid_config) == []
