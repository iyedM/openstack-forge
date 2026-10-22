"""Unit tests for the ``nova-controller`` and ``nova-compute`` steps."""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from pathlib import Path

import pytest

from openstack_forge.cli import main
from openstack_forge.execution import (
    APT_GET_INSTALL,
    DEFAULT_REGISTRY,
    DPKG_QUERY_STATUS,
    MARIADB_EXECUTE_SQL,
    MARIADB_QUERY_SQL,
    MASK,
    NOVA_ADMIN_SECRET_NAME,
    NOVA_CELL0_DB_NAME,
    NOVA_COMPUTE_CONF_TARGET,
    NOVA_COMPUTE_PACKAGE,
    NOVA_COMPUTE_STEP_ID,
    NOVA_COMPUTE_UNIT,
    NOVA_CONF_TARGET,
    NOVA_CONTROLLER_PACKAGES,
    NOVA_CONTROLLER_STEP_ID,
    NOVA_DB_NAMES,
    NOVA_DB_SECRET_NAME,
    NOVA_DB_USER,
    NOVA_LOCK_PATH,
    NOVA_MANAGE_API_DB_SYNC,
    NOVA_MANAGE_CREATE_CELL1,
    NOVA_MANAGE_DB_SYNC,
    NOVA_MANAGE_FAILURE_HINT,
    NOVA_MANAGE_LIST_CELLS,
    NOVA_MANAGE_MAP_CELL0,
    NOVA_NOVNCPROXY_BASE_URL,
    NOVA_PLACEMENT_SECRET_NAME,
    NOVA_RABBIT_SECRET_NAME,
    NOVA_SERVICE_NAME,
    NOVA_SERVICE_SECRET_NAME,
    NOVA_SERVICE_USER,
    OPENSTACK_ENDPOINT_CREATE,
    OPENSTACK_ENDPOINT_LIST,
    OPENSTACK_ROLE_ADD_USER_PROJECT,
    OPENSTACK_ROLE_ASSIGNMENT_LIST,
    OPENSTACK_SERVICE_CREATE,
    OPENSTACK_SERVICE_LIST,
    OPENSTACK_USER_CREATE,
    OPENSTACK_USER_LIST,
    SYSTEMCTL_IS_ACTIVE,
    SYSTEMCTL_RESTART,
    AuditLog,
    CommandExecutor,
    Engine,
    ExecutionError,
    NovaComputeStep,
    NovaControllerStep,
    Outcome,
    PreconditionError,
    ProcessOutput,
    SecretStore,
    StateStore,
    build_nova_compute_ini_entries,
    build_nova_controller_ini_entries,
    build_step,
    cell_exists_in_list_output,
    clear_registered_secrets,
    detect_libvirt_virt_type,
    detect_node_libvirt_virt_type,
    edit_nova_compute_conf,
    edit_nova_controller_conf,
    format_report,
    make_nova_compute_step,
    make_nova_controller_step,
    nova_api_db_connection,
    nova_compute_conf_settings,
    nova_controller_conf_settings,
    nova_db_connection,
    nova_transport_url,
    parse_cell_names,
    verify_nova_compute_conf_text,
    verify_nova_compute_hypervisor_conf_text,
    verify_nova_controller_conf_text,
)
from openstack_forge.orchestration import load_catalog
from tests.unit._service_fakes import ServiceHost

REPO_ROOT = Path(__file__).resolve().parents[2]
EXAMPLE_CONFIG = REPO_ROOT / "examples" / "cluster.example.yaml"

CONTROLLER = {
    "hostname": "ctl-01",
    "fqdn": "ctl-01.example.internal",
    "role": "controller",
    "management_ip": "192.0.2.11",
}
COMPUTE = {
    "hostname": "cmp-01",
    "fqdn": "cmp-01.example.internal",
    "role": "compute",
    "management_ip": "192.0.2.21",
}

TEST_ADMIN_PASS = "AdminSecretPass1234567890ABCDEFGH"
TEST_RABBIT_PASS = "RabbitSecretPass1234567890ABCDEFG"
TEST_NOVA_DBPASS = "NovaDbSecretPass1234567890ABCDEFG"
TEST_NOVA_PASS = "NovaServicePass1234567890ABCDEFGH"
TEST_PLACEMENT_PASS = "PlacementSvcPass1234567890ABCDEFG"

NOVA_CONF_REL = NOVA_CONF_TARGET.lstrip("/")
NOVA_COMPUTE_CONF_REL = NOVA_COMPUTE_CONF_TARGET.lstrip("/")

