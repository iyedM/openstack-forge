"""Tests for JSON Schema (structural) validation."""

from __future__ import annotations

from fnmatch import fnmatch
from pathlib import Path
from typing import Any

import pytest

from openstack_forge.config import ValidationIssue, load_config_file, load_schema, validate_schema
from openstack_forge.config.schema import SCHEMA_PACKAGE, SCHEMA_RESOURCE, format_location

REPO_ROOT = Path(__file__).resolve().parents[2]


def _messages(issues: list[ValidationIssue]) -> list[str]:
    return [str(issue) for issue in issues]


def test_valid_fixture_has_no_schema_issues(valid_config, schema) -> None:
    assert validate_schema(valid_config, schema) == []


def test_missing_required_fields(fixture_path, schema) -> None:
    data = load_config_file(fixture_path("missing_required_fields.yaml"))
    assert _messages(validate_schema(data, schema)) == [
        "cluster.openstack_release: required field is missing",
        "networks.provider.floating_ip_range: required field is missing",
        "nodes[0].management_ip: required field is missing",
        "nodes[1].ram_gb: required field is missing",
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


def test_provider_network_is_required(valid_config, schema) -> None:
    del valid_config["networks"]["provider"]
    assert _messages(validate_schema(valid_config, schema)) == [
        "networks.provider: required field is missing"
    ]


def test_ip_ranges_are_only_allowed_on_the_provider_network(valid_config, schema) -> None:
    valid_config["networks"]["storage"]["floating_ip_range"] = {
        "start": "198.51.100.10",
        "end": "198.51.100.20",
    }
    (issue,) = validate_schema(valid_config, schema)
    assert issue.location == "networks.storage.floating_ip_range"
    assert issue.message.startswith("unknown field")


def test_ip_range_requires_start_and_end(valid_config, schema) -> None:
    del valid_config["networks"]["provider"]["dhcp_range"]["end"]
    assert _messages(validate_schema(valid_config, schema)) == [
        "networks.provider.dhcp_range.end: required field is missing"
    ]


def test_invalid_ip_addresses(fixture_path, schema) -> None:
    data = load_config_file(fixture_path("invalid_ip_address.yaml"))
    assert _messages(validate_schema(data, schema)) == [
        "networks.management.dns[0]: '192.0.2.256' is not a valid IPv4 address",
        "networks.management.gateway: '192.0.2.1.5' is not a valid IPv4 address",
        "networks.provider.floating_ip_range.start: '203.0.113.x' is not a valid IPv4 address",
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
        "nodes[2].role: unsupported value 'storage'; "
        "expected one of: controller, compute, block, object"
    ]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("ubuntu_version", "24.04.1"),
        ("ubuntu_version", "noble"),
        ("openstack_release", "epoxy"),
        ("openstack_release", "2025.3"),
    ],
)
def test_release_fields_must_be_version_numbers(
    valid_config, schema, field: str, value: str
) -> None:
    valid_config["cluster"][field] = value
    (issue,) = validate_schema(valid_config, schema)
    assert issue.location == f"cluster.{field}"
    assert "has an invalid format" in issue.message


def test_wrong_types_and_unknown_fields(fixture_path, schema) -> None:
    data = load_config_file(fixture_path("wrong_types.yaml"))
    assert _messages(validate_schema(data, schema)) == [
        "cluster.region: unknown field "
        "(allowed fields: name, ubuntu_version, openstack_release, topology)",
        "cluster.ubuntu_version: expected string, got number; "
        "quote the value in YAML so it is read as text",
        "nodes: expected list, got mapping",
    ]


def test_invalid_resources(fixture_path, schema) -> None:
    data = load_config_file(fixture_path("invalid_resources.yaml"))
    assert _messages(validate_schema(data, schema)) == [
        "nodes[0].vcpus: must be at least 1, got 0",
        "nodes[1].ram_gb: expected integer, got string",
        "nodes[2].vcpus: expected integer, got number",
    ]


