"""Safety rules of the command layer: root/uid refusal, audit log, engine, import graph.

All commands here go through a fake backend; nothing is executed.
"""

from __future__ import annotations

import ast
import json
import stat
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path

import pytest

from openstack_forge.execution import (
    AUDIT_FILE_MODE,
    MASK,
    AuditLog,
    AuditLogError,
    CommandExecutor,
    CommandRegistry,
    CommandSpec,
    Engine,
    FunctionStep,
    MutationRefusedError,
    Outcome,
    Phase,
    ProcessOutput,
    StateStore,
    UnexpectedExitError,
    command_succeeds,
    is_host_root,
    verify_command,
)

SRC = Path(__file__).resolve().parents[2] / "src" / "openstack_forge"
TOOL = "/opt/forge-test/bin/unit-tool"


class FakeBackend:
    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []
        self.responses: dict[tuple[str, ...], ProcessOutput] = {}

    def run(
        self, argv: tuple[str, ...], *, env: Mapping[str, str], timeout: float
    ) -> ProcessOutput:
        self.calls.append(argv)
        return self.responses.get(argv, ProcessOutput(0, "", ""))


def _registry() -> CommandRegistry:
    return CommandRegistry(
        [
            CommandSpec("test.restart-unit", (TOOL, "restart", "{unit}"), {"unit": "systemd_unit"},
                        timeout=30, mutates=True),
            CommandSpec("test.unit-active", (TOOL, "is-active", "{unit}"),
                        {"unit": "systemd_unit"}, timeout=10, allowed_exit_codes={0, 3},
                        mutates=False),
            CommandSpec("test.login", (TOOL, "login", "--password=lit3ral"), timeout=5,
                        mutates=False),
        ]
    )  # fmt: skip


@pytest.fixture
def backend() -> FakeBackend:
    return FakeBackend()


@pytest.fixture
def audit(tmp_path: Path) -> AuditLog:
    times = iter(range(100))
    return AuditLog(
        tmp_path / "audit" / "commands.log",
        clock=lambda: datetime(2026, 10, 21, 12, 0, next(times), tzinfo=timezone.utc),
    )


def _host_executor(backend: FakeBackend, audit: AuditLog, **kwargs) -> CommandExecutor:
    """Root '/' and a pretend uid 0; safe only because the backend is fake."""
    audit.path.parent.mkdir(exist_ok=True)
    ticks = iter([10.0, 10.25, 20.0, 20.5, 30.0, 30.75, 40.0, 41.0])
    return CommandExecutor(
        _registry(), root=Path("/"), audit_log=audit, backend=backend, euid=lambda: 0,
        clock=lambda: next(ticks), **kwargs,
    )  # fmt: skip


# --- mutating commands and the root -------------------------------------------------------


def test_mutating_command_is_refused_under_a_non_host_root(
    tmp_path: Path, backend: FakeBackend, audit: AuditLog
) -> None:
    executor = CommandExecutor(
        _registry(), root=tmp_path, audit_log=audit, backend=backend, euid=lambda: 0
    )
    with pytest.raises(MutationRefusedError, match="not '/'"):
        executor.run("test.restart-unit", {"unit": "chrony"})
    assert backend.calls == []
    assert not audit.path.exists()


def test_read_only_command_runs_under_any_root(tmp_path: Path, backend, audit) -> None:
    audit.path.parent.mkdir()
    executor = CommandExecutor(_registry(), root=tmp_path, audit_log=audit, backend=backend)
    executor.run("test.unit-active", {"unit": "chrony"})
    assert backend.calls == [(TOOL, "is-active", "chrony")]


def test_mutating_command_requires_uid_0_and_never_uses_sudo(backend, audit) -> None:
    executor = CommandExecutor(
        _registry(), root=Path("/"), audit_log=audit, backend=backend, euid=lambda: 1000
    )
    with pytest.raises(MutationRefusedError, match="effective uid 0.*sudo is never used"):
        executor.run("test.restart-unit", {"unit": "chrony"})
    assert backend.calls == []


def test_mutating_command_runs_against_the_host_root_as_uid_0(backend, audit) -> None:
    result = _host_executor(backend, audit).run("test.restart-unit", {"unit": "chrony.service"})
    assert result.ok
    assert backend.calls == [(TOOL, "restart", "chrony.service")]


def test_root_detection_resolves_symlinks(tmp_path: Path) -> None:
    link = tmp_path / "slash"
    link.symlink_to("/")
    assert is_host_root(Path("/")) and is_host_root(link)
    assert not is_host_root(tmp_path)
    assert not is_host_root(tmp_path / "missing")


