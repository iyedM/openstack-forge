"""Exceptions raised by host diagnostics and the ``doctor`` workflow."""

from __future__ import annotations

from collections.abc import Sequence


class DoctorError(Exception):
    """Base class for all diagnostics and ``doctor`` errors."""


class CommandError(DoctorError):
    """A local command could not be executed or failed unexpectedly."""


class CommandNotFoundError(CommandError):
    """The requested command binary does not exist on ``PATH``."""

    def __init__(self, argv: Sequence[str]) -> None:
        self.argv = tuple(argv)
        self.executable = self.argv[0] if self.argv else ""
        super().__init__(f"Required command not found: {self.executable!r}")


class CommandTimeoutError(CommandError):
    """The command did not finish within its configured timeout."""

    def __init__(self, argv: Sequence[str], timeout: float) -> None:
        self.argv = tuple(argv)
        self.timeout = timeout
        command_str = " ".join(self.argv)
        super().__init__(f"Command timed out after {timeout:g}s: {command_str}")


class ParseError(DoctorError):
    """Command output or system file contents could not be parsed."""


class NodeIdentificationError(DoctorError):
    """The local host could not be uniquely matched to a node in the configuration."""
