"""Unit tests for ``openstack-forge init``, ``apply --all``, and ``verify``."""

from __future__ import annotations

import json
import socket
import subprocess
from pathlib import Path

import pytest

from openstack_forge.cli import STARTER_CLUSTER_YAML, main
from openstack_forge.execution import (
    DEFAULT_AUDIT_TARGET,
    DEFAULT_STATE_TARGET,
    IMPLEMENTED_STEPS,
    ProcessOutput,
    SecretStore,
)
from tests.unit._service_fakes import make_node_service_host


@pytest.fixture(autouse=True)
def _forbid_real_subprocess_and_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(*args: object, **kwargs: object) -> None:
        raise AssertionError(f"real subprocess/socket call is forbidden: {args!r}")

    monkeypatch.setattr(subprocess, "Popen", _boom)
    monkeypatch.setattr(subprocess, "run", _boom)
    monkeypatch.setattr(socket, "create_connection", _boom)


# --- openstack-forge init -------------------------------------------------------------------


def test_init_creates_starter_cluster_yaml_that_passes_config_validate(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    output_path = tmp_path / "cluster.yaml"
    rc = main(["init", "--output", str(output_path)])
    out = capsys.readouterr()
    assert rc == 0
    assert out.err == ""
    assert f"Initialized starter cluster configuration: {output_path}" in out.out
    assert output_path.read_text(encoding="utf-8") == STARTER_CLUSTER_YAML

    rc_val = main(["config", "validate", str(output_path)])
    val_out = capsys.readouterr()
    assert rc_val == 0
    assert "Configuration is valid" in val_out.out
    assert "3 node(s), 3 network(s)" in val_out.out


def test_init_refuses_existing_file_without_force_and_overwrites_with_force(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    output_path = tmp_path / "cluster.yaml"
    output_path.write_text("# custom existing content\n", encoding="utf-8")

    rc1 = main(["init", "--output", str(output_path)])
    out1 = capsys.readouterr()
    assert rc1 == 1
    assert "File already exists" in out1.err
    assert "use --force to overwrite" in out1.err
    assert output_path.read_text(encoding="utf-8") == "# custom existing content\n"

    rc2 = main(["init", "--output", str(output_path), "--force"])
    out2 = capsys.readouterr()
    assert rc2 == 0
    assert f"Initialized starter cluster configuration: {output_path}" in out2.out
    assert output_path.read_text(encoding="utf-8") == STARTER_CLUSTER_YAML


def test_init_refuses_symlink_target_and_symlink_parent(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    real_file = tmp_path / "real.yaml"
    real_file.write_text("original\n", encoding="utf-8")
    symlink_file = tmp_path / "link.yaml"
    symlink_file.symlink_to(real_file)

    rc = main(["init", "--output", str(symlink_file), "--force"])
    err = capsys.readouterr().err
    assert rc == 1
    assert "Refusing to write to symlink" in err
    assert real_file.read_text(encoding="utf-8") == "original\n"

    real_dir = tmp_path / "real_dir"
    real_dir.mkdir()
    symlink_dir = tmp_path / "link_dir"
    symlink_dir.symlink_to(real_dir, target_is_directory=True)

    rc_parent = main(["init", "--output", str(symlink_dir / "cluster.yaml")])
    err_parent = capsys.readouterr().err
    assert rc_parent == 1
    assert "Refusing to write to symlink" in err_parent
    assert not (real_dir / "cluster.yaml").exists()


# --- openstack-forge apply --all ------------------------------------------------------------


@pytest.mark.parametrize(
    ("node_hostname", "role", "expected_count", "first_step", "last_step"),
    [
        ("controller", "controller", 14, "hosts-file", "cinder-controller"),
        ("compute1", "compute", 5, "hosts-file", "neutron-compute"),
        ("block1", "block", 4, "hosts-file", "cinder-storage"),
    ],
)
def test_apply_all_dry_run_previews_all_node_steps_in_order(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    node_hostname: str,
    role: str,
    expected_count: int,
    first_step: str,
    last_step: str,
) -> None:
    cfg = tmp_path / "cluster.yaml"
    assert main(["init", "--output", str(cfg)]) == 0
    capsys.readouterr()

    node_root = tmp_path / node_hostname
    make_node_service_host(node_root, node_hostname)

    rc = main(
        [
            "apply",
            "--config",
            str(cfg),
            "--node",
            node_hostname,
            "--root",
            str(node_root),
            "--all",
        ]
    )
    out = capsys.readouterr()
    assert rc == 0
    assert out.err == ""
    assert (
        f"Dry run for all {expected_count} step(s) on node '{node_hostname}' "
        f"(role: {role}): no files modified and no commands run."
    ) in out.out
    assert f"=== [1/{expected_count}] {first_step} ===" in out.out
    assert f"=== [{expected_count}/{expected_count}] {last_step} ===" in out.out
    assert not (node_root / DEFAULT_STATE_TARGET.lstrip("/")).exists()
    assert not (node_root / DEFAULT_AUDIT_TARGET.lstrip("/")).exists()


def test_apply_all_mutually_exclusive_with_step(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cfg = tmp_path / "cluster.yaml"
    assert main(["init", "--output", str(cfg)]) == 0
    capsys.readouterr()

    with pytest.raises(SystemExit) as excinfo:
        main(["apply", "--config", str(cfg), "--step", "hosts-file", "--all"])
    assert excinfo.value.code == 2
    assert "not allowed with argument" in capsys.readouterr().err


def test_apply_all_rejects_unimplemented_step_in_plan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import openstack_forge.execution as execution_pkg

    cfg = tmp_path / "cluster.yaml"
    assert main(["init", "--output", str(cfg)]) == 0
    capsys.readouterr()

    node_root = tmp_path / "block1"
    make_node_service_host(node_root, "block1")
    without_cinder = {k: v for k, v in IMPLEMENTED_STEPS.items() if k != "cinder-storage"}
    monkeypatch.setattr(execution_pkg, "IMPLEMENTED_STEPS", without_cinder)

    rc = main(
        [
            "apply",
            "--config",
            str(cfg),
            "--node",
            "block1",
            "--root",
            str(node_root),
            "--all",
        ]
    )
    err = capsys.readouterr().err
    assert rc == 1
    assert "'cinder-storage'" in err
    assert "not implemented yet" in err


def test_apply_all_yes_refuses_non_host_root_without_injected_backend(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cfg = tmp_path / "cluster.yaml"
    assert main(["init", "--output", str(cfg)]) == 0
    capsys.readouterr()

    node_root = tmp_path / "block1"
    make_node_service_host(node_root, "block1")

    rc = main(
        [
            "apply",
            "--config",
            str(cfg),
            "--node",
            "block1",
            "--root",
            str(node_root),
            "--all",
            "--yes",
        ]
    )
    err = capsys.readouterr().err
    assert rc == 1
    assert "--yes with --root other than '/' is refused" in err


def test_apply_all_yes_converges_and_resumes_from_checkpoint_after_mid_plan_failure(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cfg = tmp_path / "cluster.yaml"
    assert main(["init", "--output", str(cfg)]) == 0
    capsys.readouterr()

    node_root = tmp_path / "compute1"
    host = make_node_service_host(node_root, "compute1")
    assert main(["secrets", "init", "--root", str(node_root)]) == 0
    capsys.readouterr()

    # Cause step 4 of 5 ('nova-compute') to fail during package installation on run 1.
    nova_install = ("/usr/bin/apt-get", "install", "--yes", "nova-compute")
    host.failures[nova_install] = ProcessOutput(
        100, "", "E: Sub-process /usr/bin/dpkg returned 1\n"
    )

    rc1 = main(
        [
            "apply",
            "--config",
            str(cfg),
            "--node",
            "compute1",
            "--root",
            str(node_root),
            "--all",
            "--yes",
        ],
        backend=host,
    )
    out1 = capsys.readouterr()
    assert rc1 == 1
    assert "hosts-file: applied" in out1.out
    assert "time-sync-other: applied" in out1.out
    assert "openstack-packages: applied" in out1.out
    assert "nova-compute: failed" in out1.out
    assert "Not started: neutron-compute" in out1.out

    state_path = node_root / DEFAULT_STATE_TARGET.lstrip("/")
    state_1 = json.loads(state_path.read_text(encoding="utf-8"))["steps"]
    assert state_1["hosts-file"]["status"] == "done"
    assert state_1["time-sync-other"]["status"] == "done"
    assert state_1["openstack-packages"]["status"] == "done"
    assert state_1["nova-compute"]["status"] == "failed"
    assert "neutron-compute" not in state_1

    # Fix the failure and re-run apply --all --yes: earlier 3 done steps are skipped
    # and execution resumes from nova-compute through neutron-compute.
    del host.failures[nova_install]
    rc2 = main(
        [
            "apply",
            "--config",
            str(cfg),
            "--node",
            "compute1",
            "--root",
            str(node_root),
            "--all",
            "--yes",
        ],
        backend=host,
    )
    out2 = capsys.readouterr()
    assert rc2 == 0
    assert "hosts-file: skipped" in out2.out
    assert "time-sync-other: skipped" in out2.out
    assert "openstack-packages: skipped" in out2.out
    assert "nova-compute: applied" in out2.out
    assert "neutron-compute: applied" in out2.out
    assert "All steps are done." in out2.out

    # Third run: all 5 steps are skipped.
    rc3 = main(
        [
            "apply",
            "--config",
            str(cfg),
            "--node",
            "compute1",
            "--root",
            str(node_root),
            "--all",
            "--yes",
        ],
        backend=host,
    )
    out3 = capsys.readouterr()
    assert rc3 == 0
    for step_id in (
        "hosts-file",
        "time-sync-other",
        "openstack-packages",
        "nova-compute",
        "neutron-compute",
    ):
        assert f"{step_id}: skipped" in out3.out


# --- openstack-forge verify -----------------------------------------------------------------


def test_verify_single_step_and_full_node_before_and_after_apply(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cfg = tmp_path / "cluster.yaml"
    assert main(["init", "--output", str(cfg)]) == 0
    capsys.readouterr()

    node_root = tmp_path / "block1"
    host = make_node_service_host(node_root, "block1")

    # 1. Verify before apply: fails with exit code 1 and never writes state.json or audit.log
    rc_before = main(
        [
            "verify",
            "--config",
            str(cfg),
            "--node",
            "block1",
            "--root",
            str(node_root),
        ],
        backend=host,
    )
    out_before = capsys.readouterr()
    assert rc_before == 1
    assert "FAIL  hosts-file (recorded: not_recorded)" in out_before.out
    assert "FAIL  cinder-storage (recorded: not_recorded)" in out_before.out
    assert (
        "Verified 4 step(s) on node 'block1' (role: block): 0 passed, 4 failed." in out_before.out
    )
    assert not (node_root / DEFAULT_STATE_TARGET.lstrip("/")).exists()
    assert not (node_root / DEFAULT_AUDIT_TARGET.lstrip("/")).exists()

    # 2. Verify JSON before apply
    rc_json_before = main(
        [
            "verify",
            "--config",
            str(cfg),
            "--node",
            "block1",
            "--root",
            str(node_root),
            "--json",
        ],
        backend=host,
    )
    payload_before = json.loads(capsys.readouterr().out)
    assert rc_json_before == 1
    assert payload_before["node"] == "block1"
    assert payload_before["role"] == "block"
    assert payload_before["ok"] is False
    assert payload_before["passed"] == 0
    assert payload_before["failed"] == 4
    assert [s["id"] for s in payload_before["steps"]] == [
        "hosts-file",
        "time-sync-other",
        "openstack-packages",
        "cinder-storage",
    ]
    assert all(s["recorded_status"] == "not_recorded" for s in payload_before["steps"])
    assert all(s["ok"] is False and s["problems"] for s in payload_before["steps"])

    # 3. Converge block1 with secrets init + apply --all --yes
    assert main(["secrets", "init", "--root", str(node_root)]) == 0
    assert (
        main(
            [
                "apply",
                "--config",
                str(cfg),
                "--node",
                "block1",
                "--root",
                str(node_root),
                "--all",
                "--yes",
            ],
            backend=host,
        )
        == 0
    )
    capsys.readouterr()

    # 4. Verify single step (--step hosts-file) in text mode
    rc_single = main(
        [
            "verify",
            "--config",
            str(cfg),
            "--node",
            "block1",
            "--root",
            str(node_root),
            "--step",
            "hosts-file",
        ],
        backend=host,
    )
    out_single = capsys.readouterr()
    assert rc_single == 0
    assert "PASS  hosts-file (recorded: done)" in out_single.out
    assert (
        "Verified 1 step(s) on node 'block1' (role: block): 1 passed, 0 failed." in out_single.out
    )

    # 5. Verify all steps (--all --json) after apply
    rc_after = main(
        [
            "verify",
            "--config",
            str(cfg),
            "--node",
            "block1",
            "--root",
            str(node_root),
            "--all",
            "--json",
        ],
        backend=host,
    )
    payload_after = json.loads(capsys.readouterr().out)
    assert rc_after == 0
    assert payload_after["ok"] is True
    assert payload_after["passed"] == 4
    assert payload_after["failed"] == 0
    assert all(s["recorded_status"] == "done" and s["ok"] is True for s in payload_after["steps"])
    assert all(s["problems"] == [] for s in payload_after["steps"])

    # Confirm no secret values appear in verify output
    secrets = SecretStore(node_root).load()
    raw_json = json.dumps(payload_after)
    for secret_value in secrets.values():
        assert secret_value not in raw_json
        assert secret_value not in out_single.out
        assert secret_value not in out_before.out
