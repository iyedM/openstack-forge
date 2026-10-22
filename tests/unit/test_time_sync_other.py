"""Tests for the time-sync-other step (fake backend, temporary roots, no root, no network).

The executor used by step tests gets root "/" with a fake backend and a fake uid 0, so
mutating specs pass the safety check while nothing is executed; every file edit uses a
temporary root. The sample chrony.conf below is test data, not a copy of Ubuntu's file.
"""

from __future__ import annotations

import os
import socket
import subprocess
from collections.abc import Mapping
from pathlib import Path

import pytest

from openstack_forge.cli import main
from openstack_forge.config import load_config
from openstack_forge.execution import (
    APT_FAILURE_HINT,
    APT_UPDATE_FAILURE_HINT,
    CHRONY_BLOCK_NAME,
    DEFAULT_AUDIT_TARGET,
    DEFAULT_REGISTRY,
    DEFAULT_STATE_TARGET,
    DISABLED_LINE_PREFIX,
    IMPLEMENTED_STEPS,
    TIME_SYNC_NOT_CHECKED,
    AuditLog,
    CommandExecutor,
    Engine,
    ExecutionError,
    FileFormatError,
    MutationRefusedError,
    Outcome,
    Phase,
    ProcessOutput,
    StateStore,
    StepStatus,
    TimeSyncOtherStep,
    UnsafePathError,
    build_step,
    edit_chrony_conf_other,
    verify_chrony_other_text,
)
from openstack_forge.orchestration import load_catalog

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURE_CONFIG = REPO_ROOT / "tests" / "fixtures" / "valid_cluster.yaml"
EXAMPLE_CONFIG = REPO_ROOT / "examples" / "cluster.example.yaml"

SAMPLE_CONF = """\
# Sample chrony configuration used by the tests.
confdir /etc/chrony/conf.d
pool ntp.example.org iburst maxsources 4
pool 2.debian.pool.ntp.org offline iburst
  server 192.0.2.250 iburst
#pool commented.example.org iburst
sourcedir /run/chrony-dhcp
driftfile /var/lib/chrony/chrony.drift
makestep 1 3
"""
BEGIN, END = f"# BEGIN {CHRONY_BLOCK_NAME}", f"# END {CHRONY_BLOCK_NAME}"
BLOCK = f"{BEGIN}\nserver controller iburst\n{END}\n"
EDITED_CONF = (
    "# Sample chrony configuration used by the tests.\n"
    "confdir /etc/chrony/conf.d\n"
    f"{DISABLED_LINE_PREFIX}pool ntp.example.org iburst maxsources 4\n"
    f"{DISABLED_LINE_PREFIX}pool 2.debian.pool.ntp.org offline iburst\n"
    f"{DISABLED_LINE_PREFIX}  server 192.0.2.250 iburst\n"
    "#pool commented.example.org iburst\n"
    "sourcedir /run/chrony-dhcp\n"
    "driftfile /var/lib/chrony/chrony.drift\n"
    "makestep 1 3\n"
    f"{BLOCK}"
)

DPKG = ("/usr/bin/dpkg-query", "--status", "chrony")
UPDATE = ("/usr/bin/apt-get", "update")
APT = ("/usr/bin/apt-get", "install", "--yes", "chrony")
RESTART = ("/usr/bin/systemctl", "restart", "chrony")
IS_ACTIVE = ("/usr/bin/systemctl", "is-active", "chrony")
# The reviewed specs this step may use; it must not need any new command.
REUSED_COMMANDS = {
    "apt-get.update", "apt-get.install", "systemctl.restart", "systemctl.is-active",
    "dpkg-query.status",
}  # fmt: skip


@pytest.fixture(autouse=True)
def _forbid_processes_and_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*args, **kwargs):
        raise AssertionError(f"no real process or network access in these tests: {args!r}")

    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(os, "system", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket, "socket", forbidden)