# Realistic excerpt of /etc/nova/nova.conf from Ubuntu 24.04 nova-common
# (3:31.0.0-0ubuntu1.2~cloud1, conffile MD5 9127d52a0e8b6812f4159720ef1eed7a).
DEFAULT_NOVA_CONF = (
    "[DEFAULT]\n"
    "#my_ip = <host_ipv4>\n"
    "#transport_url = <None>\n"
    "\n"
    "[api]\n"
    "#auth_strategy = keystone\n"
    "\n"
    "[api_database]\n"
    "#connection = <None>\n"
    "connection = sqlite:////var/lib/nova/nova-api.sqlite\n"
    "\n"
    "[database]\n"
    "#connection = <None>\n"
    "connection = sqlite:////var/lib/nova/nova.sqlite\n"
    "\n"
    "[glance]\n"
    "#api_servers = <None>\n"
    "\n"
    "[keystone_authtoken]\n"
    "#www_authenticate_uri = <None>\n"
    "#auth_url = <None>\n"
    "#memcached_servers = <None>\n"
    "#auth_type = <None>\n"
    "#project_domain_name = Default\n"
    "#user_domain_name = Default\n"
    "#project_name = <None>\n"
    "#username = <None>\n"
    "#password = <None>\n"
    "\n"
    "[oslo_concurrency]\n"
    "#lock_path = /var/lib/nova/tmp\n"
    "\n"
    "[placement]\n"
    "#region_name = <None>\n"
    "#project_domain_name = Default\n"
    "#project_name = <None>\n"
    "#auth_type = <None>\n"
    "#user_domain_name = Default\n"
    "#auth_url = <None>\n"
    "#username = <None>\n"
    "#password = <None>\n"
    "\n"
    "[scheduler]\n"
    "#discover_hosts_in_cells_interval = -1\n"
    "\n"
    "[service_user]\n"
    "#send_service_user_token = false\n"
    "\n"
    "[vnc]\n"
    "#enabled = true\n"
    "#server_listen = 127.0.0.1\n"
    "#server_proxyclient_address = 127.0.0.1\n"
    "#novncproxy_base_url = http://127.0.0.1:6080/vnc_auto.html\n"
)

DEFAULT_NOVA_COMPUTE_CONF = (
    "[DEFAULT]\ncompute_driver=libvirt.LibvirtDriver\n[libvirt]\nvirt_type=kvm\n"
)


@pytest.fixture(autouse=True)
def _clean_redaction() -> Iterator[None]:
    clear_registered_secrets()
    yield
    clear_registered_secrets()


def _seed_nova_controller_secrets(
    root: Path,
    *,
    admin_pass: str = TEST_ADMIN_PASS,
    rabbit_pass: str = TEST_RABBIT_PASS,
    db_pass: str = TEST_NOVA_DBPASS,
    nova_pass: str = TEST_NOVA_PASS,
    placement_pass: str = TEST_PLACEMENT_PASS,
) -> SecretStore:
    secrets_dir = root / "etc" / "openstack-forge"
    secrets_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(secrets_dir, 0o700)
    secrets_file = secrets_dir / "secrets.json"
    secrets_file.write_text(
        json.dumps(
            {
                NOVA_ADMIN_SECRET_NAME: admin_pass,
                NOVA_RABBIT_SECRET_NAME: rabbit_pass,
                NOVA_DB_SECRET_NAME: db_pass,
                NOVA_SERVICE_SECRET_NAME: nova_pass,
                NOVA_PLACEMENT_SECRET_NAME: placement_pass,
            }
        ),
        encoding="utf-8",
    )
    os.chmod(secrets_file, 0o600)
    store = SecretStore(root)
    store.ensure()
    return store


def _seed_nova_compute_only_secrets(
    root: Path,
    *,
    rabbit_pass: str = TEST_RABBIT_PASS,
    nova_pass: str = TEST_NOVA_PASS,
    placement_pass: str = TEST_PLACEMENT_PASS,
) -> SecretStore:
    """Write ONLY the 3 compute-node secrets (no ADMIN_PASS, no NOVA_DBPASS)."""
    secrets_dir = root / "etc" / "openstack-forge"
    secrets_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(secrets_dir, 0o700)
    secrets_file = secrets_dir / "secrets.json"
    secrets_file.write_text(
        json.dumps(
            {
                NOVA_RABBIT_SECRET_NAME: rabbit_pass,
                NOVA_SERVICE_SECRET_NAME: nova_pass,
                NOVA_PLACEMENT_SECRET_NAME: placement_pass,
            }
        ),
        encoding="utf-8",
    )
    os.chmod(secrets_file, 0o600)
    return SecretStore(root)


def _make_controller_host(root: Path, **kwargs: object) -> ServiceHost:
    kwargs.setdefault(
        "creates",
        {
            "nova-api": (NOVA_CONF_REL, DEFAULT_NOVA_CONF),
        },
    )
    kwargs.setdefault("starts", set(NOVA_CONTROLLER_PACKAGES))
    kwargs.setdefault("watch", NOVA_CONF_REL)
    kwargs.setdefault(
        "openstack_projects",
        ({"ID": "proj-admin", "Name": "admin"}, {"ID": "proj-service", "Name": "service"}),
    )
    return ServiceHost(root, **kwargs)  # type: ignore[arg-type]


def _make_compute_host(root: Path, **kwargs: object) -> ServiceHost:
    kwargs.setdefault(
        "creates",
        {
            NOVA_COMPUTE_PACKAGE: (
                (NOVA_CONF_REL, DEFAULT_NOVA_CONF),
                (NOVA_COMPUTE_CONF_REL, DEFAULT_NOVA_COMPUTE_CONF),
            ),
        },
    )
    kwargs.setdefault("starts", {NOVA_COMPUTE_PACKAGE})
    kwargs.setdefault("watch_files", (NOVA_CONF_REL, NOVA_COMPUTE_CONF_REL))
    return ServiceHost(root, **kwargs)  # type: ignore[arg-type]


