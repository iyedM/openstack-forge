"""Tests for the mariadb step (fake backend, temporary roots only; nothing is executed).

As in the other command steps' tests, the executor gets root "/" with a fake backend and a
fake uid 0, so mutating specs pass the safety check while nothing runs; file edits always
use a temporary root.
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
    DEFAULT_AUDIT_TARGET,
    DEFAULT_REGISTRY,
    DEFAULT_STATE_TARGET,
    AuditLog,
    CommandExecutor,
    Engine,
    ExecutionError,
    MariaDBStep,
    Outcome,
    Phase,
    PreconditionError,
    ProcessOutput,
    StateStore,
    edit_mariadb_conf,
    mariadb_conf_content,
    verify_mariadb_text,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURE_CONFIG = REPO_ROOT / "tests" / "fixtures" / "valid_cluster.yaml"
EXAMPLE_CONFIG = REPO_ROOT / "examples" / "cluster.example.yaml"
CONF = "etc/mysql/mariadb.conf.d/99-openstack.cnf"

INSTALL_SERVER = ("/usr/bin/apt-get", "install", "--yes", "mariadb-server")
INSTALL_PYMYSQL = ("/usr/bin/apt-get", "install", "--yes", "python3-pymysql")
RESTART = ("/usr/bin/systemctl", "restart", "mariadb")
IS_ACTIVE = ("/usr/bin/systemctl", "is-active", "mariadb")
DPKG_SERVER = ("/usr/bin/dpkg-query", "--status", "mariadb-server")
DPKG_PYMYSQL = ("/usr/bin/dpkg-query", "--status", "python3-pymysql")

EXPECTED_CONTENT = (
    "[mysqld]\n"
    "bind-address = 192.0.2.11\n"
    "default-storage-engine = innodb\n"
    "innodb_file_per_table = on\n"
    "max_connections = 4096\n"
    "collation-server = utf8_general_ci\n"
    "character-set-server = utf8\n"
)


@pytest.fixture(autouse=True)
def _forbid_processes_and_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*args, **kwargs):
        raise AssertionError(f"no real process or network access in these tests: {args!r}")

    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(os, "system", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)


class FakeHost:
    """Fake backend simulating dpkg, apt-get and systemctl for MariaDB; runs nothing."""

    def __init__(self, root: Path, *, installed: set[str] = frozenset(), active=False) -> None:
        self.root = root
        self.installed = set(installed)
        self.active = active
        self.calls: list[tuple[str, ...]] = []
        self.failures: dict[tuple[str, ...], ProcessOutput] = {}
        self.conf_at_restart: str | None = None

    def run(
        self, argv: tuple[str, ...], *, env: Mapping[str, str], timeout: float
    ) -> ProcessOutput:
        self.calls.append(argv)
        if argv in self.failures:
            return self.failures[argv]
        if argv[:2] == ("/usr/bin/dpkg-query", "--status"):
            if argv[2] in self.installed:
                return ProcessOutput(0, f"Package: {argv[2]}\nStatus: install ok installed\n", "")
            return ProcessOutput(1, "", f"dpkg-query: package '{argv[2]}' is not installed\n")
        if argv[:3] == ("/usr/bin/apt-get", "install", "--yes"):
            self.installed.add(argv[3])
            if argv[3] == "mariadb-server":  # the package brings the configuration directory
                (self.root / "etc/mysql/mariadb.conf.d").mkdir(parents=True, exist_ok=True)
            return ProcessOutput(0, f"Setting up {argv[3]} ...\n", "")
        if argv == RESTART:
            conf = self.root / CONF
            self.conf_at_restart = conf.read_text() if conf.exists() else None
            self.active = True
            return ProcessOutput(0, "", "")
        if argv == IS_ACTIVE:
            if self.active:
                return ProcessOutput(0, "active\n", "")
            return ProcessOutput(3, "inactive\n", "")
        raise AssertionError(f"unexpected argv {argv!r}")


@pytest.fixture
def root(tmp_path: Path) -> Path:
    directory = tmp_path / "root"
    (directory / "etc").mkdir(parents=True)
    return directory


@pytest.fixture
def config() -> dict:
    return load_config(FIXTURE_CONFIG)


def _node(config: Mapping, role: str = "controller") -> Mapping:
    return next(node for node in config["nodes"] if node["role"] == role)


def _executor(tmp_path: Path, backend, *, root: Path = Path("/")) -> CommandExecutor:
    (tmp_path / "audit").mkdir(exist_ok=True)
    return CommandExecutor(
        DEFAULT_REGISTRY, root=root, audit_log=AuditLog(tmp_path / "audit/commands.log"),
        backend=backend, euid=lambda: 0,
    )  # fmt: skip


def _step(root: Path, config: Mapping, host: FakeHost, tmp_path: Path) -> MariaDBStep:
    return MariaDBStep(root, config, _node(config), _executor(tmp_path, host))


def _configured(root: Path, text: str = EXPECTED_CONTENT) -> Path:
    path = root / CONF
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


# --- role and file edit ---------------------------------------------------------------------


@pytest.mark.parametrize("role", ["compute", "block", "object"])
def test_step_refuses_non_controller_roles(root: Path, config, tmp_path, role: str) -> None:
    with pytest.raises(ExecutionError, match="only runs on the controller"):
        MariaDBStep(root, config, _node(config, role), _executor(tmp_path, FakeHost(root)))


def test_edit_creates_the_file_with_the_guide_keys(root: Path, config) -> None:
    (root / "etc/mysql/mariadb.conf.d").mkdir(parents=True)
    change = edit_mariadb_conf(root, _node(config), dry_run=False)
    assert (change.changed, change.created, change.backup) == (True, True, None)
    assert (root / CONF).read_text() == EXPECTED_CONTENT == mariadb_conf_content(_node(config))
    assert sorted(p.name for p in (root / "etc/mysql/mariadb.conf.d").iterdir()) == [
        "99-openstack.cnf"
    ]


def test_edit_never_creates_the_configuration_directory(root: Path, config) -> None:
    with pytest.raises(PreconditionError, match="created when mariadb-server is installed"):
        edit_mariadb_conf(root, _node(config), dry_run=False)
    assert not (root / "etc/mysql").exists()


def test_edit_fixes_a_wrong_key_and_keeps_other_content(root: Path, config) -> None:
    original = "# local notes\n[mysqld]\nbind-address = 127.0.0.1\nmax_connections = 100\n"
    path = _configured(root, original)
    change = edit_mariadb_conf(root, _node(config), dry_run=False)
    assert change.backup == f"/{CONF}.orig"
    assert (root / f"{CONF}.orig").read_text() == original
    assert path.read_text().startswith("# local notes\n[mysqld]\nbind-address = 192.0.2.11\n")
    assert verify_mariadb_text(path.read_text(), _node(config)) == []


def test_edit_is_idempotent(root: Path, config) -> None:
    path = _configured(root)
    before = (path.read_bytes(), path.stat().st_mtime_ns, path.stat().st_ino)
    change = edit_mariadb_conf(root, _node(config), dry_run=False)
    assert (change.changed, change.diff) == (False, "")
    assert (path.read_bytes(), path.stat().st_mtime_ns, path.stat().st_ino) == before


# --- verify_mariadb_text -------------------------------------------------------------------


def test_verify_text_reports_each_missing_or_wrong_key(config) -> None:
    text = (
        "[client]\nbind-address = 198.51.100.1\n"  # another section does not count
        "[mysqld]\nbind-address = 127.0.0.1\nmax_connections = 4096\n"
        "max_connections = 151\ncharacter-set-server = utf8\n"
    )
    assert verify_mariadb_text(text, _node(config)) == [
        "bind-address is '127.0.0.1' in [mysqld], expected '192.0.2.11'",
        "default-storage-engine is not set in [mysqld] (expected 'innodb')",
        "innodb_file_per_table is not set in [mysqld] (expected 'on')",
        "max_connections is '151' in [mysqld], expected '4096'",
        "collation-server is not set in [mysqld] (expected 'utf8_general_ci')",
    ]


def test_verify_text_ignores_comments(config) -> None:
    text = EXPECTED_CONTENT + "#bind-address = 127.0.0.1\n; max_connections = 1\n"
    assert verify_mariadb_text(text, _node(config)) == []


# --- check() -------------------------------------------------------------------------------


def test_check_is_false_without_the_file_and_runs_no_command(root, config, tmp_path) -> None:
    host = FakeHost(root, installed={"mariadb-server", "python3-pymysql"})
    assert _step(root, config, host, tmp_path).check() is False
    (root / "etc/mysql/mariadb.conf.d").mkdir(parents=True)
    assert _step(root, config, host, tmp_path).check() is False
    assert host.calls == []


def test_check_is_false_when_a_key_differs(root, config, tmp_path) -> None:
    _configured(root, EXPECTED_CONTENT.replace("max_connections = 4096", "max_connections = 1"))
    host = FakeHost(root, installed={"mariadb-server", "python3-pymysql"})
    assert _step(root, config, host, tmp_path).check() is False
    assert host.calls == []


def test_check_is_false_when_mariadb_server_is_missing(root, config, tmp_path) -> None:
    _configured(root)
    host = FakeHost(root, installed={"python3-pymysql"})
    assert _step(root, config, host, tmp_path).check() is False
    assert host.calls == [DPKG_SERVER]


def test_check_is_false_when_pymysql_is_missing(root, config, tmp_path) -> None:
    _configured(root)
    host = FakeHost(root, installed={"mariadb-server"})
    assert _step(root, config, host, tmp_path).check() is False
    assert host.calls == [DPKG_SERVER, DPKG_PYMYSQL]


def test_check_is_true_when_file_and_both_packages_are_in_place(root, config, tmp_path) -> None:
    _configured(root)
    host = FakeHost(root, installed={"mariadb-server", "python3-pymysql"})
    assert _step(root, config, host, tmp_path).check() is True
    assert host.calls == [DPKG_SERVER, DPKG_PYMYSQL]


# --- apply() and the engine ----------------------------------------------------------------


def test_apply_command_sequence_and_edit_timing(root: Path, config, tmp_path) -> None:
    host = FakeHost(root)
    report = Engine(StateStore(tmp_path / "state.json")).run([_step(root, config, host, tmp_path)])

    assert report.ok, report.failure
    assert [o.outcome for o in report.outcomes] == [Outcome.APPLIED]
    # check() stops at the missing directory; verify() runs dpkg-query then is-active.
    assert host.calls == [INSTALL_SERVER, INSTALL_PYMYSQL, RESTART, DPKG_PYMYSQL, IS_ACTIVE]
    # The file was written after the installs and before the restart.
    assert host.conf_at_restart == EXPECTED_CONTENT


def test_apply_fails_cleanly_if_the_package_brings_no_directory(root, config, tmp_path) -> None:
    host = FakeHost(root)
    host.failures[INSTALL_SERVER] = ProcessOutput(0, "", "")  # "succeeds", creates nothing
    report = Engine(StateStore(tmp_path / "state.json")).run([_step(root, config, host, tmp_path)])
    assert report.failure.phase is Phase.APPLY
    assert report.failure.message == "apply() raised PreconditionError"
    assert host.calls == [INSTALL_SERVER, INSTALL_PYMYSQL]
    assert RESTART not in host.calls


def test_rerun_after_success_changes_nothing(root: Path, config, tmp_path) -> None:
    host = FakeHost(root)
    engine = Engine(StateStore(tmp_path / "state.json"))
    assert engine.run([_step(root, config, host, tmp_path)]).ok
    conf = root / CONF
    snapshot = (conf.read_bytes(), conf.stat().st_mtime_ns, engine.store.path.read_bytes())
    host.calls.clear()

    report = engine.run([_step(root, config, host, tmp_path)])

    assert [o.outcome for o in report.outcomes] == [Outcome.SKIPPED]
    assert host.calls == [DPKG_SERVER, DPKG_PYMYSQL]
    assert (conf.read_bytes(), conf.stat().st_mtime_ns, engine.store.path.read_bytes()) == snapshot


def test_mutating_commands_are_refused_under_a_non_host_root(root, config, tmp_path) -> None:
    host = FakeHost(root)
    step = MariaDBStep(root, config, _node(config), _executor(tmp_path, host, root=root))
    report = Engine(StateStore(tmp_path / "state.json")).run([step])
    assert report.failure.message == "apply() raised MutationRefusedError"
    assert host.calls == []
    assert not (root / "etc/mysql").exists()


def test_apt_failure_hint_is_reported(root, config, tmp_path) -> None:
    host = FakeHost(root)
    host.failures[INSTALL_SERVER] = ProcessOutput(
        100, "", "E: Unable to locate package mariadb-server\n"
    )
    report = Engine(StateStore(tmp_path / "state.json")).run([_step(root, config, host, tmp_path)])
    assert "dpkg --audit" in report.failure.next_action
    assert host.calls == [INSTALL_SERVER]


# --- verify() ------------------------------------------------------------------------------

NOT_CHECKED = (
    "SQL connectivity and TCP listening on 192.0.2.11:3306 are not checked, and "
    "mysql_secure_installation is not run."
)


def _verify(root: Path, config, tmp_path, **host_kwargs):
    host = FakeHost(root, **host_kwargs)
    return _step(root, config, host, tmp_path).verify(), host


def test_verify_success_message(root: Path, config, tmp_path) -> None:
    _configured(root)
    result, host = _verify(root, config, tmp_path, installed={"python3-pymysql"}, active=True)
    assert result.ok
    assert result.details == (
        f"/{CONF} sets the guide's [mysqld] keys; python3-pymysql is installed; mariadb is "
        f"active. {NOT_CHECKED}"
    )
    assert host.calls == [DPKG_PYMYSQL, IS_ACTIVE]


def test_verify_missing_file(root: Path, config, tmp_path) -> None:
    (root / "etc/mysql/mariadb.conf.d").mkdir(parents=True)
    result, _ = _verify(root, config, tmp_path, installed={"python3-pymysql"}, active=True)
    assert not result.ok
    assert result.details == f"/{CONF} does not exist. {NOT_CHECKED}"


def test_verify_missing_directory(root: Path, config, tmp_path) -> None:
    result, _ = _verify(root, config, tmp_path, installed={"python3-pymysql"}, active=True)
    assert not result.ok
    assert result.details == (
        f"/{CONF} does not exist (/etc/mysql/mariadb.conf.d is missing). {NOT_CHECKED}"
    )


@pytest.mark.parametrize(
    ("old", "new", "problem"),
    [
        (
            "bind-address = 192.0.2.11",
            "bind-address = 0.0.0.0",
            "bind-address is '0.0.0.0' in [mysqld], expected '192.0.2.11'",
        ),
        (
            "character-set-server = utf8\n",
            "",
            "character-set-server is not set in [mysqld] (expected 'utf8')",
        ),
    ],
)
def test_verify_wrong_or_missing_key(root, config, tmp_path, old, new, problem) -> None:
    _configured(root, EXPECTED_CONTENT.replace(old, new))
    result, _ = _verify(root, config, tmp_path, installed={"python3-pymysql"}, active=True)
    assert not result.ok
    assert result.details == f"{problem}. {NOT_CHECKED}"


def test_verify_missing_pymysql(root: Path, config, tmp_path) -> None:
    _configured(root)
    result, _ = _verify(root, config, tmp_path, installed={"mariadb-server"}, active=True)
    assert result.details == f"python3-pymysql is not installed. {NOT_CHECKED}"


def test_verify_inactive_service(root: Path, config, tmp_path) -> None:
    _configured(root)
    result, _ = _verify(root, config, tmp_path, installed={"python3-pymysql"}, active=False)
    assert result.details == f"the mariadb service is not active (state: inactive). {NOT_CHECKED}"


# --- CLI -----------------------------------------------------------------------------------


def _apply_args(root: Path, *extra: str) -> list[str]:
    return [
        "apply", "--config", str(EXAMPLE_CONFIG), "--step", "mariadb",
        "--node", "forge-controller-01", "--root", str(root), *extra,
    ]  # fmt: skip


def _commands_section() -> str:
    return (
        "Commands apply() would run, in order, if check() finds the step not done:\n"
        "  /usr/bin/apt-get install --yes mariadb-server\n"
        "  /usr/bin/apt-get install --yes python3-pymysql\n"
        "  /usr/bin/systemctl restart mariadb\n"
    )


def test_cli_dry_run_when_the_directory_does_not_exist_yet(
    root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rc = main(_apply_args(root))
    out = capsys.readouterr().out
    assert rc == 0
    assert "WARNING: Step 'mariadb' is marked UNVERIFIED" in out
    assert "no files modified and no commands run" in out
    assert (
        "/etc/mysql/mariadb.conf.d does not exist yet; it is created when mariadb-server is "
        "installed. After that, /etc/mysql/mariadb.conf.d/99-openstack.cnf is written with:\n"
        "[mysqld]\nbind-address = 192.0.2.11\ndefault-storage-engine = innodb\n"
        "innodb_file_per_table = on\nmax_connections = 4096\n"
        "collation-server = utf8_general_ci\ncharacter-set-server = utf8\n"
    ) in out
    assert _commands_section() in out
    assert (
        "  /usr/bin/dpkg-query --status mariadb-server\n"
        "  /usr/bin/dpkg-query --status python3-pymysql\n"
        "  /usr/bin/systemctl is-active mariadb\n"
    ) in out
    assert "mysql_secure_installation is not run" in out
    assert not (root / "etc/mysql").exists()


def test_cli_dry_run_when_the_directory_exists(
    root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (root / "etc/mysql/mariadb.conf.d").mkdir(parents=True)
    rc = main(_apply_args(root))
    out = capsys.readouterr().out
    assert rc == 0
    assert "--- a/etc/mysql/mariadb.conf.d/99-openstack.cnf" in out
    assert "+bind-address = 192.0.2.11" in out
    assert "+character-set-server = utf8" in out
    assert _commands_section() in out
    assert not (root / CONF).exists()
    assert not (root / DEFAULT_STATE_TARGET.lstrip("/")).exists()
    assert not (root / DEFAULT_AUDIT_TARGET.lstrip("/")).exists()


def test_cli_yes_with_a_non_host_root_is_refused(
    root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (root / "etc/mysql/mariadb.conf.d").mkdir(parents=True)
    rc = main(_apply_args(root, "--yes"))
    err = capsys.readouterr().err
    assert rc == 1
    assert "--yes with --root other than '/' is refused" in err
    assert not (root / CONF).exists()
    assert not (root / DEFAULT_STATE_TARGET.lstrip("/")).exists()
    assert not (root / DEFAULT_AUDIT_TARGET.lstrip("/")).exists()


def test_cli_rejects_a_non_controller_node(root: Path, capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(
        [
            "apply",
            "--config",
            str(EXAMPLE_CONFIG),
            "--step",
            "mariadb",
            "--node",
            "forge-compute-01",
            "--root",
            str(root),
        ]  # fmt: skip
    )
    assert rc == 1
    assert "does not apply to role 'compute'" in capsys.readouterr().err