class FakeHost:
    """Fake backend simulating dpkg, apt-get and systemctl; runs nothing."""

    def __init__(self, root: Path, *, installed: bool = False, active: bool = False) -> None:
        self.root = root
        self.installed = installed
        self.active = active
        self.calls: list[tuple[str, ...]] = []
        self.failures: dict[tuple[str, ...], ProcessOutput] = {}

    def run(
        self, argv: tuple[str, ...], *, env: Mapping[str, str], timeout: float
    ) -> ProcessOutput:
        self.calls.append(argv)
        if argv in self.failures:
            return self.failures[argv]
        if argv == DPKG:
            if self.installed:
                return ProcessOutput(0, "Package: chrony\nStatus: install ok installed\n", "")
            return ProcessOutput(1, "", "dpkg-query: package 'chrony' is not installed\n")
        if argv == UPDATE:
            return ProcessOutput(0, "Reading package lists... Done\n", "")
        if argv == APT:
            self.installed = True
            conf = self.root / "etc/chrony/chrony.conf"
            if not conf.exists():  # the package installs its configuration file
                conf.parent.mkdir(parents=True, exist_ok=True)
                conf.write_text(SAMPLE_CONF)
            return ProcessOutput(0, "Setting up chrony ...\n", "")
        if argv == RESTART:
            self.active = True
            return ProcessOutput(0, "", "")
        if argv == IS_ACTIVE:
            return (
                ProcessOutput(0, "active\n", "")
                if self.active
                else ProcessOutput(3, "inactive\n", "")
            )
        raise AssertionError(f"unexpected argv {argv!r}")


@pytest.fixture
def root(tmp_path: Path) -> Path:
    directory = tmp_path / "root"
    (directory / "etc/chrony").mkdir(parents=True)
    return directory


@pytest.fixture
def config() -> dict:
    return load_config(FIXTURE_CONFIG)


def _node(config: Mapping, role: str = "compute") -> Mapping:
    return next(node for node in config["nodes"] if node["role"] == role)


def _executor(tmp_path: Path, backend, *, root: Path = Path("/")) -> CommandExecutor:
    (tmp_path / "audit").mkdir(exist_ok=True)
    return CommandExecutor(
        DEFAULT_REGISTRY, root=root, audit_log=AuditLog(tmp_path / "audit/commands.log"),
        backend=backend, euid=lambda: 0,
    )  # fmt: skip


def _step(
    root: Path, config: Mapping, executor: CommandExecutor, role: str = "compute"
) -> TimeSyncOtherStep:
    return TimeSyncOtherStep(root, config, _node(config, role), executor)


def _run(
    root: Path, config: Mapping, host: FakeHost, tmp_path: Path, executor_root: Path = Path("/")
):
    store = StateStore(tmp_path / "state.json")
    executor = _executor(tmp_path, host, root=executor_root)
    return Engine(store).run([_step(root, config, executor)]), store, executor


def _write_conf(root: Path, text: str = SAMPLE_CONF) -> Path:
    path = root / "etc/chrony/chrony.conf"
    path.write_text(text)
    return path


# --- file edits ---------------------------------------------------------------------------


def test_sources_are_commented_out_and_the_controller_is_the_only_server(root: Path) -> None:
    path = _write_conf(root)
    change = edit_chrony_conf_other(root, dry_run=False)
    assert change.changed and change.backup == "/etc/chrony/chrony.conf.orig"
    assert path.read_text() == EDITED_CONF


def test_edit_is_idempotent_and_never_touches_its_own_block(root: Path) -> None:
    path = _write_conf(root)
    edit_chrony_conf_other(root, dry_run=False)
    before = (path.read_bytes(), path.stat().st_mtime_ns, path.stat().st_ino)
    second = edit_chrony_conf_other(root, dry_run=False)
    assert (second.changed, second.diff) == (False, "")
    assert (path.read_bytes(), path.stat().st_mtime_ns, path.stat().st_ino) == before
    assert verify_chrony_other_text(path.read_text()) == []


