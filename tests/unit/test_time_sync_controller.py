"""Tests for the time-sync-controller step (fake backend, temporary roots only).

The executor used by step tests gets root "/" with a fake backend and a fake uid 0, so
mutating specs pass the safety check while nothing is executed; every file edit uses a
temporary root. The sample chrony.conf below is test data, not a copy of Ubuntu's file.
"""

from __future__ import annotations

import copy
import os
import socket
import subprocess
from collections.abc import Mapping
from pathlib import Path

import pytest

from openstack_forge.cli import main
from openstack_forge.config import ConfigValidationError, load_config, validate_config
from openstack_forge.execution import (
    APT_UPDATE_FAILURE_HINT,
    CHRONY_BLOCK_NAME,
    DEFAULT_AUDIT_TARGET,
    DEFAULT_REGISTRY,
    DEFAULT_STATE_TARGET,
    DISABLED_LINE_PREFIX,
    MASK,
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
    TimeSyncControllerStep,
    UnsafePathError,
    edit_chrony_conf,
    parse_dpkg_status,
    verify_chrony_text,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURE_CONFIG = REPO_ROOT / "tests" / "fixtures" / "valid_cluster.yaml"
EXAMPLE_CONFIG = REPO_ROOT / "examples" / "cluster.example.yaml"

SAMPLE_CONF = """\
# Sample chrony configuration used by the tests.
confdir /etc/chrony/conf.d
pool ntp.example.org iburst maxsources 4
pool 1.pool.example.org iburst
server 192.0.2.250 iburst
#pool commented.example.org iburst
sourcedir /run/chrony-dhcp
driftfile /var/lib/chrony/chrony.drift
makestep 1 3
"""
BEGIN, END = f"# BEGIN {CHRONY_BLOCK_NAME}", f"# END {CHRONY_BLOCK_NAME}"

DPKG = ("/usr/bin/dpkg-query", "--status", "chrony")
UPDATE = ("/usr/bin/apt-get", "update")
APT = ("/usr/bin/apt-get", "install", "--yes", "chrony")
RESTART = ("/usr/bin/systemctl", "restart", "chrony")
IS_ACTIVE = ("/usr/bin/systemctl", "is-active", "chrony")


@pytest.fixture(autouse=True)
def _forbid_processes_and_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*args, **kwargs):
        raise AssertionError(f"no real process or network access in these tests: {args!r}")

    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(os, "system", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)


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


@pytest.fixture
def servers_config(config: dict) -> dict:
    configured = copy.deepcopy(config)
    configured["cluster"]["ntp_servers"] = ["ntp1.example.internal", "192.0.2.123"]
    return configured


def _controller(config: Mapping) -> Mapping:
    return next(node for node in config["nodes"] if node["role"] == "controller")


def _executor(tmp_path: Path, backend, *, root: Path = Path("/")) -> CommandExecutor:
    (tmp_path / "audit").mkdir(exist_ok=True)
    return CommandExecutor(
        DEFAULT_REGISTRY, root=root, audit_log=AuditLog(tmp_path / "audit/commands.log"),
        backend=backend, euid=lambda: 0,
    )  # fmt: skip


def _step(root: Path, config: Mapping, executor: CommandExecutor) -> TimeSyncControllerStep:
    return TimeSyncControllerStep(root, config, _controller(config), executor)


def _write_conf(root: Path, text: str = SAMPLE_CONF) -> Path:
    path = root / "etc/chrony/chrony.conf"
    path.write_text(text)
    return path


# --- file edits ---------------------------------------------------------------------------


def test_without_ntp_servers_the_default_pool_is_kept(root: Path, config: dict) -> None:
    path = _write_conf(root)
    change = edit_chrony_conf(root, config, dry_run=False)
    assert change.changed and change.backup == "/etc/chrony/chrony.conf.orig"
    assert path.read_text() == SAMPLE_CONF + f"{BEGIN}\nallow 192.0.2.0/24\n{END}\n"


