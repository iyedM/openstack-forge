"""JSON Schema validation of cluster configuration structure."""

from __future__ import annotations

import copy
import functools
import json
from collections import Counter
from collections.abc import Iterable, Mapping
from importlib import resources
from pathlib import Path
from typing import TYPE_CHECKING, Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError, ValidationError

from openstack_forge.config.errors import ConfigError, ValidationIssue

if TYPE_CHECKING:  # importlib.resources.abc only exists from Python 3.11.
    from importlib.resources.abc import Traversable

# The schema ships as package data so that regular (non-editable) installs can validate.
SCHEMA_PACKAGE = "openstack_forge.config"
SCHEMA_RESOURCE = "schemas/cluster.schema.json"

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


def schema_resource() -> Traversable:
    """Return the bundled cluster schema as an importlib.resources Traversable."""
    package = resources.files(SCHEMA_PACKAGE)
    for part in SCHEMA_RESOURCE.split("/"):
        package = package.joinpath(part)
    return package


def _load_schema_from_source(source: Path | Traversable) -> dict[str, Any]:
    try:
        schema = json.loads(source.read_text(encoding="utf-8"))
    except OSError as exc:
        reason = exc.strerror or exc.__class__.__name__
        raise ConfigError(f"Cannot read configuration schema {source}: {reason}") from exc
    except json.JSONDecodeError as exc:
        raise ConfigError(f"Configuration schema {source} is not valid JSON: {exc}") from exc
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as exc:
        raise ConfigError(f"Configuration schema {source} is invalid: {exc.message}") from exc
    return schema


@functools.lru_cache(maxsize=1)
def _bundled_schema_and_validator() -> tuple[dict[str, Any], Draft202012Validator]:
    schema = _load_schema_from_source(schema_resource())
    validator = Draft202012Validator(schema, format_checker=Draft202012Validator.FORMAT_CHECKER)
    return schema, validator


def load_schema(path: Path | None = None) -> dict[str, Any]:
    """Load the cluster JSON Schema (the bundled one by default) and check that it is valid."""
    if path is None:
        schema, _ = _bundled_schema_and_validator()
        return copy.deepcopy(schema)
    return _load_schema_from_source(path)


def validate_schema(data: Any, schema: Mapping[str, Any]) -> list[ValidationIssue]:
    """Return every structural problem in ``data``, ordered by location."""
    bundled_schema, bundled_validator = _bundled_schema_and_validator()
    if schema == bundled_schema:
        validator = bundled_validator
    else:
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
        message = f"required field is missing{_role_context(error)}"
        return [ValidationIssue(format_location([*path, field]), message) for field in missing]
    if error.validator == "not" and set(error.validator_value) == {"required"}:
        # {"not": {"required": [...]}} is how the schema forbids a field.
        fields = error.validator_value["required"]
        forbidden = [field for field in fields if field in error.instance]
        message = f"field is not allowed{_role_context(error) or ' here'}"
        return [ValidationIssue(format_location([*path, field]), message) for field in forbidden]
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
    if validator == "minimum":
        return f"must be at least {expected}, got {value!r}"
    if validator == "uniqueItems":
        repeated = [item for item, count in Counter(map(repr, value)).items() if count > 1]
        return f"contains duplicate entries: {', '.join(repeated)}"
    if validator == "minLength":
        return "must not be empty" if expected == 1 else f"must be at least {expected} characters"
    if validator == "minItems":
        return f"must contain at least {expected} item(s)"
    if validator == "minProperties":
        return f"must define at least {expected} entry(ies)"
    return error.message


def _role_context(error: ValidationError) -> str:
    """Name the node role when an error comes from a role-conditional (if/then/else) rule."""
    conditional = any(part in ("then", "else") for part in error.absolute_schema_path)
    role = error.instance.get("role") if isinstance(error.instance, Mapping) else None
    return f" for role {role!r}" if conditional and isinstance(role, str) else ""


def _describe_type_error(expected: str | list[str], value: Any) -> str:
    expected_types = [expected] if isinstance(expected, str) else expected
    wanted = " or ".join(_JSON_TO_YAML_TYPE.get(name, name) for name in expected_types)
    actual = _YAML_TYPE_NAMES.get(type(value), type(value).__name__)
    message = f"expected {wanted}, got {actual}"
    if wanted == "string" and actual in {"number", "boolean", "date", "datetime"}:
        message += "; quote the value in YAML so it is read as text"
    return message
