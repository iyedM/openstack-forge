"""Unit tests for the ``placement`` execution step (Milestone 17)."""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from _service_fakes import ServiceHost

from openstack_forge.cli import main
from openstack_forge.execution import (
    APACHE2_UNIT,
    APT_GET_INSTALL,
    DEFAULT_REGISTRY,
    DPKG_QUERY_STATUS,
    MARIADB_EXECUTE_SQL,
    MARIADB_QUERY_SQL,
    MASK,
    OPENSTACK_ENDPOINT_CREATE,
    OPENSTACK_ENDPOINT_LIST,
    OPENSTACK_ROLE_ADD_USER_PROJECT,
    OPENSTACK_ROLE_ASSIGNMENT_LIST,
    OPENSTACK_SERVICE_CREATE,
    OPENSTACK_SERVICE_LIST,
    OPENSTACK_USER_CREATE,
    OPENSTACK_USER_LIST,
    PLACEMENT_ADMIN_SECRET_NAME,
    PLACEMENT_CONF_TARGET,
    PLACEMENT_DB_NAME,
    PLACEMENT_DB_SECRET_NAME,
    PLACEMENT_DB_USER,
    PLACEMENT_MANAGE_DB_SYNC,
    PLACEMENT_MANAGE_FAILURE_HINT,
    PLACEMENT_PACKAGE,
    PLACEMENT_SERVICE_NAME,
    PLACEMENT_SERVICE_SECRET_NAME,
    PLACEMENT_SERVICE_USER,
    PLACEMENT_STEP_ID,
    SYSTEMCTL_IS_ACTIVE,
    SYSTEMCTL_RESTART,
    AuditLog,
    CommandExecutor,
    Engine,
    ExecutionError,
    Outcome,
    PlacementStep,
    PreconditionError,
    ProcessOutput,
    SecretStore,
    StateStore,
    build_step,
    clear_registered_secrets,
    edit_placement_conf,
    format_report,
    make_placement_step,
    placement_conf_settings,
    placement_db_connection,
    verify_placement_conf_text,
)
from openstack_forge.orchestration import load_catalog

REPO_ROOT = Path(__file__).resolve().parents[2]
EXAMPLE_CONFIG = REPO_ROOT / "examples" / "cluster.example.yaml"
CONTROLLER = {"hostname": "ctl-01", "role": "controller", "management_ip": "192.0.2.11"}

TEST_ADMIN_PASS = "AdminBootstrapPassword1234567890XyZ"
TEST_PLACEMENT_DBPASS = "PlacementDbSecretPass1234567890AbCd"
TEST_PLACEMENT_PASS = "PlacementServicePass1234567890EfGhi"

PLACEMENT_CONF_REL = "etc/placement/placement.conf"

# Realistic default /etc/placement/placement.conf shipped by placement-common
DEFAULT_PLACEMENT_CONF = (
    "[DEFAULT]\n"
    "#debug = false\n"
    "\n"
    "[api]\n"
    "#auth_strategy = keystone\n"
    "\n"
    "[keystone_authtoken]\n"
    "#auth_url = <None>\n"
    "#memcached_servers = <None>\n"
    "#auth_type = <None>\n"
    "#project_domain_name = Default\n"
    "#user_domain_name = Default\n"
    "#project_name = <None>\n"
    "#username = <None>\n"
    "#password = <None>\n"
    "\n"
    "[placement_database]\n"
    "#connection = <None>\n"
    "connection = sqlite:////var/lib/placement/placement.sqlite\n"
)


@pytest.fixture(autouse=True)
def _clean_redaction() -> Iterator[None]:
    clear_registered_secrets()
    yield
    clear_registered_secrets()


def _seed_placement_secrets(
    root: Path,
    *,
    admin_pass: str = TEST_ADMIN_PASS,
    db_pass: str = TEST_PLACEMENT_DBPASS,
    placement_pass: str = TEST_PLACEMENT_PASS,
) -> SecretStore:
    """Create a valid 0600 secrets.json under ``root`` with Placement and Admin secrets."""
    secrets_dir = root / "etc" / "openstack-forge"
    secrets_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(secrets_dir, 0o700)
    secrets_file = secrets_dir / "secrets.json"
    secrets_file.write_text(
        json.dumps(
            {
                PLACEMENT_ADMIN_SECRET_NAME: admin_pass,
                PLACEMENT_DB_SECRET_NAME: db_pass,
                PLACEMENT_SERVICE_SECRET_NAME: placement_pass,
            }
        ),
        encoding="utf-8",
    )
    os.chmod(secrets_file, 0o600)
    store = SecretStore(root)
    store.ensure()
    return store