def test_with_ntp_servers_existing_sources_are_commented_out(
    root: Path, servers_config: dict
) -> None:
    path = _write_conf(root)
    edit_chrony_conf(root, servers_config, dry_run=False)
    assert path.read_text() == (
        "# Sample chrony configuration used by the tests.\n"
        "confdir /etc/chrony/conf.d\n"
        f"{DISABLED_LINE_PREFIX}pool ntp.example.org iburst maxsources 4\n"
        f"{DISABLED_LINE_PREFIX}pool 1.pool.example.org iburst\n"
        f"{DISABLED_LINE_PREFIX}server 192.0.2.250 iburst\n"
        "#pool commented.example.org iburst\n"
        "sourcedir /run/chrony-dhcp\n"
        "driftfile /var/lib/chrony/chrony.drift\n"
        "makestep 1 3\n"
        f"{BEGIN}\n"
        "server ntp1.example.internal iburst\n"
        "server 192.0.2.123 iburst\n"
        "allow 192.0.2.0/24\n"
        f"{END}\n"
    )


@pytest.mark.parametrize("use_servers", [False, True])
def test_edit_is_idempotent_and_never_touches_its_own_block(
    root: Path, config: dict, servers_config: dict, use_servers: bool
) -> None:
    chosen = servers_config if use_servers else config
    path = _write_conf(root)
    edit_chrony_conf(root, chosen, dry_run=False)
    before = (path.read_bytes(), path.stat().st_mtime_ns, path.stat().st_ino)
    second = edit_chrony_conf(root, chosen, dry_run=False)
    assert (second.changed, second.diff) == (False, "")
    assert (path.read_bytes(), path.stat().st_mtime_ns, path.stat().st_ino) == before
    assert verify_chrony_text(path.read_text(), chosen) == []


def test_removing_ntp_servers_restores_the_default_sources(
    root: Path, config: dict, servers_config: dict
) -> None:
    path = _write_conf(root)
    edit_chrony_conf(root, servers_config, dry_run=False)
    edit_chrony_conf(root, config, dry_run=False)
    assert path.read_text() == SAMPLE_CONF + f"{BEGIN}\nallow 192.0.2.0/24\n{END}\n"


def test_changing_ntp_servers_replaces_the_block(root: Path, servers_config: dict) -> None:
    path = _write_conf(root)
    edit_chrony_conf(root, servers_config, dry_run=False)
    servers_config["cluster"]["ntp_servers"] = ["ntp3.example.internal"]
    edit_chrony_conf(root, servers_config, dry_run=False)
    text = path.read_text()
    assert text.count(BEGIN) == 1
    assert "server ntp3.example.internal iburst\nallow 192.0.2.0/24\n" in text
    assert "ntp1.example.internal" not in text


def test_dry_run_edit_writes_nothing(root: Path, servers_config: dict) -> None:
    path = _write_conf(root)
    change = edit_chrony_conf(root, servers_config, dry_run=True)
    assert change.changed and "+allow 192.0.2.0/24" in change.diff
    assert f"+{DISABLED_LINE_PREFIX}pool ntp.example.org" in change.diff
    assert path.read_text() == SAMPLE_CONF
    assert not (root / "etc/chrony/chrony.conf.orig").exists()


def test_missing_chrony_conf_is_not_created(root: Path, config: dict) -> None:
    with pytest.raises(UnsafePathError, match="installed by the chrony package"):
        edit_chrony_conf(root, config, dry_run=False)
    assert not (root / "etc/chrony/chrony.conf").exists()


def test_broken_markers_are_refused(root: Path, config: dict) -> None:
    path = _write_conf(root, SAMPLE_CONF + f"{BEGIN}\nallow 10.0.0.0/8\n")
    with pytest.raises(FileFormatError, match="broken"):
        edit_chrony_conf(root, config, dry_run=False)
    assert path.read_text().endswith("allow 10.0.0.0/8\n")


def test_step_requires_the_controller_and_a_management_network(
    root: Path, config: dict, tmp_path: Path
) -> None:
    executor = _executor(tmp_path, FakeHost(root))
    compute = next(node for node in config["nodes"] if node["role"] == "compute")
    with pytest.raises(ExecutionError, match="only runs on the controller"):
        TimeSyncControllerStep(root, config, compute, executor)
    no_mgmt = copy.deepcopy(config)
    del no_mgmt["networks"]["management"]
    with pytest.raises(ExecutionError, match="networks.management"):
        _step(root, no_mgmt, executor)


