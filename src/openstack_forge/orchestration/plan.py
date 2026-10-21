"""Build and format a read-only deployment plan from a validated cluster configuration.

Planning only reads the configuration and the step catalog. It never runs a command,
opens a connection or changes anything on any host.
"""

from __future__ import annotations

import heapq
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from openstack_forge.config.semantic import ROLE_LIMITS
from openstack_forge.orchestration.catalog import CatalogNote, Step, StepCatalog
from openstack_forge.orchestration.errors import PlanError

# Ties in the dependency order are broken by catalog order, then role (controller
# first, as in ROLE_LIMITS), then the node's position in the configuration.
_ROLE_RANK: dict[str, int] = {role: rank for rank, role in enumerate(ROLE_LIMITS)}


@dataclass(frozen=True)
class Dependency:
    """A step instance that must finish first; ``node`` is None if no node runs the step."""

    step_id: str
    node: str | None

    def describe(self) -> str:
        if self.node is None:
            return f"{self.step_id} (not run by any node in this cluster)"
        return f"{self.step_id} on {self.node}"


@dataclass(frozen=True)
class PlanItem:
    """One catalog step applied to one node, at its position in the cluster-wide order."""

    sequence: int
    node: str
    role: str
    step: Step
    waits_for: tuple[Dependency, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "sequence": self.sequence,
            "node": self.node,
            "role": self.role,
            "id": self.step.id,
            "title": self.step.title,
            "files_modified": list(self.step.files_modified),
            "verification": self.step.verification,
            "doc_url": self.step.doc_url,
            "status": self.step.status,
            "waits_for": [{"step": dep.step_id, "node": dep.node} for dep in self.waits_for],
        }


@dataclass(frozen=True)
class Plan:
    """An ordered, read-only deployment plan for a cluster or a single node."""

    cluster_name: str
    ubuntu_version: str
    openstack_release: str
    node_filter: str | None
    node_count: int
    items: tuple[PlanItem, ...]
    notes: tuple[CatalogNote, ...]

    @property
    def unverified_count(self) -> int:
        return sum(1 for item in self.items if not item.step.verified)

    @property
    def verified_count(self) -> int:
        return len(self.items) - self.unverified_count

    def to_dict(self) -> dict[str, Any]:
        return {
            "cluster": self.cluster_name,
            "ubuntu_version": self.ubuntu_version,
            "openstack_release": self.openstack_release,
            "node": self.node_filter,
            "read_only": True,
            "summary": {
                "steps": len(self.items),
                "nodes": self.node_count,
                "verified": self.verified_count,
                "unverified": self.unverified_count,
            },
            "steps": [item.to_dict() for item in self.items],
            "notes": [
                {"id": note.id, "title": note.title, "text": note.text, "doc_url": note.doc_url}
                for note in self.notes
            ],
        }


def build_plan(
    config: Mapping[str, Any],
    catalog: StepCatalog,
    *,
    node: str | None = None,
) -> Plan:
    """Order every applicable catalog step on every node, optionally keeping one node.

    Sequence numbers always come from the cluster-wide order, so a single-node plan shows
    where its steps fall relative to the steps on other nodes.
    """
    cluster = config["cluster"]
    if cluster["openstack_release"] != catalog.openstack_release:
        raise PlanError(
            f"The step catalog describes OpenStack {catalog.openstack_release}, but the "
            f"configuration declares {cluster['openstack_release']}."
        )
    nodes = config["nodes"]
    selected = _select_node(nodes, node) if node is not None else None

    items = _order_items(nodes, catalog)
    if selected is not None:
        items = [item for item in items if item.node == selected["hostname"]]
    return Plan(
        cluster_name=cluster["name"],
        ubuntu_version=cluster["ubuntu_version"],
        openstack_release=cluster["openstack_release"],
        node_filter=selected["hostname"] if selected is not None else None,
        node_count=1 if selected is not None else len(nodes),
        items=tuple(items),
        notes=catalog.notes,
    )


def _select_node(nodes: Sequence[Mapping[str, Any]], hostname: str) -> Mapping[str, Any]:
    target = hostname.strip().lower()
    for candidate in nodes:
        if candidate["hostname"].lower() == target:
            return candidate
    declared = ", ".join(candidate["hostname"] for candidate in nodes)
    raise PlanError(
        f"Node {hostname.strip()!r} was not found in configuration (declared nodes: {declared})."
    )


