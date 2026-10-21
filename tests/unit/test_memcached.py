"""Tests for the memcached step (fake backend, temporary roots only; nothing is executed).

``REAL_DEFAULT_CONF`` is the verbatim 52-line ``/usr/share/memcached/memcached.conf.default``
shipped by ``memcached 1.6.24-1ubuntu0.3`` on Ubuntu 24.04 (copied to ``/etc/memcached.conf``
by ``memcached.postinst``); ``DEV_MACHINE_CONF`` is ``/etc/memcached.conf`` as read on the
development machine (where line 35 had been edited to the machine's local IP, sanitized to
the RFC 5737 documentation address ``-l 192.0.2.170`` for the repository, while ``-l ::1``
on line 36 remained active).
"""

from __future__ import annotations

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
    ExecutionError,
    MemcachedStep,
    Outcome,
    Phase,
    PreconditionError,
    ProcessOutput,
    StateStore,
    edit_memcached_conf,
    verify_memcached_text,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURE_CONFIG = REPO_ROOT / "tests" / "fixtures" / "valid_cluster.yaml"
EXAMPLE_CONFIG = REPO_ROOT / "examples" / "cluster.example.yaml"
CONF = "etc/memcached.conf"
BEGIN, END = "# BEGIN openstack-forge memcached", "# END openstack-forge memcached"
BLOCK = f"{BEGIN}\n-l 192.0.2.11\n{END}\n"

INSTALL = ("/usr/bin/apt-get", "install", "--yes", "memcached")
INSTALL_PY = ("/usr/bin/apt-get", "install", "--yes", "python3-memcache")
RESTART = ("/usr/bin/systemctl", "restart", "memcached")
IS_ACTIVE = ("/usr/bin/systemctl", "is-active", "memcached")
DPKG = ("/usr/bin/dpkg-query", "--status", "memcached")
DPKG_PY = ("/usr/bin/dpkg-query", "--status", "python3-memcache")

# Verbatim /usr/share/memcached/memcached.conf.default from memcached 1.6.24-1ubuntu0.3
# (copied to /etc/memcached.conf by /var/lib/dpkg/info/memcached.postinst).
REAL_DEFAULT_CONF = """\
# memcached default config file
# 2003 - Jay Bonci <jaybonci@debian.org>
# This configuration file is read by the start-memcached script provided as
# part of the Debian GNU/Linux distribution.

# Run memcached as a daemon. This command is implied, and is not needed for the
# daemon to run. See the README.Debian that comes with this package for more
# information.
-d

# Log memcached's output to /var/log/memcached
logfile /var/log/memcached.log

# Be verbose
# -v

# Be even more verbose (print client commands as well)
# -vv

# Start with a cap of 64 megs of memory. It's reasonable, and the daemon default
# Note that the daemon will grow to this size, but does not start out holding this much
# memory
-m 64

# Default connection port is 11211
-p 11211

# Run the daemon as root. The start-memcached will default to running as root if no
# -u command is present in this config file
-u memcache

# Specify which IP address to listen on. The default is to listen on all IP addresses
# This parameter is one of the only security measures that memcached has, so make sure
# it's listening on a firewalled interface.
-l 127.0.0.1
-l ::1

# Limit the number of simultaneous incoming connections. The daemon default is 1024
# -c 1024

# Lock down all paged memory. Consult with the README and homepage before you do this
# -k

# Return error when memory is exhausted (rather than removing items)
# -M

# Maximize core file limit
# -r

# Use a pidfile
-P /var/run/memcached/memcached.pid
"""
# /etc/memcached.conf read on the development machine, with its local IP sanitized to the
# RFC 5737 documentation address 192.0.2.170 for the repository (line 36 still -l ::1).
DEV_MACHINE_CONF = REAL_DEFAULT_CONF.replace("-l 127.0.0.1\n", "-l 192.0.2.170\n")
SAMPLE = REAL_DEFAULT_CONF
BOTH = {"memcached", "python3-memcache"}


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
    (directory / "etc").mkdir(parents=True)
    return directory


@pytest.fixture
def config() -> dict:
    return load_config(FIXTURE_CONFIG)


def _node(config: Mapping, role: str = "controller") -> Mapping:
    return next(node for node in config["nodes"] if node["role"] == role)


