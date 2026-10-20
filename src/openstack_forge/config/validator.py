"""Combine file loading, schema validation and semantic validation."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from openstack_forge.config.errors import ConfigValidationError
from openstack_forge.config.loader import load_config_file
from openstack_forge.config.schema import load_schema, validate_schema
from openstack_forge.config.semantic import validate_semantics


def validate_config(data: Any, schema: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Validate parsed configuration data and return it unchanged if it is valid.

    Semantic checks only run once the structure is schema-valid, because they rely on
    required fields being present and correctly typed.
    """
    issues = validate_schema(data, load_schema() if schema is None else schema)
    if not issues:
        issues = validate_semantics(data)
    if issues:
        raise ConfigValidationError(issues)
    return data


def load_config(path: str | Path) -> dict[str, Any]:
    """Load a YAML cluster configuration file and validate it."""
    return validate_config(load_config_file(path))
