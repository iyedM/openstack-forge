"""Tests for the etcd step (fake backend, temporary roots only; nothing is executed).

``REAL_PACKAGE_DEFAULT`` is the verbatim five-line comment-only ``/etc/default/etcd``
conffile shipped by ``etcd-server 3.4.30-1ubuntu0.24.04.3`` on Ubuntu 24.04 (dpkg conffile
MD5 ``b7921986da4a49739814359cc296d2fb``); ``DEV_MACHINE_ETCD`` is the 15-line
``/etc/default/etcd`` read on the development machine, where the nine OpenStack guide
``ETCD_*`` lines had been appended after that header (with the development machine's local IP
sanitized to the RFC 5737 documentation address ``192.0.2.170`` for the repository).
"""

from __future__ import annotations

import copy
import os
import socket
import subprocess
from collections.abc import Mapping
from pathlib import Path

import pytest
from _service_fakes import ServiceHost

from openstack_forge.cli import main
from openstack_forge.config import load_config
from openstack_forge.execution import (
    DEFAULT_AUDIT_TARGET,
    DEFAULT_REGISTRY,
    DEFAULT_STATE_TARGET,
    DISABLED_LINE_PREFIX,
    AuditLog,
    CommandExecutor,
    Engine,
    EtcdStep,
    ExecutionError,
    Outcome,
    Phase,
    PreconditionError,
    ProcessOutput,
    StateStore,
    edit_etcd_defaults,
    etcd_block,
    verify_etcd_text,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURE_CONFIG = REPO_ROOT / "tests" / "fixtures" / "valid_cluster.yaml"
EXAMPLE_CONFIG = REPO_ROOT / "examples" / "cluster.example.yaml"
DEFAULTS = "etc/default/etcd"
BEGIN, END = "# BEGIN openstack-forge etcd", "# END openstack-forge etcd"

POLICY = ("/usr/bin/apt-cache", "policy", "etcd-server")
INSTALL = ("/usr/bin/apt-get", "install", "--yes", "etcd-server")
ENABLE = ("/usr/bin/systemctl", "enable", "etcd")
RESTART = ("/usr/bin/systemctl", "restart", "etcd")
IS_ACTIVE = ("/usr/bin/systemctl", "is-active", "etcd")
IS_ENABLED = ("/usr/bin/systemctl", "is-enabled", "etcd")
DPKG = ("/usr/bin/dpkg-query", "--status", "etcd-server")

EXPECTED_BLOCK = (
    'ETCD_NAME="controller"\n'
    'ETCD_DATA_DIR="/var/lib/etcd"\n'
    'ETCD_INITIAL_CLUSTER_STATE="new"\n'
    'ETCD_INITIAL_CLUSTER_TOKEN="etcd-cluster-01"\n'
    'ETCD_INITIAL_CLUSTER="controller=http://192.0.2.11:2380"\n'
    'ETCD_INITIAL_ADVERTISE_PEER_URLS="http://192.0.2.11:2380"\n'
    'ETCD_ADVERTISE_CLIENT_URLS="http://192.0.2.11:2379"\n'
    'ETCD_LISTEN_PEER_URLS="http://0.0.0.0:2380"\n'
    'ETCD_LISTEN_CLIENT_URLS="http://192.0.2.11:2379"\n'
)
BLOCK = f"{BEGIN}\n{EXPECTED_BLOCK}{END}\n"

# Verbatim pristine /etc/default/etcd conffile from etcd-server 3.4.30-1ubuntu0.24.04.3
# (dpkg conffile MD5 b7921986da4a49739814359cc296d2fb).
REAL_PACKAGE_DEFAULT = (
    "## etcd(1) daemon options\n"
    '## See "/usr/share/doc/etcd-server/op-guide/configuration.md.gz"\n'
    "## for available options.\n"
    "##\n"
    "## Use environment to override, for example: ETCD_NAME=default\n"
)
# /etc/default/etcd read on the development machine (the five-line header followed by nine
# hand-edited OpenStack guide assignments, with the development machine's local IP sanitized
# to the RFC 5737 documentation address 192.0.2.170 for the repository).
DEV_MACHINE_ETCD = (
    REAL_PACKAGE_DEFAULT + 'ETCD_NAME="controller"\n'
    'ETCD_DATA_DIR="/var/lib/etcd"\n'
    'ETCD_INITIAL_CLUSTER_STATE="new"\n'
    'ETCD_INITIAL_CLUSTER_TOKEN="etcd-cluster-01"\n'
    'ETCD_INITIAL_CLUSTER="controller=http://192.0.2.170:2380"\n'
    'ETCD_INITIAL_ADVERTISE_PEER_URLS="http://192.0.2.170:2380"\n'
    'ETCD_ADVERTISE_CLIENT_URLS="http://192.0.2.170:2379"\n'
    'ETCD_LISTEN_PEER_URLS="http://0.0.0.0:2380"\n'
    'ETCD_LISTEN_CLIENT_URLS="http://192.0.2.170:2379"\n'
)
SAMPLE = (
    REAL_PACKAGE_DEFAULT + '#ETCD_DATA_DIR="/var/lib/etcd/default"\n'
    'ETCD_NAME="default"\n'
    'export ETCD_LISTEN_CLIENT_URLS="http://localhost:2379"\n'
    'ETCD_LISTEN_METRICS_URLS="http://localhost:2381"\n'
)


@pytest.fixture(autouse=True)
def _forbid_processes_and_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*args, **kwargs):
        raise AssertionError(f"no real process or network access in these tests: {args!r}")

    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(os, "system", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)


