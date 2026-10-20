"""Load and validate the bundled deployment step catalog.

The catalog is data shipped as package data next to its JSON Schema, the same way as
the cluster configuration schema. Structural rules live in the schema; the rules it
cannot express (unique ids, existing dependencies, no cycles) are checked here.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import TYPE_CHECKING, Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

from openstack_forge.config.errors import ValidationIssue
from openstack_forge.config.schema import validate_schema
from openstack_forge.orchestration.errors import CatalogError

if TYPE_CHECKING:  # importlib.resources.abc only exists from Python 3.11.
    from importlib.resources.abc import Traversable

CATALOG_PACKAGE = "openstack_forge.orchestration"
CATALOG_RESOURCE = "data/steps.json"
CATALOG_SCHEMA_RESOURCE = "schemas/steps.schema.json"

STATUS_VERIFIED = "verified"
STATUS_UNVERIFIED = "unverified"


@dataclass(frozen=True)
class Step:
    """One deployment step: what it changes and how to check it, never how to run it."""

    id: str
    title: str
    roles: tuple[str, ...]
    depends_on: tuple[str, ...]
    files_modified: tuple[str, ...]
    verification: str
    doc_url: str
    status: str

    @property
    def verified(self) -> bool:
        return self.status == STATUS_VERIFIED


@dataclass(frozen=True)
class CatalogNote:
    """A documented open decision or caveat that is deliberately not a step."""

    id: str
    title: str
    text: str
    doc_url: str


@dataclass(frozen=True)
class StepCatalog:
    """The validated step catalog, with steps in catalog order."""

    openstack_release: str
    steps: tuple[Step, ...]
    notes: tuple[CatalogNote, ...]

    def step(self, step_id: str) -> Step:
        for step in self.steps:
            if step.id == step_id:
                return step
        raise KeyError(step_id)


def package_resource(relative_path: str) -> Traversable:
    """Return a file bundled in the orchestration package."""
    resource = resources.files(CATALOG_PACKAGE)
    for part in relative_path.split("/"):
        resource = resource.joinpath(part)
    return resource


def load_catalog_schema(path: Path | None = None) -> dict[str, Any]:
    """Load the step catalog JSON Schema and check that it is itself a valid schema."""
    schema = _read_json(package_resource(CATALOG_SCHEMA_RESOURCE) if path is None else path)
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as exc:
        raise CatalogError(f"Step catalog schema is invalid: {exc.message}") from exc
    return schema


def load_catalog(path: Path | None = None) -> StepCatalog:
    """Load the bundled step catalog (or ``path``), validate it and return it."""
    source = package_resource(CATALOG_RESOURCE) if path is None else path
    data = _read_json(source)
    issues = validate_catalog(data, load_catalog_schema())
    if issues:
        raise CatalogError(f"Step catalog {source} is invalid:", issues)
    return _build_catalog(data)


def validate_catalog(data: Any, schema: Mapping[str, Any]) -> list[ValidationIssue]:
    """Return schema problems, then (only if structurally valid) semantic problems."""
    issues = validate_schema(data, schema)
    if issues:
        return issues
    steps = data["steps"]
    issues = _check_unique_ids(steps, "steps") + _check_unique_ids(data["notes"], "notes")
    issues += _check_dependencies_exist(steps)
    if not issues:
        issues += _check_no_cycles(steps)
    return issues


def _read_json(source: Path | Traversable) -> Any:
    try:
        return json.loads(source.read_text(encoding="utf-8"))
    except OSError as exc:
        reason = exc.strerror or exc.__class__.__name__
        raise CatalogError(f"Cannot read step catalog file {source}: {reason}") from exc
    except json.JSONDecodeError as exc:
        raise CatalogError(f"Step catalog file {source} is not valid JSON: {exc}") from exc


def _check_unique_ids(entries: Sequence[Mapping[str, Any]], field: str) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    first_seen: dict[str, int] = {}
    for index, entry in enumerate(entries):
        entry_id = entry["id"]
        if entry_id in first_seen:
            issues.append(
                ValidationIssue(
                    f"{field}[{index}].id",
                    f"duplicate id {entry_id!r}; already used by {field}[{first_seen[entry_id]}]",
                )
            )
        else:
            first_seen[entry_id] = index
    return issues


def _check_dependencies_exist(steps: Sequence[Mapping[str, Any]]) -> list[ValidationIssue]:
    known = {step["id"] for step in steps}
    return [
        ValidationIssue(f"steps[{index}].depends_on[{position}]", f"unknown step {dependency!r}")
        for index, step in enumerate(steps)
        for position, dependency in enumerate(step["depends_on"])
        if dependency not in known
    ]


def _check_no_cycles(steps: Sequence[Mapping[str, Any]]) -> list[ValidationIssue]:
    cycle = _find_cycle({step["id"]: step["depends_on"] for step in steps})
    if cycle is None:
        return []
    return [ValidationIssue("steps", f"dependency cycle: {' -> '.join(cycle)}")]


def _find_cycle(graph: Mapping[str, Sequence[str]]) -> list[str] | None:
    """Return one dependency cycle as a path that starts and ends on the same id, if any."""
    done: set[str] = set()
    path: list[str] = []

    def visit(step_id: str) -> list[str] | None:
        path.append(step_id)
        for dependency in graph[step_id]:
            if dependency in path:
                return [*path[path.index(dependency) :], dependency]
            if dependency not in done:
                cycle = visit(dependency)
                if cycle:
                    return cycle
        path.pop()
        done.add(step_id)
        return None

    for step_id in graph:
        if step_id not in done:
            cycle = visit(step_id)
            if cycle:
                return cycle
    return None


def _build_catalog(data: Mapping[str, Any]) -> StepCatalog:
    steps = tuple(
        Step(
            id=raw["id"],
            title=raw["title"],
            roles=tuple(raw["roles"]),
            depends_on=tuple(raw["depends_on"]),
            files_modified=tuple(raw["files_modified"]),
            verification=raw["verification"],
            doc_url=raw["doc_url"],
            status=raw["status"],
        )
        for raw in data["steps"]
    )
    notes = tuple(
        CatalogNote(id=raw["id"], title=raw["title"], text=raw["text"], doc_url=raw["doc_url"])
        for raw in data["notes"]
    )
    return StepCatalog(openstack_release=data["openstack_release"], steps=steps, notes=notes)
