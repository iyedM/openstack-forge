"""Security hardening unit tests covering all 7 AppSec / Linux / OpenStack audit axes."""

from __future__ import annotations

import os
import re
import stat
from pathlib import Path

import pytest

from openstack_forge.config.schema import load_schema, validate_schema
from openstack_forge.diagnostics.doctor import _resolve_device_path
from openstack_forge.diagnostics.errors import CommandError as DiagnosticsCommandError
from openstack_forge.diagnostics.runner import SubprocessRunner
from openstack_forge.execution.audit import AuditLog
from openstack_forge.execution.chrony import build_chrony_block
from openstack_forge.execution.commands import (
    DEFAULT_REGISTRY,
    OPENSTACK_ADMIN_AUTH_ARGS,
    CommandResult,
)
from openstack_forge.execution.errors import (
    AuditLogError,
    ExecutionError,
    FileFormatError,
    PreconditionError,
    UnexpectedExitError,
    UnsafePathError,
)
from openstack_forge.execution.files import ini_set, managed_block
from openstack_forge.execution.fsutil import (
    atomic_write_text,
    backup_once,
    read_text_file,
)
from openstack_forge.execution.horizon import horizon_allowed_hosts
from openstack_forge.execution.hosts import build_127_0_1_1_pattern, build_hosts_block
from openstack_forge.execution.keystone import write_admin_openrc
from openstack_forge.execution.preview import StepPreview
from openstack_forge.execution.redaction import (
    MASK,
    clear_registered_secrets,
    redact_text,
    register_secret,
)
from openstack_forge.execution.state import StateStore, apply_lock


def test_axis1_openstack_cli_specs_use_env_password_not_argv() -> None:
    """All 11 openstack.* specs pass admin_password via OS_PASSWORD env, never --os-password."""
    assert "--os-password" not in OPENSTACK_ADMIN_AUTH_ARGS
    openstack_ids = [cid for cid in DEFAULT_REGISTRY.ids() if cid.startswith("openstack.")]
    assert len(openstack_ids) == 11
    for cid in openstack_ids:
        spec = DEFAULT_REGISTRY.get(cid)
        assert spec.env_params.get("admin_password") == "OS_PASSWORD"
        assert all("--os-password" not in token for token in spec.argv)

    # Only the 4 documented specs pass a target user password in argv due to upstream CLI limits
    argv_secret_specs = {
        cid
        for cid in DEFAULT_REGISTRY.ids()
        for spec in (DEFAULT_REGISTRY.get(cid),)
        if any(f"{{{param}}}" in token for token in spec.argv for param in spec.sensitive_params)
    }
    assert argv_secret_specs == {
        "rabbitmqctl.add-user",
        "rabbitmqctl.change-password",
        "openstack.user-create",
        "openstack.user-set-password",
    }


