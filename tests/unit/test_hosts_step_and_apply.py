"""Unit tests for the `hosts-file` step and `openstack-forge apply` (tmp_path only)."""

from __future__ import annotations

import json
import os
import socket
import stat
import subprocess
from pathlib import Path

import pytest

from openstack_forge.cli import main
from openstack_forge.config import load_config
from openstack_forge.execution import (
    CONTROLLER_ALIAS,
    DEFAULT_STATE_TARGET,
    DISABLED_LINE_PREFIX,
    HOSTS_BLOCK_NAME,
    HOSTS_STEP_ID,
    IMPLEMENTED_STEPS,
    HostsFileStep,
    apply_hosts_file,
    build_hosts_block,
    parse_hosts,
    resolve_in_hosts,
    uncomment_lines,
    verify_hosts_file,
    verify_hosts_text,
)
from openstack_forge.orchestration import load_catalog

REPO_ROOT = Path(__file__).resolve().parents[2]
EXAMPLE_CONFIG = REPO_ROOT / "examples" / "cluster.example.yaml"


@pytest.fixture(autouse=True)
def _forbid_subprocess_and_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def no_subprocess(*args, **kwargs):
        raise AssertionError(f"subprocess is forbidden in Milestone 6: {args!r}")

    def no_network(*args, **kwargs):
        raise AssertionError(f"network access is forbidden in Milestone 6: {args!r}")

    monkeypatch.setattr(subprocess, "run", no_subprocess)
    monkeypatch.setattr(subprocess, "Popen", no_subprocess)
    monkeypatch.setattr(socket, "create_connection", no_network)


@pytest.fixture
def root(tmp_path: Path) -> Path:
    directory = tmp_path / "root"
    (directory / "etc").mkdir(parents=True)
    return directory


# --- Managed block content & controller alias -------------------------------------------


def test_build_hosts_block_includes_all_roles_and_controller_alias() -> None:
    config = load_config(EXAMPLE_CONFIG)
    block = build_hosts_block(config)
    assert block == (
        f"10.10.40.11 forge-controller-01.example.internal forge-controller-01 {CONTROLLER_ALIAS}\n"
        "10.10.40.21 forge-compute-01.example.internal forge-compute-01\n"
        "10.10.40.22 forge-compute-02.example.internal forge-compute-02\n"
        "10.10.40.31 forge-block-01.example.internal forge-block-01\n"
        "10.10.40.32 forge-object-01.example.internal forge-object-01\n"
    )


def test_hosts_step_remains_unverified_in_catalog() -> None:
    catalog = load_catalog()
    step = catalog.step(HOSTS_STEP_ID)
    assert step.status == "unverified"
    assert set(IMPLEMENTED_STEPS) == {HOSTS_STEP_ID}


# --- 127.0.1.1 and 127.0.0.1 handling ---------------------------------------------------


def test_127_0_1_1_for_this_node_is_commented_out_and_other_lines_preserved(
    root: Path,
) -> None:
    config = load_config(EXAMPLE_CONFIG)
    original = (
        "127.0.0.1 localhost forge-controller-01\n"
        "127.0.1.1 forge-controller-01.example.internal forge-controller-01\n"
        "127.0.1.1 forge-compute-01.example.internal forge-compute-01\n"
        "127.0.1.1 other-box\n"
        "::1 localhost ip6-localhost ip6-loopback\n"
    )
    hosts_path = root / "etc/hosts"
    hosts_path.write_text(original, encoding="utf-8")

    change = apply_hosts_file(root, config, "forge-controller-01")
    assert change.changed
    assert change.backup == "/etc/hosts.orig"
    assert (root / "etc/hosts.orig").read_text(encoding="utf-8") == original

    updated = hosts_path.read_text(encoding="utf-8")
    # 127.0.0.1 is never touched, even when it mentions forge-controller-01
    assert "127.0.0.1 localhost forge-controller-01\n" in updated
    # This node's 127.0.1.1 line is commented out
    expected_disabled = (
        f"{DISABLED_LINE_PREFIX}127.0.1.1 "
        "forge-controller-01.example.internal forge-controller-01\n"
    )
    assert expected_disabled in updated
    # Another host's 127.0.1.1 line is untouched
    assert "\n127.0.1.1 forge-compute-01.example.internal forge-compute-01\n" in updated
    assert "\n127.0.1.1 other-box\n" in updated
    # Managed block is present
    assert f"# BEGIN {HOSTS_BLOCK_NAME}\n" in updated
    assert (
        f"10.10.40.11 forge-controller-01.example.internal forge-controller-01 {CONTROLLER_ALIAS}\n"
        in updated
    )


