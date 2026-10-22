"""Unit tests for the ``cinder-controller`` and ``cinder-storage`` steps (runs nothing)."""

from __future__ import annotations

import json
import os
import socket
import subprocess
from pathlib import Path
from typing import Any

import pytest

from openstack_forge.execution import (
    ALLOW_LVM_ROOT_FILTER_OVERRIDE_KEY,
    AUDIT_FILE_MODE,
    CINDER_CONF_TARGET,
    CINDER_CONTROLLER_PACKAGES,
    CINDER_CONTROLLER_STEP_ID,
    CINDER_CONTROLLER_UNITS,
    CINDER_DB_NAME,
    CINDER_ENDPOINT_URL,
    CINDER_MANAGE_DB_SYNC,
    CINDER_MANAGE_FAILURE_HINT,
    CINDER_SERVICE_NAME,
    CINDER_SERVICE_TYPE,
    CINDER_STORAGE_INSTALL_DOC_URL,
    CINDER_STORAGE_LVM_FILTER_WARNING,
    CINDER_STORAGE_PACKAGES,
    CINDER_STORAGE_STEP_ID,
    CINDER_STORAGE_UNITS,
    CINDER_TGT_BLOCK_MARKER,
    CINDER_TGT_CONF_TARGET,
    CINDER_TGT_INCLUDE_LINE,
    CINDER_VG_NAME,
    DEFAULT_AUDIT_TARGET,
    DEFAULT_REGISTRY,
    DEFAULT_SECRETS_TARGET,
    DEFAULT_STATE_TARGET,
    DISABLED_LINE_PREFIX,
    LVM_CONF_TARGET,
    LVM_FAILURE_HINT,
    LVM_PVCREATE,
    LVM_PVS,
    LVM_VGCREATE_CINDER_VOLUMES,
    LVM_VGS,
    NOVA_CONF_TARGET,
    AuditLog,
    CinderControllerStep,
    CinderStorageStep,
    CommandExecutor,
    Engine,
    ExecutionError,
    FileFormatError,
    Outcome,
    ParameterError,
    PreconditionError,
    SecretStore,
    StateStore,
    build_step,
    check_cinder_lvm_root_safety,
    cinder_controller_conf_settings,
    cinder_db_connection,
    cinder_storage_conf_settings,
    cinder_transport_url,
    clear_registered_secrets,
    edit_cinder_controller_conf,
    edit_cinder_storage_conf,
    edit_cinder_tgt_conf,
    edit_lvm_conf,
    edit_nova_controller_cinder_conf,
    ensure_lvm_pv_and_vg,
    lvm_filter_for_disk,
    lvm_pv_and_vg_problems,
    lvm_pv_and_vg_ready,
    make_cinder_controller_step,
    make_cinder_storage_step,
    nova_controller_cinder_conf_settings,
    parse_lvm_names,
    resolve_block_disk,
    resolve_target,
    validate_parameter,
    verify_cinder_controller_conf_text,
    verify_cinder_storage_conf_text,
    verify_cinder_tgt_conf_text,
    verify_lvm_conf_text,
    verify_nova_controller_cinder_conf_text,
)
from tests.unit._service_fakes import DEV_MACHINE_SANITIZED_IP, ServiceHost

TEST_ADMIN_PASS = "TestAdminSecretValue0123456789AB"
TEST_RABBIT_PASS = "TestRabbitSecretValue0123456789A"
TEST_CINDER_DBPASS = "TestCinderDbSecretValue012345678"
TEST_CINDER_PASS = "TestCinderServiceSecretValue0123"

CONTROLLER_NODE: dict[str, Any] = {
    "role": "controller",
    "hostname": "controller",
    "fqdn": "controller.example.test",
    "management_ip": DEV_MACHINE_SANITIZED_IP,
}
BLOCK_NODE: dict[str, Any] = {
    "role": "block",
    "hostname": "block1",
    "fqdn": "block1.example.test",
    "management_ip": "192.0.2.41",
    "disk": "/dev/sdb",
}
COMPUTE_NODE: dict[str, Any] = {
    "role": "compute",
    "hostname": "compute1",
    "fqdn": "compute1.example.test",
    "management_ip": "192.0.2.31",
}
SAMPLE_CONFIG: dict[str, Any] = {
    "nodes": [CONTROLLER_NODE, COMPUTE_NODE, BLOCK_NODE],
}