def _make_executor(
    tmp_path: Path, host: ServiceHost, monkeypatch: pytest.MonkeyPatch
) -> CommandExecutor:
    monkeypatch.setattr("openstack_forge.execution.commands.is_host_root", lambda _: True)
    (tmp_path / "audit").mkdir(exist_ok=True)
    return CommandExecutor(
        DEFAULT_REGISTRY,
        root=tmp_path / "root",
        audit_log=AuditLog(tmp_path / "audit/commands.log"),
        backend=host,
        echo=lambda _: None,
        euid=lambda: 0,
    )


# --- Pure helpers: detect_libvirt_virt_type & cell_exists_in_list_output ------------


@pytest.mark.parametrize(
    ("cpuinfo", "expected"),
    [
        ("processor\t: 0\nflags\t\t: fpu vme de pse vmx msr\n", "kvm"),
        ("processor\t: 0\nflags\t\t: fpu vme de pse svm msr\n", "kvm"),
        ("processor\t: 0\nFeatures\t: fp asimd evtstrm vmx\n", "kvm"),
        ("processor\t: 0\nflags\t\t: fpu vme de pse msr pae\n", "qemu"),
        ("processor\t: 0\nmodel name\t: Fake vmx CPU without flag\nflags\t\t: fpu sse2\n", "qemu"),
        ("processor\t: 0\nflags\t\t: fpu vmx_fake svm_extra\n", "qemu"),
        ("", "qemu"),
    ],
)
def test_detect_libvirt_virt_type_pure_helper(cpuinfo: str, expected: str) -> None:
    assert detect_libvirt_virt_type(cpuinfo) == expected


def test_cell_exists_in_list_output_pure_helper() -> None:
    border = (
        "+-------+--------------------------------------+"
        "----------------------------------------------------+"
        "--------------------------------------------------------------+----------+\n"
    )
    table = (
        border + "|  Name |                 UUID                 |"
        "                   Transport URL                    |"
        "                     Database Connection                      | Disabled |\n"
        + border
        + "| cell0 | 00000000-0000-0000-0000-000000000000 |"
        "                       none:/                       |"
        " mysql+pymysql://nova:****@controller/nova_cell0?charset=utf8 |  False   |\n"
        "| cell1 | f690f4fd-2bc5-4f15-8145-db561a7b9d3d |"
        " rabbit://openstack:****@controller:5672/           |"
        " mysql+pymysql://nova:****@controller/nova?charset=utf8       |  False   |\n" + border
    )
    assert parse_cell_names(table) == frozenset({"cell0", "cell1"})
    assert cell_exists_in_list_output(table, "cell0") is True
    assert cell_exists_in_list_output(table, "cell1") is True
    assert cell_exists_in_list_output(table, "cell2") is False
    assert cell_exists_in_list_output("", "cell1") is False

    plain = "Name UUID\ncell0 00000000-0000-0000-0000-000000000000\n"
    assert cell_exists_in_list_output(plain, "cell0") is True
    assert cell_exists_in_list_output(plain, "cell1") is False


# --- Catalog entries and role restrictions ------------------------------------------


def test_nova_catalog_entries_and_role_restrictions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    catalog = load_catalog()
    ctl_step = catalog.step(NOVA_CONTROLLER_STEP_ID)
    assert ctl_step.roles == ("controller",)
    assert ctl_step.depends_on == (
        "mariadb",
        "rabbitmq",
        "memcached",
        "keystone",
        "glance",
        "placement",
    )
    assert ctl_step.files_modified == (NOVA_CONF_TARGET,)
    assert (
        ctl_step.doc_url
        == "https://docs.openstack.org/nova/2025.1/install/controller-install-ubuntu.html"
    )
    assert ctl_step.status == "unverified"

    cmp_step = catalog.step(NOVA_COMPUTE_STEP_ID)
    assert cmp_step.roles == ("compute",)
    assert cmp_step.depends_on == ("openstack-packages", "nova-controller")
    assert cmp_step.files_modified == (NOVA_CONF_TARGET, NOVA_COMPUTE_CONF_TARGET)
    assert (
        cmp_step.doc_url
        == "https://docs.openstack.org/nova/2025.1/install/compute-install-ubuntu.html"
    )
    assert cmp_step.status == "unverified"

    root = tmp_path / "root"
    root.mkdir()
    host = _make_controller_host(root)
    executor = _make_executor(tmp_path, host, monkeypatch)

    for bad_role in ("compute", "block", "object"):
        with pytest.raises(ExecutionError, match="controller"):
            make_nova_controller_step(
                root,
                {},
                {"hostname": "node-01", "role": bad_role, "management_ip": "192.0.2.21"},
                executor=executor,
            )

    for bad_role in ("controller", "block", "object"):
        with pytest.raises(ExecutionError, match="compute"):
            make_nova_compute_step(
                root,
                {},
                {"hostname": "node-01", "role": bad_role, "management_ip": "192.0.2.11"},
                executor=executor,
            )