def _make_host(root: Path, **kwargs: object) -> ServiceHost:
    kwargs.setdefault(
        "creates",
        {
            PLACEMENT_PACKAGE: (PLACEMENT_CONF_REL, DEFAULT_PLACEMENT_CONF),
        },
    )
    kwargs.setdefault("watch", PLACEMENT_CONF_REL)
    kwargs.setdefault(
        "openstack_projects",
        ({"ID": "proj-admin", "Name": "admin"}, {"ID": "proj-service", "Name": "service"}),
    )
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


# --- Catalog, role, and INI configuration helpers -----------------------------------


def test_placement_catalog_entry_and_role_restriction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    catalog = load_catalog()
    step = catalog.step(PLACEMENT_STEP_ID)
    assert step.roles == ("controller",)
    assert step.depends_on == ("mariadb", "memcached", "keystone")
    assert step.files_modified == (PLACEMENT_CONF_TARGET,)
    assert step.doc_url == "https://docs.openstack.org/placement/2025.1/install/install-ubuntu.html"
    assert step.status == "unverified"

    root = tmp_path / "root"
    root.mkdir()
    host = _make_host(root)
    executor = _make_executor(tmp_path, host, monkeypatch)
    for role in ("compute", "block", "object"):
        with pytest.raises(ExecutionError, match="controller"):
            make_placement_step(
                root,
                {},
                {"hostname": "node-01", "role": role, "management_ip": "192.0.2.21"},
                executor=executor,
            )