UBUNTU_DEFAULT_CINDER_CONF = """\
[DEFAULT]
rootwrap_config = /etc/cinder/rootwrap.conf
api_paste_confg = /etc/cinder/api-paste.ini
iscsi_helper = lioadm
volume_name_template = volume-%s
volume_group = cinder-volumes
verbose = True
auth_strategy = keystone
state_path = /var/lib/cinder
lock_path = /var/lock/cinder
volumes_dir = /var/lib/cinder/volumes
enabled_backends = lvm

[database]
connection = sqlite:////var/lib/cinder/cinder.sqlite

[keystone_authtoken]
#www_authenticate_uri = <None>

[oslo_concurrency]
#lock_path = <None>
"""

UBUNTU_DEFAULT_NOVA_CONF = """\
[DEFAULT]
my_ip = 192.0.2.170

[cinder]
#os_region_name = <None>
"""

UBUNTU_DEFAULT_LVM_CONF = """\
config {
\tchecks = 1
}

devices {
\tdir = "/dev"
\tscan = [ "/dev" ]
\t# filter = [ "a|.*|" ]
\twrite_cache_state = 1
}
"""

UBUNTU_DEFAULT_TGT_CINDER_CONF = "include /var/lib/cinder/volumes/*\n"


@pytest.fixture(autouse=True)
def _forbid_real_subprocess_and_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(*args: object, **kwargs: object) -> None:
        raise AssertionError(f"real subprocess/socket call is forbidden: {args!r}")

    monkeypatch.setattr(subprocess, "Popen", _boom)
    monkeypatch.setattr(subprocess, "run", _boom)
    monkeypatch.setattr(socket, "create_connection", _boom)
    monkeypatch.setattr("openstack_forge.execution.commands.is_host_root", lambda _: True)
    clear_registered_secrets()
    yield
    clear_registered_secrets()


def _seed_controller_secrets(root: Path) -> SecretStore:
    secrets_dir = root / "etc" / "openstack-forge"
    secrets_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(secrets_dir, 0o700)
    secrets_file = secrets_dir / "secrets.json"
    secrets_file.write_text(
        json.dumps(
            {
                "ADMIN_PASS": TEST_ADMIN_PASS,
                "RABBIT_PASS": TEST_RABBIT_PASS,
                "CINDER_DBPASS": TEST_CINDER_DBPASS,
                "CINDER_PASS": TEST_CINDER_PASS,
            }
        ),
        encoding="utf-8",
    )
    os.chmod(secrets_file, 0o600)
    store = SecretStore(root)
    store.ensure()
    return store


def _seed_storage_secrets_only(root: Path) -> SecretStore:
    secrets_dir = root / "etc" / "openstack-forge"
    secrets_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(secrets_dir, 0o700)
    secrets_file = secrets_dir / "secrets.json"
    secrets_file.write_text(
        json.dumps(
            {
                "RABBIT_PASS": TEST_RABBIT_PASS,
                "CINDER_DBPASS": TEST_CINDER_DBPASS,
                "CINDER_PASS": TEST_CINDER_PASS,
            }
        ),
        encoding="utf-8",
    )
    os.chmod(secrets_file, 0o600)
    return SecretStore(root)


def _seed_nova_conf(root: Path) -> None:
    nova_path = root / "etc/nova/nova.conf"
    nova_path.parent.mkdir(parents=True, exist_ok=True)
    nova_path.write_text(UBUNTU_DEFAULT_NOVA_CONF, encoding="utf-8")


def _controller_package_creates() -> dict[str, tuple[str, str]]:
    return {
        "cinder-api": ("etc/cinder/cinder.conf", UBUNTU_DEFAULT_CINDER_CONF),
    }