@pytest.fixture
def root(tmp_path: Path) -> Path:
    directory = tmp_path / "root"
    (directory / "etc/default").mkdir(parents=True)
    return directory


@pytest.fixture
def config() -> dict:
    return load_config(FIXTURE_CONFIG)


def _node(config: Mapping, role: str = "controller") -> Mapping:
    return next(node for node in config["nodes"] if node["role"] == role)


def _host(root: Path, **kwargs) -> ServiceHost:
    kwargs.setdefault("creates", {"etcd-server": (DEFAULTS, REAL_PACKAGE_DEFAULT)})
    return ServiceHost(root, watch=DEFAULTS, **kwargs)


def _executor(tmp_path: Path, backend, *, root: Path = Path("/")) -> CommandExecutor:
    (tmp_path / "audit").mkdir(exist_ok=True)
    return CommandExecutor(
        DEFAULT_REGISTRY, root=root, audit_log=AuditLog(tmp_path / "audit/commands.log"),
        backend=backend, euid=lambda: 0,
    )  # fmt: skip


def _step(root: Path, config, host, tmp_path) -> EtcdStep:
    return EtcdStep(root, config, _node(config), _executor(tmp_path, host))


def _defaults(root: Path, text: str = SAMPLE) -> Path:
    path = root / DEFAULTS
    path.write_text(text)
    return path


def _run(root, config, host, tmp_path, store=None):
    store = store or StateStore(tmp_path / "state.json")
    return Engine(store).run([_step(root, config, host, tmp_path)])


# --- role, values and file edit -------------------------------------------------------------


@pytest.mark.parametrize("role", ["compute", "block", "object"])
def test_step_refuses_non_controller_roles(root: Path, config, tmp_path, role: str) -> None:
    with pytest.raises(ExecutionError, match="only runs on the controller"):
        EtcdStep(root, config, _node(config, role), _executor(tmp_path, _host(root)))


def test_block_has_the_page_keys_for_the_controller(config) -> None:
    assert etcd_block(_node(config)) == EXPECTED_BLOCK


def test_values_come_from_the_configuration(config) -> None:
    other = copy.deepcopy(_node(config))
    other["management_ip"] = "198.51.100.7"
    block = etcd_block(other)
    assert 'ETCD_INITIAL_CLUSTER="controller=http://198.51.100.7:2380"' in block
    assert 'ETCD_LISTEN_CLIENT_URLS="http://198.51.100.7:2379"' in block
    assert "192.0.2.11" not in block and "203.0.113.11" not in block


