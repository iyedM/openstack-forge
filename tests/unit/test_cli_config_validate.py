"""Tests for the ``openstack-forge config validate`` command."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from openstack_forge.cli import main

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_example_configuration_is_valid(capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = main(["config", "validate", str(REPO_ROOT / "examples/cluster.example.yaml")])
    captured = capsys.readouterr()
    assert exit_code == 0
    assert "Configuration is valid" in captured.out
    assert "cluster 'forge-demo': 5 node(s), 3 network(s)" in captured.out
    assert "OpenStack 2025.1 (Epoxy) on Ubuntu 24.04, from cloud-archive:epoxy" in captured.out
    assert captured.err == ""


def test_missing_file(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = main(["config", "validate", str(tmp_path / "absent.yaml")])
    captured = capsys.readouterr()
    assert exit_code == 1
    assert "Configuration file not found" in captured.err
    assert captured.out == ""


def test_default_path_is_cluster_yaml(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    assert main(["config", "validate"]) == 1
    assert "cluster.yaml" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("fixture", "expected"),
    [
        ("malformed_yaml.yaml", "Invalid YAML"),
        ("empty.yaml", "Configuration file is empty"),
        ("duplicate_yaml_key.yaml", "duplicate key 'management'"),
        ("missing_required_fields.yaml", "cluster.openstack_release: required field is missing"),
        ("invalid_ip_address.yaml", "'192.0.2.300' is not a valid IPv4 address"),
        ("unsupported_role.yaml", "nodes[2].role: unsupported value 'storage'"),
        ("duplicate_nodes.yaml", "duplicate hostname 'CMP-01'"),
        ("inconsistent_network.yaml", "refers to undefined network 'external'"),
        ("wrong_types.yaml", "nodes: expected list, got mapping"),
        ("unsupported_release.yaml", "unsupported Ubuntu release '22.04'; supported: 24.04"),
        ("invalid_role_counts.yaml", "requires at least 1 'compute' node; found 0"),
        ("invalid_resources.yaml", "nodes[0].vcpus: must be at least 1, got 0"),
        ("invalid_disks.yaml", "nodes[1].disk: field is not allowed for role 'compute'"),
        ("provider_range_conflicts.yaml", "includes the gateway 192.0.2.1"),
        ("provider_range_invalid.yaml", "start 203.0.113.199 is after end 203.0.113.100"),
    ],
)
def test_invalid_fixtures_fail_with_clear_message(
    fixture_path, capsys: pytest.CaptureFixture[str], fixture: str, expected: str
) -> None:
    exit_code = main(["config", "validate", str(fixture_path(fixture))])
    captured = capsys.readouterr()
    assert exit_code == 1
    assert expected in captured.err
    assert captured.out == ""


def test_validation_errors_are_listed_with_a_count(
    fixture_path, capsys: pytest.CaptureFixture[str]
) -> None:
    main(["config", "validate", str(fixture_path("missing_required_fields.yaml"))])
    err = capsys.readouterr().err
    assert err.startswith("Configuration is invalid:")
    assert "  - nodes[0].management_ip: required field is missing" in err
    assert err.rstrip().endswith("Found 4 error(s).")


def test_installed_command_exit_codes(fixture_path) -> None:
    def run(path: Path) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "openstack_forge.cli", "config", "validate", str(path)],
            capture_output=True,
            text=True,
            check=False,
        )

    valid = run(REPO_ROOT / "examples/cluster.example.yaml")
    assert valid.returncode == 0, valid.stderr
    assert "Configuration is valid" in valid.stdout

    invalid = run(fixture_path("unsupported_role.yaml"))
    assert invalid.returncode == 1
    assert "unsupported value 'storage'" in invalid.stderr


def test_init_command_generates_valid_starter_config(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    output_file = tmp_path / "cluster.yaml"
    assert main(["init", "--output", str(output_file)]) == 0
    assert "Initialized starter cluster configuration" in capsys.readouterr().out
    assert main(["config", "validate", str(output_file)]) == 0
    assert "Configuration is valid" in capsys.readouterr().out
