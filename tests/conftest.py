"""Shared pytest fixtures."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from openstack_forge.config import load_config_file, load_schema

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURES_DIR = REPO_ROOT / "tests" / "fixtures"


@pytest.fixture
def fixture_path() -> Callable[[str], Path]:
    """Return a function that resolves a file name inside tests/fixtures."""

    def resolve(name: str) -> Path:
        path = FIXTURES_DIR / name
        assert path.is_file(), f"missing test fixture: {path}"
        return path

    return resolve


@pytest.fixture
def valid_config() -> dict[str, Any]:
    """A fresh, mutable copy of the baseline valid configuration."""
    return load_config_file(FIXTURES_DIR / "valid_cluster.yaml")


@pytest.fixture(scope="session")
def schema() -> dict[str, Any]:
    return load_schema()