def test_real_package_default_etcd_file_keeps_header_and_appends_block(root: Path, config) -> None:
    path = _defaults(root, REAL_PACKAGE_DEFAULT)
    change = edit_etcd_defaults(root, _node(config), dry_run=False)
    assert change.changed and change.backup == "/etc/default/etcd.orig"
    assert path.read_text() == REAL_PACKAGE_DEFAULT + BLOCK
    assert verify_etcd_text(path.read_text(), _node(config)) == []


def test_dev_machine_etcd_file_disables_all_nine_existing_assignments(root: Path, config) -> None:
    path = _defaults(root, DEV_MACHINE_ETCD)
    change = edit_etcd_defaults(root, _node(config), dry_run=False)
    assert change.changed and change.backup == "/etc/default/etcd.orig"
    disabled = "".join(
        f"{DISABLED_LINE_PREFIX}{line}\n"
        for line in DEV_MACHINE_ETCD.removeprefix(REAL_PACKAGE_DEFAULT).splitlines()
    )
    assert path.read_text() == REAL_PACKAGE_DEFAULT + disabled + BLOCK
    assert verify_etcd_text(path.read_text(), _node(config)) == []


def test_existing_assignments_are_disabled_and_others_kept(root: Path, config) -> None:
    path = _defaults(root)
    change = edit_etcd_defaults(root, _node(config), dry_run=False)
    assert change.backup == "/etc/default/etcd.orig"
    assert path.read_text() == (
        REAL_PACKAGE_DEFAULT
        + '#ETCD_DATA_DIR="/var/lib/etcd/default"\n'
        + f'{DISABLED_LINE_PREFIX}ETCD_NAME="default"\n'
        + f'{DISABLED_LINE_PREFIX}export ETCD_LISTEN_CLIENT_URLS="http://localhost:2379"\n'
        + 'ETCD_LISTEN_METRICS_URLS="http://localhost:2381"\n'
        + BLOCK
    )
    assert verify_etcd_text(path.read_text(), _node(config)) == []


def test_edit_is_idempotent(root: Path, config) -> None:
    path = _defaults(root)
    edit_etcd_defaults(root, _node(config), dry_run=False)
    before = (path.read_bytes(), path.stat().st_mtime_ns, path.stat().st_ino)
    change = edit_etcd_defaults(root, _node(config), dry_run=False)
    assert (change.changed, change.diff) == (False, "")
    assert (path.read_bytes(), path.stat().st_mtime_ns, path.stat().st_ino) == before


def test_edit_never_creates_the_file(root: Path, config) -> None:
    with pytest.raises(PreconditionError, match="installed by the etcd-server package"):
        edit_etcd_defaults(root, _node(config), dry_run=False)
    assert not (root / DEFAULTS).exists()


def test_verify_text_problems(config) -> None:
    node = _node(config)
    assert verify_etcd_text(SAMPLE, node) == ["the managed block 'openstack-forge etcd' is missing"]
    wrong = BLOCK.replace("http://192.0.2.11:2379", "http://127.0.0.1:2379")
    assert verify_etcd_text(wrong, node) == [
        'ETCD_ADVERTISE_CLIENT_URLS is "http://127.0.0.1:2379" in the managed block, '
        'expected "http://192.0.2.11:2379"',
        'ETCD_LISTEN_CLIENT_URLS is "http://127.0.0.1:2379" in the managed block, '
        'expected "http://192.0.2.11:2379"',
    ]
    missing = BLOCK.replace('ETCD_DATA_DIR="/var/lib/etcd"\n', "")
    assert verify_etcd_text(missing, node) == [
        'ETCD_DATA_DIR is missing from the managed block (expected "/var/lib/etcd")'
    ]
    assert verify_etcd_text('ETCD_NAME="x"\n' + BLOCK, node) == [
        'active assignment(s) outside the block: ETCD_NAME="x"'
    ]


# --- check() -------------------------------------------------------------------------------


def test_check_false_when_file_missing_runs_no_command(root, config, tmp_path) -> None:
    host = _host(root, installed={"etcd-server"}, enabled={"etcd"})
    assert _step(root, config, host, tmp_path).check() is False
    assert host.calls == []


def test_check_false_when_edit_needed_runs_no_command(root, config, tmp_path) -> None:
    _defaults(root)
    host = _host(root, installed={"etcd-server"}, enabled={"etcd"})
    assert _step(root, config, host, tmp_path).check() is False
    assert host.calls == []