def _host(root: Path, **kwargs) -> ServiceHost:
    kwargs.setdefault("creates", {"memcached": (CONF, SAMPLE)})
    return ServiceHost(root, watch=CONF, **kwargs)


def _executor(tmp_path: Path, backend, *, root: Path = Path("/")) -> CommandExecutor:
    (tmp_path / "audit").mkdir(exist_ok=True)
    return CommandExecutor(
        DEFAULT_REGISTRY, root=root, audit_log=AuditLog(tmp_path / "audit/commands.log"),
        backend=backend, euid=lambda: 0,
    )  # fmt: skip


def _step(root: Path, config, host, tmp_path) -> MemcachedStep:
    return MemcachedStep(root, config, _node(config), _executor(tmp_path, host))


def _conf(root: Path, text: str = SAMPLE) -> Path:
    path = root / CONF
    path.write_text(text)
    return path


def _run(root, config, host, tmp_path, store=None):
    store = store or StateStore(tmp_path / "state.json")
    return Engine(store).run([_step(root, config, host, tmp_path)])


# --- role and file edit ---------------------------------------------------------------------


@pytest.mark.parametrize("role", ["compute", "block", "object"])
def test_step_refuses_non_controller_roles(root: Path, config, tmp_path, role: str) -> None:
    with pytest.raises(ExecutionError, match="only runs on the controller"):
        MemcachedStep(root, config, _node(config, role), _executor(tmp_path, _host(root)))


def test_real_default_listen_lines_are_both_disabled_and_comments_kept(root: Path, config) -> None:
    path = _conf(root, REAL_DEFAULT_CONF)
    change = edit_memcached_conf(root, _node(config), dry_run=False)
    assert change.changed and change.backup == "/etc/memcached.conf.orig"
    expected = (
        REAL_DEFAULT_CONF.replace(
            "-l 127.0.0.1\n-l ::1\n",
            f"{DISABLED_LINE_PREFIX}-l 127.0.0.1\n{DISABLED_LINE_PREFIX}-l ::1\n",
        )
        + BLOCK
    )
    assert path.read_text() == expected
    assert verify_memcached_text(path.read_text(), _node(config)) == []


def test_dev_machine_memcached_conf_disables_both_existing_listen_lines(root: Path, config) -> None:
    path = _conf(root, DEV_MACHINE_CONF)
    change = edit_memcached_conf(root, _node(config), dry_run=False)
    assert change.changed and change.backup == "/etc/memcached.conf.orig"
    expected = (
        DEV_MACHINE_CONF.replace(
            "-l 192.0.2.170\n-l ::1\n",
            f"{DISABLED_LINE_PREFIX}-l 192.0.2.170\n{DISABLED_LINE_PREFIX}-l ::1\n",
        )
        + BLOCK
    )
    assert path.read_text() == expected
    assert verify_memcached_text(path.read_text(), _node(config)) == []


def test_no_listen_line_at_all(root: Path, config) -> None:
    path = _conf(root, "-m 64\n")
    edit_memcached_conf(root, _node(config), dry_run=False)
    assert path.read_text() == "-m 64\n" + BLOCK


@pytest.mark.parametrize(
    "line", ["-l 0.0.0.0", "-l127.0.0.1", "--listen=10.9.9.9", "--listen 10.9.9.9", "  -l ::1"]
)
def test_already_changed_listen_lines_are_disabled(root: Path, config, line: str) -> None:
    path = _conf(root, f"-m 64\n{line}\n")
    edit_memcached_conf(root, _node(config), dry_run=False)
    assert path.read_text() == f"-m 64\n{DISABLED_LINE_PREFIX}{line}\n" + BLOCK


def test_other_options_and_comments_are_untouched(root: Path, config) -> None:
    text = "# -l 127.0.0.1 is the default\n--listen-backlog=1024\n-p 11211\n-L\n"
    path = _conf(root, text)
    edit_memcached_conf(root, _node(config), dry_run=False)
    assert path.read_text() == text + BLOCK


def test_edit_is_idempotent(root: Path, config) -> None:
    path = _conf(root)
    edit_memcached_conf(root, _node(config), dry_run=False)
    before = (path.read_bytes(), path.stat().st_mtime_ns, path.stat().st_ino)
    change = edit_memcached_conf(root, _node(config), dry_run=False)
    assert (change.changed, change.diff) == (False, "")
    assert (path.read_bytes(), path.stat().st_mtime_ns, path.stat().st_ino) == before


