"""Persistent per-step execution state, written atomically with permissions 0600.

The state file records what happened, never configuration values: every stored string
passes through :func:`redact_text`, and error summaries are truncated.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

from openstack_forge.execution.errors import StateError
from openstack_forge.execution.fsutil import atomic_write_text
from openstack_forge.execution.redaction import redact_text

STATE_VERSION = 1
STATE_FILE_MODE = 0o600
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

    def load(self) -> dict[str, StepRecord]:
        """Return all records; an absent file means no step has started yet."""
        if not self.path.exists():
            return {}
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
