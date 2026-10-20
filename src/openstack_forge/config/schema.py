"""JSON Schema validation of cluster configuration structure."""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError, ValidationError

from openstack_forge.config.errors import ConfigError, ValidationIssue

# Resolves to <repo>/schemas/cluster.schema.json from a source checkout or editable install.
DEFAULT_SCHEMA_PATH = Path(__file__).resolve().parents[3] / "schemas" / "cluster.schema.json"

# Type names as a YAML author sees them, keyed by the Python type PyYAML produces.
_YAML_TYPE_NAMES: dict[type, str] = {
    dict: "mapping",
    list: "list",
    str: "string",
    bool: "boolean",
    int: "number",
    float: "number",
    type(None): "null",
}
_JSON_TO_YAML_TYPE = {"object": "mapping", "array": "list"}


def load_schema(path: Path = DEFAULT_SCHEMA_PATH) -> dict[str, Any]:
    """Load the cluster JSON Schema and check that it is itself a valid schema."""
    try:
        schema = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        reason = exc.strerror or exc.__class__.__name__
        raise ConfigError(f"Cannot read configuration schema {path}: {reason}") from exc
    except json.JSONDecodeError as exc:
        raise ConfigError(f"Configuration schema {path} is not valid JSON: {exc}") from exc
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as exc:
        raise ConfigError(f"Configuration schema {path} is invalid: {exc.message}") from exc
    return schema


def validate_schema(data: Any, schema: Mapping[str, Any]) -> list[ValidationIssue]:
    """Return every structural problem in ``data``, ordered by location."""
    validator = Draft202012Validator(schema, format_checker=Draft202012Validator.FORMAT_CHECKER)
    errors = sorted(validator.iter_errors(data), key=lambda e: _sort_key(e.absolute_path))
    issues: list[ValidationIssue] = []
    for error in errors:
        issues.extend(_issues_from_error(error))
    # A "required" error is emitted once per missing field but we report all missing
    # fields for each, so drop the repeats while keeping order.
    return list(dict.fromkeys(issues))


def format_location(path: Iterable[str | int]) -> str:
    """Render a JSON path such as ("nodes", 1, "role") as ``nodes[1].role``."""
    location = ""
    for part in path:
        if isinstance(part, int):
            location += f"[{part}]"
        else:
            location += f".{part}" if location else str(part)
    return location or "(root)"


def _sort_key(path: Iterable[str | int]) -> tuple[tuple[int, Any], ...]:
    return tuple((0, part) if isinstance(part, int) else (1, part) for part in path)


def _issues_from_error(error: ValidationError) -> list[ValidationIssue]:
    path = list(error.absolute_path)
    location = format_location(path)

    if error.validator == "required":
        missing = [field for field in error.validator_value if field not in error.instance]
        return [
            ValidationIssue(format_location([*path, field]), "required field is missing")
            for field in missing
        ]
    if error.validator == "additionalProperties" and error.validator_value is False:
        allowed = list(error.schema.get("properties", {}))
        unknown = [key for key in error.instance if key not in allowed]
        hint = f" (allowed fields: {', '.join(allowed)})" if allowed else ""
        return [
            ValidationIssue(format_location([*path, key]), f"unknown field{hint}")
            for key in unknown
        ]
    return [ValidationIssue(location, _describe(error))]


def _describe(error: ValidationError) -> str:
    validator, expected, value = error.validator, error.validator_value, error.instance

    if validator == "enum":
        choices = ", ".join(str(choice) for choice in expected)
        return f"unsupported value {value!r}; expected one of: {choices}"
    if validator == "format":
        if expected == "ipv4":
            return f"{value!r} is not a valid IPv4 address"
        return f"{value!r} is not a valid {expected}"
    if validator == "pattern":
        hint = error.schema.get("description")
        return f"{value!r} has an invalid format" + (f"; expected {hint}" if hint else "")
    if validator == "type":
        return _describe_type_error(expected, value)
    if validator == "minLength":
        return "must not be empty" if expected == 1 else f"must be at least {expected} characters"
    if validator == "minItems":
        return f"must contain at least {expected} item(s)"
    if validator == "minProperties":
        return f"must define at least {expected} entry(ies)"
    return error.message


def _describe_type_error(expected: str | list[str], value: Any) -> str:
    expected_types = [expected] if isinstance(expected, str) else expected
    wanted = " or ".join(_JSON_TO_YAML_TYPE.get(name, name) for name in expected_types)
    actual = _YAML_TYPE_NAMES.get(type(value), type(value).__name__)
    message = f"expected {wanted}, got {actual}"
    if wanted == "string" and actual in {"number", "boolean", "date", "datetime"}:
        message += "; quote the value in YAML so it is read as text"
    return message