@pytest.mark.parametrize(
    "server", ["ntp; rm -rf /", "ntp\nallow 0.0.0.0/0", "$(id)", "-x", "999.1.1.1", "a b"]
)
def test_ntp_servers_cannot_inject_configuration(config: dict, server: str) -> None:
    config["cluster"]["ntp_servers"] = [server]
    with pytest.raises(ConfigValidationError):
        validate_config(config)


# --- pure verification helpers -------------------------------------------------------------


def test_verify_chrony_text_reports_problems(config: dict, servers_config: dict) -> None:
    assert verify_chrony_text(SAMPLE_CONF, config) == [
        f"the managed block '{CHRONY_BLOCK_NAME}' is missing"
    ]
    wrong = SAMPLE_CONF + f"{BEGIN}\nallow 10.0.0.0/8\n{END}\n"
    assert verify_chrony_text(wrong, config) == [
        "'allow 192.0.2.0/24' is missing from the managed block",
        "the managed block differs from the configuration",
    ]
    block = f"{BEGIN}\nserver ntp1.example.internal iburst\nserver 192.0.2.123 iburst\n"
    block += f"allow 192.0.2.0/24\n{END}\n"
    assert verify_chrony_text(SAMPLE_CONF + block, servers_config) == [
        "active source line(s) outside the block: pool ntp.example.org iburst maxsources 4, "
        "pool 1.pool.example.org iburst, server 192.0.2.250 iburst"
    ]


@pytest.mark.parametrize(
    ("text", "installed"),
    [
        ("Package: chrony\nStatus: install ok installed\n", True),
        ("Package: chrony\nStatus: deinstall ok config-files\n", False),
        ("Package: chrony\nStatus: install reinstreq half-configured\n", False),
        ("Package: chrony\nStatus: install ok half-configured\n", False),
        ("Package: chrony\n", False),
        ("", False),
    ],
)
def test_parse_dpkg_status(text: str, installed: bool) -> None:
    assert parse_dpkg_status(text) is installed


# --- check / apply / verify through the engine ---------------------------------------------


def test_fresh_host_install_edit_restart_verify(tmp_path: Path, config: dict) -> None:
    root = tmp_path / "root"
    (root / "etc").mkdir(parents=True)  # no chrony directory: the package is not installed
    host = FakeHost(root)
    executor = _executor(tmp_path, host)
    store = StateStore(tmp_path / "state.json")

    report = Engine(store).run([_step(root, config, executor)])

    assert report.ok, report.failure
    assert [o.outcome for o in report.outcomes] == [Outcome.APPLIED]
    # check() stops at the missing file, so no dpkg-query is needed before applying.
    assert host.calls == [UPDATE, APT, RESTART, IS_ACTIVE]
    assert (
        (root / "etc/chrony/chrony.conf")
        .read_text()
        .endswith(f"{BEGIN}\nallow 192.0.2.0/24\n{END}\n")
    )
    assert store.get("time-sync-controller").status is StepStatus.DONE


def test_installed_and_configured_host_is_skipped(root: Path, config: dict, tmp_path) -> None:
    _write_conf(root, SAMPLE_CONF + f"{BEGIN}\nallow 192.0.2.0/24\n{END}\n")
    host = FakeHost(root, installed=True, active=True)
    report = Engine(StateStore(tmp_path / "state.json")).run(
        [_step(root, config, _executor(tmp_path, host))]
    )
    assert [o.outcome for o in report.outcomes] == [Outcome.SKIPPED]
    assert host.calls == [DPKG]


def test_rerun_after_success_runs_no_mutating_command(root: Path, servers_config, tmp_path) -> None:
    _write_conf(root)
    host = FakeHost(root, installed=True)
    executor = _executor(tmp_path, host)
    engine = Engine(StateStore(tmp_path / "state.json"))
    assert engine.run([_step(root, servers_config, executor)]).ok
    conf = root / "etc/chrony/chrony.conf"
    snapshot = (conf.read_bytes(), conf.stat().st_mtime_ns, engine.store.path.read_bytes())
    host.calls.clear()

    report = engine.run([_step(root, servers_config, executor)])

    assert [o.outcome for o in report.outcomes] == [Outcome.SKIPPED]
    assert host.calls == [DPKG]
    assert (conf.read_bytes(), conf.stat().st_mtime_ns, engine.store.path.read_bytes()) == snapshot


