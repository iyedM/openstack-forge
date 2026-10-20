"""Injectable command runner for read-only local host inspection."""

from __future__ import annotations

import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from openstack_forge.diagnostics.errors import (
    CommandError,
    CommandNotFoundError,
    CommandTimeoutError,
)

DEFAULT_TIMEOUT: float = 10.0


@dataclass(frozen=True)
class CommandResult:
    """Captured output and exit status of a single command invocation."""

    argv: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str = ""


class CommandRunner(Protocol):
    """Protocol for running an external command without a shell."""

    def run(self, argv: Sequence[str], *, timeout: float | None = None) -> CommandResult:
        """Execute ``argv`` and return its captured output and exit code."""


class SubprocessRunner:
    """Run local commands with ``subprocess.run`` (never ``shell=True``)."""

    def __init__(self, default_timeout: float = DEFAULT_TIMEOUT) -> None:
        if default_timeout <= 0:
            raise ValueError("default_timeout must be positive")
        self.default_timeout = default_timeout

    def run(self, argv: Sequence[str], *, timeout: float | None = None) -> CommandResult:
        """Execute ``argv`` without a shell and enforce a timeout."""
        if not argv:
            raise CommandError("Command argv must not be empty")
        cmd = tuple(str(arg) for arg in argv)
        effective_timeout = self.default_timeout if timeout is None else timeout
        if effective_timeout <= 0:
            raise ValueError("timeout must be positive")

        try:
            completed = subprocess.run(
                list(cmd),
                capture_output=True,
                text=True,
                check=False,
                shell=False,
                timeout=effective_timeout,
            )
        except FileNotFoundError as exc:
            raise CommandNotFoundError(cmd) from exc
        except subprocess.TimeoutExpired as exc:
            raise CommandTimeoutError(cmd, effective_timeout) from exc
        except OSError as exc:
            reason = exc.strerror or exc.__class__.__name__
            raise CommandError(f"Failed to execute {cmd[0]!r}: {reason}") from exc

        return CommandResult(
            argv=cmd,
            returncode=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
        )