def test_a_source_line_added_later_is_commented_out_on_the_next_run(root: Path) -> None:
    path = _write_conf(root, EDITED_CONF + "pool late.example.org iburst\n")
    edit_chrony_conf_other(root, dry_run=False)
    assert path.read_text() == EDITED_CONF + f"{DISABLED_LINE_PREFIX}pool late.example.org iburst\n"


def test_a_wrong_block_is_replaced(root: Path) -> None:
    path = _write_conf(root, SAMPLE_CONF + f"{BEGIN}\nserver 198.51.100.1 iburst\n{END}\n")
    edit_chrony_conf_other(root, dry_run=False)
    assert path.read_text() == EDITED_CONF


def test_dry_run_edit_writes_nothing(root: Path) -> None:
    path = _write_conf(root)
    change = edit_chrony_conf_other(root, dry_run=True)
    assert change.changed
    assert "+server controller iburst" in change.diff
    assert f"+{DISABLED_LINE_PREFIX}pool 2.debian.pool.ntp.org offline iburst" in change.diff
    assert path.read_text() == SAMPLE_CONF
    assert not (root / "etc/chrony/chrony.conf.orig").exists()


def test_missing_chrony_conf_is_not_created(root: Path) -> None:
    with pytest.raises(UnsafePathError, match="installed by the chrony package"):
        edit_chrony_conf_other(root, dry_run=False)
    assert not (root / "etc/chrony/chrony.conf").exists()


def test_broken_markers_are_refused(root: Path) -> None:
    path = _write_conf(root, SAMPLE_CONF + f"{BEGIN}\nserver controller iburst\n")
    with pytest.raises(FileFormatError, match="broken"):
        edit_chrony_conf_other(root, dry_run=False)
    assert path.read_text().endswith("server controller iburst\n")


# --- pure verification ---------------------------------------------------------------------


def test_verify_chrony_other_text_reports_problems() -> None:
    assert verify_chrony_other_text(EDITED_CONF) == []
    assert verify_chrony_other_text(SAMPLE_CONF) == [
        f"the managed block '{CHRONY_BLOCK_NAME}' is missing"
    ]
    assert verify_chrony_other_text(SAMPLE_CONF + f"{BEGIN}\n") == [
        f"the managed block '{CHRONY_BLOCK_NAME}' has broken markers"
    ]
    wrong = EDITED_CONF.replace("server controller iburst", "server 198.51.100.1 iburst")
    assert verify_chrony_other_text(wrong) == [
        "'server controller iburst' is missing from the managed block",
        "the managed block differs from the expected content",
    ]
    extra = EDITED_CONF.replace("controller iburst\n", "controller iburst\nserver x iburst\n")
    assert verify_chrony_other_text(extra) == [
        "the managed block differs from the expected content"
    ]
    assert verify_chrony_other_text(SAMPLE_CONF + BLOCK) == [
        "active source line(s) outside the block: pool ntp.example.org iburst maxsources 4, "
        "pool 2.debian.pool.ntp.org offline iburst, server 192.0.2.250 iburst"
    ]


# --- roles ---------------------------------------------------------------------------------


def test_controller_is_refused(root: Path, config: dict, tmp_path: Path) -> None:
    executor = _executor(tmp_path, FakeHost(root))
    with pytest.raises(ExecutionError, match=r"only runs on compute, block, object nodes") as exc:
        _step(root, config, executor, role="controller")
    assert "time-sync-controller" in str(exc.value)


@pytest.mark.parametrize("role", ["compute", "block", "object"])
def test_step_builds_for_every_non_controller_role(
    root: Path, config: dict, tmp_path: Path, role: str
) -> None:
    step = build_step(
        "time-sync-other", root, config, _node(config, role),
        executor=_executor(tmp_path, FakeHost(root)),
    )  # fmt: skip
    assert isinstance(step, TimeSyncOtherStep) and step.declares_commands