def test_configured_file_but_missing_package_reinstalls(root: Path, config, tmp_path) -> None:
    _write_conf(root, SAMPLE_CONF + f"{BEGIN}\nallow 192.0.2.0/24\n{END}\n")
    host = FakeHost(root, installed=False)
    report = Engine(StateStore(tmp_path / "state.json")).run(
        [_step(root, config, _executor(tmp_path, host))]
    )
    assert report.ok
    assert host.calls == [DPKG, UPDATE, APT, RESTART, IS_ACTIVE]


def test_apply_updates_the_package_lists_before_installing(root: Path, config, tmp_path) -> None:
    # A fresh image may have stale or empty package lists.
    _write_conf(root)
    host = FakeHost(root)
    _step(root, config, _executor(tmp_path, host)).apply()
    assert host.calls[:2] == [UPDATE, APT]
    assert host.calls.count(UPDATE) == 1


def test_failed_update_stops_before_the_install(root: Path, config, tmp_path) -> None:
    _write_conf(root)
    host = FakeHost(root)
    host.failures[UPDATE] = ProcessOutput(100, "", "E: Failed to fetch http://mirror/...\n")
    report = Engine(StateStore(tmp_path / "state.json")).run(
        [_step(root, config, _executor(tmp_path, host))]
    )
    failure = report.failure
    assert (failure.phase, failure.message) == (Phase.APPLY, "apply() raised UnexpectedExitError")
    assert APT_UPDATE_FAILURE_HINT in failure.next_action
    assert "network" in failure.next_action
    assert host.calls == [UPDATE]  # no install, no file edit, no restart
    assert (root / "etc/chrony/chrony.conf").read_text() == SAMPLE_CONF


def test_mutating_commands_are_refused_under_a_non_host_root(
    root: Path, config: dict, tmp_path: Path
) -> None:
    _write_conf(root)
    host = FakeHost(root)
    executor = _executor(tmp_path, host, root=root)  # the same temporary root as the files
    store = StateStore(tmp_path / "state.json")

    report = Engine(store).run([_step(root, config, executor)])

    assert report.failure.phase is Phase.APPLY
    assert report.failure.message == f"apply() raised {MutationRefusedError.__name__}"
    assert "not '/'" in report.failure.details
    assert APT not in host.calls and RESTART not in host.calls and UPDATE not in host.calls
    assert (root / "etc/chrony/chrony.conf").read_text() == SAMPLE_CONF  # apply stopped first


def test_verify_fails_when_the_service_is_not_active(root: Path, config, tmp_path) -> None:
    _write_conf(root)
    host = FakeHost(root, installed=True)
    host.failures[RESTART] = ProcessOutput(0, "", "")  # restart "succeeds" but nothing starts
    report = Engine(StateStore(tmp_path / "state.json")).run(
        [_step(root, config, _executor(tmp_path, host))]
    )
    assert report.failure.phase is Phase.VERIFY
    assert report.failure.details == "the chrony service is not active (state: inactive)"


def test_verify_says_time_sync_is_not_checked(root: Path, config, tmp_path) -> None:
    _write_conf(root)
    step = _step(root, config, _executor(tmp_path, FakeHost(root, installed=True, active=True)))
    step.apply()
    result = step.verify()
    assert result.ok
    assert "Actual time synchronization is not checked" in result.details


