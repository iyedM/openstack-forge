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
    assert "OpenStack Forge CLI scaffold" in result.stdout


def test_cli_version() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "openstack_forge.cli", "--version"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0
    assert "openstack-forge" in result.stdout