def test_edit_and_verify_placement_conf_redacts_secrets_and_is_idempotent(
    tmp_path: Path,
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    _seed_placement_secrets(root)

    # Missing file before package installation raises PreconditionError
    with pytest.raises(PreconditionError, match="placement-api is installed"):
        edit_placement_conf(root, TEST_PLACEMENT_DBPASS, TEST_PLACEMENT_PASS, dry_run=False)

    conf_path = root / PLACEMENT_CONF_REL
    conf_path.parent.mkdir(parents=True, exist_ok=True)
    conf_path.write_text(DEFAULT_PLACEMENT_CONF, encoding="utf-8")

    preview = edit_placement_conf(root, TEST_PLACEMENT_DBPASS, TEST_PLACEMENT_PASS, dry_run=True)
    assert preview.changed is True
    assert TEST_PLACEMENT_DBPASS not in preview.diff
    assert TEST_PLACEMENT_PASS not in preview.diff
    assert MASK in preview.diff
    assert conf_path.read_text(encoding="utf-8") == DEFAULT_PLACEMENT_CONF

    change = edit_placement_conf(root, TEST_PLACEMENT_DBPASS, TEST_PLACEMENT_PASS, dry_run=False)
    assert change.changed is True
    assert TEST_PLACEMENT_DBPASS not in change.diff
    assert TEST_PLACEMENT_PASS not in change.diff
    assert (root / f"{PLACEMENT_CONF_REL}.orig").exists()

    written = conf_path.read_text(encoding="utf-8")
    assert placement_db_connection(TEST_PLACEMENT_DBPASS) in written
    assert "auth_strategy = keystone" in written
    assert "auth_url = http://controller:5000/v3" in written
    assert "memcached_servers = controller:11211" in written
    assert "auth_type = password" in written
    assert "project_domain_name = Default" in written
    assert "user_domain_name = Default" in written
    assert "project_name = service" in written
    assert "username = placement" in written
    assert f"password = {TEST_PLACEMENT_PASS}" in written
    assert verify_placement_conf_text(written, TEST_PLACEMENT_DBPASS, TEST_PLACEMENT_PASS) == []

    # Second call is a no-op
    again = edit_placement_conf(root, TEST_PLACEMENT_DBPASS, TEST_PLACEMENT_PASS, dry_run=False)
    assert again.changed is False
    assert again.diff == ""


def test_verify_placement_conf_text_detects_missing_and_mismatched_keys_with_redaction() -> None:
    problems = verify_placement_conf_text(
        "[placement_database]\nconnection = mysql+pymysql://placement:BadDbPass@controller/placement\n"
        "[keystone_authtoken]\npassword = BadServicePass\n",
        TEST_PLACEMENT_DBPASS,
        TEST_PLACEMENT_PASS,
    )
    joined = "; ".join(problems)
    assert TEST_PLACEMENT_DBPASS not in joined
    assert TEST_PLACEMENT_PASS not in joined
    assert "BadDbPass" not in joined
    assert "BadServicePass" not in joined
    assert "connection is" in joined
    assert "password is" in joined
    assert "auth_strategy is not set in [api]" in joined
    assert "auth_url is not set in [keystone_authtoken]" in joined
    assert len(placement_conf_settings(TEST_PLACEMENT_DBPASS, TEST_PLACEMENT_PASS)) == 10


# --- Preconditions: missing secrets.json or missing individual secrets --------------


def test_placement_apply_fails_when_secrets_file_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    host = _make_host(root)
    executor = _make_executor(tmp_path, host, monkeypatch)
    step = PlacementStep(root=root, config={}, local_node=CONTROLLER, executor=executor)

    assert step.check() is False
    with pytest.raises(PreconditionError, match="secrets init"):
        step.apply()
    assert host.calls == []

    ver = step.verify()
    assert ver.ok is False
    assert "secrets init" in ver.details


@pytest.mark.parametrize(
    "missing_secret",
    [PLACEMENT_ADMIN_SECRET_NAME, PLACEMENT_DB_SECRET_NAME, PLACEMENT_SERVICE_SECRET_NAME],
)
def test_placement_fails_when_required_secret_is_missing_from_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, missing_secret: str
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    store = _seed_placement_secrets(root)
    data = json.loads(store.path.read_text(encoding="utf-8"))
    del data[missing_secret]
    store.path.write_text(json.dumps(data), encoding="utf-8")
    os.chmod(store.path, 0o600)

    host = _make_host(root)
    executor = _make_executor(tmp_path, host, monkeypatch)
    step = PlacementStep(root=root, config={}, local_node=CONTROLLER, executor=executor)

    assert step.check() is False
    with pytest.raises(PreconditionError, match=missing_secret):
        step.apply()


# --- Full fresh controller apply, ordering, and idempotence -------------------------


def test_placement_fresh_apply_orders_conf_edit_before_db_sync_and_restart_and_is_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    _seed_placement_secrets(root)
    host = _make_host(root)
    executor = _make_executor(tmp_path, host, monkeypatch)
    step = build_step(PLACEMENT_STEP_ID, root, {}, CONTROLLER, executor=executor)

    state = StateStore(root / "var/lib/openstack-forge/state.json")
    (root / "var/lib/openstack-forge").mkdir(parents=True, exist_ok=True)
    engine = Engine(state)

    report = engine.run([step])
    assert report.ok is True
    assert report.outcomes[0].outcome is Outcome.APPLIED

    # Verify MariaDB database & grants
    assert PLACEMENT_DB_NAME in host.mariadb_databases
    assert (PLACEMENT_DB_USER, "localhost", PLACEMENT_DB_NAME) in host.mariadb_grants
    assert (PLACEMENT_DB_USER, "%", PLACEMENT_DB_NAME) in host.mariadb_grants
    assert host.mariadb_passwords[(PLACEMENT_DB_USER, "localhost")] == TEST_PLACEMENT_DBPASS
    assert host.mariadb_passwords[(PLACEMENT_DB_USER, "%")] == TEST_PLACEMENT_DBPASS

    # Verify Keystone user, role, service, and 3 endpoints
    assert any(u["Name"] == PLACEMENT_SERVICE_USER for u in host.openstack_users)
    assert host.openstack_user_passwords[PLACEMENT_SERVICE_USER] == TEST_PLACEMENT_PASS
    assert {
        "Role": "admin",
        "User": f"{PLACEMENT_SERVICE_USER}@Default",
        "Group": "",
        "Project": "service@Default",
        "Domain": "",
        "System": "",
        "Inherited": False,
    } in host.openstack_role_assignments
    assert len([s for s in host.openstack_services if s["Name"] == PLACEMENT_SERVICE_NAME]) == 1
    placement_eps = [
        e for e in host.openstack_endpoints if e["Service Name"] == PLACEMENT_SERVICE_NAME
    ]
    assert len(placement_eps) == 3
    assert {e["Interface"] for e in placement_eps} == {"public", "internal", "admin"}
    assert {e["URL"] for e in placement_eps} == {"http://controller:8778"}
    assert {e["Service Type"] for e in placement_eps} == {"placement"}

    # Verify placement.conf was already edited BEFORE placement-manage db sync and BEFORE restart
    assert host.placement_db_synced is True
    assert len(host.snapshots) == 2
    for event_name, snap_text in host.snapshots:
        assert event_name in ("placement-manage db sync", "restart")
        assert snap_text is not None
        assert placement_db_connection(TEST_PLACEMENT_DBPASS) in snap_text
        assert f"password = {TEST_PLACEMENT_PASS}" in snap_text

    # Neither ADMIN_PASS nor PLACEMENT_DBPASS ever appears in argv
    for argv in host.calls:
        assert all(TEST_ADMIN_PASS not in arg for arg in argv)
        assert all(TEST_PLACEMENT_DBPASS not in arg for arg in argv)

    # Second Engine.run skips the step without running any mutating command
    calls_before_second_run = len(host.calls)
    report2 = engine.run([step])
    assert report2.ok is True
    assert report2.outcomes[0].outcome is Outcome.SKIPPED
    for argv in host.calls[calls_before_second_run:]:
        assert argv[0] in (
            "/usr/bin/dpkg-query",
            "/usr/bin/systemctl",
            "/usr/bin/mariadb",
            "/usr/bin/openstack",
        )
        assert "create" not in argv
        assert "install" not in argv
        assert "restart" not in argv

    # Calling step.apply() directly a second time converges user password with user set
    # and never creates duplicate Keystone services or endpoints
    step.apply()
    assert len([s for s in host.openstack_services if s["Name"] == PLACEMENT_SERVICE_NAME]) == 1
    placement_eps = [
        e for e in host.openstack_endpoints if e["Service Name"] == PLACEMENT_SERVICE_NAME
    ]
    assert len(placement_eps) == 3
    assert {e["Interface"] for e in placement_eps} == {"public", "internal", "admin"}
    assert {e["URL"] for e in placement_eps} == {"http://controller:8778"}
    assert {e["Service Type"] for e in placement_eps} == {"placement"}
    assert any(
        argv[:2] == ("/usr/bin/openstack", "--os-auth-url") and "set" in argv for argv in host.calls
    )


# --- Every check() and verify() failure branch --------------------------------------


def test_placement_check_and_verify_failure_branches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    _seed_placement_secrets(root)
    host = _make_host(root)
    executor = _make_executor(tmp_path, host, monkeypatch)
    step = PlacementStep(root=root, config={}, local_node=CONTROLLER, executor=executor)

    step.apply()
    assert step.check() is True
    assert step.verify().ok is True

    # 1. Config drift in /etc/placement/placement.conf -> check() is False with zero commands
    conf_path = root / PLACEMENT_CONF_REL
    good_conf = conf_path.read_text(encoding="utf-8")
    conf_path.write_text(
        good_conf.replace("auth_strategy = keystone", "auth_strategy = noauth2"),
        encoding="utf-8",
    )
    calls_before = len(host.calls)
    assert step.check() is False
    assert len(host.calls) == calls_before
    ver = step.verify()
    assert ver.ok is False
    assert "auth_strategy is 'noauth2'" in ver.details
    conf_path.write_text(good_conf, encoding="utf-8")

    # 2. Package not installed
    host.installed.remove(PLACEMENT_PACKAGE)
    assert step.check() is False
    ver = step.verify()
    assert ver.ok is False
    assert "package 'placement-api' is not installed" in ver.details
    host.installed.add(PLACEMENT_PACKAGE)

    # 3. apache2 unit inactive
    host.active.remove(APACHE2_UNIT)
    assert step.check() is False
    ver = step.verify()
    assert ver.ok is False
    assert "apache2" in ver.details
    host.active.add(APACHE2_UNIT)

    # 4. MariaDB database missing
    host.mariadb_databases.remove(PLACEMENT_DB_NAME)
    assert step.check() is False
    ver = step.verify()
    assert ver.ok is False
    assert "MariaDB database 'placement' does not exist" in ver.details
    host.mariadb_databases.add(PLACEMENT_DB_NAME)

    # 5. Keystone user missing
    saved_users = list(host.openstack_users)
    host.openstack_users.clear()
    assert step.check() is False
    ver = step.verify()
    assert ver.ok is False
    assert "Keystone user 'placement' (domain 'default') does not exist" in ver.details
    host.openstack_users[:] = saved_users

    # 6. Keystone endpoint missing or wrong URL
    saved_eps = list(host.openstack_endpoints)
    host.openstack_endpoints = [ep for ep in saved_eps if ep["Interface"] != "public"]
    assert step.check() is False
    ver = step.verify()
    assert ver.ok is False
    assert (
        "Keystone public endpoint for 'placement' ('placement') in RegionOne does not exist"
        in ver.details
    )
    host.openstack_endpoints[:] = saved_eps

    # 7. Symlink on /etc/placement/placement.conf
    other = root / "etc" / "other.conf"
    other.write_text("", encoding="utf-8")
    conf_path.unlink()
    conf_path.symlink_to(other)
    assert step.check() is False
    ver = step.verify()
    assert ver.ok is False
    assert "symlink" in ver.details


def test_placement_db_sync_failure_attaches_placement_manage_hint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    _seed_placement_secrets(root)
    host = _make_host(root)
    host.failures[("/usr/bin/placement-manage", "db", "sync")] = ProcessOutput(
        1, "", "CRITICAL placement [-] DBConnectionError\n"
    )
    executor = _make_executor(tmp_path, host, monkeypatch)
    step = PlacementStep(root=root, config={}, local_node=CONTROLLER, executor=executor)
    (root / "var/lib/openstack-forge").mkdir(parents=True, exist_ok=True)
    report = Engine(StateStore(root / "var/lib/openstack-forge/state.json")).run([step])
    assert report.ok is False
    text = format_report(report)
    assert PLACEMENT_MANAGE_FAILURE_HINT in text


# --- Dry-run preview and CLI apply --------------------------------------------------


def test_placement_dry_run_preview_and_cli_apply_mask_all_secrets(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    host = _make_host(root)
    (tmp_path / "audit").mkdir(exist_ok=True)
    executor = CommandExecutor(
        DEFAULT_REGISTRY,
        root=root,
        audit_log=AuditLog(tmp_path / "audit/commands.log"),
        backend=host,
        dry_run=True,
    )
    step = PlacementStep(root=root, config={}, local_node=CONTROLLER, executor=executor)

    # Preview before secrets init and before package install
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
        PLACEMENT_MANAGE_DB_SYNC,
        SYSTEMCTL_RESTART,
    ]
    assert [c.command_id for c in preview_before.read_only_commands] == [
        DPKG_QUERY_STATUS,
        SYSTEMCTL_IS_ACTIVE,
        MARIADB_QUERY_SQL,
        OPENSTACK_USER_LIST,
        OPENSTACK_ROLE_ASSIGNMENT_LIST,
        OPENSTACK_SERVICE_LIST,
        OPENSTACK_ENDPOINT_LIST,
    ]
    assert any("secrets init" in note for note in preview_before.notes)

    # Preview after secrets init and package install
    _seed_placement_secrets(root)
    conf_path = root / PLACEMENT_CONF_REL
    conf_path.parent.mkdir(parents=True, exist_ok=True)
    conf_path.write_text(DEFAULT_PLACEMENT_CONF, encoding="utf-8")

    preview_after = step.dry_run_preview()
    assert len(preview_after.file_changes) == 1
    assert preview_after.file_changes[0].changed is True
    for secret in (TEST_ADMIN_PASS, TEST_PLACEMENT_DBPASS, TEST_PLACEMENT_PASS):
        assert secret not in preview_after.file_changes[0].diff
        for cmd in (*preview_after.commands, *preview_after.read_only_commands):
            assert secret not in cmd.describe(DEFAULT_REGISTRY)
            assert secret not in " ".join(cmd.argv(DEFAULT_REGISTRY))
            assert secret not in repr(cmd)
        for note in preview_after.notes:
            assert secret not in note

    # CLI dry-run
    rc = main(
        [
            "apply",
            "--config",
            str(EXAMPLE_CONFIG),
            "--step",
            PLACEMENT_STEP_ID,
            "--node",
            "forge-controller-01",
            "--root",
            str(root),
        ]
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "Dry run for step 'placement'" in out
    assert TEST_ADMIN_PASS not in out
    assert TEST_PLACEMENT_DBPASS not in out
    assert TEST_PLACEMENT_PASS not in out
