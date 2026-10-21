"""Append-only, redacted JSON-lines audit log of executed commands.

The file is opened with ``O_APPEND`` (never truncated or rewritten), refuses symlinks,
and is kept at mode 0600. Each record is one JSON line written with a single ``write``
and flushed to disk before the call returns.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from openstack_forge.execution.errors import AuditLogError
from openstack_forge.execution.redaction import redact_data

AUDIT_FILE_MODE = 0o600
# Default location of the command audit log on a managed host (mapped under the root).
DEFAULT_AUDIT_TARGET = "/var/log/openstack-forge/commands.log"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class AuditLog:
    """Append redacted records to ``path``; the parent directory must already exist."""

    def __init__(self, path: Path, *, clock: Callable[[], datetime] = _utc_now) -> None:
        self.path = Path(path)
        self._clock = clock

    def record(self, entry: Mapping[str, Any]) -> None:
        """Append ``entry`` (redacted, with a UTC ``time``) as one JSON line."""
        payload = {
            "time": self._clock().astimezone(timezone.utc).isoformat(timespec="milliseconds"),
            **redact_data(dict(entry)),
        }
        line = (json.dumps(payload, sort_keys=True) + "\n").encode("utf-8")
        flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC
        try:
            fd = os.open(self.path, flags, AUDIT_FILE_MODE)
        except OSError as exc:
            raise AuditLogError(f"Cannot open audit log {self.path}: {exc.strerror}") from exc
        try:
            os.fchmod(fd, AUDIT_FILE_MODE)
            written = os.write(fd, line)
            if written != len(line):
                raise AuditLogError(f"Short write to audit log {self.path}")
            os.fsync(fd)
        except OSError as exc:
            raise AuditLogError(f"Cannot write audit log {self.path}: {exc.strerror}") from exc
        finally:
            os.close(fd)

    def read(self) -> list[dict[str, Any]]:
        """Return all records (for tests and inspection)."""
        if not self.path.exists():
            return []
        return [json.loads(line) for line in self.path.read_text(encoding="utf-8").splitlines()]
