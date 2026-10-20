"""Tests for JSON Schema (structural) validation."""

from __future__ import annotations

from typing import Any

import pytest

from openstack_forge.config import ValidationIssue, load_config_file, validate_schema
from openstack_forge.config.schema import format_location


def _messages(issues: list[ValidationIssue]) -> list[str]:
    return [str(issue) for issue in issues]


def test_valid_fixture_has_no_schema_issues(valid_config, schema) -> None:
    assert validate_schema(valid_config, schema) == []


def test_missing_required_fields(fixture_path, schema) -> None:
    data = load_config_file(fixture_path("missing_required_fields.yaml"))
    assert _messages(validate_schema(data, schema)) == [
        "cluster.openstack_release: required field is missing",
        "nodes[0].management_ip: required field is missing",
    ]


def test_several_missing_fields_in_one_object_are_each_reported_once(valid_config, schema) -> None:
    del valid_config["cluster"]["name"]
    del valid_config["cluster"]["topology"]
    assert _messages(validate_schema(valid_config, schema)) == [
        "cluster.name: required field is missing",
        "cluster.topology: required field is missing",
    ]


def test_missing_top_level_section(valid_config, schema) -> None:
    del valid_config["networks"]
    assert _messages(validate_schema(valid_config, schema)) == [
        "networks: required field is missing"
    ]


def test_invalid_ip_addresses(fixture_path, schema) -> None:
    data = load_config_file(fixture_path("invalid_ip_address.yaml"))
    assert _messages(validate_schema(data, schema)) == [
        "networks.management.dns[0]: '192.0.2.256' is not a valid IPv4 address",
        "networks.management.gateway: '192.0.2.1.5' is not a valid IPv4 address",
        "nodes[0].management_ip: '192.0.2.300' is not a valid IPv4 address",
    ]


@pytest.mark.parametrize("address", ["192.0.2", "192.0.2.011", "::1", "localhost", ""])
def test_management_ip_must_be_ipv4(valid_config, schema, address: str) -> None:
    valid_config["nodes"][0]["management_ip"] = address
    issues = validate_schema(valid_config, schema)
    assert [issue.location for issue in issues] == ["nodes[0].management_ip"]


def test_unsupported_role(fixture_path, schema) -> None:
    data = load_config_file(fixture_path("unsupported_role.yaml"))
    assert _messages(validate_schema(data, schema)) == [
        "nodes[1].role: unsupported value 'database'; expected one of: controller, compute, storage"
    ]


def test_wrong_types_and_unknown_fields(fixture_path, schema) -> None:
    data = load_config_file(fixture_path("wrong_types.yaml"))
    assert _messages(validate_schema(data, schema)) == [
        "cluster.region: unknown field "
        "(allowed fields: name, ubuntu_version, openstack_release, topology)",
        "cluster.ubuntu_version: expected string, got number; "
        "quote the value in YAML so it is read as text",
        "nodes: expected list, got mapping",
    ]


def test_root_must_be_a_mapping(schema) -> None:
    assert _messages(validate_schema(["not", "a", "mapping"], schema)) == [
        "(root): expected mapping, got list"
    ]


def test_empty_node_list_is_rejected(valid_config, schema) -> None:
    valid_config["nodes"] = []
    assert _messages(validate_schema(valid_config, schema)) == [
        "nodes: must contain at least 1 item(s)"
    ]


def test_node_without_interfaces_is_rejected(valid_config, schema) -> None:
    valid_config["nodes"][0]["interfaces"] = {}
    assert _messages(validate_schema(valid_config, schema)) == [
        "nodes[0].interfaces: must define at least 1 entry(ies)"
    ]


def test_subnet_pattern_error_explains_expected_format(valid_config, schema) -> None:
    valid_config["networks"]["storage"]["subnet"] = "198.51.100.0"
    (issue,) = validate_schema(valid_config, schema)
    assert issue.location == "networks.storage.subnet"
    assert "CIDR notation" in issue.message


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("hostname", "ctl_01"),
        ("hostname", "-ctl"),
        ("hostname", "ctl-01.cluster.test"),
        ("fqdn", "ctl-01"),
        ("fqdn", "ctl-01..cluster.test"),
        ("fqdn", "ctl 01.cluster.test"),
    ],
)
def test_invalid_host_names_are_rejected(valid_config, schema, field: str, value: str) -> None:
    valid_config["nodes"][0][field] = value
    issues = validate_schema(valid_config, schema)
    assert [issue.location for issue in issues] == [f"nodes[0].{field}"]


def test_errors_are_ordered_by_numeric_index(valid_config, schema) -> None:
    nodes: list[dict[str, Any]] = valid_config["nodes"]
    nodes.extend(dict(nodes[1]) for _ in range(9))
    for index in (10, 2):
        nodes[index]["role"] = "bogus"
    locations = [issue.location for issue in validate_schema(valid_config, schema)]
    assert locations == ["nodes[2].role", "nodes[10].role"]


def test_format_location() -> None:
    assert format_location([]) == "(root)"
    assert format_location(["nodes", 3, "interfaces", "management"]) == (
        "nodes[3].interfaces.management"
    )
