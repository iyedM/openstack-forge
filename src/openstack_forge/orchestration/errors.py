"""Exceptions raised by the step catalog and the ``plan`` workflow."""

from __future__ import annotations

from collections.abc import Sequence

from openstack_forge.config.errors import ValidationIssue


class OrchestrationError(Exception):
    """Base class for all step catalog and planning errors."""


class CatalogError(OrchestrationError):
    """The step catalog cannot be read or does not satisfy its schema and rules."""

    def __init__(self, message: str, issues: Sequence[ValidationIssue] = ()) -> None:
        self.issues = tuple(issues)
        details = "".join(f"\n  - {issue}" for issue in self.issues)
        super().__init__(f"{message}{details}")


class PlanError(OrchestrationError):
    """A plan cannot be built for the requested cluster or node."""