def test_127_0_1_1_matching_only_fqdn_is_commented_out(root: Path) -> None:
    config = load_config(EXAMPLE_CONFIG)
    (root / "etc/hosts").write_text(
        "127.0.0.1 localhost\n127.0.1.1 forge-compute-01.example.internal\n",
        encoding="utf-8",
    )
    change = apply_hosts_file(root, config, "forge-compute-01")
    assert change.changed
    text = (root / "etc/hosts").read_text(encoding="utf-8")
    assert f"{DISABLED_LINE_PREFIX}127.0.1.1 forge-compute-01.example.internal\n" in text
    assert verify_hosts_file(root, config, "forge-compute-01").ok


def test_hosts_file_without_any_127_0_1_1_line(root: Path) -> None:
    config = load_config(EXAMPLE_CONFIG)
    (root / "etc/hosts").write_text("127.0.0.1 localhost\n", encoding="utf-8")
    change = apply_hosts_file(root, config, "forge-controller-01")
    assert change.changed
    assert DISABLED_LINE_PREFIX not in (root / "etc/hosts").read_text(encoding="utf-8")
    assert verify_hosts_file(root, config, "forge-controller-01").ok


def test_commented_127_0_1_1_can_be_reversed_with_uncomment_lines(root: Path) -> None:
    config = load_config(EXAMPLE_CONFIG)
    (root / "etc/hosts").write_text(
        "127.0.0.1 localhost\n127.0.1.1 forge-controller-01\n",
        encoding="utf-8",
    )
    apply_hosts_file(root, config, "forge-controller-01")
    rev = uncomment_lines(root, "/etc/hosts")
    assert rev.changed
    assert "\n127.0.1.1 forge-controller-01\n" in (root / "etc/hosts").read_text(encoding="utf-8")


# --- Idempotent re-run ------------------------------------------------------------------


def test_second_apply_is_idempotent_and_writes_nothing(root: Path) -> None:
    config = load_config(EXAMPLE_CONFIG)
    hosts_path = root / "etc/hosts"
    hosts_path.write_text("127.0.0.1 localhost\n127.0.1.1 forge-controller-01\n", encoding="utf-8")

    first = apply_hosts_file(root, config, "forge-controller-01")
    assert first.changed
    info_before = hosts_path.stat()
    bytes_before = hosts_path.read_bytes()

    second = apply_hosts_file(root, config, "forge-controller-01")
    assert not second.changed
    assert second.diff == ""
    assert second.backup is None
    info_after = hosts_path.stat()
    assert hosts_path.read_bytes() == bytes_before
    assert (info_after.st_ino, info_after.st_mtime_ns) == (
        info_before.st_ino,
        info_before.st_mtime_ns,
    )


# --- Pure /etc/hosts parser and verify() ------------------------------------------------


def test_parse_hosts_and_first_match_wins() -> None:
    text = (
        "# comment\n"
        "127.0.0.1 localhost\n"
        "127.0.1.1 forge-controller-01.example.internal forge-controller-01 # inline comment\n"
        "10.10.40.11 forge-controller-01.example.internal forge-controller-01 controller\n"
    )
    entries = parse_hosts(text)
    assert entries == [
        ("127.0.0.1", ("localhost",)),
        ("127.0.1.1", ("forge-controller-01.example.internal", "forge-controller-01")),
        (
            "10.10.40.11",
            ("forge-controller-01.example.internal", "forge-controller-01", "controller"),
        ),
    ]
    # First matching line wins, just like the system resolver:
    assert resolve_in_hosts(entries, "forge-controller-01") == "127.0.1.1"
    assert resolve_in_hosts(entries, "FORGE-CONTROLLER-01.EXAMPLE.INTERNAL") == "127.0.1.1"
    assert resolve_in_hosts(entries, "controller") == "10.10.40.11"
    assert resolve_in_hosts(entries, "unknown") is None