def _storage_package_creates() -> dict[str, tuple[str, str]]:
    return {
        "lvm2": ("etc/lvm/lvm.conf", UBUNTU_DEFAULT_LVM_CONF),
        "cinder-volume": ("etc/cinder/cinder.conf", UBUNTU_DEFAULT_CINDER_CONF),
        "tgt": ("etc/tgt/conf.d/cinder.conf", UBUNTU_DEFAULT_TGT_CINDER_CONF),
    }


def _make_executor(root: Path, host: ServiceHost, *, dry_run: bool = False) -> CommandExecutor:
    audit_path = root / DEFAULT_AUDIT_TARGET.lstrip("/")
    audit_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    audit = AuditLog(audit_path)
    return CommandExecutor(
        DEFAULT_REGISTRY,
        root=root,
        audit_log=audit,
        backend=host,
        dry_run=dry_run,
        echo=lambda _: None,
        euid=lambda: 0,
    )


# --- CommandSpec and parameter validation tests ----------------------------------------


def test_cinder_and_lvm_commands_are_registered_in_default_registry() -> None:
    spec_db = DEFAULT_REGISTRY.get(CINDER_MANAGE_DB_SYNC)
    assert spec_db.argv == ("/usr/bin/cinder-manage", "db", "sync")
    assert spec_db.mutates is True
    assert spec_db.failure_hint == CINDER_MANAGE_FAILURE_HINT

    spec_pvs = DEFAULT_REGISTRY.get(LVM_PVS)
    assert spec_pvs.argv == ("/usr/sbin/pvs", "--noheadings", "-o", "pv_name")
    assert spec_pvs.mutates is False

    spec_pvcreate = DEFAULT_REGISTRY.get(LVM_PVCREATE)
    assert spec_pvcreate.build_argv({"device": "/dev/sdb"}) == ("/usr/sbin/pvcreate", "/dev/sdb")
    assert spec_pvcreate.mutates is True
    assert spec_pvcreate.failure_hint == LVM_FAILURE_HINT

    spec_vgs = DEFAULT_REGISTRY.get(LVM_VGS)
    assert spec_vgs.argv == ("/usr/sbin/vgs", "--noheadings", "-o", "vg_name")
    assert spec_vgs.mutates is False

    spec_vgcreate = DEFAULT_REGISTRY.get(LVM_VGCREATE_CINDER_VOLUMES)
    assert spec_vgcreate.build_argv({"device": "/dev/sdb"}) == (
        "/usr/sbin/vgcreate",
        "cinder-volumes",
        "/dev/sdb",
    )
    assert spec_vgcreate.mutates is True


def test_endpoint_url_accepts_project_id_macro_and_rejects_other_metacharacters() -> None:
    assert (
        validate_parameter("endpoint_url", "url", CINDER_ENDPOINT_URL)
        == "http://controller:8776/v3/%(project_id)s"
    )
    for bad in (
        "http://controller:8776/v3/%(other)s",
        "http://controller:8776/v3/%s",
        "http://controller:8776/v3/(project_id)",
        "http://controller:8776/v3/%(project_id)s%(project_id)s",
    ):
        with pytest.raises(ParameterError):
            validate_parameter("endpoint_url", "url", bad)


def test_device_path_parameter_validation() -> None:
    for good in ("/dev/sdb", "/dev/vdb", "/dev/nvme0n1", "/dev/xvda"):
        assert validate_parameter("device_path", "device", good) == good
    for bad in ("sdb", "/dev/", "/dev/sdb;rm", "/etc/passwd", "/dev/../etc/passwd"):
        with pytest.raises(ParameterError):
            validate_parameter("device_path", "device", bad)


# --- Helper and file-editing tests -----------------------------------------------------


def test_resolve_block_disk_and_lvm_filter() -> None:
    assert resolve_block_disk(BLOCK_NODE) == "/dev/sdb"
    assert lvm_filter_for_disk("/dev/sdb") == '[ "a/sdb/", "r/.*/"]'
    assert lvm_filter_for_disk("/dev/nvme0n1") == '[ "a/nvme0n1/", "r/.*/"]'
    with pytest.raises(PreconditionError, match="disk"):
        resolve_block_disk({"role": "block", "hostname": "block1"})
    with pytest.raises(PreconditionError, match="disk"):
        resolve_block_disk({"role": "block", "hostname": "block1", "disk": "sdb"})