def _order_items(nodes: Sequence[Mapping[str, Any]], catalog: StepCatalog) -> list[PlanItem]:
    step_rank = {step.id: rank for rank, step in enumerate(catalog.steps)}
    node_rank = {
        node["hostname"]: (_ROLE_RANK[node["role"]], index) for index, node in enumerate(nodes)
    }
    runners = {
        step.id: [node["hostname"] for node in nodes if node["role"] in step.roles]
        for step in catalog.steps
    }

    # Every (node, step) instance and the instances it waits for.
    waits: dict[tuple[str, str], tuple[Dependency, ...]] = {}
    roles: dict[str, str] = {}
    for node in nodes:
        roles[node["hostname"]] = node["role"]
        for step in catalog.steps:
            if node["role"] in step.roles:
                waits[(node["hostname"], step.id)] = _resolve_dependencies(
                    node, step, catalog, runners
                )

    def sort_key(key: tuple[str, str]) -> tuple[int, tuple[int, int]]:
        hostname, step_id = key
        return step_rank[step_id], node_rank[hostname]

    dependents: dict[tuple[str, str], list[tuple[str, str]]] = {key: [] for key in waits}
    remaining: dict[tuple[str, str], int] = {}
    for key, dependencies in waits.items():
        resolved = [(dep.node, dep.step_id) for dep in dependencies if dep.node is not None]
        remaining[key] = len(resolved)
        for dependency in resolved:
            dependents[dependency].append(key)

    # Kahn's algorithm; the catalog has no cycles, so every instance is reached.
    ready = [(sort_key(key), key) for key, count in remaining.items() if count == 0]
    heapq.heapify(ready)
    ordered: list[PlanItem] = []
    while ready:
        _, key = heapq.heappop(ready)
        hostname, step_id = key
        ordered.append(
            PlanItem(
                sequence=len(ordered) + 1,
                node=hostname,
                role=roles[hostname],
                step=catalog.step(step_id),
                waits_for=waits[key],
            )
        )
        for dependent in dependents[key]:
            remaining[dependent] -= 1
            if remaining[dependent] == 0:
                heapq.heappush(ready, (sort_key(dependent), dependent))
    return ordered


def _resolve_dependencies(
    node: Mapping[str, Any],
    step: Step,
    catalog: StepCatalog,
    runners: Mapping[str, Sequence[str]],
) -> tuple[Dependency, ...]:
    """Wait on this node if it also runs the dependency, otherwise on every node that does.

    Only the dependencies that apply to the node's role are considered: a role-scoped
    ``depends_on`` entry is ignored on nodes whose role it does not list.
    """
    dependencies: list[Dependency] = []
    for dependency_id in step.dependencies_for(node["role"]):
        if node["role"] in catalog.step(dependency_id).roles:
            dependencies.append(Dependency(dependency_id, node["hostname"]))
        elif runners[dependency_id]:
            dependencies += [Dependency(dependency_id, other) for other in runners[dependency_id]]
        else:
            dependencies.append(Dependency(dependency_id, None))
    return tuple(dependencies)


def format_plan_text(plan: Plan) -> str:
    """Format a :class:`Plan` for a terminal."""
    scope = f"node '{plan.node_filter}'" if plan.node_filter else f"cluster '{plan.cluster_name}'"
    lines = [
        f"Deployment plan for {scope}: OpenStack {plan.openstack_release} on Ubuntu "
        f"{plan.ubuntu_version}, {plan.node_count} node(s)",
        "Read-only: no command has been run and nothing has been changed.",
    ]
    if plan.unverified_count:
        lines += [
            "",
            f"WARNING: {plan.unverified_count} of {len(plan.items)} step(s) are UNVERIFIED. "
            "They have not been compared with the official OpenStack "
            f"{plan.openstack_release} documentation; review each linked page first.",
        ]
    for item in plan.items:
        lines += ["", *_format_item(item)]
    if plan.notes:
        lines += ["", "Open decisions (not steps):"]
        for note in plan.notes:
            lines += [
                f"  - {note.id}: {note.title}",
                f"    {note.text}",
                f"    docs: {note.doc_url}",
            ]
    lines += [
        "",
        f"Summary: {len(plan.items)} step(s) on {plan.node_count} node(s); "
        f"{plan.verified_count} verified, {plan.unverified_count} unverified.",
    ]
    return "\n".join(lines)


def _format_item(item: PlanItem) -> list[str]:
    step = item.step
    flag = "" if step.verified else "  [UNVERIFIED]"
    files = ", ".join(step.files_modified) or "(none listed)"
    lines = [
        f"{item.sequence:>3}. {item.node} ({item.role})  {step.id}{flag}",
        f"     {step.title}",
        f"     files:        {files}",
        f"     verification: {step.verification}",
        f"     docs:         {step.doc_url}",
    ]
    lines += [f"     waits for:    {dependency.describe()}" for dependency in item.waits_for]
    return lines


def format_plan_json(plan: Plan) -> str:
    """Serialize a :class:`Plan` to pretty-printed JSON."""
    return json.dumps(plan.to_dict(), indent=2)