@pytest.mark.parametrize("field", ["vcpus", "ram_gb"])
def test_resources_reject_booleans(valid_config, schema, field: str) -> None:
    valid_config["nodes"][0][field] = True
    assert _messages(validate_schema(valid_config, schema)) == [
        f"nodes[0].{field}: expected integer, got boolean"
    ]


def test_invalid_disks(fixture_path, schema) -> None:
    data = load_config_file(fixture_path("invalid_disks.yaml"))
    assert _messages(validate_schema(data, schema)) == [
        "nodes[0].disks: field is not allowed for role 'controller'",
        "nodes[1].disk: field is not allowed for role 'compute'",
        "nodes[2].disk: required field is missing for role 'block'",
        "nodes[3].disks: contains duplicate entries: '/dev/sdb'",
        "nodes[3].disks[1]: 'sdc' has an invalid format; expected an absolute block device "
        'path under /dev such as "/dev/sdb" or "/dev/disk/by-id/..."',
    ]


def test_object_node_requires_disks(valid_config, schema) -> None:
    del valid_config["nodes"][3]["disks"]
    assert _messages(validate_schema(valid_config, schema)) == [
        "nodes[3].disks: required field is missing for role 'object'"
    ]


def test_object_node_requires_at_least_one_disk(valid_config, schema) -> None:
    valid_config["nodes"][3]["disks"] = []
    assert _messages(validate_schema(valid_config, schema)) == [
        "nodes[3].disks: must contain at least 1 item(s)"
    ]


def test_block_node_takes_a_single_disk_not_a_list(valid_config, schema) -> None:
    valid_config["nodes"][2]["disk"] = ["/dev/sdb", "/dev/sdc"]
    assert _messages(validate_schema(valid_config, schema)) == [
        "nodes[2].disk: expected string, got list"
    ]


@pytest.mark.parametrize(
    "path",
    ["/dev/sdb", "/dev/nvme0n1", "/dev/disk/by-id/wwn-0x5000c500a1b2c3d4", "/dev/mapper/vg-lv"],
)
def test_valid_device_paths(valid_config, schema, path: str) -> None:
    valid_config["nodes"][2]["disk"] = path
    assert validate_schema(valid_config, schema) == []


@pytest.mark.parametrize(
    "path", ["sdb", "/dev/", "/dev/../etc/passwd", "/dev/disk/../sdb", "/dev/sd b", "/tmp/sdb"]
)
def test_invalid_device_paths(valid_config, schema, path: str) -> None:
    valid_config["nodes"][2]["disk"] = path
    issues = validate_schema(valid_config, schema)
    assert [issue.location for issue in issues] == ["nodes[2].disk"]


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
    nodes.extend(dict(nodes[1]) for _ in range(9))  # indices 4-12 are compute nodes
    for index in (10, 4):
        nodes[index]["role"] = "bogus"
    locations = [issue.location for issue in validate_schema(valid_config, schema)]
    assert locations == ["nodes[4].role", "nodes[10].role"]


def test_format_location() -> None:
    assert format_location([]) == "(root)"
    assert format_location(["nodes", 3, "interfaces", "management"]) == (
        "nodes[3].interfaces.management"
    )


def test_schema_is_loaded_from_the_package() -> None:
    package_dir = REPO_ROOT / "src" / Path(*SCHEMA_PACKAGE.split("."))
    bundled = package_dir / SCHEMA_RESOURCE
    assert bundled.is_file()
    assert load_schema() == load_schema(bundled)


def test_schema_is_declared_as_package_data() -> None:
    tomllib = pytest.importorskip("tomllib")  # Python 3.11+
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    patterns = pyproject["tool"]["setuptools"]["package-data"][SCHEMA_PACKAGE]
    assert any(fnmatch(SCHEMA_RESOURCE, pattern) for pattern in patterns)