def test_step_is_registered_and_still_unverified() -> None:
    assert "time-sync-other" in IMPLEMENTED_STEPS
    step = load_catalog().step("time-sync-other")
    assert step.status == "unverified"
    assert list(step.roles) == ["compute", "block", "object"]
    assert step.files_modified == ("/etc/chrony/chrony.conf",)


# --- check() -------------------------------------------------------------------------------


def test_check_is_false_without_the_file_and_runs_nothing(root, config, tmp_path) -> None:
    host = FakeHost(root, installed=True)
    assert _step(root, config, _executor(tmp_path, host)).check() is False
    assert host.calls == []


def test_check_is_false_when_the_file_needs_an_edit(root, config, tmp_path) -> None:
    _write_conf(root)
    host = FakeHost(root, installed=True)
    assert _step(root, config, _executor(tmp_path, host)).check() is False
    assert host.calls == []  # the pure file check fails first


def test_check_is_false_when_chrony_is_not_installed(root, config, tmp_path) -> None:
    _write_conf(root, EDITED_CONF)
    host = FakeHost(root, installed=False)
    assert _step(root, config, _executor(tmp_path, host)).check() is False
    assert host.calls == [DPKG]


def test_check_is_true_when_configured_and_installed(root, config, tmp_path) -> None:
    _write_conf(root, EDITED_CONF)
    host = FakeHost(root, installed=True)
    assert _step(root, config, _executor(tmp_path, host)).check() is True
    assert host.calls == [DPKG]


# --- apply() through the engine ------------------------------------------------------------


def test_fresh_host_update_install_edit_restart_verify(tmp_path: Path, config: dict) -> None:
    root = tmp_path / "root"
    (root / "etc").mkdir(parents=True)  # no chrony directory: the package is not installed
    host = FakeHost(root)
    report, store, executor = _run(root, config, host, tmp_path)

    assert report.ok, report.failure
    assert [o.outcome for o in report.outcomes] == [Outcome.APPLIED]
    # check() stops at the missing file, so no dpkg-query is needed before applying.
    assert host.calls == [UPDATE, APT, RESTART, IS_ACTIVE]
    assert (root / "etc/chrony/chrony.conf").read_text() == EDITED_CONF
    assert store.get("time-sync-other").status is StepStatus.DONE
    starts = [e for e in executor.audit_log.read() if e["event"] == "start"]
    assert [(e["command_id"], e["argv"], e["mutates"]) for e in starts] == [
        ("apt-get.update", list(UPDATE), True),
        ("apt-get.install", list(APT), True),
        ("systemctl.restart", list(RESTART), True),
        ("systemctl.is-active", list(IS_ACTIVE), False),
    ]
    assert {e["command_id"] for e in starts} <= REUSED_COMMANDS


def test_existing_file_is_edited_between_install_and_restart(root, config, tmp_path) -> None:
    path = _write_conf(root)
    host = FakeHost(root)
    seen: list[str] = []
    original = host.run

    def spy(argv, **kw):
        seen.append(f"{argv[1]}:{'edited' if path.read_text() == EDITED_CONF else 'original'}")
        return original(argv, **kw)

    host.run = spy  # type: ignore[method-assign]
    _step(root, config, _executor(tmp_path, host)).apply()
    assert seen == ["update:original", "install:original", "restart:edited"]


def test_installed_and_configured_host_is_skipped(root: Path, config: dict, tmp_path) -> None:
    _write_conf(root, EDITED_CONF)
    host = FakeHost(root, installed=True, active=True)
    report, _, _ = _run(root, config, host, tmp_path)
    assert [o.outcome for o in report.outcomes] == [Outcome.SKIPPED]
    assert host.calls == [DPKG]


def test_rerun_after_success_runs_no_mutating_command(root: Path, config, tmp_path) -> None:
    _write_conf(root)
    host = FakeHost(root, installed=True)
    executor = _executor(tmp_path, host)
    engine = Engine(StateStore(tmp_path / "state.json"))
    assert engine.run([_step(root, config, executor)]).ok
    conf = root / "etc/chrony/chrony.conf"
    snapshot = (conf.read_bytes(), conf.stat().st_mtime_ns, engine.store.path.read_bytes())
    host.calls.clear()

    report = engine.run([_step(root, config, executor)])

    assert [o.outcome for o in report.outcomes] == [Outcome.SKIPPED]
    assert host.calls == [DPKG]
    assert (conf.read_bytes(), conf.stat().st_mtime_ns, engine.store.path.read_bytes()) == snapshot


