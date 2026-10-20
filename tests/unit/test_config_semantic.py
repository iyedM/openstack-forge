"""Tests for semantic (cross-field) validation."""

from __future__ import annotations

import inspect

from openstack_forge.config import (
    ValidationIssue,
    load_config_file,
    validate_schema,
    validate_semantics,
)
from openstack_forge.config import semantic as semantic_module


def _messages(issues: list[ValidationIssue]) -> list[str]:
    return [str(issue) for issue in issues]


def test_valid_fixture_has_no_semantic_issues(valid_config) -> None:
    assert validate_semantics(valid_config) == []


def test_duplicate_nodes(fixture_path, schema) -> None:
    data = load_config_file(fixture_path("duplicate_nodes.yaml"))
    assert validate_schema(data, schema) == []
    assert _messages(validate_semantics(data)) == [
        "nodes[1].hostname: duplicate hostname 'CMP-01'; already used by nodes[0]",
        "nodes[1].fqdn: duplicate fqdn 'cmp-01.cluster.test'; already used by nodes[0]",
        "nodes[1].management_ip: duplicate management_ip '192.0.2.21'; already used by nodes[0]",
    ]


def test_duplicate_reports_point_at_first_occurrence(valid_config) -> None:
    valid_config["nodes"][2]["management_ip"] = valid_config["nodes"][0]["management_ip"]
    assert _messages(validate_semantics(valid_config)) == [
        "nodes[2].management_ip: duplicate management_ip '192.0.2.11'; already used by nodes[0]"
    ]


def test_inconsistent_network_configuration(fixture_path, schema) -> None:
    data = load_config_file(fixture_path("inconsistent_network.yaml"))
    assert validate_schema(data, schema) == []
    assert _messages(validate_semantics(data)) == [
        "networks.management.gateway: 198.51.100.1 is outside subnet 192.0.2.0/24",
        "networks.tenant.subnet: invalid IPv4 network: 203.0.113.5/24 has host bits set",
        "networks.storage.subnet: 192.0.2.128/25 overlaps network 'management' (192.0.2.0/24)",
        "nodes[0].fqdn: 'ctl-02.cluster.test' does not start with hostname 'ctl-01'",
        "nodes[0].interfaces.provider: refers to undefined network 'provider' "
        "(defined networks: management, storage, tenant)",
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


def test_fqdn_comparison_is_case_insensitive(valid_config) -> None:
    valid_config["nodes"][0]["fqdn"] = "CTL-01.Cluster.Test"
    assert validate_semantics(valid_config) == []


def test_semantic_rules_do_not_duplicate_schema_roles(schema) -> None:
    roles = schema["properties"]["nodes"]["items"]["properties"]["role"]["enum"]
    source = inspect.getsource(semantic_module)
    assert not any(f'"{role}"' in source for role in roles)