# --- Controller INI configuration helpers & secret redaction ------------------------


def test_edit_and_verify_nova_controller_conf_redacts_secrets_and_is_idempotent(
    tmp_path: Path,
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    _seed_nova_controller_secrets(root)

    with pytest.raises(PreconditionError, match="nova-api is installed"):
        edit_nova_controller_conf(
            root,
            CONTROLLER,
            TEST_NOVA_DBPASS,
            TEST_RABBIT_PASS,
            TEST_NOVA_PASS,
            TEST_PLACEMENT_PASS,
            dry_run=False,
        )

    conf_path = root / NOVA_CONF_REL
    conf_path.parent.mkdir(parents=True, exist_ok=True)
    conf_path.write_text(DEFAULT_NOVA_CONF, encoding="utf-8")

    entries = build_nova_controller_ini_entries(
        CONTROLLER,
        TEST_NOVA_DBPASS,
        TEST_RABBIT_PASS,
        TEST_NOVA_PASS,
        TEST_PLACEMENT_PASS,
    )
    assert len(entries) == 37
    assert entries == nova_controller_conf_settings(
        CONTROLLER,
        TEST_NOVA_DBPASS,
        TEST_RABBIT_PASS,
        TEST_NOVA_PASS,
        TEST_PLACEMENT_PASS,
    )

    preview = edit_nova_controller_conf(
        root,
        CONTROLLER,
        TEST_NOVA_DBPASS,
        TEST_RABBIT_PASS,
        TEST_NOVA_PASS,
        TEST_PLACEMENT_PASS,
        dry_run=True,
    )
    assert preview.changed is True
    for secret in (TEST_NOVA_DBPASS, TEST_RABBIT_PASS, TEST_NOVA_PASS, TEST_PLACEMENT_PASS):
        assert secret not in preview.diff
    assert MASK in preview.diff

    change = edit_nova_controller_conf(
        root,
        CONTROLLER,
        TEST_NOVA_DBPASS,
        TEST_RABBIT_PASS,
        TEST_NOVA_PASS,
        TEST_PLACEMENT_PASS,
        dry_run=False,
    )
    assert change.changed is True
    for secret in (TEST_NOVA_DBPASS, TEST_RABBIT_PASS, TEST_NOVA_PASS, TEST_PLACEMENT_PASS):
        assert secret not in change.diff
    assert (root / f"{NOVA_CONF_REL}.orig").exists()

    written = conf_path.read_text(encoding="utf-8")
    assert nova_api_db_connection(TEST_NOVA_DBPASS) in written
    assert nova_db_connection(TEST_NOVA_DBPASS) in written
    assert "sqlite" not in written
    assert nova_transport_url(TEST_RABBIT_PASS) in written
    assert "my_ip = 192.0.2.11" in written
    assert "auth_strategy = keystone" in written
    assert "www_authenticate_uri = http://controller:5000/" in written
    assert "auth_url = http://controller:5000/" in written
    assert "memcached_servers = controller:11211" in written
    assert "send_service_user_token = true" in written
    assert "server_listen = $my_ip" in written
    assert "server_proxyclient_address = $my_ip" in written
    assert "api_servers = http://controller:9292" in written
    assert f"lock_path = {NOVA_LOCK_PATH}" in written
    assert "auth_url = http://controller:5000/v3" in written
    assert f"password = {TEST_NOVA_PASS}" in written
    assert f"password = {TEST_PLACEMENT_PASS}" in written
    assert "discover_hosts_in_cells_interval = 300" in written
    assert (
        verify_nova_controller_conf_text(
            written,
            CONTROLLER,
            TEST_NOVA_DBPASS,
            TEST_RABBIT_PASS,
            TEST_NOVA_PASS,
            TEST_PLACEMENT_PASS,
        )
        == []
    )

    again = edit_nova_controller_conf(
        root,
        CONTROLLER,
        TEST_NOVA_DBPASS,
        TEST_RABBIT_PASS,
        TEST_NOVA_PASS,
        TEST_PLACEMENT_PASS,
        dry_run=False,
    )
    assert again.changed is False
    assert again.diff == ""


# --- NovaControllerStep preconditions, full apply, idempotence, and failure branches -


@pytest.mark.parametrize(
    "missing_secret",
    [
        NOVA_ADMIN_SECRET_NAME,
        NOVA_RABBIT_SECRET_NAME,
        NOVA_DB_SECRET_NAME,
        NOVA_SERVICE_SECRET_NAME,
        NOVA_PLACEMENT_SECRET_NAME,
    ],
)
def test_nova_controller_fails_when_required_secret_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, missing_secret: str
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    store = _seed_nova_controller_secrets(root)
    data = json.loads(store.path.read_text(encoding="utf-8"))
    del data[missing_secret]
    store.path.write_text(json.dumps(data), encoding="utf-8")
    os.chmod(store.path, 0o600)

    host = _make_controller_host(root)
    executor = _make_executor(tmp_path, host, monkeypatch)
    step = NovaControllerStep(root=root, config={}, local_node=CONTROLLER, executor=executor)

    assert step.check() is False
    with pytest.raises(PreconditionError, match=missing_secret):
        step.apply()


def test_nova_controller_fresh_apply_orders_edits_cells_and_restarts_and_is_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    _seed_nova_controller_secrets(root)
    host = _make_controller_host(root)
    executor = _make_executor(tmp_path, host, monkeypatch)
    step = build_step(NOVA_CONTROLLER_STEP_ID, root, {}, CONTROLLER, executor=executor)

    state = StateStore(root / "var/lib/openstack-forge/state.json")
    (root / "var/lib/openstack-forge").mkdir(parents=True, exist_ok=True)
    engine = Engine(state)

    report = engine.run([step])
    assert report.ok is True
    assert report.outcomes[0].outcome is Outcome.APPLIED

    # Verify all 3 MariaDB databases (nova_api, nova, nova_cell0) and grants
    assert set(NOVA_DB_NAMES).issubset(host.mariadb_databases)
    for db_name in NOVA_DB_NAMES:
        assert (NOVA_DB_USER, "localhost", db_name) in host.mariadb_grants
        assert (NOVA_DB_USER, "%", db_name) in host.mariadb_grants
    assert host.mariadb_passwords[(NOVA_DB_USER, "localhost")] == TEST_NOVA_DBPASS
    assert host.mariadb_passwords[(NOVA_DB_USER, "%")] == TEST_NOVA_DBPASS

    # Verify Keystone user, role, service, and 3 endpoints (http://controller:8774/v2.1)
    assert any(u["Name"] == NOVA_SERVICE_USER for u in host.openstack_users)
    assert host.openstack_user_passwords[NOVA_SERVICE_USER] == TEST_NOVA_PASS
    assert {
        "Role": "admin",
        "User": f"{NOVA_SERVICE_USER}@Default",
        "Group": "",
        "Project": "service@Default",
        "Domain": "",
        "System": "",
        "Inherited": False,
    } in host.openstack_role_assignments
    assert len([s for s in host.openstack_services if s["Name"] == NOVA_SERVICE_NAME]) == 1
    nova_eps = [e for e in host.openstack_endpoints if e["Service Name"] == NOVA_SERVICE_NAME]
    assert len(nova_eps) == 3
    assert {e["Interface"] for e in nova_eps} == {"public", "internal", "admin"}
    assert {e["URL"] for e in nova_eps} == {"http://controller:8774/v2.1"}
    assert {e["Service Type"] for e in nova_eps} == {"compute"}

    # Verify nova-manage sequence and cell0/cell1 registration
    assert host.nova_api_db_synced is True
    assert host.nova_db_synced is True
    assert host.nova_cells == {"cell0", "cell1"}

    # Verify nova.conf was already edited BEFORE api_db sync, map_cell0, create_cell, db sync,
    # and all 4 service restarts
    assert len(host.snapshots) == 8
    for _, snap_text in host.snapshots:
        assert snap_text is not None
        assert nova_api_db_connection(TEST_NOVA_DBPASS) in snap_text
        assert nova_db_connection(TEST_NOVA_DBPASS) in snap_text
        assert nova_transport_url(TEST_RABBIT_PASS) in snap_text

    # Verify exact service restart order: nova-api, nova-scheduler, nova-conductor, nova-novncproxy
    restart_units = [
        argv[2] for argv in host.calls if argv[:2] == ("/usr/bin/systemctl", "restart")
    ]
    assert restart_units == [
        "nova-api",
        "nova-scheduler",
        "nova-conductor",
        "nova-novncproxy",
    ]

    # Second Engine.run skips the step
    calls_before_second_run = len(host.calls)
    report2 = engine.run([step])
    assert report2.ok is True
    assert report2.outcomes[0].outcome is Outcome.SKIPPED
    for argv in host.calls[calls_before_second_run:]:
        assert "create_cell" not in argv
        assert "install" not in argv
        assert "restart" not in argv

    # Direct second step.apply() checks list_cells and does NOT re-run create_cell
    # (ServiceHost would exit with code 2 if create_cell were re-run when cell1 exists!)
    calls_before_reapply = len(host.calls)
    step.apply()
    reapply_calls = host.calls[calls_before_reapply:]
    assert ("/usr/bin/nova-manage", "cell_v2", "list_cells") in reapply_calls
    assert (
        "/usr/bin/nova-manage",
        "cell_v2",
        "create_cell",
        "--name=cell1",
        "--verbose",
    ) not in reapply_calls
    assert ("/usr/bin/nova-manage", "api_db", "sync") in reapply_calls
    assert ("/usr/bin/nova-manage", "cell_v2", "map_cell0") in reapply_calls
    assert ("/usr/bin/nova-manage", "db", "sync") in reapply_calls


def test_nova_controller_check_and_verify_failure_branches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    _seed_nova_controller_secrets(root)
    host = _make_controller_host(root)
    executor = _make_executor(tmp_path, host, monkeypatch)
    step = NovaControllerStep(root=root, config={}, local_node=CONTROLLER, executor=executor)

    step.apply()
    assert step.check() is True
    ver_ok = step.verify()
    assert ver_ok.ok is True
    assert "Warning: MariaDB schema completeness" in ver_ok.details
    assert "not that 'nova-manage db sync' completed every migration" in ver_ok.details

    # 1. Config drift in /etc/nova/nova.conf -> check() is False with zero external commands
    conf_path = root / NOVA_CONF_REL
    good_conf = conf_path.read_text(encoding="utf-8")
    conf_path.write_text(
        good_conf.replace(
            "discover_hosts_in_cells_interval = 300",
            "discover_hosts_in_cells_interval = -1",
        ),
        encoding="utf-8",
    )
    calls_before = len(host.calls)
    assert step.check() is False
    assert len(host.calls) == calls_before
    ver = step.verify()
    assert ver.ok is False
    assert "discover_hosts_in_cells_interval is '-1'" in ver.details
    conf_path.write_text(good_conf, encoding="utf-8")

    # 2. One of the 4 packages missing
    host.installed.remove("nova-scheduler")
    assert step.check() is False
    ver = step.verify()
    assert ver.ok is False
    assert "package 'nova-scheduler' is not installed" in ver.details
    host.installed.add("nova-scheduler")

    # 3. One of the 4 units inactive
    host.active.remove("nova-novncproxy")
    assert step.check() is False
    ver = step.verify()
    assert ver.ok is False
    assert "nova-novncproxy" in ver.details
    host.active.add("nova-novncproxy")

    # 4. Missing nova_cell0 database
    host.mariadb_databases.remove(NOVA_CELL0_DB_NAME)
    assert step.check() is False
    ver = step.verify()
    assert ver.ok is False
    assert "MariaDB database 'nova_cell0' does not exist" in ver.details
    host.mariadb_databases.add(NOVA_CELL0_DB_NAME)

    # 5. Missing cell1 in nova-manage cell_v2 list_cells
    host.nova_cells.remove("cell1")
    assert step.check() is False
    ver = step.verify()
    assert ver.ok is False
    assert "Nova cell 'cell1' is not registered" in ver.details
    host.nova_cells.add("cell1")

    # 6. Symlink on /etc/nova/nova.conf
    other = root / "etc" / "other.conf"
    other.write_text("", encoding="utf-8")
    conf_path.unlink()
    conf_path.symlink_to(other)
    assert step.check() is False
    ver = step.verify()
    assert ver.ok is False
    assert "symlink" in ver.details


def test_nova_controller_command_failure_attaches_nova_manage_hint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    _seed_nova_controller_secrets(root)
    host = _make_controller_host(root)
    host.failures[("/usr/bin/nova-manage", "api_db", "sync")] = ProcessOutput(
        1, "", "CRITICAL nova [-] DBConnectionError\n"
    )
    executor = _make_executor(tmp_path, host, monkeypatch)
    step = NovaControllerStep(root=root, config={}, local_node=CONTROLLER, executor=executor)
    (root / "var/lib/openstack-forge").mkdir(parents=True, exist_ok=True)
    report = Engine(StateStore(root / "var/lib/openstack-forge/state.json")).run([step])
    assert report.ok is False
    assert NOVA_MANAGE_FAILURE_HINT in format_report(report)


def test_nova_controller_dry_run_preview_and_cli_apply_mask_all_secrets(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    host = _make_controller_host(root)
    (tmp_path / "audit").mkdir(exist_ok=True)
    executor = CommandExecutor(
        DEFAULT_REGISTRY,
        root=root,
        audit_log=AuditLog(tmp_path / "audit/commands.log"),
        backend=host,
        dry_run=True,
    )
    step = NovaControllerStep(root=root, config={}, local_node=CONTROLLER, executor=executor)

    preview_before = step.dry_run_preview()
    assert [c.command_id for c in preview_before.commands] == [
        MARIADB_EXECUTE_SQL,
        OPENSTACK_USER_CREATE,
        OPENSTACK_ROLE_ADD_USER_PROJECT,
        OPENSTACK_SERVICE_CREATE,
        OPENSTACK_ENDPOINT_CREATE,
        OPENSTACK_ENDPOINT_CREATE,
        OPENSTACK_ENDPOINT_CREATE,
        APT_GET_INSTALL,
        APT_GET_INSTALL,
        APT_GET_INSTALL,
        APT_GET_INSTALL,
        NOVA_MANAGE_API_DB_SYNC,
        NOVA_MANAGE_MAP_CELL0,
        NOVA_MANAGE_CREATE_CELL1,
        NOVA_MANAGE_DB_SYNC,
        SYSTEMCTL_RESTART,
        SYSTEMCTL_RESTART,
        SYSTEMCTL_RESTART,
        SYSTEMCTL_RESTART,
    ]
    assert [c.command_id for c in preview_before.read_only_commands] == [
        DPKG_QUERY_STATUS,
        DPKG_QUERY_STATUS,
        DPKG_QUERY_STATUS,
        DPKG_QUERY_STATUS,
        SYSTEMCTL_IS_ACTIVE,
        SYSTEMCTL_IS_ACTIVE,
        SYSTEMCTL_IS_ACTIVE,
        SYSTEMCTL_IS_ACTIVE,
        MARIADB_QUERY_SQL,
        OPENSTACK_USER_LIST,
        OPENSTACK_ROLE_ASSIGNMENT_LIST,
        OPENSTACK_SERVICE_LIST,
        OPENSTACK_ENDPOINT_LIST,
        NOVA_MANAGE_LIST_CELLS,
    ]

    _seed_nova_controller_secrets(root)
    conf_path = root / NOVA_CONF_REL
    conf_path.parent.mkdir(parents=True, exist_ok=True)
    conf_path.write_text(DEFAULT_NOVA_CONF, encoding="utf-8")

    preview_after = step.dry_run_preview()
    assert len(preview_after.file_changes) == 1
    assert preview_after.file_changes[0].changed is True
    for secret in (
        TEST_ADMIN_PASS,
        TEST_RABBIT_PASS,
        TEST_NOVA_DBPASS,
        TEST_NOVA_PASS,
        TEST_PLACEMENT_PASS,
    ):
        assert secret not in preview_after.file_changes[0].diff
        for cmd in (*preview_after.commands, *preview_after.read_only_commands):
            assert secret not in cmd.describe(DEFAULT_REGISTRY)
            assert secret not in " ".join(cmd.argv(DEFAULT_REGISTRY))
            assert secret not in repr(cmd)
        for note in preview_after.notes:
            assert secret not in note

    rc = main(
        [
            "apply",
            "--config",
            str(EXAMPLE_CONFIG),
            "--step",
            NOVA_CONTROLLER_STEP_ID,
            "--node",
            "forge-controller-01",
            "--root",
            str(root),
        ]
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "Dry run for step 'nova-controller'" in out
    for secret in (
        TEST_ADMIN_PASS,
        TEST_RABBIT_PASS,
        TEST_NOVA_DBPASS,
        TEST_NOVA_PASS,
        TEST_PLACEMENT_PASS,
    ):
        assert secret not in out


# --- NovaComputeStep: least-privilege secrets, /proc/cpuinfo, idempotence -----------


def test_nova_compute_least_privilege_secrets_and_conf_helpers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    # Seed ONLY the 3 compute-node secrets (no ADMIN_PASS, no NOVA_DBPASS!)
    _seed_nova_compute_only_secrets(root)

    entries = build_nova_compute_ini_entries(
        COMPUTE, TEST_RABBIT_PASS, TEST_NOVA_PASS, TEST_PLACEMENT_PASS
    )
    assert len(entries) == 35
    assert entries == nova_compute_conf_settings(
        COMPUTE, TEST_RABBIT_PASS, TEST_NOVA_PASS, TEST_PLACEMENT_PASS
    )

    host = _make_compute_host(root)
    executor = _make_executor(tmp_path, host, monkeypatch)
    step = NovaComputeStep(root=root, config={}, local_node=COMPUTE, executor=executor)

    # With /proc/cpuinfo absent -> virt_type = qemu
    assert detect_node_libvirt_virt_type(root) == "qemu"
    step.apply()
    assert step.check() is True
    assert step.verify().ok is True

    nova_written = (root / NOVA_CONF_REL).read_text(encoding="utf-8")
    assert nova_transport_url(TEST_RABBIT_PASS) in nova_written
    assert "my_ip = 192.0.2.21" in nova_written
    assert "server_listen = 0.0.0.0" in nova_written
    assert "server_proxyclient_address = $my_ip" in nova_written
    assert f"novncproxy_base_url = {NOVA_NOVNCPROXY_BASE_URL}" in nova_written
    assert "api_servers = http://controller:9292" in nova_written
    assert f"lock_path = {NOVA_LOCK_PATH}" in nova_written
    assert "auth_url = http://controller:5000/" in nova_written
    assert "auth_url = http://controller:5000/v3" in nova_written
    assert (
        verify_nova_compute_conf_text(
            nova_written, COMPUTE, TEST_RABBIT_PASS, TEST_NOVA_PASS, TEST_PLACEMENT_PASS
        )
        == []
    )

    compute_written = (root / NOVA_COMPUTE_CONF_REL).read_text(encoding="utf-8")
    assert "virt_type = qemu" in compute_written
    assert verify_nova_compute_hypervisor_conf_text(compute_written, "qemu") == []


@pytest.mark.parametrize(
    ("cpuinfo_content", "expected_virt_type"),
    [
        ("processor\t: 0\nflags\t\t: fpu vme de pse vmx msr\n", "kvm"),
        ("processor\t: 0\nflags\t\t: fpu vme de pse svm msr\n", "kvm"),
        ("processor\t: 0\nflags\t\t: fpu vme de pse msr\n", "qemu"),
    ],
)
def test_nova_compute_applies_virt_type_from_proc_cpuinfo_and_is_idempotent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    cpuinfo_content: str,
    expected_virt_type: str,
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    _seed_nova_compute_only_secrets(root)
    proc_dir = root / "proc"
    proc_dir.mkdir(parents=True, exist_ok=True)
    (proc_dir / "cpuinfo").write_text(cpuinfo_content, encoding="utf-8")

    host = _make_compute_host(root)
    executor = _make_executor(tmp_path, host, monkeypatch)
    step = build_step(NOVA_COMPUTE_STEP_ID, root, {}, COMPUTE, executor=executor)

    state = StateStore(root / "var/lib/openstack-forge/state.json")
    (root / "var/lib/openstack-forge").mkdir(parents=True, exist_ok=True)
    engine = Engine(state)

    report = engine.run([step])
    assert report.ok is True
    assert report.outcomes[0].outcome is Outcome.APPLIED

    compute_written = (root / NOVA_COMPUTE_CONF_REL).read_text(encoding="utf-8")
    assert f"virt_type = {expected_virt_type}" in compute_written
    assert verify_nova_compute_hypervisor_conf_text(compute_written, expected_virt_type) == []

    # Verify both files were edited BEFORE systemctl restart nova-compute
    assert len(host.multi_snapshots) == 1
    event_name, files_map = host.multi_snapshots[0]
    assert event_name == "restart"
    assert files_map[NOVA_CONF_REL] is not None
    assert nova_transport_url(TEST_RABBIT_PASS) in files_map[NOVA_CONF_REL]  # type: ignore[operator]
    assert files_map[NOVA_COMPUTE_CONF_REL] is not None
    assert f"virt_type = {expected_virt_type}" in files_map[NOVA_COMPUTE_CONF_REL]  # type: ignore[operator]

    # Second Engine.run skips the step
    calls_before = len(host.calls)
    report2 = engine.run([step])
    assert report2.ok is True
    assert report2.outcomes[0].outcome is Outcome.SKIPPED
    for argv in host.calls[calls_before:]:
        assert "install" not in argv
        assert "restart" not in argv


@pytest.mark.parametrize(
    "missing_secret",
    [NOVA_RABBIT_SECRET_NAME, NOVA_SERVICE_SECRET_NAME, NOVA_PLACEMENT_SECRET_NAME],
)
def test_nova_compute_fails_when_compute_secret_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, missing_secret: str
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    store = _seed_nova_compute_only_secrets(root)
    data = json.loads(store.path.read_text(encoding="utf-8"))
    del data[missing_secret]
    store.path.write_text(json.dumps(data), encoding="utf-8")
    os.chmod(store.path, 0o600)

    host = _make_compute_host(root)
    executor = _make_executor(tmp_path, host, monkeypatch)
    step = NovaComputeStep(root=root, config={}, local_node=COMPUTE, executor=executor)

    assert step.check() is False
    with pytest.raises(PreconditionError, match=missing_secret):
        step.apply()


def test_nova_compute_check_and_verify_failure_branches_and_cli_dry_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    _seed_nova_compute_only_secrets(root)
    host = _make_compute_host(root)
    executor = _make_executor(tmp_path, host, monkeypatch)
    step = NovaComputeStep(root=root, config={}, local_node=COMPUTE, executor=executor)

    step.apply()
    assert step.check() is True
    assert step.verify().ok is True

    # 1. Drift in /etc/nova/nova-compute.conf
    compute_conf_path = root / NOVA_COMPUTE_CONF_REL
    good_compute_conf = compute_conf_path.read_text(encoding="utf-8")
    compute_conf_path.write_text(
        good_compute_conf.replace("virt_type = qemu", "virt_type = kvm"),
        encoding="utf-8",
    )
    assert step.check() is False
    ver = step.verify()
    assert ver.ok is False
    assert "virt_type is 'kvm'" in ver.details
    compute_conf_path.write_text(good_compute_conf, encoding="utf-8")

    # 2. Package missing
    host.installed.remove(NOVA_COMPUTE_PACKAGE)
    assert step.check() is False
    ver = step.verify()
    assert ver.ok is False
    assert "package 'nova-compute' is not installed" in ver.details
    host.installed.add(NOVA_COMPUTE_PACKAGE)

    # 3. Unit inactive
    host.active.remove(NOVA_COMPUTE_UNIT)
    assert step.check() is False
    ver = step.verify()
    assert ver.ok is False
    assert "nova-compute" in ver.details
    host.active.add(NOVA_COMPUTE_UNIT)

    # 4. Dry-run preview and CLI apply mask all secrets
    edit_nova_compute_conf(
        root,
        COMPUTE,
        "DifferentRabbitSecret1234567890AB",
        TEST_NOVA_PASS,
        TEST_PLACEMENT_PASS,
        dry_run=False,
    )
    preview = step.dry_run_preview()
    assert len(preview.file_changes) == 2
    for secret in (TEST_RABBIT_PASS, TEST_NOVA_PASS, TEST_PLACEMENT_PASS):
        for fc in preview.file_changes:
            assert secret not in fc.diff
        for note in preview.notes:
            assert secret not in note

    rc = main(
        [
            "apply",
            "--config",
            str(EXAMPLE_CONFIG),
            "--step",
            NOVA_COMPUTE_STEP_ID,
            "--node",
            "forge-compute-01",
            "--root",
            str(root),
        ]
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "Dry run for step 'nova-compute'" in out
    for secret in (TEST_RABBIT_PASS, TEST_NOVA_PASS, TEST_PLACEMENT_PASS):
        assert secret not in out