def test_edit_and_verify_lvm_conf_handles_devices_block_and_conflicting_filter(
    tmp_path: Path,
) -> None:
    lvm_path, _ = resolve_target(tmp_path, LVM_CONF_TARGET)
    lvm_path.parent.mkdir(parents=True, exist_ok=True)
    lvm_path.write_text(UBUNTU_DEFAULT_LVM_CONF, encoding="utf-8")

    assert verify_lvm_conf_text(UBUNTU_DEFAULT_LVM_CONF, "/dev/sdb") != []
    change1 = edit_lvm_conf(tmp_path, "/dev/sdb", dry_run=False)
    assert change1.changed is True
    edited = lvm_path.read_text(encoding="utf-8")
    assert '\tfilter = [ "a/sdb/", "r/.*/"]\n' in edited
    assert verify_lvm_conf_text(edited, "/dev/sdb") == []

    # Second call is a no-op
    change2 = edit_lvm_conf(tmp_path, "/dev/sdb", dry_run=False)
    assert change2.changed is False
    assert lvm_path.read_text(encoding="utf-8") == edited

    # Conflicting active filter inside devices { ... } is commented out and replaced
    conflicting = UBUNTU_DEFAULT_LVM_CONF.replace(
        '\t# filter = [ "a|.*|" ]', '\tfilter = [ "a|.*|" ]'
    )
    assert verify_lvm_conf_text(conflicting, "/dev/sdb") != []
    lvm_path.write_text(conflicting, encoding="utf-8")
    change3 = edit_lvm_conf(tmp_path, "/dev/sdb", dry_run=False)
    assert change3.changed is True
    fixed = lvm_path.read_text(encoding="utf-8")
    assert f'{DISABLED_LINE_PREFIX}\tfilter = [ "a|.*|" ]' in fixed
    assert verify_lvm_conf_text(fixed, "/dev/sdb") == []

    # Missing devices { ... } block raises FileFormatError
    lvm_path.write_text("config {\n}\n", encoding="utf-8")
    with pytest.raises(FileFormatError, match="devices"):
        edit_lvm_conf(tmp_path, "/dev/sdb", dry_run=False)


def test_edit_and_verify_cinder_controller_and_nova_confs(tmp_path: Path) -> None:
    cinder_path, _ = resolve_target(tmp_path, CINDER_CONF_TARGET)
    nova_path, _ = resolve_target(tmp_path, NOVA_CONF_TARGET)
    cinder_path.parent.mkdir(parents=True, exist_ok=True)
    nova_path.parent.mkdir(parents=True, exist_ok=True)
    cinder_path.write_text(UBUNTU_DEFAULT_CINDER_CONF, encoding="utf-8")
    nova_path.write_text(UBUNTU_DEFAULT_NOVA_CONF, encoding="utf-8")

    change_c = edit_cinder_controller_conf(
        tmp_path,
        CONTROLLER_NODE,
        TEST_CINDER_DBPASS,
        TEST_RABBIT_PASS,
        TEST_CINDER_PASS,
        dry_run=False,
    )
    assert change_c.changed is True
    c_text = cinder_path.read_text(encoding="utf-8")
    assert (
        verify_cinder_controller_conf_text(
            c_text,
            CONTROLLER_NODE,
            TEST_CINDER_DBPASS,
            TEST_RABBIT_PASS,
            TEST_CINDER_PASS,
        )
        == []
    )
    assert cinder_db_connection(TEST_CINDER_DBPASS) in c_text
    assert cinder_transport_url(TEST_RABBIT_PASS) in c_text
    assert f"my_ip = {DEV_MACHINE_SANITIZED_IP}" in c_text

    change_n = edit_nova_controller_cinder_conf(tmp_path, dry_run=False)
    assert change_n.changed is True
    n_text = nova_path.read_text(encoding="utf-8")
    assert verify_nova_controller_cinder_conf_text(n_text) == []
    assert "os_region_name = RegionOne" in n_text
    assert len(cinder_controller_conf_settings(CONTROLLER_NODE, "db", "rb", "ci")) == 14
    assert len(nova_controller_cinder_conf_settings()) == 1