def test_dry_run_of_a_mutating_command_is_allowed_under_any_root(
    tmp_path: Path, backend: FakeBackend, audit: AuditLog
) -> None:
    printed: list[str] = []
    executor = CommandExecutor(
        _registry(), root=tmp_path, audit_log=audit, backend=backend, dry_run=True,
        echo=printed.append,
    )  # fmt: skip
    executor.run("test.restart-unit", {"unit": "chrony"})
    assert printed == [f"DRY RUN [test.restart-unit]: {TOOL} restart chrony"]
    assert backend.calls == []


# --- audit log --------------------------------------------------------------------------


def test_audit_log_records_start_and_finish(backend: FakeBackend, audit: AuditLog) -> None:
    backend.responses[(TOOL, "is-active", "chrony")] = ProcessOutput(3, "inactive\n", "")
    executor = _host_executor(backend, audit)
    executor.run("test.restart-unit", {"unit": "chrony"})
    executor.run("test.unit-active", {"unit": "chrony"})

    entries = audit.read()
    assert [e["event"] for e in entries] == ["start", "finish", "start", "finish"]
    first_start, first_finish, second_start, second_finish = entries
    assert first_start["invocation"] == first_finish["invocation"] != second_start["invocation"]
    assert first_start == {
        "time": "2026-10-21T12:00:00.000+00:00",
        "invocation": first_start["invocation"],
        "event": "start",
        "command_id": "test.restart-unit",
        "argv": [TOOL, "restart", "chrony"],
        "mutates": True,
    }
    assert first_finish["outcome"] == "ok"
    assert (first_finish["exit_code"], first_finish["duration_s"]) == (0, 0.25)
    assert (second_finish["exit_code"], second_finish["duration_s"]) == (3, 0.5)
    assert "inactive" not in audit.path.read_text()  # output is never logged


def test_audit_log_is_redacted(backend: FakeBackend, audit: AuditLog) -> None:
    _host_executor(backend, audit).run("test.login")
    text = audit.path.read_text()
    assert "lit3ral" not in text
    assert f"--password={MASK}" in json.loads(text.splitlines()[0])["argv"]


def test_audit_log_permissions_are_0600(backend: FakeBackend, audit: AuditLog) -> None:
    audit.path.parent.mkdir()
    audit.path.write_text("")
    audit.path.chmod(0o644)
    _host_executor(backend, audit).run("test.restart-unit", {"unit": "chrony"})
    assert AUDIT_FILE_MODE == 0o600
    assert stat.S_IMODE(audit.path.stat().st_mode) == 0o600


def test_audit_log_is_append_only(backend: FakeBackend, audit: AuditLog) -> None:
    audit.path.parent.mkdir()
    previous = '{"event": "earlier record"}\n'
    audit.path.write_text(previous)
    executor = _host_executor(backend, audit)
    executor.run("test.restart-unit", {"unit": "chrony"})
    executor.run("test.restart-unit", {"unit": "chrony"})
    text = audit.path.read_text()
    assert text.startswith(previous)
    assert len(text.splitlines()) == 5


def test_command_does_not_run_if_the_audit_log_cannot_be_written(
    tmp_path: Path, backend: FakeBackend
) -> None:
    target = tmp_path / "elsewhere.log"
    target.write_text("")
    (tmp_path / "commands.log").symlink_to(target)
    executor = CommandExecutor(
        _registry(), root=Path("/"), audit_log=AuditLog(tmp_path / "commands.log"),
        backend=backend, euid=lambda: 0,
    )  # fmt: skip
    with pytest.raises(AuditLogError):
        executor.run("test.restart-unit", {"unit": "chrony"})
    assert backend.calls == []
    assert target.read_text() == ""


def test_audit_log_in_a_missing_directory_is_an_error(tmp_path: Path, backend) -> None:
    executor = CommandExecutor(
        _registry(), root=tmp_path, audit_log=AuditLog(tmp_path / "missing" / "log"),
        backend=backend,
    )  # fmt: skip
    with pytest.raises(AuditLogError, match="Cannot open audit log"):
        executor.run("test.unit-active", {"unit": "chrony"})
    assert backend.calls == []


# --- engine integration --------------------------------------------------------------------