@pytest.mark.parametrize(
    ("installed", "enabled", "calls", "expected"),
    [
        (set(), {"etcd"}, [DPKG], False),
        ({"etcd-server"}, set(), [DPKG, IS_ENABLED], False),
        ({"etcd-server"}, {"etcd"}, [DPKG, IS_ENABLED], True),
    ],
)
def test_check_package_and_enablement(
    root, config, tmp_path, installed, enabled, calls, expected
) -> None:
    _defaults(root, BLOCK)
    host = _host(root, installed=installed, enabled=enabled)
    assert _step(root, config, host, tmp_path).check() is expected
    assert host.calls == calls


# --- apply() and the engine -----------------------------------------------------------------


def test_apply_sequence_and_edit_before_enable_and_restart(root, config, tmp_path) -> None:
    host = _host(root)
    report = _run(root, config, host, tmp_path)
    assert report.ok, report.failure
    assert [o.outcome for o in report.outcomes] == [Outcome.APPLIED]
    assert host.calls == [POLICY, INSTALL, ENABLE, RESTART, IS_ENABLED, IS_ACTIVE]
    assert [event for event, _ in host.snapshots] == ["enable", "restart"]
    assert all(text == REAL_PACKAGE_DEFAULT + BLOCK for _, text in host.snapshots)


@pytest.mark.parametrize("empty_stdout", [False, True])
def test_missing_apt_candidate_fails_clearly_before_install(
    root: Path, config, tmp_path, empty_stdout: bool
) -> None:
    host = _host(root, no_candidate={"etcd-server"})
    if empty_stdout:
        host.failures[POLICY] = ProcessOutput(0, "", "N: Unable to locate package etcd-server\n")
    report = _run(root, config, host, tmp_path)
    assert report.failure.phase is Phase.APPLY
    assert report.failure.message == "apply() raised PreconditionError"
    assert "'apt-cache policy etcd-server' reports no installation candidate" in (
        report.failure.details
    )
    assert "universe" in report.failure.details
    assert "OpenStack Forge does not enable it" in report.failure.details
    assert host.calls == [POLICY]
    assert not (root / DEFAULTS).exists()


def test_package_that_creates_no_file(root: Path, config, tmp_path) -> None:
    host = _host(root, creates={})
    report = _run(root, config, host, tmp_path)
    assert report.failure.message == "apply() raised PreconditionError"
    assert host.calls == [POLICY, INSTALL]
    assert not (root / DEFAULTS).exists()


def test_rerun_after_success_changes_nothing(root: Path, config, tmp_path) -> None:
    host = _host(root)
    store = StateStore(tmp_path / "state.json")
    assert _run(root, config, host, tmp_path, store).ok
    path = root / DEFAULTS
    snapshot = (path.read_bytes(), path.stat().st_mtime_ns, store.path.read_bytes())
    host.calls.clear()
    report = _run(root, config, host, tmp_path, store)
    assert [o.outcome for o in report.outcomes] == [Outcome.SKIPPED]
    assert host.calls == [DPKG, IS_ENABLED]
    assert (path.read_bytes(), path.stat().st_mtime_ns, store.path.read_bytes()) == snapshot


def test_mutating_commands_refused_under_non_host_root(root, config, tmp_path) -> None:
    host = _host(root)
    step = EtcdStep(root, config, _node(config), _executor(tmp_path, host, root=root))
    report = Engine(StateStore(tmp_path / "state.json")).run([step])
    assert report.failure.message == "apply() raised MutationRefusedError"
    assert host.calls == [POLICY]
    assert INSTALL not in host.calls


# --- verify() ----------------------------------------------------------------------------

NOT_CHECKED = (
    "No TCP connection to http://192.0.2.11:2379 is attempted and cluster health is not "
    "checked. Unconfirmed without starting etcd (etcd(1) lists only command-line flags): that "
    "/usr/bin/etcd honors the seven ETCD_* environment variables beyond ETCD_NAME and "
    "ETCD_DATA_DIR at runtime."
)


def _verify(root, config, tmp_path, **kwargs):
    return _step(root, config, _host(root, **kwargs), tmp_path).verify()