def test_edit_and_verify_cinder_storage_and_tgt_confs(tmp_path: Path) -> None:
    cinder_path, _ = resolve_target(tmp_path, CINDER_CONF_TARGET)
    tgt_path, _ = resolve_target(tmp_path, CINDER_TGT_CONF_TARGET)
    cinder_path.parent.mkdir(parents=True, exist_ok=True)
    tgt_path.parent.mkdir(parents=True, exist_ok=True)
    cinder_path.write_text(UBUNTU_DEFAULT_CINDER_CONF, encoding="utf-8")
    tgt_path.write_text(UBUNTU_DEFAULT_TGT_CINDER_CONF, encoding="utf-8")

    change_s = edit_cinder_storage_conf(
        tmp_path,
        BLOCK_NODE,
        TEST_CINDER_DBPASS,
        TEST_RABBIT_PASS,
        TEST_CINDER_PASS,
        dry_run=False,
    )
    assert change_s.changed is True
    s_text = cinder_path.read_text(encoding="utf-8")
    assert (
        verify_cinder_storage_conf_text(
            s_text,
            BLOCK_NODE,
            TEST_CINDER_DBPASS,
            TEST_RABBIT_PASS,
            TEST_CINDER_PASS,
        )
        == []
    )
    assert "volume_driver = cinder.volume.drivers.lvm.LVMVolumeDriver" in s_text
    assert "volume_group = cinder-volumes" in s_text
    assert "target_protocol = iscsi" in s_text
    assert "target_helper = tgtadm" in s_text
    assert "enabled_backends = lvm" in s_text
    assert "glance_api_servers = http://controller:9292" in s_text
    assert len(cinder_storage_conf_settings(BLOCK_NODE, "db", "rb", "ci")) == 20

    # Ubuntu default /etc/tgt/conf.d/cinder.conf already has include /var/lib/cinder/volumes/*
    change_tgt1 = edit_cinder_tgt_conf(tmp_path, dry_run=False)
    assert change_tgt1.changed is False
    assert verify_cinder_tgt_conf_text(UBUNTU_DEFAULT_TGT_CINDER_CONF) == []

    # When include line is missing, edit_cinder_tgt_conf appends managed block
    tgt_path.write_text("# empty tgt conf\n", encoding="utf-8")
    assert verify_cinder_tgt_conf_text("# empty tgt conf\n") != []
    change_tgt2 = edit_cinder_tgt_conf(tmp_path, dry_run=False)
    assert change_tgt2.changed is True
    tgt_updated = tgt_path.read_text(encoding="utf-8")
    assert f"# BEGIN {CINDER_TGT_BLOCK_MARKER}" in tgt_updated
    assert CINDER_TGT_INCLUDE_LINE in tgt_updated
    assert verify_cinder_tgt_conf_text(tgt_updated) == []


def test_parse_lvm_names() -> None:
    assert parse_lvm_names("  /dev/sdb\n  /dev/sdc \n\n") == frozenset({"/dev/sdb", "/dev/sdc"})
    assert parse_lvm_names("") == frozenset()


# --- CinderControllerStep lifecycle tests ----------------------------------------------


def test_cinder_controller_step_rejects_non_controller_role(tmp_path: Path) -> None:
    root = tmp_path / "root"
    host = ServiceHost(root)
    executor = _make_executor(root, host)
    with pytest.raises(ExecutionError, match="controller"):
        make_cinder_controller_step(root, SAMPLE_CONFIG, COMPUTE_NODE, executor=executor)


