"""Persistent per-step execution state, written atomically with permissions 0600.

The state file records what happened, never configuration values: every stored string
passes through :func:`redact_text`, and error summaries are truncated.
"""

from __future__ import annotations

import errno
import fcntl
import json
import os
import stat
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

from openstack_forge.execution.errors import StateError, UnsafePathError
from openstack_forge.execution.fsutil import atomic_write_text, resolve_target
from openstack_forge.execution.redaction import redact_text

STATE_VERSION = 1
STATE_FILE_MODE = 0o600
LOCK_FILE_MODE = 0o600
DEFAULT_LOCK_TARGET = "/var/lib/openstack-forge/apply.lock"
MAX_ERROR_LENGTH = 2000


class StepStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


@dataclass(frozen=True)
class StepRecord:
    status: StepStatus
    started_at: str | None = None
    finished_at: str | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "error": self.error,
        }


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class StateStore:
    """JSON state file mapping step ids to :class:`StepRecord` values."""

    def __init__(self, path: Path, *, clock: Callable[[], datetime] = _utc_now) -> None:
        self.path = Path(path)
        self._clock = clock

    def _reject_symlinks(self) -> None:
        if os.path.islink(self.path) or os.path.islink(self.path.parent):
            raise UnsafePathError(f"Refusing to use state file {self.path}: it is a symlink")

    def load(self) -> dict[str, StepRecord]:
        """Return all records; an absent file means no step has started yet."""
        self._reject_symlinks()
        if not self.path.exists():
            return {}
        if not self.path.is_file():
            raise UnsafePathError(f"Refusing to use state file {self.path}: not a regular file")
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise StateError(f"Cannot read state file {self.path}: {exc}") from exc
        return _parse_records(payload, self.path)

    def get(self, step_id: str) -> StepRecord | None:
        return self.load().get(step_id)

    def mark_running(self, step_id: str) -> None:
        now = self._timestamp()
        self._update(step_id, lambda _: StepRecord(StepStatus.RUNNING, started_at=now))

    def mark_done(self, step_id: str) -> None:
        now = self._timestamp()

        def done(record: StepRecord | None) -> StepRecord:
            started = record.started_at if record and record.started_at else now
            return StepRecord(StepStatus.DONE, started_at=started, finished_at=now)

        self._update(step_id, done)

    def mark_failed(self, step_id: str, error: str) -> None:
        now = self._timestamp()
        summary = summarise_error(error)

        def failed(record: StepRecord | None) -> StepRecord:
            started = record.started_at if record and record.started_at else now
            return StepRecord(StepStatus.FAILED, started, finished_at=now, error=summary)

        self._update(step_id, failed)

    def _update(self, step_id: str, change: Callable[[StepRecord | None], StepRecord]) -> None:
        records = self.load()
        records[step_id] = change(records.get(step_id))
        payload = {
            "version": STATE_VERSION,
            "steps": {key: record.to_dict() for key, record in records.items()},
        }
        try:
            atomic_write_text(self.path, json.dumps(payload, indent=2) + "\n", mode=STATE_FILE_MODE)
        except OSError as exc:
            raise StateError(f"Cannot write state file {self.path}: {exc}") from exc

    def _timestamp(self) -> str:
        return self._clock().astimezone(timezone.utc).isoformat(timespec="seconds")


def summarise_error(error: str) -> str:
    """Redact ``error`` and cut it to :data:`MAX_ERROR_LENGTH` characters."""
    text = redact_text(error.strip())
    if len(text) > MAX_ERROR_LENGTH:
        return text[: MAX_ERROR_LENGTH - 15] + "... (truncated)"
    return text


def _parse_records(payload: Any, path: Path) -> dict[str, StepRecord]:
    if not isinstance(payload, Mapping) or payload.get("version") != STATE_VERSION:
        raise StateError(f"State file {path} has an unsupported format or version")
    steps = payload.get("steps")
    if not isinstance(steps, Mapping):
        raise StateError(f"State file {path} has no 'steps' mapping")
    records: dict[str, StepRecord] = {}
    for step_id, raw in steps.items():
        try:
            record = StepRecord(StepStatus(raw["status"]))
            records[step_id] = replace(
                record,
                started_at=raw.get("started_at"),
                finished_at=raw.get("finished_at"),
                error=raw.get("error"),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise StateError(f"State file {path} has an invalid record for {step_id!r}") from exc
    return records


def _read_lock_holder(fd: int) -> str:
    try:
        os.lseek(fd, 0, os.SEEK_SET)
        raw = os.read(fd, 64).decode("utf-8", errors="replace").strip()
    except OSError:
        return ""
    if raw.isdigit():
        return f" (pid {raw})"
    return ""


@contextmanager
def apply_lock(
    root: Path,
    target: str = DEFAULT_LOCK_TARGET,
    *,
    timeout: float = 0.0,
    poll_interval: float = 0.05,
) -> Iterator[Path]:
    """Acquire an exclusive advisory lock under the state directory around ``apply``.

    Raises :class:`StateError` immediately (or after ``timeout`` seconds when ``timeout > 0``)
    if another ``openstack-forge apply`` process holds the lock.
    """
    lock_path, display = resolve_target(root, target)
    raw_candidate = Path(root).joinpath(*Path(display).parts[1:])
    if raw_candidate.is_symlink() or lock_path.is_symlink() or lock_path.parent.is_symlink():
        raise UnsafePathError(f"Refusing to use lock file {display}: it is a symlink")
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(lock_path, flags, LOCK_FILE_MODE)
    except OSError as exc:
        raise StateError(f"Cannot open apply lock file {lock_path}: {exc}") from exc

    acquired = False
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise UnsafePathError(f"Refusing to use lock file {display}: it is not a regular file")
        try:
            os.fchmod(fd, LOCK_FILE_MODE)
        except OSError:
            pass

        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                break
            except OSError as exc:
                if exc.errno not in (errno.EACCES, errno.EAGAIN):
                    raise StateError(f"Cannot lock {lock_path}: {exc}") from exc
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    holder = _read_lock_holder(fd)
                    raise StateError(
                        f"Another 'openstack-forge apply' process{holder} holds the lock on "
                        f"{display} ({lock_path}); wait for it to finish and retry."
                    ) from exc
                time.sleep(min(max(0.001, poll_interval), remaining))

        try:
            os.ftruncate(fd, 0)
            os.lseek(fd, 0, os.SEEK_SET)
            os.write(fd, f"{os.getpid()}\n".encode("ascii"))
        except OSError:
            pass

        yield lock_path
    finally:
        if acquired:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
        try:
            os.close(fd)
        except OSError:
            pass
