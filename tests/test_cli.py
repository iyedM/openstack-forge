"""Basic CLI smoke tests."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import yaml

import openstack_forge

REPO_ROOT = Path(__file__).resolve().parents[1]
SUBPROCESS_ENV = {**os.environ, "PYTHONPATH": str(REPO_ROOT / "src")}


def test_package_imports() -> None:
    assert openstack_forge.__version__ == "0.1.0"


def test_cli_help() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "openstack_forge.cli", "--help"],
        capture_output=True,
        text=True,
        check=False,
        env=SUBPROCESS_ENV,
    )
    assert result.returncode == 0
    assert "OpenStack Forge CLI" in result.stdout


def test_cli_version() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "openstack_forge.cli", "--version"],
        capture_output=True,
        text=True,
        check=False,
        env=SUBPROCESS_ENV,
    )
    assert result.returncode == 0
    assert "openstack-forge" in result.stdout


def test_cli_help_does_not_import_subpackages() -> None:
    code = (
        "import sys; "
        "from openstack_forge.cli import build_parser; "
        "build_parser(); "
        "loaded = [m for m in sys.modules if m.startswith(("
        "'openstack_forge.config', 'openstack_forge.diagnostics', "
        "'openstack_forge.orchestration', 'openstack_forge.execution'))]; "
        "assert not loaded, loaded"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=False,
        env=SUBPROCESS_ENV,
    )
    assert result.returncode == 0, result.stderr


def test_cli_config_and_secrets_without_subcommand_print_subparser_help(capsys) -> None:
    from openstack_forge.cli import main

    rc_config = main(["config"])
    out_config = capsys.readouterr().out
    assert rc_config == 0
    assert "usage: openstack-forge config" in out_config

    rc_secrets = main(["secrets"])
    out_secrets = capsys.readouterr().out
    assert rc_secrets == 0
    assert "usage: openstack-forge secrets" in out_secrets


def test_github_workflow_yaml_files_are_valid() -> None:
    workflows_dir = REPO_ROOT / ".github" / "workflows"
    workflow_files = sorted(workflows_dir.glob("*.yml"))
    assert workflow_files
    for wf in workflow_files:
        parsed = yaml.safe_load(wf.read_text(encoding="utf-8"))
        assert isinstance(parsed, dict), wf
        assert "jobs" in parsed, wf


def test_cli_status_human_and_json(tmp_path: Path, capsys) -> None:
    import json

    from openstack_forge.cli import main
    from openstack_forge.execution import DEFAULT_STATE_TARGET, StateStore, resolve_target

    config_path = str(REPO_ROOT / "examples" / "cluster.example.yaml")
    assert main(["secrets", "init", "--root", str(tmp_path)]) == 0
    capsys.readouterr()

    state_path, _ = resolve_target(tmp_path, DEFAULT_STATE_TARGET)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    store = StateStore(state_path)
    store.mark_done("hosts-file")

    rc = main(
        [
            "status",
            "--config",
            config_path,
            "--node",
            "forge-compute-01",
            "--root",
            str(tmp_path),
        ]
    )
    out = capsys.readouterr().out
    assert rc == 0
    assert "Status for node 'forge-compute-01' (role: compute)" in out
    assert "16/16 present" in out
    assert "hosts-file" in out
    assert "1 done, 0 failed, 4 not_recorded" in out

    rc_json = main(
        [
            "status",
            "--config",
            config_path,
            "--node",
            "forge-compute-01",
            "--root",
            str(tmp_path),
            "--json",
        ]
    )
    payload = json.loads(capsys.readouterr().out)
    assert rc_json == 0
    assert payload["node"] == "forge-compute-01"
    assert payload["secrets"]["present"] == 16
    assert payload["summary"] == {
        "total": 5,
        "done": 1,
        "failed": 0,
        "running": 0,
        "not_recorded": 4,
    }

    store.mark_failed("time-sync-other", "chrony failed")
    rc_fail = main(
        [
            "status",
            "--config",
            config_path,
            "--node",
            "forge-compute-01",
            "--root",
            str(tmp_path),
        ]
    )
    out_fail = capsys.readouterr().out
    assert rc_fail == 1
    assert "error: chrony failed" in out_fail