def test_cinder_controller_dry_run_preview_masks_all_secrets(tmp_path: Path) -> None:
    root = tmp_path / "root"
    _seed_controller_secrets(root)
    for rel, content in _controller_package_creates().values():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    _seed_nova_conf(root)

    host = ServiceHost(root)
    executor = _make_executor(root, host, dry_run=True)
    step = make_cinder_controller_step(root, SAMPLE_CONFIG, CONTROLLER_NODE, executor=executor)
    preview = step.dry_run_preview()
    assert len(preview.file_changes) == 2

    diff_and_notes = "\n".join([*(fc.diff for fc in preview.file_changes), *preview.notes])
    for secret in (
        TEST_ADMIN_PASS,
        TEST_RABBIT_PASS,
        TEST_CINDER_DBPASS,
        TEST_CINDER_PASS,
    ):
        assert secret not in diff_and_notes


def test_cinder_controller_full_lifecycle_and_idempotence(tmp_path: Path) -> None:
    root = tmp_path / "root"
    _seed_controller_secrets(root)
    _seed_nova_conf(root)

    host = ServiceHost(
        root,
        creates=_controller_package_creates(),
        openstack_projects=[{"ID": "proj-service", "Name": "service"}],
    )
    executor = _make_executor(root, host)
    step = build_step(
        CINDER_CONTROLLER_STEP_ID,
        root,
        SAMPLE_CONFIG,
        CONTROLLER_NODE,
        executor=executor,
    )
    assert isinstance(step, CinderControllerStep)
    assert step.check() is False

    state_path = root / DEFAULT_STATE_TARGET.lstrip("/")
    state_path.parent.mkdir(parents=True, exist_ok=True)
    engine = Engine(StateStore(state_path))
    report1 = engine.run([step])
    assert report1.ok
    assert report1.outcomes[0].outcome is Outcome.APPLIED
    assert CINDER_DB_NAME in host.mariadb_databases
    assert host.cinder_db_synced is True
    assert set(CINDER_CONTROLLER_PACKAGES).issubset(host.installed)
    assert set(CINDER_CONTROLLER_UNITS).issubset(host.active)
    assert [
        (ep["Service Name"], ep["Service Type"], ep["Interface"], ep["URL"])
        for ep in host.openstack_endpoints
    ] == [
        (CINDER_SERVICE_NAME, CINDER_SERVICE_TYPE, "public", CINDER_ENDPOINT_URL),
        (CINDER_SERVICE_NAME, CINDER_SERVICE_TYPE, "internal", CINDER_ENDPOINT_URL),
        (CINDER_SERVICE_NAME, CINDER_SERVICE_TYPE, "admin", CINDER_ENDPOINT_URL),
    ]
    assert step.verify().ok is True
    assert step.check() is True

    # Audit log is 0600 and contains zero secrets
    audit_path = root / DEFAULT_AUDIT_TARGET.lstrip("/")
    assert (audit_path.stat().st_mode & 0o777) == AUDIT_FILE_MODE
    audit_text = audit_path.read_text(encoding="utf-8")
    for secret in (
        TEST_ADMIN_PASS,
        TEST_RABBIT_PASS,
        TEST_CINDER_DBPASS,
        TEST_CINDER_PASS,
    ):
        assert secret not in audit_text

    # Re-running via Engine is idempotent (SKIPPED, zero mutating calls)
    calls_before = len(host.calls)
    report2 = engine.run([step])
    assert report2.ok
    assert report2.outcomes[0].outcome is Outcome.SKIPPED
    for call in host.calls[calls_before:]:
        assert call[0] in {
            "/usr/bin/dpkg-query",
            "/usr/bin/systemctl",
            "/usr/bin/mariadb",
            "/usr/bin/openstack",
        }


def test_cinder_controller_fails_when_nova_conf_missing(tmp_path: Path) -> None:
    root = tmp_path / "root"
    _seed_controller_secrets(root)
    host = ServiceHost(
        root,
        creates=_controller_package_creates(),
        openstack_projects=[{"ID": "proj-service", "Name": "service"}],
    )
    executor = _make_executor(root, host)
    step = make_cinder_controller_step(root, SAMPLE_CONFIG, CONTROLLER_NODE, executor=executor)
    with pytest.raises(PreconditionError, match="/etc/nova/nova.conf"):
        step.apply()