def test_verify_success(root: Path, config, tmp_path) -> None:
    _defaults(root, BLOCK)
    result = _verify(root, config, tmp_path, enabled={"etcd"}, active={"etcd"})
    assert result.ok
    assert result.details == (
        f"/etc/default/etcd sets the page's nine ETCD_* keys; etcd is enabled and active. "
        f"{NOT_CHECKED}"
    )


@pytest.mark.parametrize(
    ("text", "enabled", "active", "problem"),
    [
        (None, {"etcd"}, {"etcd"}, "/etc/default/etcd does not exist"),
        (
            BLOCK.replace('ETCD_NAME="controller"', 'ETCD_NAME="default"'),
            {"etcd"},
            {"etcd"},
            'ETCD_NAME is "default" in the managed block, expected "controller"',
        ),
        (
            'ETCD_DATA_DIR="/srv/etcd"\n' + BLOCK,
            {"etcd"},
            {"etcd"},
            'active assignment(s) outside the block: ETCD_DATA_DIR="/srv/etcd"',
        ),
        (BLOCK, set(), {"etcd"}, "the etcd service is not enabled (status: disabled)"),
        (BLOCK, {"etcd"}, set(), "the etcd service is not active (state: inactive)"),
    ],
)
def test_verify_failures(root, config, tmp_path, text, enabled, active, problem) -> None:
    if text is not None:
        _defaults(root, text)
    result = _verify(root, config, tmp_path, enabled=enabled, active=active)
    assert not result.ok
    assert result.details == f"{problem}. {NOT_CHECKED}"


# --- CLI -----------------------------------------------------------------------------------


def _args(root: Path, *extra: str, node: str = "forge-controller-01") -> list[str]:
    return [
        "apply", "--config", str(EXAMPLE_CONFIG), "--step", "etcd", "--node", node,
        "--root", str(root), *extra,
    ]  # fmt: skip


COMMANDS = (
    "Commands apply() would run, in order, if check() finds the step not done:\n"
    "  /usr/bin/apt-get install --yes etcd-server\n"
    "  /usr/bin/systemctl enable etcd\n"
    "  /usr/bin/systemctl restart etcd\n"
)
READ_ONLY = (
    "  /usr/bin/apt-cache policy etcd-server\n"
    "  /usr/bin/dpkg-query --status etcd-server\n"
    "  /usr/bin/systemctl is-enabled etcd\n"
    "  /usr/bin/systemctl is-active etcd\n"
)


def test_cli_dry_run_with_the_file(root: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _defaults(root)
    assert main(_args(root)) == 0
    out = capsys.readouterr().out
    assert "WARNING: Step 'etcd' is marked UNVERIFIED" in out
    assert "--- a/etc/default/etcd" in out
    assert '+ETCD_INITIAL_CLUSTER="controller=http://192.0.2.11:2380"' in out
    assert COMMANDS in out and READ_ONLY in out
    assert "EnvironmentFile=-/etc/default/%p" in out
    assert "peers listen on all interfaces (0.0.0.0:2380)" in out
    assert (root / DEFAULTS).read_text() == SAMPLE


def test_cli_dry_run_without_the_file(root: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(_args(root)) == 0
    out = capsys.readouterr().out
    assert "/etc/default/etcd does not exist yet; it is installed by the etcd-server package" in out
    assert 'ETCD_LISTEN_CLIENT_URLS="http://192.0.2.11:2379"\n# END openstack-forge etcd' in out
    assert COMMANDS in out
    assert not (root / DEFAULTS).exists()


def test_cli_yes_with_non_host_root_is_refused(
    root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _defaults(root)
    assert main(_args(root, "--yes")) == 1
    assert "--yes with --root other than '/' is refused" in capsys.readouterr().err
    assert (root / DEFAULTS).read_text() == SAMPLE
    assert not (root / DEFAULT_STATE_TARGET.lstrip("/")).exists()
    assert not (root / DEFAULT_AUDIT_TARGET.lstrip("/")).exists()


def test_cli_rejects_non_controller(root: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(_args(root, node="forge-object-01")) == 1
    assert "does not apply to role 'object'" in capsys.readouterr().err