def test_edit_never_creates_the_file(root: Path, config) -> None:
    with pytest.raises(PreconditionError, match="created when memcached is installed"):
        edit_memcached_conf(root, _node(config), dry_run=False)
    assert not (root / CONF).exists()


def test_verify_text_problems(config) -> None:
    node = _node(config)
    assert verify_memcached_text(SAMPLE, node) == [
        "the managed block 'openstack-forge memcached' is missing"
    ]
    wrong = f"-m 64\n{BEGIN}\n-l 10.0.0.11\n{END}\n"
    assert verify_memcached_text(wrong, node) == [
        "the managed block does not contain exactly '-l 192.0.2.11'"
    ]
    extra = "-l 127.0.0.1\n" + BLOCK
    assert verify_memcached_text(extra, node) == [
        "active listen line(s) outside the block: -l 127.0.0.1"
    ]


# --- check() -------------------------------------------------------------------------------


def test_check_false_when_file_missing_runs_no_command(root, config, tmp_path) -> None:
    host = _host(root, installed=BOTH)
    assert _step(root, config, host, tmp_path).check() is False
    assert host.calls == []


def test_check_false_when_edit_needed_runs_no_command(root, config, tmp_path) -> None:
    _conf(root)
    host = _host(root, installed=BOTH)
    assert _step(root, config, host, tmp_path).check() is False
    assert host.calls == []


@pytest.mark.parametrize(
    ("installed", "calls", "expected"),
    [
        ({"python3-memcache"}, [DPKG], False),
        ({"memcached"}, [DPKG, DPKG_PY], False),
        (BOTH, [DPKG, DPKG_PY], True),
    ],
)
def test_check_packages(root, config, tmp_path, installed, calls, expected) -> None:
    _conf(root, BLOCK)
    host = _host(root, installed=installed)
    assert _step(root, config, host, tmp_path).check() is expected
    assert host.calls == calls


# --- apply() and the engine -----------------------------------------------------------------


def test_apply_sequence_and_edit_before_restart(root: Path, config, tmp_path) -> None:
    host = _host(root)
    report = _run(root, config, host, tmp_path)
    assert report.ok, report.failure
    assert [o.outcome for o in report.outcomes] == [Outcome.APPLIED]
    assert host.calls == [INSTALL, INSTALL_PY, RESTART, DPKG_PY, IS_ACTIVE]
    ((event, text_at_restart),) = host.snapshots
    assert event == "restart" and text_at_restart.endswith(BLOCK)


def test_package_that_creates_no_file(root: Path, config, tmp_path) -> None:
    host = _host(root, creates={})
    report = _run(root, config, host, tmp_path)
    assert report.failure.message == "apply() raised PreconditionError"
    assert host.calls == [INSTALL, INSTALL_PY]
    assert not (root / CONF).exists()


def test_rerun_after_success_changes_nothing(root: Path, config, tmp_path) -> None:
    host = _host(root)
    store = StateStore(tmp_path / "state.json")
    assert _run(root, config, host, tmp_path, store).ok
    conf = root / CONF
    snapshot = (conf.read_bytes(), conf.stat().st_mtime_ns, store.path.read_bytes())
    host.calls.clear()
    report = _run(root, config, host, tmp_path, store)
    assert [o.outcome for o in report.outcomes] == [Outcome.SKIPPED]
    assert host.calls == [DPKG, DPKG_PY]
    assert (conf.read_bytes(), conf.stat().st_mtime_ns, store.path.read_bytes()) == snapshot


def test_mutating_commands_refused_under_non_host_root(root, config, tmp_path) -> None:
    host = _host(root)
    step = MemcachedStep(root, config, _node(config), _executor(tmp_path, host, root=root))
    report = Engine(StateStore(tmp_path / "state.json")).run([step])
    assert report.failure.message == "apply() raised MutationRefusedError"
    assert host.calls == []


def test_apt_failure(root: Path, config, tmp_path) -> None:
    host = _host(root)
    host.failures[INSTALL] = ProcessOutput(100, "", "E: Unable to locate package memcached\n")
    report = _run(root, config, host, tmp_path)
    assert report.failure.phase is Phase.APPLY
    assert "dpkg --audit" in report.failure.next_action
    assert host.calls == [INSTALL]


