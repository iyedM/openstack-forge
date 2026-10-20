"""Tests for ``openstack-forge doctor`` CLI, runner, JSON/table output, and exit codes."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest

from openstack_forge.cli import main
from openstack_forge.diagnostics import (
    CPUINFO_CMD,
    HOSTNAME_CMD,
    IP_ADDR_CMD,
    LSBLK_CMD,
    MEMINFO_CMD,
    NPROC_CMD,
    OS_RELEASE_CMD,
    READLINK_CMD,
    VIRT_CMD,
    CheckStatus,
    CommandError,
    CommandNotFoundError,
    CommandResult,
    CommandTimeoutError,
    SubprocessRunner,
    run_doctor,
)

ResponseSpec = CommandResult | Exception | str | tuple[int, str] | tuple[int, str, str]


class FakeCommandRunner:
    """In-memory CommandRunner double backed by recorded command outputs."""

    def __init__(self, responses: Mapping[Sequence[str] | str, ResponseSpec] | None = None) -> None:
        self._responses: dict[tuple[str, ...] | str, ResponseSpec] = {}
        self.calls: list[tuple[tuple[str, ...], float | None]] = []
        if responses:
            for key, value in responses.items():
                self.register(key, value)

    def register(self, key: Sequence[str] | str, response: ResponseSpec) -> None:
        normalised_key: tuple[str, ...] | str = (
            key if isinstance(key, str) else tuple(str(part) for part in key)
        )
        self._responses[normalised_key] = response

    def run(self, argv: Sequence[str], *, timeout: float | None = None) -> CommandResult:
        cmd = tuple(str(part) for part in argv)
        self.calls.append((cmd, timeout))
        spec = self._lookup(cmd)
        if isinstance(spec, Exception):
            raise spec
        if isinstance(spec, CommandResult):
            return spec
        if isinstance(spec, str):
            return CommandResult(argv=cmd, returncode=0, stdout=spec, stderr="")
        if len(spec) == 2:
            returncode, stdout = spec
            return CommandResult(argv=cmd, returncode=returncode, stdout=stdout, stderr="")
        returncode, stdout, stderr = spec
        return CommandResult(argv=cmd, returncode=returncode, stdout=stdout, stderr=stderr)

    def _lookup(self, cmd: tuple[str, ...]) -> ResponseSpec:
        if cmd in self._responses:
            return self._responses[cmd]
        if cmd and cmd[0] in self._responses:
            return self._responses[cmd[0]]
        raise CommandError(f"Unmocked command in FakeCommandRunner: {' '.join(cmd)}")


# Symlink resolution for the disks declared in valid_cluster.yaml, as readlink would report it.
DEFAULT_RESOLVED_PATHS: dict[str, str] = {
    "/dev/sdb": "/dev/sdb",
    "/dev/disk/by-id/wwn-0x5000c500a1b2c3d4": "/dev/sdc",
}


def _build_runner(
    doctor_fixture_text,
    *,
    hostname: str = "ctl-01\n",
    os_release_fixture: str = "os_release_ubuntu_2404.txt",
    nproc: str = "16\n",
    meminfo_fixture: str = "meminfo_64gb.txt",
    virt: tuple[int, str] = (0, "kvm\n"),
    cpuinfo_fixture: str = "cpuinfo_vmx.txt",
    lsblk_fixture: str = "lsblk_clean.json",
    ip_addr_fixture: str = "ip_addr_controller.json",
    resolved_paths: Mapping[str, str] = DEFAULT_RESOLVED_PATHS,
) -> FakeCommandRunner:
    runner = FakeCommandRunner(
        {
            HOSTNAME_CMD: hostname,
            OS_RELEASE_CMD: doctor_fixture_text(os_release_fixture),
            NPROC_CMD: nproc,
            MEMINFO_CMD: doctor_fixture_text(meminfo_fixture),
            VIRT_CMD: virt,
            CPUINFO_CMD: doctor_fixture_text(cpuinfo_fixture),
            LSBLK_CMD: doctor_fixture_text(lsblk_fixture),
            IP_ADDR_CMD: doctor_fixture_text(ip_addr_fixture),
        }
    )
    for declared, target in resolved_paths.items():
        runner.register((*READLINK_CMD, declared), f"{target}\n")
    return runner


# --- CLI & role end-to-end checks ----------------------------------------------


def test_doctor_controller_all_pass_table_output(
    fixture_path, doctor_fixture_text, capsys: pytest.CaptureFixture[str]
) -> None:
    runner = _build_runner(
        doctor_fixture_text,
        hostname="ctl-01\n",
        nproc="8\n",
        meminfo_fixture="meminfo_32gb.txt",
        ip_addr_fixture="ip_addr_controller.json",
    )
    exit_code = main(
        ["doctor", "--config", str(fixture_path("valid_cluster.yaml"))],
        runner=runner,
    )
    captured = capsys.readouterr()
    assert exit_code == 0
    assert captured.err == ""
    assert "Doctor report for node 'ctl-01'" in captured.out
    assert "identified by: hostname" in captured.out
    assert "Summary: 6 passed, 0 warning(s), 0 failed (overall: PASS)" in captured.out
    # Controller should not run lsblk or cpuinfo checks
    called_cmds = [cmd for cmd, _ in runner.calls]
    assert LSBLK_CMD not in called_cmds
    assert CPUINFO_CMD not in called_cmds


def test_doctor_compute_node_warn_exits_zero(
    fixture_path, doctor_fixture_text, capsys: pytest.CaptureFixture[str]
) -> None:
    runner = _build_runner(
        doctor_fixture_text,
        hostname="cmp-01\n",
        nproc="16\n",
        meminfo_fixture="meminfo_64gb.txt",
        virt=(1, "none\n"),
        cpuinfo_fixture="cpuinfo_no_virt.txt",
        ip_addr_fixture="ip_addr_compute.json",
    )
    exit_code = main(
        ["doctor", "--config", str(fixture_path("valid_cluster.yaml"))],
        runner=runner,
    )
    captured = capsys.readouterr()
    assert exit_code == 0
    assert "virt_type=qemu" in captured.out
    assert "physical (none)" in captured.out
    assert "Summary: 6 passed, 1 warning(s), 0 failed (overall: WARN)" in captured.out


def test_doctor_block_node_identified_by_ip(
    fixture_path, doctor_fixture_text, capsys: pytest.CaptureFixture[str]
) -> None:
    runner = _build_runner(
        doctor_fixture_text,
        hostname="unconfigured-hostname\n",
        nproc="4\n",
        meminfo_fixture="meminfo_16gb.txt",
        lsblk_fixture="lsblk_clean.json",
        ip_addr_fixture="ip_addr_block.json",
    )
    exit_code = main(
        ["doctor", "--config", str(fixture_path("valid_cluster.yaml"))],
        runner=runner,
    )
    captured = capsys.readouterr()
    assert exit_code == 0
    assert "Doctor report for node 'blk-01'" in captured.out
    assert "identified by: management_ip" in captured.out
    assert "Disk /dev/sdb exists, is a block disk, is not the root disk, and is empty." in (
        captured.out
    )
    # ip -j addr should only be invoked once (cached from node identification)
    ip_calls = [cmd for cmd, _ in runner.calls if cmd == IP_ADDR_CMD]
    assert len(ip_calls) == 1


def test_doctor_object_node_identified_by_cli_flag(
    fixture_path, doctor_fixture_text, capsys: pytest.CaptureFixture[str]
) -> None:
    runner = _build_runner(
        doctor_fixture_text,
        nproc="4\n",
        meminfo_fixture="meminfo_16gb.txt",
        lsblk_fixture="lsblk_clean.json",
        ip_addr_fixture="ip_addr_object.json",
    )
    exit_code = main(
        [
            "doctor",
            "--config",
            str(fixture_path("valid_cluster.yaml")),
            "--node",
            "obj-01",
        ],
        runner=runner,
    )
    captured = capsys.readouterr()
    assert exit_code == 0
    assert "Doctor report for node 'obj-01'" in captured.out
    assert "identified by: cli" in captured.out
    assert "disk:/dev/sdb" in captured.out
    assert "disk:/dev/disk/by-id/wwn-0x5000c500a1b2c3d4" in captured.out
    # hostname command is not needed when --node is passed
    assert all(cmd != HOSTNAME_CMD for cmd, _ in runner.calls)


# --- JSON output & exit codes --------------------------------------------------


def test_doctor_json_output_pass(
    fixture_path, doctor_fixture_text, capsys: pytest.CaptureFixture[str]
) -> None:
    runner = _build_runner(
        doctor_fixture_text,
        nproc="16\n",
        meminfo_fixture="meminfo_64gb.txt",
        cpuinfo_fixture="cpuinfo_vmx.txt",
        ip_addr_fixture="ip_addr_compute.json",
    )
    exit_code = main(
        [
            "doctor",
            "--config",
            str(fixture_path("valid_cluster.yaml")),
            "--node",
            "cmp-01",
            "--json",
        ],
        runner=runner,
    )
    captured = capsys.readouterr()
    assert exit_code == 0
    payload = json.loads(captured.out)
    assert payload["cluster"] == "fixture-cluster"
    assert payload["node"] == {
        "hostname": "cmp-01",
        "fqdn": "cmp-01.cluster.test",
        "role": "compute",
        "management_ip": "192.0.2.21",
        "identified_by": "cli",
    }
    assert payload["status"] == "PASS"
    assert payload["summary"] == {"pass": 7, "warn": 0, "fail": 0, "total": 7}
    check_names = [c["name"] for c in payload["checks"]]
    assert check_names == [
        "os",
        "vcpus",
        "ram",
        "virt_environment",
        "cpu_virtualization",
        "interfaces",
        "provider_interface",
    ]


def test_doctor_json_output_fail_exits_one(
    fixture_path, doctor_fixture_text, capsys: pytest.CaptureFixture[str]
) -> None:
    runner = _build_runner(
        doctor_fixture_text,
        os_release_fixture="os_release_ubuntu_2204.txt",
        nproc="2\n",
        meminfo_fixture="meminfo_8gb.txt",
        lsblk_fixture="lsblk_non_empty.json",
        ip_addr_fixture="ip_addr_block.json",
    )
    exit_code = main(
        [
            "doctor",
            "--config",
            str(fixture_path("valid_cluster.yaml")),
            "--node",
            "blk-01",
            "--json",
        ],
        runner=runner,
    )
    captured = capsys.readouterr()
    assert exit_code == 1
    payload = json.loads(captured.out)
    assert payload["status"] == "FAIL"
    assert payload["summary"]["fail"] == 4
    statuses = {c["name"]: c["status"] for c in payload["checks"]}
    assert statuses["os"] == "FAIL"
    assert statuses["vcpus"] == "FAIL"
    assert statuses["ram"] == "FAIL"
    assert statuses["disk"] == "FAIL"
    assert statuses["interfaces"] == "PASS"


# --- node identification failures & invalid config -----------------------------


def test_doctor_ambiguous_ip_fails_with_actionable_message(
    fixture_path, doctor_fixture_text, capsys: pytest.CaptureFixture[str]
) -> None:
    runner = _build_runner(
        doctor_fixture_text,
        hostname="unknown-host\n",
        ip_addr_fixture="ip_addr_ambiguous.json",
    )
    exit_code = main(
        ["doctor", "--config", str(fixture_path("valid_cluster.yaml"))],
        runner=runner,
    )
    captured = capsys.readouterr()
    assert exit_code == 1
    assert captured.out == ""
    assert "ambiguous" in captured.err
    assert "--node HOSTNAME" in captured.err


def test_doctor_node_not_found_fails_with_actionable_message(
    fixture_path, doctor_fixture_text, capsys: pytest.CaptureFixture[str]
) -> None:
    runner = _build_runner(
        doctor_fixture_text,
        hostname="unknown-host\n",
        ip_addr_fixture="ip_addr_wrong_mgmt_ip.json",
    )
    exit_code = main(
        ["doctor", "--config", str(fixture_path("valid_cluster.yaml"))],
        runner=runner,
    )
    captured = capsys.readouterr()
    assert exit_code == 1
    assert captured.out == ""
    assert "Could not identify local node" in captured.err
    assert "--node HOSTNAME" in captured.err


def test_doctor_reuses_config_validator(fixture_path, capsys: pytest.CaptureFixture[str]) -> None:
    runner = FakeCommandRunner()
    exit_code = main(
        ["doctor", "--config", str(fixture_path("unsupported_release.yaml"))],
        runner=runner,
    )
    captured = capsys.readouterr()
    assert exit_code == 1
    assert "Configuration is invalid" in captured.err
    assert "unsupported Ubuntu release '22.04'" in captured.err
    assert runner.calls == []


def test_doctor_missing_config_file(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = main(
        ["doctor", "--config", str(tmp_path / "missing.yaml")],
        runner=FakeCommandRunner(),
    )
    assert exit_code == 1
    assert "Configuration file not found" in capsys.readouterr().err


# --- command missing & command timeout -----------------------------------------


def test_doctor_command_missing_in_check_reports_fail(valid_config, doctor_fixture_text) -> None:
    runner = _build_runner(
        doctor_fixture_text,
        nproc="4\n",
        meminfo_fixture="meminfo_16gb.txt",
        ip_addr_fixture="ip_addr_block.json",
    )
    runner.register(LSBLK_CMD, CommandNotFoundError(LSBLK_CMD))

    report = run_doctor(valid_config, node_hostname="blk-01", runner=runner)
    assert report.status is CheckStatus.FAIL
    assert report.exit_code == 1
    disk_check = next(c for c in report.checks if c.name == "disk")
    assert disk_check.status is CheckStatus.FAIL
    assert disk_check.observed == "command missing (lsblk)"
    assert "Required command not found: 'lsblk'" in disk_check.message


def test_doctor_command_timeout_in_check_reports_fail(valid_config, doctor_fixture_text) -> None:
    runner = _build_runner(
        doctor_fixture_text,
        nproc="4\n",
        meminfo_fixture="meminfo_16gb.txt",
        ip_addr_fixture="ip_addr_object.json",
    )
    runner.register(LSBLK_CMD, CommandTimeoutError(LSBLK_CMD, 5.0))

    report = run_doctor(valid_config, node_hostname="obj-01", runner=runner)
    assert report.status is CheckStatus.FAIL
    assert report.exit_code == 1
    disk_checks = [c for c in report.checks if c.name.startswith("disk:")]
    assert len(disk_checks) == 2
    assert all(c.status is CheckStatus.FAIL for c in disk_checks)
    assert all(c.observed == "command timed out (lsblk)" for c in disk_checks)
    assert all("timed out after 5s" in c.message for c in disk_checks)


# --- PTTYPE and declared disk symlink resolution --------------------------------

NON_WWN_BY_ID = "/dev/disk/by-id/ata-QEMU_HARDDISK_QM00003"


def test_lsblk_requests_partition_table_type() -> None:
    columns = LSBLK_CMD[LSBLK_CMD.index("-o") + 1].split(",")
    assert "PTTYPE" in columns


def test_doctor_resolves_non_wwn_by_id_path(valid_config, doctor_fixture_text) -> None:
    valid_config["nodes"][3]["disks"] = [NON_WWN_BY_ID, "/dev/sdb"]
    runner = _build_runner(
        doctor_fixture_text,
        nproc="4\n",
        meminfo_fixture="meminfo_16gb.txt",
        ip_addr_fixture="ip_addr_object.json",
        resolved_paths={NON_WWN_BY_ID: "/dev/sdc", "/dev/sdb": "/dev/sdb"},
    )

    report = run_doctor(valid_config, node_hostname="obj-01", runner=runner)

    disk_checks = {c.name: c for c in report.checks if c.name.startswith("disk:")}
    by_id = disk_checks[f"disk:{NON_WWN_BY_ID}"]
    assert by_id.status is CheckStatus.PASS
    assert by_id.observed.startswith("/dev/sdc (empty disk")
    assert disk_checks["disk:/dev/sdb"].status is CheckStatus.PASS
    # Each declared path is resolved as one argv element, without a shell or privileges.
    readlink_calls = [cmd for cmd, _ in runner.calls if cmd[0] == "readlink"]
    assert readlink_calls == [
        ("readlink", "--canonicalize-existing", "--", NON_WWN_BY_ID),
        ("readlink", "--canonicalize-existing", "--", "/dev/sdb"),
    ]
    assert all(cmd[0] != "sudo" for cmd, _ in runner.calls)


def test_doctor_by_id_path_to_disk_with_partition_table_fails(
    valid_config, doctor_fixture_text
) -> None:
    valid_config["nodes"][2]["disk"] = NON_WWN_BY_ID
    runner = _build_runner(
        doctor_fixture_text,
        nproc="4\n",
        meminfo_fixture="meminfo_16gb.txt",
        lsblk_fixture="lsblk_non_empty.json",
        ip_addr_fixture="ip_addr_block.json",
        resolved_paths={NON_WWN_BY_ID: "/dev/sdg"},
    )

    report = run_doctor(valid_config, node_hostname="blk-01", runner=runner)

    disk_check = next(c for c in report.checks if c.name == "disk")
    assert disk_check.status is CheckStatus.FAIL
    assert disk_check.observed == "/dev/sdg (partition table: dos)"
    assert "has a partition table (dos)" in disk_check.message
    assert report.exit_code == 1


def test_doctor_unresolvable_by_id_path_is_missing(valid_config, doctor_fixture_text) -> None:
    valid_config["nodes"][2]["disk"] = NON_WWN_BY_ID
    runner = _build_runner(
        doctor_fixture_text,
        nproc="4\n",
        meminfo_fixture="meminfo_16gb.txt",
        ip_addr_fixture="ip_addr_block.json",
        resolved_paths={},
    )
    # readlink -e exits 1 with no output when the path does not exist.
    runner.register((*READLINK_CMD, NON_WWN_BY_ID), (1, ""))

    report = run_doctor(valid_config, node_hostname="blk-01", runner=runner)

    disk_check = next(c for c in report.checks if c.name == "disk")
    assert disk_check.status is CheckStatus.FAIL
    assert disk_check.observed == "missing"
    assert disk_check.message == f"Declared disk {NON_WWN_BY_ID} does not exist in lsblk output."


def test_doctor_readlink_missing_reports_fail(valid_config, doctor_fixture_text) -> None:
    runner = _build_runner(
        doctor_fixture_text,
        nproc="4\n",
        meminfo_fixture="meminfo_16gb.txt",
        ip_addr_fixture="ip_addr_block.json",
        resolved_paths={},
    )
    readlink_argv = (*READLINK_CMD, "/dev/sdb")
    runner.register(readlink_argv, CommandNotFoundError(readlink_argv))

    report = run_doctor(valid_config, node_hostname="blk-01", runner=runner)

    disk_check = next(c for c in report.checks if c.name == "disk")
    assert disk_check.status is CheckStatus.FAIL
    assert disk_check.observed == "command missing (readlink)"
    assert "Required command not found: 'readlink'" in disk_check.message


def test_doctor_command_missing_during_node_resolution_exits_one(
    fixture_path, capsys: pytest.CaptureFixture[str]
) -> None:
    runner = FakeCommandRunner({HOSTNAME_CMD: CommandNotFoundError(HOSTNAME_CMD)})
    exit_code = main(
        ["doctor", "--config", str(fixture_path("valid_cluster.yaml"))],
        runner=runner,
    )
    captured = capsys.readouterr()
    assert exit_code == 1
    assert "Required command not found: 'hostname'" in captured.err


def test_subprocess_runner_command_missing_and_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = SubprocessRunner(default_timeout=2.5)

    def raise_missing(*args: Any, **kwargs: Any) -> Any:
        assert kwargs.get("shell") is False
        raise FileNotFoundError(2, "No such file or directory", args[0][0])

    monkeypatch.setattr(subprocess, "run", raise_missing)
    with pytest.raises(CommandNotFoundError, match="'nonexistent-binary'"):
        runner.run(["nonexistent-binary", "--flag"])

    def raise_timeout(*args: Any, **kwargs: Any) -> Any:
        assert kwargs.get("shell") is False
        assert kwargs.get("timeout") == 1.5
        raise subprocess.TimeoutExpired(cmd=args[0], timeout=1.5)

    monkeypatch.setattr(subprocess, "run", raise_timeout)
    with pytest.raises(CommandTimeoutError, match="timed out after 1.5s: lsblk -J -b"):
        runner.run(["lsblk", "-J", "-b"], timeout=1.5)


def test_subprocess_runner_success_and_validation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(ValueError, match="default_timeout must be positive"):
        SubprocessRunner(default_timeout=0)

    runner = SubprocessRunner()
    with pytest.raises(CommandError, match="must not be empty"):
        runner.run([])
    with pytest.raises(ValueError, match="timeout must be positive"):
        runner.run(["nproc"], timeout=-1)

    def fake_completed(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        assert kwargs["shell"] is False
        return subprocess.CompletedProcess(args=args[0], returncode=0, stdout="8\n", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_completed)
    res = runner.run(["nproc"])
    assert res.argv == ("nproc",)
    assert res.returncode == 0
    assert res.stdout == "8\n"