# --- CinderStorageStep lifecycle tests -------------------------------------------------


def test_cinder_storage_step_rejects_non_block_role(tmp_path: Path) -> None:
    root = tmp_path / "root"
    host = ServiceHost(root)
    executor = _make_executor(root, host)
    with pytest.raises(ExecutionError, match="block"):
        make_cinder_storage_step(root, SAMPLE_CONFIG, CONTROLLER_NODE, executor=executor)


def test_cinder_storage_least_privilege_secrets_and_full_lifecycle(tmp_path: Path) -> None:
    root = tmp_path / "root"
    # Seed ONLY RABBIT_PASS, CINDER_DBPASS, and CINDER_PASS (no ADMIN_PASS on block node):
    _seed_storage_secrets_only(root)

    host = ServiceHost(
        root,
        creates=_storage_package_creates(),
    )
    executor = _make_executor(root, host)
    step = build_step(
        CINDER_STORAGE_STEP_ID,
        root,
        SAMPLE_CONFIG,
        BLOCK_NODE,
        executor=executor,
    )
    assert isinstance(step, CinderStorageStep)
    assert step.check() is False

    state_path = root / DEFAULT_STATE_TARGET.lstrip("/")
    state_path.parent.mkdir(parents=True, exist_ok=True)
    engine = Engine(StateStore(state_path))
    report1 = engine.run([step])
    assert report1.ok
    assert report1.outcomes[0].outcome is Outcome.APPLIED
    assert set(CINDER_STORAGE_PACKAGES).issubset(host.installed)
    assert set(CINDER_STORAGE_UNITS).issubset(host.active)
    assert host.lvm_pvs == {"/dev/sdb"}
    assert host.lvm_vgs == {CINDER_VG_NAME}
    assert step.verify().ok is True
    assert step.check() is True

    # Re-running via Engine is idempotent (SKIPPED, does not re-run pvcreate or vgcreate)
    calls_before = len(host.calls)
    report2 = engine.run([step])
    assert report2.ok
    assert report2.outcomes[0].outcome is Outcome.SKIPPED
    for call in host.calls[calls_before:]:
        assert call[0] not in {"/usr/sbin/pvcreate", "/usr/sbin/vgcreate"}

    # Even calling ensure_lvm_pv_and_vg directly when PV and VG exist makes zero mutating calls
    calls_before_lvm = len(host.calls)
    ensure_lvm_pv_and_vg(executor, "/dev/sdb")
    for call in host.calls[calls_before_lvm:]:
        assert call[0] in {"/usr/sbin/pvs", "/usr/sbin/vgs"}

    # Drift detection: removing VG or PV causes check() and verify() to fail
    host.lvm_vgs.clear()
    assert lvm_pv_and_vg_ready(executor, "/dev/sdb") is False
    assert step.check() is False
    v_vg = step.verify()
    assert v_vg.ok is False
    assert "LVM volume group 'cinder-volumes' does not exist" in v_vg.details

    host.lvm_pvs.clear()
    probs = lvm_pv_and_vg_problems(executor, "/dev/sdb")
    assert len(probs) == 2


def test_cinder_storage_dry_run_preview_masks_all_secrets(tmp_path: Path) -> None:
    root = tmp_path / "root"
    _seed_storage_secrets_only(root)
    for rel, content in _storage_package_creates().values():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")

    host = ServiceHost(root)
    executor = _make_executor(root, host, dry_run=True)
    step = make_cinder_storage_step(root, SAMPLE_CONFIG, BLOCK_NODE, executor=executor)
    preview = step.dry_run_preview()
    assert len(preview.file_changes) == 3

    diff_and_notes = "\n".join([*(fc.diff for fc in preview.file_changes), *preview.notes])
    for secret in (TEST_RABBIT_PASS, TEST_CINDER_DBPASS, TEST_CINDER_PASS):
        assert secret not in diff_and_notes