# --- verify() ----------------------------------------------------------------------------

NOT_CHECKED = (
    "No TCP connection to 192.0.2.11:11211 is attempted, and what memcached actually listens "
    "on at runtime is not checked (unconfirmed on a live service)."
)


def _verify(root, config, tmp_path, **kwargs):
    return _step(root, config, _host(root, **kwargs), tmp_path).verify()


def test_verify_success(root: Path, config, tmp_path) -> None:
    _conf(root, BLOCK)
    result = _verify(root, config, tmp_path, installed=BOTH, active={"memcached"})
    assert result.ok
    assert result.details == (
        "/etc/memcached.conf sets '-l 192.0.2.11' and no other listen line; python3-memcache "
        f"is installed; memcached is active. {NOT_CHECKED}"
    )


@pytest.mark.parametrize(
    ("text", "installed", "active", "problem"),
    [
        (None, BOTH, {"memcached"}, "/etc/memcached.conf does not exist"),
        (
            f"{BEGIN}\n-l 10.0.0.11\n{END}\n",
            BOTH,
            {"memcached"},
            "the managed block does not contain exactly '-l 192.0.2.11'",
        ),
        (
            "-l 0.0.0.0\n" + BLOCK,
            BOTH,
            {"memcached"},
            "active listen line(s) outside the block: -l 0.0.0.0",
        ),
        (BLOCK, {"memcached"}, {"memcached"}, "python3-memcache is not installed"),
        (BLOCK, BOTH, set(), "the memcached service is not active (state: inactive)"),
    ],
)
def test_verify_failures(root, config, tmp_path, text, installed, active, problem) -> None:
    if text is not None:
        _conf(root, text)
    result = _verify(root, config, tmp_path, installed=installed, active=active)
    assert not result.ok
    assert result.details == f"{problem}. {NOT_CHECKED}"


# --- CLI -----------------------------------------------------------------------------------


def _args(root: Path, *extra: str, node: str = "forge-controller-01") -> list[str]:
    return [
        "apply", "--config", str(EXAMPLE_CONFIG), "--step", "memcached", "--node", node,
        "--root", str(root), *extra,
    ]  # fmt: skip


COMMANDS = (
    "Commands apply() would run, in order, if check() finds the step not done:\n"
    "  /usr/bin/apt-get install --yes memcached\n"
    "  /usr/bin/apt-get install --yes python3-memcache\n"
    "  /usr/bin/systemctl restart memcached\n"
)


def test_cli_dry_run_with_the_file(root: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _conf(root)
    assert main(_args(root)) == 0
    out = capsys.readouterr().out
    assert "WARNING: Step 'memcached' is marked UNVERIFIED" in out
    assert "--- a/etc/memcached.conf" in out
    assert f"+{DISABLED_LINE_PREFIX}-l 127.0.0.1" in out
    assert f"+{DISABLED_LINE_PREFIX}-l ::1" in out
    assert "+-l 10.10.40.11" in out
    assert COMMANDS in out
    assert "systemd-memcached-wrapper" in out
    assert "the management network must be trusted" in out
    assert (root / CONF).read_text() == SAMPLE


def test_cli_dry_run_without_the_file(root: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(_args(root)) == 0
    out = capsys.readouterr().out
    assert "/etc/memcached.conf does not exist yet; it is created when memcached is installed" in (
        out
    )
    assert f"{BEGIN}\n-l 10.10.40.11\n{END}\n" in out
    assert COMMANDS in out
    assert not (root / CONF).exists()


def test_cli_yes_with_non_host_root_is_refused(
    root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _conf(root)
    assert main(_args(root, "--yes")) == 1
    assert "--yes with --root other than '/' is refused" in capsys.readouterr().err
    assert (root / CONF).read_text() == SAMPLE
    assert not (root / DEFAULT_STATE_TARGET.lstrip("/")).exists()
    assert not (root / DEFAULT_AUDIT_TARGET.lstrip("/")).exists()


def test_cli_rejects_non_controller(root: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(_args(root, node="forge-compute-01")) == 1
    assert "does not apply to role 'compute'" in capsys.readouterr().err
