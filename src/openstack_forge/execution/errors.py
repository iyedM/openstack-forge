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