def test_verify_passes_when_hosts_file_is_valid(root: Path) -> None:
    config = load_config(EXAMPLE_CONFIG)
    (root / "etc/hosts").write_text(
        "127.0.0.1 localhost\n127.0.1.1 forge-object-01\n", encoding="utf-8"
    )
    step = HostsFileStep(root, config, "forge-object-01")
    assert not step.check()
    step.apply()
    assert step.check()
    result = step.verify()
    assert result.ok and "All 5 cluster node(s) resolve" in result.details


def test_verify_fails_when_hosts_file_is_missing(root: Path) -> None:
    config = load_config(EXAMPLE_CONFIG)
    result = verify_hosts_file(root, config, "forge-controller-01")
    assert not result.ok
    assert "does not exist" in result.details


def test_verify_fails_when_127_0_1_1_still_shadows_local_node() -> None:
    config = load_config(EXAMPLE_CONFIG)
    text = (
        "127.0.1.1 forge-controller-01.example.internal forge-controller-01\n"
        + build_hosts_block(config)
    )
    result = verify_hosts_text(text, config, "forge-controller-01")
    assert not result.ok
    assert "127.0.1.1" in result.details
    assert "forge-controller-01" in result.details


def test_verify_fails_when_earlier_line_overrides_node_ip() -> None:
    config = load_config(EXAMPLE_CONFIG)
    text = "10.10.40.99 forge-compute-01\n" + build_hosts_block(config)
    result = verify_hosts_text(text, config, "forge-controller-01")
    assert not result.ok
    assert "forge-compute-01" in result.details
    assert "10.10.40.99" in result.details


def test_verify_fails_when_controller_alias_is_missing() -> None:
    config = load_config(EXAMPLE_CONFIG)
    text = (
        "10.10.40.11 forge-controller-01.example.internal forge-controller-01\n"
        "10.10.40.21 forge-compute-01.example.internal forge-compute-01\n"
        "10.10.40.22 forge-compute-02.example.internal forge-compute-02\n"
        "10.10.40.31 forge-block-01.example.internal forge-block-01\n"
        "10.10.40.32 forge-object-01.example.internal forge-object-01\n"
    )
    result = verify_hosts_text(text, config, "forge-controller-01")
    assert not result.ok
    assert "controller" in result.details


# --- Ownership preservation during hosts-file step --------------------------------------


def test_hosts_file_step_preserves_mode_and_supplementary_group(root: Path) -> None:
    config = load_config(EXAMPLE_CONFIG)
    hosts_path = root / "etc/hosts"
    hosts_path.write_text("127.0.0.1 localhost\n127.0.1.1 forge-controller-01\n", encoding="utf-8")
    hosts_path.chmod(0o640)

    egid = os.getegid()
    supplementary = [gid for gid in os.getgroups() if gid != egid]
    chosen_gid = egid
    for candidate in supplementary:
        try:
            os.chown(hosts_path, -1, candidate)
        except PermissionError:
            continue
        chosen_gid = candidate
        break

    apply_hosts_file(root, config, "forge-controller-01")
    info = hosts_path.stat()
    assert stat.S_IMODE(info.st_mode) == 0o640
    assert info.st_uid == os.geteuid()
    assert info.st_gid == chosen_gid


# --- CLI `openstack-forge apply` --------------------------------------------------------


