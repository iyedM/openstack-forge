"""Tests for the ``openstack-forge plan`` command."""

from __future__ import annotations

import json
import os
import socket
import subprocess
from pathlib import Path

import pytest

from openstack_forge.cli import main

REPO_ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = REPO_ROOT / "examples" / "cluster.example.yaml"


@pytest.fixture
def forbid_commands_and_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail the test if anything tries to start a process or open a connection."""

    def forbidden(*args, **kwargs):
        raise AssertionError("plan must not run commands or use the network")

    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(os, "system", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)


def test_plan_for_the_example_cluster(
    forbid_commands_and_network, capsys: pytest.CaptureFixture[str]
) -> None:
    exit_code = main(["plan", "--config", str(EXAMPLE)])
    captured = capsys.readouterr()
    assert exit_code == 0
    assert captured.err == ""
    out = captured.out
    assert out.startswith(
        "Deployment plan for cluster 'forge-demo': OpenStack 2025.1 on Ubuntu 24.04, 5 node(s)"
    )
    assert "Read-only: no command has been run and nothing has been changed." in out
    assert "WARNING: 28 of 28 step(s) are UNVERIFIED" in out
    assert out.count("[UNVERIFIED]") == 28
    assert "Summary: 28 step(s) on 5 node(s); 0 verified, 28 unverified." in out
    assert "Open decisions (not steps):" in out


def test_plan_for_one_node(
    forbid_commands_and_network, fixture_path, capsys: pytest.CaptureFixture[str]
) -> None:
    exit_code = main(
        ["plan", "--config", str(fixture_path("valid_cluster.yaml")), "--node", "cmp-01"]
    )
    out = capsys.readouterr().out
    assert exit_code == 0
    assert out.startswith("Deployment plan for node 'cmp-01'")
    assert "ctl-01 (controller)" not in out
    assert "Summary: 5 step(s) on 1 node(s); 0 verified, 5 unverified." in out


def test_plan_json(
    forbid_commands_and_network, fixture_path, capsys: pytest.CaptureFixture[str]
) -> None:
    exit_code = main(
        ["plan", "--config", str(fixture_path("valid_cluster.yaml")), "--node", "ctl-01", "--json"]
    )
    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert payload["node"] == "ctl-01"
    assert payload["summary"] == {"steps": 12, "nodes": 1, "verified": 0, "unverified": 12}
    assert {step["status"] for step in payload["steps"]} == {"unverified"}


def test_invalid_configuration_exits_one(
    forbid_commands_and_network, fixture_path, capsys: pytest.CaptureFixture[str]
) -> None:
    exit_code = main(["plan", "--config", str(fixture_path("invalid_role_counts.yaml"))])
    captured = capsys.readouterr()
    assert exit_code == 1
    assert captured.out == ""
    assert "Configuration is invalid" in captured.err
    assert "requires at least 1 'compute' node; found 0" in captured.err


def test_missing_configuration_exits_one(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["plan", "--config", str(tmp_path / "absent.yaml")]) == 1
    assert "Configuration file not found" in capsys.readouterr().err


def test_default_config_path_is_cluster_yaml(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    assert main(["plan"]) == 1
    assert "cluster.yaml" in capsys.readouterr().err


def test_unknown_node_exits_one(fixture_path, capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = main(["plan", "--config", str(fixture_path("valid_cluster.yaml")), "--node", "x"])
    captured = capsys.readouterr()
    assert exit_code == 1
    assert captured.out == ""
    assert "Node 'x' was not found in configuration" in captured.err


def test_plan_help_lists_options(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as excinfo:
        main(["plan", "--help"])
    assert excinfo.value.code == 0
    out = capsys.readouterr().out
    for option in ("--config", "--node", "--json"):
        assert option in out