def test_axis2_storage_permissions_and_symlink_rejection(tmp_path: Path) -> None:
    """StateStore, apply_lock, AuditLog, and write_admin_openrc enforce 0600 and reject symlinks."""
    # 1. admin-openrc is written to /etc/openstack-forge/admin-openrc with 0700 dir and 0600 file
    change = write_admin_openrc(tmp_path, "A" * 32, dry_run=False)
    assert change.changed is True
    openrc_path = tmp_path / "etc/openstack-forge/admin-openrc"
    assert stat.S_IMODE(openrc_path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(openrc_path.stat().st_mode) == 0o600
    assert not openrc_path.with_name("admin-openrc.orig").exists()

    # 2. StateStore creates 0600 state.json and rejects symlinked state.json on load()
    state_dir = tmp_path / "var/lib/openstack-forge"
    state_dir.mkdir(parents=True, exist_ok=True)
    store = StateStore(state_dir / "state.json")
    store.mark_done("hosts-file")
    assert stat.S_IMODE(store.path.stat().st_mode) == 0o600
    store.path.unlink()
    outside = tmp_path / "outside.json"
    outside.write_text("{}", encoding="utf-8")
    store.path.symlink_to(outside)
    with pytest.raises(UnsafePathError, match="symlink"):
        store.load()
    store.path.unlink()

    # 3. apply_lock creates 0600 lock file and rejects symlinked lock file
    with apply_lock(tmp_path):
        lock_path = tmp_path / "var/lib/openstack-forge/apply.lock"
        assert stat.S_IMODE(lock_path.stat().st_mode) == 0o600
    lock_path.unlink()
    lock_path.symlink_to(outside)
    with pytest.raises(UnsafePathError, match="symlink"):
        with apply_lock(tmp_path):
            pass

    # 4. AuditLog creates 0600 audit.log and rejects symlinked audit.log
    audit_dir = tmp_path / "var/log/openstack-forge"
    audit_dir.mkdir(parents=True, exist_ok=True)
    audit = AuditLog(audit_dir / "audit.log")
    audit.record({"command_id": "dpkg-query.package-status", "returncode": 0})
    assert stat.S_IMODE(audit.path.stat().st_mode) == 0o600
    audit.path.unlink()
    audit.path.symlink_to(outside)
    with pytest.raises(AuditLogError, match="symlink"):
        audit.record({"command_id": "dpkg-query.package-status", "returncode": 0})
    with pytest.raises(AuditLogError, match="symlink"):
        audit.read()


def test_axis3_redaction_in_exceptions_command_result_repr_and_preview() -> None:
    """Secrets in stderr, CommandResult.__repr__, and StepPreview.notes are always masked."""
    clear_registered_secrets()
    try:
        secret = "SuperSecretValue9876543210ABCDEF"
        register_secret(secret)

        exc = UnexpectedExitError(
            "openstack.project-list",
            1,
            f"HTTP 401: invalid token for {secret} and password=InlineLeak123",
        )
        assert secret not in exc.stderr
        assert "InlineLeak123" not in exc.stderr
        assert secret not in str(exc)
        assert MASK in exc.stderr

        res = CommandResult(
            command_id="openstack.project-list",
            argv=("/usr/bin/openstack", "project", "list"),
            returncode=0,
            stdout=f"token={secret}",
            stderr=f"mysql+pymysql://keystone:{secret}@controller/keystone",
        )
        rendered = repr(res)
        assert secret not in rendered
        assert MASK in rendered

        preview = StepPreview(
            notes=(
                f"Note with {secret}",
                "Precondition not yet met: keystone requires KEYSTONE_DBPASS in "
                "/etc/openstack-forge/secrets.json: run 'openstack-forge secrets init' first",
            )
        )
        assert secret not in preview.notes[0]
        assert MASK in preview.notes[0]
        assert "openstack-forge secrets init" in preview.notes[1]
        assert redact_text(preview.notes[1]) == preview.notes[1]
    finally:
        clear_registered_secrets()


def test_axis4_fsutil_and_files_reject_symlinks_and_nul_bytes(tmp_path: Path) -> None:
    """atomic_write_text, backup_once, read_text_file, and file editors reject symlinks and NUL."""
    target = tmp_path / "etc/test.conf"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("[DEFAULT]\nkey = value\n", encoding="utf-8")

    # Dangling .orig symlink must be rejected by backup_once
    orig_link = target.with_name("test.conf.orig")
    orig_link.symlink_to(tmp_path / "nonexistent-target")
    with pytest.raises(UnsafePathError, match="symlink"):
        backup_once(target)
    orig_link.unlink()

    # Symlink target must be rejected by atomic_write_text
    outside = tmp_path / "outside.conf"
    outside.write_text("outside\n", encoding="utf-8")
    link_target = tmp_path / "etc/link.conf"
    link_target.symlink_to(outside)
    with pytest.raises(UnsafePathError, match="symlink"):
        atomic_write_text(link_target, "compromised\n", mode=0o600)
    assert outside.read_text(encoding="utf-8") == "outside\n"

    # NUL bytes in existing file rejected by read_text_file
    nul_file = tmp_path / "etc/nul.conf"
    nul_file.write_bytes(b"[DEFAULT]\nkey = val\x00ue\n")
    with pytest.raises(FileFormatError, match="NUL"):
        read_text_file(nul_file, "/etc/nul.conf")

    # NUL bytes in managed_block or ini_set inputs rejected
    with pytest.raises(FileFormatError, match="NUL"):
        managed_block(tmp_path, "/etc/test.conf", "block", "line\x00bad\n", dry_run=True)
    with pytest.raises(ValueError, match="NUL"):
        ini_set(tmp_path, "/etc/test.conf", "DEFAULT", "key", "val\x00bad", dry_run=True)


def test_axis5_injection_guards_in_generators_diagnostics_and_schema() -> None:
    """Config generators, diagnostics runner/doctor, and JSON schema reject injection payloads."""
    with pytest.raises(PreconditionError, match="invalid 'hostname'"):
        horizon_allowed_hosts(
            {"hostname": "ctrl' + __import__('os').system('id') + '", "fqdn": "ctrl.example.test"}
        )
    with pytest.raises(PreconditionError, match="invalid 'fqdn'"):
        horizon_allowed_hosts({"hostname": "ctrl", "fqdn": "ctrl.example.test\nPWNED = True"})

    with pytest.raises(ExecutionError, match="Invalid NTP server"):
        build_chrony_block(
            {
                "cluster": {"ntp_servers": ["0.ubuntu.pool.ntp.org\nallow 0.0.0.0/0"]},
                "networks": {"management": {"subnet": "192.0.2.0/24"}},
            }
        )
    with pytest.raises(ExecutionError, match="Invalid management subnet"):
        build_chrony_block(
            {
                "cluster": {"ntp_servers": ["0.ubuntu.pool.ntp.org"]},
                "networks": {"management": {"subnet": "192.0.2.0/24\ncmdport 123"}},
            }
        )

    with pytest.raises(ExecutionError, match="Invalid hostname"):
        build_hosts_block(
            [
                {
                    "hostname": "ctrl\n192.0.2.99 evil",
                    "fqdn": "ctrl.example.test",
                    "management_ip": "192.0.2.10",
                    "role": "controller",
                }
            ]
        )
    with pytest.raises(ExecutionError, match="Invalid fqdn"):
        build_127_0_1_1_pattern(
            {
                "hostname": "ctrl",
                "fqdn": "ctrl.example.test\n192.0.2.99 evil",
                "management_ip": "192.0.2.10",
            }
        )

    runner = SubprocessRunner()
    with pytest.raises(DiagnosticsCommandError, match="NUL bytes or line breaks"):
        runner.run(["ip\x00bad", "-o", "addr"])
    with pytest.raises(DiagnosticsCommandError, match="NUL bytes or line breaks"):
        runner.run(["ip", "addr\nshow"])

    with pytest.raises(DiagnosticsCommandError, match="Invalid block device path"):
        _resolve_device_path(runner, "../../etc/shadow")
    with pytest.raises(DiagnosticsCommandError, match="Invalid block device path"):
        _resolve_device_path(runner, "/dev/sdb;id")

    bad_config = {
        "cluster": {
            "name": "test-cluster",
            "ubuntu_version": "24.04",
            "openstack_release": "2025.1",
            "topology": "controller-compute",
        },
        "networks": {
            "management": {
                "subnet": "192.0.2.0/24",
                "gateway": "192.0.2.1",
                "interface": "eth0",
            },
            "provider": {
                "subnet": "203.0.113.0/24",
                "gateway": "203.0.113.1",
                "interface": "eth0; rm -rf /",
                "floating_ip_range": {"start": "203.0.113.100", "end": "203.0.113.200"},
            },
        },
        "nodes": [
            {
                "hostname": "controller",
                "fqdn": "controller.example.test",
                "role": "controller",
                "management_ip": "192.0.2.10",
            }
        ],
    }
    errors = validate_schema(bad_config, load_schema())
    assert any("interface" in str(err) for err in errors)
    assert not os.path.exists("/nonexistent-forge-check")


def test_axis6_no_rfc1918_or_private_keys_in_repo() -> None:
    """Repository contains zero RFC 1918 private IPs or PEM private keys."""
    repo_root = Path(__file__).resolve().parents[2]
    rfc1918_re = re.compile(
        r"\b(?:10\.\d{1,3}\.\d{1,3}\.\d{1,3}"
        r"|192\.168\.\d{1,3}\.\d{1,3}"
        r"|172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3})\b"
    )
    privkey_re = re.compile(r"BEGIN (?:RSA |EC |OPENSSH |DSA |)PRIVATE KEY")

    scan_roots = (
        repo_root / "src",
        repo_root / "tests",
        repo_root / "docs",
        repo_root / "examples",
        repo_root / "README.md",
        repo_root / "CHANGELOG.md",
    )
    offenders: list[str] = []
    for root in scan_roots:
        paths = [root] if root.is_file() else sorted(p for p in root.rglob("*") if p.is_file())
        for path in paths:
            if "__pycache__" in path.parts or path.suffix in {".pyc", ".png", ".jpg"}:
                continue
            text = path.read_text(encoding="utf-8")
            rel = path.relative_to(repo_root)
            for match in rfc1918_re.finditer(text):
                offenders.append(f"{rel}: RFC1918 {match.group(0)}")
            if path != Path(__file__) and privkey_re.search(text):
                offenders.append(f"{rel}: private key header")
    assert offenders == []
