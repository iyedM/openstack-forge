"""Load and validate the bundled deployment step catalog.

The catalog is data shipped as package data next to its JSON Schema, the same way as
the cluster configuration schema. Structural rules live in the schema; the rules it
cannot express (unique ids, existing dependencies, role-scoped dependencies limited to
the step's own roles, no duplicate dependency, no cycles) are checked here.

A ``depends_on`` entry is either a step id, which applies on every role of the step, or
``{"step": <id>, "roles": [...]}``, which applies only on nodes whose role is listed.
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
    depends_on: tuple[str, ...]  # every dependency id, in catalog order
    files_modified: tuple[str, ...]
    verification: str
    doc_url: str
    status: str
    # (dependency id, roles) for each depends_on entry that applies only on those roles.
    dependency_roles: tuple[tuple[str, tuple[str, ...]], ...] = ()

    @property
    def verified(self) -> bool:
        return self.status == STATUS_VERIFIED

    def dependencies_for(self, role: str) -> tuple[str, ...]:
        """The ``depends_on`` ids that apply on a node with ``role``."""
        restricted = dict(self.dependency_roles)
        return tuple(
            dependency
            for dependency in self.depends_on
            if dependency not in restricted or role in restricted[dependency]
        )


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
    issues += _check_dependency_roles(steps)
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


def _dependency_entries(
    step: Mapping[str, Any], index: int
) -> list[tuple[str, str, tuple[str, ...] | None]]:
    """``(location, step id, roles or None)`` for each ``depends_on`` entry of a step."""
    entries: list[tuple[str, str, tuple[str, ...] | None]] = []
    for position, entry in enumerate(step["depends_on"]):
        location = f"steps[{index}].depends_on[{position}]"
        if isinstance(entry, str):
            entries.append((location, entry, None))
        else:
            entries.append((location, entry["step"], tuple(entry["roles"])))
    return entries


def _dependency_ids(step: Mapping[str, Any]) -> list[str]:
    return [dependency for _, dependency, _ in _dependency_entries(step, 0)]


def _check_dependencies_exist(steps: Sequence[Mapping[str, Any]]) -> list[ValidationIssue]:
    known = {step["id"] for step in steps}
    return [
        ValidationIssue(
            location if roles is None else f"{location}.step", f"unknown step {dependency!r}"
        )
        for index, step in enumerate(steps)
        for location, dependency, roles in _dependency_entries(step, index)
        if dependency not in known
    ]


def _check_dependency_roles(steps: Sequence[Mapping[str, Any]]) -> list[ValidationIssue]:
    """Role-scoped entries name only the step's own roles; no dependency is listed twice."""
    issues: list[ValidationIssue] = []
    for index, step in enumerate(steps):
        first_seen: dict[str, str] = {}
        for location, dependency, roles in _dependency_entries(step, index):
            if dependency in first_seen:
                issues.append(
                    ValidationIssue(
                        location,
                        f"duplicate dependency {dependency!r}; already listed at "
                        f"{first_seen[dependency]}",
                    )
                )
            else:
                first_seen[dependency] = location
            for role in roles or ():
                if role not in step["roles"]:
                    issues.append(
                        ValidationIssue(
                            f"{location}.roles",
                            f"role {role!r} does not run this step (its roles: "
                            f"{', '.join(step['roles'])})",
                        )
                    )
    return issues


def _check_no_cycles(steps: Sequence[Mapping[str, Any]]) -> list[ValidationIssue]:
    # Every dependency counts, whatever its roles, so a cycle is refused even if no single
    # role would follow it.
    cycle = _find_cycle({step["id"]: _dependency_ids(step) for step in steps})
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
            depends_on=tuple(_dependency_ids(raw)),
            files_modified=tuple(raw["files_modified"]),
            verification=raw["verification"],
            doc_url=raw["doc_url"],
            status=raw["status"],
            dependency_roles=tuple(
                (dependency, roles)
                for _, dependency, roles in _dependency_entries(raw, 0)
                if roles is not None
            ),
        )
        for raw in data["steps"]
    )
    notes = tuple(
        CatalogNote(id=raw["id"], title=raw["title"], text=raw["text"], doc_url=raw["doc_url"])
        for raw in data["notes"]
    )
    return StepCatalog(openstack_release=data["openstack_release"], steps=steps, notes=notes)
