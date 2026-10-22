"""Tests for the JSON state store and the redaction helper."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from openstack_forge import cli
from openstack_forge.execution import (
    DEFAULT_LOCK_TARGET,
    LOCK_FILE_MODE,
    MASK,
    STATE_FILE_MODE,
    StateError,
    StateStore,
    StepRecord,
    StepStatus,
    UnsafePathError,
    apply_lock,
    fsutil,
    redact_data,
    redact_text,
)
from openstack_forge.execution.state import MAX_ERROR_LENGTH


class SimulatedCrash(BaseException):
    """Stands in for the process being killed at a precise point."""


@pytest.fixture
def clock():
    times = iter(
        datetime(2026, 10, 21, 9, 0, tzinfo=timezone.utc) + timedelta(seconds=s)
        for s in range(1000)
    )
    return lambda: next(times)


@pytest.fixture
def store(tmp_path: Path, clock) -> StateStore:
    return StateStore(tmp_path / "state.json", clock=clock)


# --- records and timestamps ------------------------------------------------------------


def test_missing_state_file_means_nothing_started(store: StateStore) -> None:
    assert store.load() == {}
    assert store.get("hosts-file") is None
    assert not store.path.exists()


def test_status_transitions_and_timestamps(store: StateStore) -> None:
    store.mark_running("hosts-file")
    assert store.get("hosts-file") == StepRecord(
        StepStatus.RUNNING, started_at="2026-10-21T09:00:00+00:00"
    )
    store.mark_done("hosts-file")
    assert store.get("hosts-file") == StepRecord(
        StepStatus.DONE,
        started_at="2026-10-21T09:00:00+00:00",
        finished_at="2026-10-21T09:00:01+00:00",
    )
    store.mark_running("time-sync")
    store.mark_failed("time-sync", "chrony did not start")
    assert store.get("time-sync") == StepRecord(
        StepStatus.FAILED,
        started_at="2026-10-21T09:00:02+00:00",
        finished_at="2026-10-21T09:00:03+00:00",
        error="chrony did not start",
    )
    assert set(store.load()) == {"hosts-file", "time-sync"}


def test_done_without_running_gets_both_timestamps(store: StateStore) -> None:
    store.mark_done("already-there")
    record = store.get("already-there")
    assert record.started_at == record.finished_at == "2026-10-21T09:00:00+00:00"


def test_state_file_format(store: StateStore) -> None:
    store.mark_running("hosts-file")
    store.mark_done("hosts-file")
    assert json.loads(store.path.read_text()) == {
        "version": 1,
        "steps": {
            "hosts-file": {
                "status": "done",
                "started_at": "2026-10-21T09:00:00+00:00",
                "finished_at": "2026-10-21T09:00:01+00:00",
                "error": None,
            }
        },
    }


def test_status_values() -> None:
    assert [status.value for status in StepStatus] == ["pending", "running", "done", "failed"]


# --- permissions --------------------------------------------------------------------------


def test_state_file_permissions_are_0600(store: StateStore) -> None:
    old_umask = os.umask(0o022)
    try:
        store.mark_running("hosts-file")
    finally:
        os.umask(old_umask)
    assert STATE_FILE_MODE == 0o600
    assert stat.S_IMODE(store.path.stat().st_mode) == 0o600


def test_permissions_are_tightened_on_an_existing_file(store: StateStore) -> None:
    store.path.write_text('{"version": 1, "steps": {}}')
    store.path.chmod(0o644)
    store.mark_running("hosts-file")
    assert stat.S_IMODE(store.path.stat().st_mode) == 0o600


# --- atomic writes --------------------------------------------------------------------------


def test_crash_before_rename_keeps_previous_state(
    store: StateStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    store.mark_running("hosts-file")
    before = store.path.read_bytes()

    def crash(*args, **kwargs):
        raise SimulatedCrash

    monkeypatch.setattr(fsutil.os, "replace", crash)
    with pytest.raises(SimulatedCrash):
        store.mark_done("hosts-file")
    assert store.path.read_bytes() == before
    assert store.get("hosts-file").status is StepStatus.RUNNING
    assert sorted(p.name for p in store.path.parent.iterdir()) == ["state.json"]


def test_hard_crash_leaves_state_readable_and_writable(
    store: StateStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    store.mark_running("hosts-file")
    before = store.path.read_bytes()

    def crash(*args, **kwargs):
        raise SimulatedCrash

    # A killed process runs no cleanup, so the temporary file stays behind.
    with monkeypatch.context() as patch:
        patch.setattr(fsutil.os, "replace", crash)
        patch.setattr(fsutil.os, "unlink", lambda path: None)
        with pytest.raises(SimulatedCrash):
            store.mark_done("hosts-file")
    leftovers = [p for p in store.path.parent.iterdir() if p.name != "state.json"]
    assert len(leftovers) == 1 and leftovers[0].name.startswith(".state.json.")
    assert store.path.read_bytes() == before

    store.mark_done("hosts-file")
    assert store.get("hosts-file").status is StepStatus.DONE


def test_crash_while_writing_keeps_previous_state(
    store: StateStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    store.mark_running("hosts-file")
    before = store.path.read_bytes()

    def crash(*args, **kwargs):
        raise SimulatedCrash

    monkeypatch.setattr(fsutil.os, "fsync", crash)
    with pytest.raises(SimulatedCrash):
        store.mark_failed("hosts-file", "boom")
    assert store.path.read_bytes() == before


# --- invalid state files ------------------------------------------------------------------


@pytest.mark.parametrize(
    "content",
    [
        "not json",
        "[]",
        '{"version": 2, "steps": {}}',
        '{"version": 1}',
        '{"version": 1, "steps": {"a": {"status": "finished"}}}',
        '{"version": 1, "steps": {"a": {}}}',
    ],
)
def test_invalid_state_file_is_an_error(store: StateStore, content: str) -> None:
    store.path.write_text(content)
    with pytest.raises(StateError):
        store.load()


def test_unwritable_state_location(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "missing-dir" / "state.json")
    with pytest.raises(StateError, match="Cannot write state file"):
        store.mark_running("hosts-file")


# --- no secrets in state -------------------------------------------------------------------


def test_errors_are_redacted_before_they_are_stored(store: StateStore) -> None:
    store.mark_failed(
        "mariadb",
        "login failed: password=hunter2 url=mysql://root:R00T@ctl/ token: abc123",
    )
    text = store.path.read_text()
    for secret in ("hunter2", "R00T", "abc123"):
        assert secret not in text
    assert MASK in store.get("mariadb").error


def test_long_errors_are_truncated(store: StateStore) -> None:
    store.mark_failed("mariadb", "x" * 10_000)
    error = store.get("mariadb").error
    assert len(error) == MAX_ERROR_LENGTH
    assert error.endswith("... (truncated)")


# --- redaction helper ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("password = s3cr3t", f"password = {MASK}"),
        ("admin_password=hunter2", f"admin_password={MASK}"),
        ("DB_PASSWORD='two words'", f"DB_PASSWORD='{MASK}'"),
        (
            '{"client_secret": "a\\"b", "user": "nova"}',
            f'{{"client_secret": "{MASK}", "user": "nova"}}',
        ),
        ("X-Auth-Token: gAAAAB", f"X-Auth-Token: {MASK}"),
        ("export OS_PASSWORD=pw", f"export OS_PASSWORD={MASK}"),
        ("openstack --os-password pw list", f"openstack --os-password {MASK} list"),
        ("--token=abc", f"--token={MASK}"),
        ("rabbit://openstack:RABBIT_PASS@ctl:5672/", f"rabbit://openstack:{MASK}@ctl:5672/"),
        ("Passwd: x\nuser = admin", f"Passwd: {MASK}\nuser = admin"),
        ("username = admin", "username = admin"),
        ("no secrets here", "no secrets here"),
    ],
)
def test_redact_text(text: str, expected: str) -> None:
    assert redact_text(text) == expected


def test_redact_text_is_idempotent() -> None:
    once = redact_text("password = x and token=y")
    assert redact_text(once) == once


def test_redact_data() -> None:
    data = {
        "Password": "a",
        "nested": [{"api_token": 1}, "secret=b"],
        "user": "nova",
        "count": 3,
    }
    assert redact_data(data) == {
        "Password": MASK,
        "nested": [{"api_token": MASK}, f"secret={MASK}"],
        "user": "nova",
        "count": 3,
    }


# --- apply lock (two-process concurrency) -------------------------------------------------


def test_apply_lock_two_processes_and_cli_refusal(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "root"
    etc = root / "etc"
    etc.mkdir(parents=True, exist_ok=True)
    (etc / "hosts").write_text("127.0.0.1 localhost\n", encoding="utf-8")

    config_path = tmp_path / "cluster.yaml"
    assert cli.main(["init", "--output", str(config_path)]) == 0
    capsys.readouterr()

    child_code = (
        "import sys\n"
        "from pathlib import Path\n"
        "from openstack_forge.execution import apply_lock\n"
        "with apply_lock(Path(sys.argv[1])):\n"
        "    sys.stdout.write('LOCKED\\n')\n"
        "    sys.stdout.flush()\n"
        "    sys.stdin.read()\n"
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", child_code, str(root)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert proc.stdout is not None
        ready_line = proc.stdout.readline().strip()
        assert ready_line == "LOCKED"

        lock_file = root / DEFAULT_LOCK_TARGET.lstrip("/")
        assert lock_file.exists()
        assert stat.S_IMODE(lock_file.stat().st_mode) == LOCK_FILE_MODE

        # Immediate failure when another process holds the lock
        with pytest.raises(StateError) as exc_info:
            with apply_lock(root):
                pass
        assert f"pid {proc.pid}" in str(exc_info.value)
        assert DEFAULT_LOCK_TARGET in str(exc_info.value)

        # Bounded wait also fails with StateError while child holds the lock
        with pytest.raises(StateError, match=DEFAULT_LOCK_TARGET):
            with apply_lock(root, timeout=0.05, poll_interval=0.01):
                pass

        # CLI `apply --yes` fails cleanly with exit code 1 while lock is held
        rc = cli.main(
            [
                "apply",
                "--step",
                "hosts-file",
                "--config",
                str(config_path),
                "--root",
                str(root),
                "--node",
                "controller",
                "--yes",
            ]
        )
        captured = capsys.readouterr()
        assert rc == 1
        assert "Another 'openstack-forge apply' process" in captured.err
    finally:
        if proc.stdin is not None:
            proc.stdin.close()
        proc.wait(timeout=5)
    assert proc.returncode == 0

    # Once the first process releases the lock, apply --yes succeeds
    rc_after = cli.main(
        [
            "apply",
            "--step",
            "hosts-file",
            "--config",
            str(config_path),
            "--root",
            str(root),
            "--node",
            "controller",
            "--yes",
        ]
    )
    assert rc_after == 0


def test_apply_lock_rejects_symlink(tmp_path: Path) -> None:
    root = tmp_path / "root"
    lock_file = root / DEFAULT_LOCK_TARGET.lstrip("/")
    lock_file.parent.mkdir(parents=True, exist_ok=True)
    inside = root / "var/lib/openstack-forge/other.lock"
    inside.write_text("", encoding="utf-8")
    lock_file.symlink_to(inside)

    with pytest.raises(UnsafePathError, match="symlink"):
        with apply_lock(root):
            pass