def test_apt_failure_suggests_dpkg_and_lock_checks(root: Path, config, tmp_path) -> None:
    host = FakeHost(root)
    host.failures[APT] = ProcessOutput(
        100,
        "",
        "E: Could not get lock /var/lib/dpkg/lock-frontend\n"
        "E: Failed to fetch http://mirror:S3CRETPASS@archive.example/chrony.deb\n",
    )
    store = StateStore(tmp_path / "state.json")
    executor = _executor(tmp_path, host)

    report = Engine(store).run([_step(root, config, executor)])

    failure = report.failure
    assert (failure.phase, failure.message) == (Phase.APPLY, "apply() raised UnexpectedExitError")
    assert "half-configured" in failure.next_action
    assert "dpkg --audit" in failure.next_action
    assert "lock" in failure.next_action
    assert "does not attempt any automatic recovery" in failure.next_action
    assert host.calls == [UPDATE, APT]  # no retry, no recovery command
    for text in (failure.details, store.path.read_text(), executor.audit_log.path.read_text()):
        assert "S3CRETPASS" not in text
    assert MASK in failure.details
    finish = executor.audit_log.read()[-1]
    assert (finish["command_id"], finish["outcome"], finish["exit_code"]) == (
        "apt-get.install",
        "unexpected_exit",
        100,
    )


def test_audit_records_for_a_successful_run(root: Path, config, tmp_path) -> None:
    _write_conf(root)
    executor = _executor(tmp_path, FakeHost(root))
    Engine(StateStore(tmp_path / "state.json")).run([_step(root, config, executor)])
    # check() already fails on the file (no block yet), so dpkg-query is not needed.
    starts = [e for e in executor.audit_log.read() if e["event"] == "start"]
    assert [(e["command_id"], e["argv"], e["mutates"]) for e in starts] == [
        ("apt-get.update", list(UPDATE), True),
        ("apt-get.install", list(APT), True),
        ("systemctl.restart", list(RESTART), True),
        ("systemctl.is-active", list(IS_ACTIVE), False),
    ]
    finishes = [e for e in executor.audit_log.read() if e["event"] == "finish"]
    assert [e["outcome"] for e in finishes] == ["ok"] * 4


# --- CLI ------------------------------------------------------------------------------------


def _apply(root: Path, *extra: str) -> list[str]:
    return [
        "apply", "--config", str(EXAMPLE_CONFIG), "--step", "time-sync-controller",
        "--node", "forge-controller-01", "--root", str(root), *extra,
    ]  # fmt: skip


def test_cli_dry_run_shows_diff_and_exact_argv(
    root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = _write_conf(root)
    rc = main(_apply(root))
    out = capsys.readouterr().out
    assert rc == 0
    assert "WARNING: Step 'time-sync-controller' is marked UNVERIFIED" in out
    assert "no files modified and no commands run" in out
    assert "--- a/etc/chrony/chrony.conf" in out
    assert "+allow 10.10.40.0/24" in out
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
    assert f"{BEGIN}\nallow 10.10.40.0/24\n{END}\n" in out
    assert "/usr/bin/apt-get install --yes chrony" in out
    assert not (root / "etc/chrony/chrony.conf").exists()


def test_cli_yes_with_a_non_host_root_is_refused(
    root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = _write_conf(root)
    rc = main(_apply(root, "--yes"))
    captured = capsys.readouterr()
    assert rc == 1
    assert "runs system commands" in captured.err
    assert "--yes with --root other than '/' is refused" in captured.err
    assert path.read_text() == SAMPLE_CONF
    assert not (root / DEFAULT_STATE_TARGET.lstrip("/")).exists()
    assert not (root / DEFAULT_AUDIT_TARGET.lstrip("/")).exists()
    assert "sudo" not in captured.out


@pytest.mark.skipif(os.geteuid() == 0, reason="must run as a normal user")
def test_cli_yes_on_the_host_requires_uid_0(capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(
        [
            "apply",
            "--config",
            str(EXAMPLE_CONFIG),
            "--step",
            "time-sync-controller",
            "--node",
            "forge-controller-01",
            "--yes",
        ]  # fmt: skip
    )
    assert rc == 1
    assert "requires root privileges (effective uid 0)" in capsys.readouterr().err


def test_cli_rejects_non_controller_node(root: Path, capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(
        [
            "apply",
            "--config",
            str(EXAMPLE_CONFIG),
            "--step",
            "time-sync-controller",
            "--node",
            "forge-compute-01",
            "--root",
            str(root),
        ]  # fmt: skip
    )
    assert rc == 1
    assert "does not apply to role 'compute'" in capsys.readouterr().err
