"""End-to-end 3-node cluster convergence, idempotence, verification, and secret-hygiene suite."""

from __future__ import annotations

import json
import socket
import subprocess
from pathlib import Path

import pytest

from openstack_forge.cli import main
from openstack_forge.execution import (
    DEFAULT_AUDIT_TARGET,
    DEFAULT_STATE_TARGET,
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


def test_full_three_node_cluster_convergence_idempotence_verify_and_secret_hygiene(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # 1. Generate and validate the 3-node cluster configuration via CLI
    cfg_path = tmp_path / "cluster.yaml"
    assert main(["init", "--output", str(cfg_path)]) == 0
    assert main(["config", "validate", str(cfg_path)]) == 0
    assert main(["plan", "--config", str(cfg_path), "--json"]) == 0
    capsys.readouterr()

    node_specs = [
        ("controller", "controller", 14),
        ("compute1", "compute", 5),
        ("block1", "block", 4),
    ]

    for hostname, role, expected_steps in node_specs:
        node_root = tmp_path / "nodes" / hostname
        host = make_node_service_host(node_root, hostname)
        captured_outputs: list[str] = []

        # Step 1: secrets init --root <node_root>
        rc_secrets = main(["secrets", "init", "--root", str(node_root)])
        cap = capsys.readouterr()
        captured_outputs.extend([cap.out, cap.err])
        assert rc_secrets == 0, f"secrets init failed on {hostname}: {cap.err}"
        assert "Created (16):" in cap.out

        secrets = SecretStore(node_root).load()
        assert len(secrets) == 16

        # Step 2: apply --all --yes (Run 1 -> all steps applied and recorded done)
        rc_apply_1 = main(
            [
                "apply",
                "--config",
                str(cfg_path),
                "--node",
                hostname,
                "--root",
                str(node_root),
                "--all",
                "--yes",
            ],
            backend=host,
        )
        cap = capsys.readouterr()
        captured_outputs.extend([cap.out, cap.err])
        assert rc_apply_1 == 0, f"apply run 1 failed on {hostname}: {cap.out}\n{cap.err}"
        assert "All steps are done." in cap.out
        assert cap.out.count(": applied") == expected_steps

        state_path = node_root / DEFAULT_STATE_TARGET.lstrip("/")
        state_payload = json.loads(state_path.read_text(encoding="utf-8"))
        assert len(state_payload["steps"]) == expected_steps
        for step_id, record in state_payload["steps"].items():
            assert record["status"] == "done", f"{hostname}:{step_id} not done: {record}"

        # Step 3: apply --all --yes (Run 2 -> 100% idempotent, all steps skipped)
        calls_before_run_2 = len(host.calls)
        rc_apply_2 = main(
            [
                "apply",
                "--config",
                str(cfg_path),
                "--node",
                hostname,
                "--root",
                str(node_root),
                "--all",
                "--yes",
            ],
            backend=host,
        )
        cap = capsys.readouterr()
        captured_outputs.extend([cap.out, cap.err])
        assert rc_apply_2 == 0, f"apply run 2 failed on {hostname}: {cap.out}\n{cap.err}"
        assert "All steps are done." in cap.out
        assert cap.out.count(": skipped") == expected_steps
        assert ": applied" not in cap.out
        # No mutating command was run on the second apply pass:
        run_2_calls = host.calls[calls_before_run_2:]
        for argv in run_2_calls:
            assert argv[:2] not in {
                ("/usr/bin/apt-get", "install"),
                ("/usr/bin/systemctl", "restart"),
                ("/usr/bin/systemctl", "reload"),
                ("/usr/bin/systemctl", "enable"),
            }

        # Step 4: verify --config <cfg> --node <hostname> --root <node_root> --json
        rc_verify = main(
            [
                "verify",
                "--config",
                str(cfg_path),
                "--node",
                hostname,
                "--root",
                str(node_root),
                "--json",
            ],
            backend=host,
        )
        cap = capsys.readouterr()
        captured_outputs.extend([cap.out, cap.err])
        assert rc_verify == 0, f"verify failed on {hostname}: {cap.out}\n{cap.err}"
        verify_report = json.loads(cap.out)
        assert verify_report["node"] == hostname
        assert verify_report["role"] == role
        assert verify_report["ok"] is True
        assert verify_report["passed"] == expected_steps
        assert verify_report["failed"] == 0
        assert len(verify_report["steps"]) == expected_steps
        for step_entry in verify_report["steps"]:
            assert step_entry["recorded_status"] == "done"
            assert step_entry["ok"] is True
            assert step_entry["problems"] == []

        # Step 5: Zero secret leakage in CLI outputs, state.json, audit.log, and .orig backups
        audit_path = node_root / DEFAULT_AUDIT_TARGET.lstrip("/")
        state_text = state_path.read_text(encoding="utf-8")
        audit_text = audit_path.read_text(encoding="utf-8")
        combined_cli_output = "\n".join(captured_outputs)

        for secret_name, secret_value in secrets.items():
            assert secret_value not in combined_cli_output, (
                f"{secret_name} leaked in CLI output on {hostname}"
            )
            assert secret_value not in state_text, (
                f"{secret_name} leaked in state.json on {hostname}"
            )
            assert secret_value not in audit_text, (
                f"{secret_name} leaked in audit.log on {hostname}"
            )

        for orig_file in node_root.rglob("*.orig"):
            orig_text = orig_file.read_text(encoding="utf-8")
            for secret_name, secret_value in secrets.items():
                assert secret_value not in orig_text, (
                    f"{secret_name} leaked in backup file {orig_file} on {hostname}"
                )
