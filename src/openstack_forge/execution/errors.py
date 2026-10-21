"""Exceptions raised by the execution building blocks."""

from __future__ import annotations


class ExecutionError(Exception):
    """Base class for all execution errors."""


class RootEscapeError(ExecutionError):
    """A target path would resolve outside the configured root directory."""


class UnsafePathError(ExecutionError):
    """A target path exists but cannot be modified safely (symlink, directory, ...)."""


class FileFormatError(ExecutionError):
    """A file's content cannot be edited safely (CRLF, broken markers, multi-line value)."""


class StateError(ExecutionError):
    """The state file cannot be read, parsed or written."""


class EngineError(ExecutionError):
    """The engine was called with steps that do not match its recorded state."""


class PreconditionError(ExecutionError):
    """A step cannot run because something it relies on is missing from the host.

    Steps never fix such a precondition silently (for example by installing a package
    that was not asked for); the operator has to.
    """


class CommandError(ExecutionError):
    """Base class for errors of the allowlisted command layer.

    ``hint`` is set by the executor from the command's ``failure_hint`` and is appended
    to the engine's suggested next action.
    """

    hint: str | None = None


class CommandSpecError(CommandError):
    """A command specification is invalid or conflicts with another registration."""


class UnknownCommandError(CommandError):
    """The command id is not in the allowlist registry."""


class ParameterError(CommandError):
    """A command parameter is missing, unexpected or fails its type's validation."""


class MutationRefusedError(CommandError):
    """A mutating command was refused (root is not "/" or the process is not root)."""


class CommandNotFoundError(CommandError):
    """The command's executable does not exist or cannot be executed."""


class CommandTimeoutError(CommandError):
    """The command did not finish within its timeout and was stopped.

    ``killed`` is True when it ignored SIGTERM for the grace period and got SIGKILL.
    """

    def __init__(self, message: str, *, killed: bool = False) -> None:
        self.killed = killed
        super().__init__(message)


class UnexpectedExitError(CommandError):
    """The command exited with a code its specification does not allow."""

    def __init__(self, command_id: str, returncode: int, stderr: str) -> None:
        self.command_id = command_id
        self.returncode = returncode
        self.stderr = stderr
        detail = f": {stderr.strip()}" if stderr.strip() else ""
        super().__init__(f"Command {command_id!r} exited with status {returncode}{detail}")


class AuditLogError(CommandError):
    """The audit log cannot be written; no command runs without its audit record."""
