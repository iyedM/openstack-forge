"""Basic CLI smoke tests."""

from __future__ import annotations

import subprocess
import sys

import openstack_forge


def test_package_imports() -> None:
    assert openstack_forge.__version__ == "0.1.0"


def test_cli_help() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "openstack_forge.cli", "--help"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0
    assert "OpenStack Forge CLI" in result.stdout


def test_cli_version() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "openstack_forge.cli", "--version"],
        capture_output=True,
        text=True,
        check=False,
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