def test_cli_apply_dry_run_is_default_prints_diff_and_writes_nothing(
    root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    hosts_path = root / "etc/hosts"
    initial = "127.0.0.1 localhost\n127.0.1.1 forge-controller-01\n"
    hosts_path.write_text(initial, encoding="utf-8")

    rc = main(
        [
            "apply",
            "--config",
            str(EXAMPLE_CONFIG),
            "--step",
            "hosts-file",
            "--node",
            "forge-controller-01",
            "--root",
            str(root),
        ]
    )
    out = capsys.readouterr()
    assert rc == 0
    assert "UNVERIFIED" in out.out
    assert "--- a/etc/hosts" in out.out
    assert f"+{DISABLED_LINE_PREFIX}127.0.1.1 forge-controller-01" in out.out
    assert (
        f"+10.10.40.11 forge-controller-01.example.internal forge-controller-01 {CONTROLLER_ALIAS}"
        in out.out
    )
    assert "no files modified" in out.out
    # Dry-run writes neither /etc/hosts, nor .orig, nor state.json:
    assert hosts_path.read_text(encoding="utf-8") == initial
    assert not (root / "etc/hosts.orig").exists()
    assert not (root / DEFAULT_STATE_TARGET.lstrip("/")).exists()


def test_cli_apply_with_yes_writes_hosts_and_0600_state_and_is_idempotent(
    root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    hosts_path = root / "etc/hosts"
    hosts_path.write_text("127.0.0.1 localhost\n127.0.1.1 forge-controller-01\n", encoding="utf-8")

    rc = main(
        [
            "apply",
            "--config",
            str(EXAMPLE_CONFIG),
            "--step",
            "hosts-file",
            "--node",
            "forge-controller-01",
            "--root",
            str(root),
            "--yes",
        ]
    )
    first_out = capsys.readouterr()
    assert rc == 0
    assert "UNVERIFIED" in first_out.out
    assert "hosts-file" in first_out.out and "done" in first_out.out

    state_path = root / DEFAULT_STATE_TARGET.lstrip("/")
    assert state_path.exists()
    assert stat.S_IMODE(state_path.stat().st_mode) == 0o600
    state_data = json.loads(state_path.read_text(encoding="utf-8"))
    assert state_data["steps"]["hosts-file"]["status"] == "done"

    hosts_stat_before = hosts_path.stat()
    state_stat_before = state_path.stat()

    # Second run with --yes is idempotent and changes nothing
    rc2 = main(
        [
            "apply",
            "--config",
            str(EXAMPLE_CONFIG),
            "--step",
            "hosts-file",
            "--node",
            "forge-controller-01",
            "--root",
            str(root),
            "--yes",
        ]
    )
    second_out = capsys.readouterr()
    assert rc2 == 0
    assert "skipped" in second_out.out
    assert hosts_path.stat().st_mtime_ns == hosts_stat_before.st_mtime_ns
    assert state_path.stat().st_mtime_ns == state_stat_before.st_mtime_ns


def test_cli_apply_yes_on_real_root_requires_effective_uid_0(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(os, "geteuid", lambda: 1000)
    rc = main(
        [
            "apply",
            "--config",
            str(EXAMPLE_CONFIG),
            "--step",
            "hosts-file",
            "--node",
            "forge-controller-01",
            "--yes",
        ]
    )
    err = capsys.readouterr().err
    assert rc == 1
    assert "requires root privileges (effective uid 0)" in err


@pytest.mark.parametrize("step_id", ["time-sync", "openstack-packages", "mariadb"])
def test_cli_apply_rejects_unimplemented_catalog_step(
    root: Path, step_id: str, capsys: pytest.CaptureFixture[str]
) -> None:
    rc = main(
        [
            "apply",
            "--config",
            str(EXAMPLE_CONFIG),
            "--step",
            step_id,
            "--node",
            "forge-controller-01",
            "--root",
            str(root),
        ]
    )
    err = capsys.readouterr().err
    assert rc == 1
    assert "not implemented" in err


def test_cli_apply_rejects_unknown_step_id(root: Path, capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(
        [
            "apply",
            "--config",
            str(EXAMPLE_CONFIG),
            "--step",
            "does-not-exist",
            "--node",
            "forge-controller-01",
            "--root",
            str(root),
        ]
    )
    err = capsys.readouterr().err
    assert rc == 1
    assert "not implemented" in err
    assert "does-not-exist" in err


def test_cli_apply_identifies_local_node_by_hostname_when_node_flag_omitted(
    root: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    (root / "etc/hosts").write_text("127.0.0.1 localhost\n", encoding="utf-8")
    monkeypatch.setattr(socket, "gethostname", lambda: "forge-compute-01.example.internal")
    rc = main(
        [
            "apply",
            "--config",
            str(EXAMPLE_CONFIG),
            "--step",
            "hosts-file",
            "--root",
            str(root),
        ]
    )
    out = capsys.readouterr().out
    assert rc == 0
    assert "+10.10.40.21 forge-compute-01.example.internal forge-compute-01" in out


def test_cli_apply_fails_when_node_cannot_be_identified(
    root: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(socket, "gethostname", lambda: "workstation")
    rc = main(
        [
            "apply",
            "--config",
            str(EXAMPLE_CONFIG),
            "--step",
            "hosts-file",
            "--root",
            str(root),
        ]
    )
    err = capsys.readouterr().err
    assert rc == 1
    assert "Could not identify local node" in err
