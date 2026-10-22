"""What a step would do, for dry runs: file diffs, planned commands and notes.

Building a preview never runs a command and never writes a file.
"""

from __future__ import annotations

import shlex
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from openstack_forge.execution.files import FileChange

if TYPE_CHECKING:  # avoid importing the command layer from file-only steps
    from openstack_forge.execution.commands import CommandRegistry


@dataclass(frozen=True)
class PlannedCommand:
    """An allowlisted command a step would run, with its parameters."""

    command_id: str
    params: Mapping[str, str] = field(default_factory=dict)

    def __repr__(self) -> str:
        from openstack_forge.execution.commands import DEFAULT_REGISTRY
        from openstack_forge.execution.redaction import MASK, redact_data

        params_copy = dict(self.params)
        if self.command_id in DEFAULT_REGISTRY.ids():
            for key in DEFAULT_REGISTRY.get(self.command_id).sensitive_params:
                if key in params_copy:
                    params_copy[key] = MASK
        return (
            f"PlannedCommand(command_id={self.command_id!r}, params={redact_data(params_copy)!r})"
        )

    def argv(self, registry: CommandRegistry) -> tuple[str, ...]:
        """The exact, validated argv (redacted), without running anything."""
        return registry.get(self.command_id).build_shown_argv(self.params)

    def describe(self, registry: CommandRegistry) -> str:
        return shlex.join(self.argv(registry))


@dataclass(frozen=True)
class StepPreview:
    """Dry-run description of a step.

    ``commands`` are the mutating commands ``apply()`` would run, in order;
    ``read_only_commands`` are those ``check()``, ``verify()`` and ``apply()`` (for its
    preconditions) may run.
    """

    file_changes: tuple[FileChange, ...] = ()
    commands: tuple[PlannedCommand, ...] = ()
    read_only_commands: tuple[PlannedCommand, ...] = ()
    notes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        from openstack_forge.execution.redaction import redact_text

        object.__setattr__(self, "notes", tuple(redact_text(note) for note in self.notes))

    @property
    def files_changed(self) -> bool:
        return any(change.changed for change in self.file_changes)