def test_failed_update_stops_before_the_install(root: Path, config, tmp_path) -> None:
    _write_conf(root)
    host = FakeHost(root)
    host.failures[UPDATE] = ProcessOutput(100, "", "E: Failed to fetch http://mirror/...\n")
    report, _, _ = _run(root, config, host, tmp_path)
    failure = report.failure
    assert (failure.phase, failure.message) == (Phase.APPLY, "apply() raised UnexpectedExitError")
    assert APT_UPDATE_FAILURE_HINT in failure.next_action
    assert host.calls == [UPDATE]  # no install, no file edit, no restart
    assert (root / "etc/chrony/chrony.conf").read_text() == SAMPLE_CONF


def test_apt_failure_carries_the_apt_hint(root: Path, config, tmp_path) -> None:
    host = FakeHost(root)
    host.failures[APT] = ProcessOutput(100, "", "E: Could not get lock /var/lib/dpkg/lock\n")
    report, _, _ = _run(root, config, host, tmp_path)
    failure = report.failure
    assert (failure.phase, failure.message) == (Phase.APPLY, "apply() raised UnexpectedExitError")
    assert APT_FAILURE_HINT in failure.next_action
    assert host.calls == [UPDATE, APT]  # no retry, no recovery command


def test_mutating_commands_are_refused_under_a_non_host_root(
    root: Path, config: dict, tmp_path: Path
) -> None:
    _write_conf(root)
    host = FakeHost(root)
    report, _, _ = _run(root, config, host, tmp_path, executor_root=root)
    assert report.failure.phase is Phase.APPLY
    assert report.failure.message == f"apply() raised {MutationRefusedError.__name__}"
    assert "not '/'" in report.failure.details
    assert host.calls == []
    assert (root / "etc/chrony/chrony.conf").read_text() == SAMPLE_CONF  # apply stopped first


# --- verify() ------------------------------------------------------------------------------


def test_verify_success_names_the_server_and_says_sync_is_not_checked(
    root, config, tmp_path
) -> None:
    _write_conf(root, EDITED_CONF)
    result = _step(root, config, _executor(tmp_path, FakeHost(root, active=True))).verify()
    assert result.ok
    assert result.details == (
        "/etc/chrony/chrony.conf has the managed block with 'server controller iburst' and "
        f"no other active pool/server line; chrony is active. {TIME_SYNC_NOT_CHECKED}"
    )


def test_verify_fails_when_the_service_is_not_active(root: Path, config, tmp_path) -> None:
    _write_conf(root)
    host = FakeHost(root, installed=True)
    host.failures[RESTART] = ProcessOutput(0, "", "")  # restart "succeeds" but nothing starts
    report, _, _ = _run(root, config, host, tmp_path)
    assert report.failure.phase is Phase.VERIFY
    assert report.failure.details == (
        f"the chrony service is not active (state: inactive). {TIME_SYNC_NOT_CHECKED}"
    )


def test_verify_reports_file_and_service_problems_together(root, config, tmp_path) -> None:
    _write_conf(root, SAMPLE_CONF + BLOCK)
    result = _step(root, config, _executor(tmp_path, FakeHost(root))).verify()
    assert not result.ok
    assert result.details.startswith("active source line(s) outside the block: pool ")
    assert "; the chrony service is not active (state: inactive). " in result.details
    assert result.details.endswith(TIME_SYNC_NOT_CHECKED)


def test_verify_when_the_file_is_missing(root, config, tmp_path) -> None:
    result = _step(root, config, _executor(tmp_path, FakeHost(root, active=True))).verify()
    assert result == result.__class__(
        False, f"/etc/chrony/chrony.conf does not exist. {TIME_SYNC_NOT_CHECKED}"
    )