def test_cinder_steps_fail_when_secrets_missing(tmp_path: Path) -> None:
    root = tmp_path / "root"
    _seed_nova_conf(root)
    host = ServiceHost(root)
    executor = _make_executor(root, host)

    ctrl_step = make_cinder_controller_step(root, SAMPLE_CONFIG, CONTROLLER_NODE, executor=executor)
    assert ctrl_step.check() is False
    with pytest.raises(PreconditionError, match=DEFAULT_SECRETS_TARGET):
        ctrl_step.apply()

    stor_step = make_cinder_storage_step(root, SAMPLE_CONFIG, BLOCK_NODE, executor=executor)
    assert stor_step.check() is False
    with pytest.raises(PreconditionError, match=DEFAULT_SECRETS_TARGET):
        stor_step.apply()


def test_cinder_storage_refuses_when_root_filesystem_on_lvm(tmp_path: Path) -> None:
    root = tmp_path / "root"
    _seed_storage_secrets_only(root)
    proc_mounts = root / "proc/mounts"
    proc_mounts.parent.mkdir(parents=True, exist_ok=True)
    proc_mounts.write_text(
        "/dev/mapper/ubuntu--vg-ubuntu--lv / ext4 rw,relatime 0 0\n"
        "proc /proc proc rw,nosuid,nodev,noexec,relatime 0 0\n",
        encoding="utf-8",
    )

    host = ServiceHost(root, creates=_storage_package_creates())
    executor = _make_executor(root, host)
    step = make_cinder_storage_step(root, SAMPLE_CONFIG, BLOCK_NODE, executor=executor)

    with pytest.raises(PreconditionError) as exc_info:
        step.apply()
    msg = str(exc_info.value)
    assert "/dev/mapper/ubuntu--vg-ubuntu--lv" in msg
    assert CINDER_STORAGE_INSTALL_DOC_URL in msg
    assert CINDER_STORAGE_LVM_FILTER_WARNING in msg
    assert ALLOW_LVM_ROOT_FILTER_OVERRIDE_KEY in msg
    assert host.calls == []

    assert step.check() is False
    v_res = step.verify()
    assert v_res.ok is False
    assert ALLOW_LVM_ROOT_FILTER_OVERRIDE_KEY in v_res.details

    preview = step.dry_run_preview()
    assert any(ALLOW_LVM_ROOT_FILTER_OVERRIDE_KEY in note for note in preview.notes)

    # Explicit opt-in override permits apply(), check(), and verify()
    override_node = {**BLOCK_NODE, ALLOW_LVM_ROOT_FILTER_OVERRIDE_KEY: True}
    override_step = make_cinder_storage_step(root, SAMPLE_CONFIG, override_node, executor=executor)
    override_step.apply()
    assert override_step.check() is True
    assert override_step.verify().ok is True


def test_check_cinder_lvm_root_safety_fstab_and_non_lvm_root(tmp_path: Path) -> None:
    root = tmp_path / "root"
    fstab = root / "etc/fstab"
    fstab.parent.mkdir(parents=True, exist_ok=True)

    # Standard partition (/dev/sda1) is allowed
    fstab.write_text("/dev/sda1 / ext4 defaults 0 1\n", encoding="utf-8")
    check_cinder_lvm_root_safety(root, BLOCK_NODE, SAMPLE_CONFIG)

    # LVM logical volume (/dev/ubuntu-vg/ubuntu-lv) in /etc/fstab is refused
    fstab.write_text("/dev/ubuntu-vg/ubuntu-lv / ext4 defaults 0 1\n", encoding="utf-8")
    with pytest.raises(PreconditionError, match="ubuntu-vg/ubuntu-lv"):
        check_cinder_lvm_root_safety(root, BLOCK_NODE, SAMPLE_CONFIG)

    # Schema-valid node.tags list containing ALLOW_LVM_ROOT_FILTER_OVERRIDE_KEY permits it
    tagged_node = {**BLOCK_NODE, "tags": [ALLOW_LVM_ROOT_FILTER_OVERRIDE_KEY]}
    check_cinder_lvm_root_safety(root, tagged_node, SAMPLE_CONFIG)