def _service_step(executor: CommandExecutor) -> FunctionStep:
    return FunctionStep(
        id="restart-chrony",
        check_fn=lambda: command_succeeds(executor, "test.unit-active", {"unit": "chrony"}),
        apply_fn=lambda: executor.run("test.restart-unit", {"unit": "chrony"}),
        verify_fn=lambda: verify_command(executor, "test.unit-active", {"unit": "chrony"}),
    )


def test_engine_step_using_commands(tmp_path: Path, backend: FakeBackend, audit) -> None:
    active = (TOOL, "is-active", "chrony")
    backend.responses[active] = ProcessOutput(3, "inactive\n", "")
    executor = _host_executor(backend, audit)
    step = _service_step(executor)

    def restart_and_activate() -> None:
        executor.run("test.restart-unit", {"unit": "chrony"})
        backend.responses[active] = ProcessOutput(0, "active\n", "")

    step = FunctionStep(step.id, step.check_fn, restart_and_activate, step.verify_fn)
    report = Engine(StateStore(tmp_path / "state.json")).run([step])

    assert report.ok
    assert [o.outcome for o in report.outcomes] == [Outcome.APPLIED]
    assert backend.calls == [active, (TOOL, "restart", "chrony"), active]


def test_engine_failure_from_a_command_stays_redacted(tmp_path: Path, backend, audit) -> None:
    backend.responses[(TOOL, "is-active", "chrony")] = ProcessOutput(3, "", "")
    backend.responses[(TOOL, "restart", "chrony")] = ProcessOutput(
        1, "", "Job failed. rabbit://openstack:RABBIT_PASS@ctl password=hunter2\n"
    )
    store = StateStore(tmp_path / "state.json")
    report = Engine(store).run([_service_step(_host_executor(backend, audit))])

    failure = report.failure
    assert (failure.step_id, failure.phase) == ("restart-chrony", Phase.APPLY)
    assert failure.message == f"apply() raised {UnexpectedExitError.__name__}"
    for text in (failure.details, store.path.read_text(), audit.path.read_text()):
        assert "RABBIT_PASS" not in text and "hunter2" not in text
    assert MASK in failure.details


def test_verify_details_from_command_output_stay_redacted(tmp_path: Path, backend, audit) -> None:
    active = (TOOL, "is-active", "chrony")
    backend.responses[active] = ProcessOutput(3, "failed: token=abc123\n", "")
    store = StateStore(tmp_path / "state.json")
    executor = _host_executor(backend, audit)
    step = FunctionStep(
        "check-only",
        check_fn=lambda: False,
        apply_fn=lambda: None,
        verify_fn=lambda: verify_command(executor, "test.unit-active", {"unit": "chrony"}),
    )
    report = Engine(store).run([step])
    assert report.failure.phase is Phase.VERIFY
    assert report.failure.details == f"failed: token={MASK}"
    assert "abc123" not in store.path.read_text()


# --- import graph ---------------------------------------------------------------------------


def _execution_imports(path: Path) -> list[tuple[str | None, int]]:
    """(enclosing function name or None for module level, line) of execution imports."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: list[tuple[str | None, int]] = []

    def visit(node: ast.AST, function: str | None) -> None:
        for child in ast.iter_child_nodes(node):
            name = function
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                name = child.name
            if isinstance(child, ast.ImportFrom) and (child.module or "").startswith(
                "openstack_forge.execution"
            ):
                found.append((name, child.lineno))
            if isinstance(child, ast.Import) and any(
                alias.name.startswith("openstack_forge.execution") for alias in child.names
            ):
                found.append((name, child.lineno))
            visit(child, name)

    visit(tree, None)
    return found


def test_only_the_apply_handler_imports_execution() -> None:
    allowed_cli_handlers = {"_apply", "_secrets_init", "_secrets_show"}
    offenders = {}
    for path in sorted(SRC.rglob("*.py")):
        if "execution" in path.relative_to(SRC).parts:
            continue
        for function, line in _execution_imports(path):
            if not (path == SRC / "cli.py" and function in allowed_cli_handlers):
                offenders[f"{path.relative_to(SRC)}:{line}"] = function
    assert offenders == {}


@pytest.mark.parametrize("module", ["hosts.py", "files.py", "fsutil.py", "state.py", "engine.py"])
def test_file_steps_do_not_use_the_command_layer(module: str) -> None:
    tree = ast.parse((SRC / "execution" / module).read_text(encoding="utf-8"))
    imported = {
        node.module if isinstance(node, ast.ImportFrom) else alias.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in getattr(node, "names", [])
    }
    assert "subprocess" not in imported
    assert "openstack_forge.execution.commands" not in imported