# --- CLI ------------------------------------------------------------------------------------


def _apply(root: Path, node: str = "forge-compute-01", *extra: str) -> list[str]:
    return [
        "apply", "--config", str(EXAMPLE_CONFIG), "--step", "time-sync-other",
        "--node", node, "--root", str(root), *extra,
    ]  # fmt: skip


@pytest.mark.parametrize("node", ["forge-compute-01", "forge-block-01", "forge-object-01"])
def test_cli_dry_run_shows_diff_and_exact_argv(
    root: Path, capsys: pytest.CaptureFixture[str], node: str
) -> None:
    path = _write_conf(root)
    rc = main(_apply(root, node))
    out = capsys.readouterr().out
    assert rc == 0
    assert "WARNING: Step 'time-sync-other' is marked UNVERIFIED" in out
    assert "no files modified and no commands run" in out
    assert "--- a/etc/chrony/chrony.conf" in out
    assert "-pool 2.debian.pool.ntp.org offline iburst" in out
    assert f"+{DISABLED_LINE_PREFIX}pool 2.debian.pool.ntp.org offline iburst" in out
    assert "+server controller iburst" in out
    assert (
        "Commands apply() would run, in order, if check() finds the step not done:\n"
        "  /usr/bin/apt-get update\n"
        "  /usr/bin/apt-get install --yes chrony\n"
        "  /usr/bin/systemctl restart chrony\n"
    ) in out
    assert (
        "Read-only commands check(), apply() and verify() may run (not run in a dry run):\n"
        "  /usr/bin/dpkg-query --status chrony\n"
        "  /usr/bin/systemctl is-active chrony\n"
    ) in out
    assert "the /etc/hosts alias written by the hosts-file step" in out
    assert "download from the configured apt sources (network)" in out
    assert path.read_text() == SAMPLE_CONF
    assert not (root / DEFAULT_STATE_TARGET.lstrip("/")).exists()
    assert not (root / DEFAULT_AUDIT_TARGET.lstrip("/")).exists()


def test_cli_dry_run_when_chrony_is_not_installed(
    root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rc = main(_apply(root))
    out = capsys.readouterr().out
    assert rc == 0
    assert (
        "/etc/chrony/chrony.conf does not exist yet; it is installed by the chrony package" in out
    )
    assert BLOCK in out
    assert "/usr/bin/apt-get install --yes chrony" in out
    assert not (root / "etc/chrony/chrony.conf").exists()


def test_cli_yes_with_a_non_host_root_is_refused(
    root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = _write_conf(root)
    rc = main(_apply(root, "forge-block-01", "--yes"))
    captured = capsys.readouterr()
    assert rc == 1
    assert "Step 'time-sync-other' runs system commands" in captured.err
    assert "--yes with --root other than '/' is refused" in captured.err
    assert path.read_text() == SAMPLE_CONF
    assert not (root / DEFAULT_STATE_TARGET.lstrip("/")).exists()
    assert not (root / DEFAULT_AUDIT_TARGET.lstrip("/")).exists()


def test_cli_rejects_the_controller(root: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = _write_conf(root)
    rc = main(_apply(root, "forge-controller-01"))
    assert rc == 1
    assert "does not apply to role 'controller'" in capsys.readouterr().err
    assert path.read_text() == SAMPLE_CONF


@pytest.mark.skipif(os.geteuid() == 0, reason="must run as a normal user")
def test_cli_yes_on_the_host_requires_uid_0(capsys: pytest.CaptureFixture[str]) -> None:
    argv = [
        "apply", "--config", str(EXAMPLE_CONFIG), "--step", "time-sync-other",
        "--node", "forge-compute-01", "--yes",
    ]  # fmt: skip
    rc = main(argv)
    assert rc == 1
    assert "requires root privileges (effective uid 0)" in capsys.readouterr().err
