"""Unit tests for the ``keystone`` execution step (Milestone 15)."""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from _service_fakes import ServiceHost

from openstack_forge.cli import main
from openstack_forge.execution import (
    APACHE2_BLOCK_NAME,
    APACHE2_CONF_TARGET,
    APACHE2_SERVER_NAME_LINE,
    APACHE2_UNIT,
    APT_GET_INSTALL,
    CREDENTIAL_KEYS_TARGET,
    DEFAULT_REGISTRY,
    DISABLED_LINE_PREFIX,
    DPKG_QUERY_STATUS,
    FERNET_KEYS_TARGET,
    KEYSTONE_ADMIN_SECRET_NAME,
    KEYSTONE_CONF_TARGET,
    KEYSTONE_DB_NAME,
    KEYSTONE_DB_SECRET_NAME,
    KEYSTONE_DB_USER,
    KEYSTONE_MANAGE_BOOTSTRAP,
    KEYSTONE_MANAGE_CREDENTIAL_SETUP,
    KEYSTONE_MANAGE_DB_SYNC,
    KEYSTONE_MANAGE_FAILURE_HINT,
    KEYSTONE_MANAGE_FERNET_SETUP,
    KEYSTONE_PACKAGE,
    KEYSTONE_STEP_ID,
    MARIADB_EXECUTE_SQL,
    MARIADB_QUERY_SQL,
    MARIADB_SQL_FAILURE_HINT,
    MASK,
    SYSTEMCTL_IS_ACTIVE,
    SYSTEMCTL_RESTART,
    AuditLog,
    CommandExecutor,
    Engine,
    ExecutionError,
    KeystoneStep,
    Outcome,
    PreconditionError,
    ProcessOutput,
    SecretStore,
    StateStore,
    UnsafePathError,
    build_step,
    clear_registered_secrets,
    edit_apache2_conf,
    edit_keystone_conf,
    format_report,
    keystone_db_connection,
    make_keystone_step,
    verify_apache2_conf_text,
    verify_keystone_conf_text,
    verify_keystone_key_repositories,
)
from openstack_forge.orchestration import load_catalog

REPO_ROOT = Path(__file__).resolve().parents[2]
EXAMPLE_CONFIG = REPO_ROOT / "examples" / "cluster.example.yaml"
CONTROLLER = {"hostname": "ctl-01", "role": "controller", "management_ip": "192.0.2.11"}
COMPUTE = {"hostname": "cmp-01", "role": "compute", "management_ip": "192.0.2.21"}

TEST_KEYSTONE_DBPASS = "KeystoneDbSecretPassword1234567890Ab"
TEST_ADMIN_PASS = "AdminBootstrapPassword1234567890XyZ"

KEYSTONE_CONF_REL = "etc/keystone/keystone.conf"
APACHE2_CONF_REL = "etc/apache2/apache2.conf"

# Realistic default /etc/keystone/keystone.conf shipped by keystone-common
DEFAULT_KEYSTONE_CONF = (
    "[DEFAULT]\n"
    "#debug = false\n"
    "\n"
    "[database]\n"
    "#connection = <None>\n"
    "connection = sqlite:////var/lib/keystone/keystone.db\n"
    "\n"
    "[token]\n"
    "#provider = fernet\n"
)

# Excerpt of pristine /etc/apache2/apache2.conf shipped by apache2 2.4.58-1ubuntu8.15
# (dpkg conffile MD5 354c9e6d2b88a0a3e0548f853840674c — contains no active ServerName line).
PRISTINE_APACHE2_CONF = (
    "# This is the main Apache server configuration file.\n"
    "DefaultRuntimeDir ${APACHE_RUN_DIR}\n"
    "PidFile ${APACHE_PID_FILE}\n"
    "Timeout 300\n"
    "IncludeOptional sites-enabled/*.conf\n"
)

# Excerpt of /etc/apache2/apache2.conf as observed on the Ubuntu 24.04 dev machine,
# where "ServerName controller" had been added on line 1 before the header comment.
DEV_MACHINE_APACHE2_CONF = "ServerName controller\n" + PRISTINE_APACHE2_CONF

APACHE2_BLOCK = (
    f"# BEGIN {APACHE2_BLOCK_NAME}\n{APACHE2_SERVER_NAME_LINE}\n# END {APACHE2_BLOCK_NAME}\n"
)


@pytest.fixture(autouse=True)
def _clean_redaction() -> Iterator[None]:
    clear_registered_secrets()
    yield
    clear_registered_secrets()


def _seed_keystone_secrets(
    root: Path,
    *,
    db_pass: str = TEST_KEYSTONE_DBPASS,
    admin_pass: str = TEST_ADMIN_PASS,
) -> SecretStore:
    """Create a valid 0600 secrets.json under ``root`` with KEYSTONE_DBPASS and ADMIN_PASS."""
    secrets_dir = root / "etc" / "openstack-forge"
    secrets_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(secrets_dir, 0o700)
    secrets_file = secrets_dir / "secrets.json"
    secrets_file.write_text(
        json.dumps(
            {
                KEYSTONE_DB_SECRET_NAME: db_pass,
                KEYSTONE_ADMIN_SECRET_NAME: admin_pass,
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
            KEYSTONE_PACKAGE: (
                (KEYSTONE_CONF_REL, DEFAULT_KEYSTONE_CONF),
                (APACHE2_CONF_REL, PRISTINE_APACHE2_CONF),
            )
        },
    )
    kwargs.setdefault("watch_files", (KEYSTONE_CONF_REL, APACHE2_CONF_REL))
    return ServiceHost(root, **kwargs)  # type: ignore[arg-type]


def _make_executor(
    root: Path, host: ServiceHost, *, dry_run: bool = False
) -> tuple[CommandExecutor, AuditLog]:
    audit = AuditLog(root / "commands.log")
    executor = CommandExecutor(
        DEFAULT_REGISTRY,
        root=root,
        audit_log=audit,
        backend=host,
        dry_run=dry_run,
        euid=lambda: 0,
    )
    return executor, audit


# --- catalog & role checks ------------------------------------------------------------


def test_keystone_catalog_entry_remains_unverified() -> None:
    step = load_catalog().step(KEYSTONE_STEP_ID)
    assert step.roles == ("controller",)
    assert step.depends_on == ("mariadb", "memcached")
    assert step.files_modified == (KEYSTONE_CONF_TARGET, APACHE2_CONF_TARGET)
    assert step.status == "unverified"
    assert (
        step.doc_url
        == "https://docs.openstack.org/keystone/2025.1/install/keystone-install-ubuntu.html"
    )


@pytest.mark.parametrize("role", ["compute", "block", "object"])
def test_keystone_step_rejects_non_controller_roles(tmp_path: Path, role: str) -> None:
    host = _make_host(tmp_path)
    executor, _ = _make_executor(tmp_path, host)
    node = {**COMPUTE, "hostname": f"forge-{role}-01", "role": role}
    with pytest.raises(ExecutionError, match="only runs on the controller"):
        make_keystone_step(tmp_path, {}, node, executor=executor)
    with pytest.raises(ExecutionError, match="only runs on the controller"):
        build_step(KEYSTONE_STEP_ID, tmp_path, {}, node, executor=executor)


# --- Preconditions on KEYSTONE_DBPASS and ADMIN_PASS in SecretStore -------------------


def test_missing_or_incomplete_secret_store_precondition(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("openstack_forge.execution.commands.is_host_root", lambda _: True)
    host = _make_host(tmp_path)
    executor, _ = _make_executor(tmp_path, host)
    step = KeystoneStep(tmp_path, {}, CONTROLLER, executor)

    # 1. Store does not exist yet: check() is pure and runs zero commands.
    assert step.check() is False
    assert host.calls == []

    # apply() fails with PreconditionError mentioning `openstack-forge secrets init`
    with pytest.raises(PreconditionError, match="openstack-forge secrets init"):
        step.apply()
    assert host.calls == []
    assert not (tmp_path / "etc" / "openstack-forge" / "secrets.json").exists()

    ver = step.verify()
    assert not ver.ok
    assert "openstack-forge secrets init" in ver.details
    host.calls.clear()

    # 2. Store has KEYSTONE_DBPASS but lacks ADMIN_PASS
    secrets_dir = tmp_path / "etc" / "openstack-forge"
    secrets_dir.mkdir(parents=True, mode=0o700)
    os.chmod(secrets_dir, 0o700)
    secrets_file = secrets_dir / "secrets.json"
    secrets_file.write_text(
        json.dumps({KEYSTONE_DB_SECRET_NAME: TEST_KEYSTONE_DBPASS}),
        encoding="utf-8",
    )
    os.chmod(secrets_file, 0o600)

    assert step.check() is False
    assert host.calls == []
    with pytest.raises(PreconditionError, match="ADMIN_PASS"):
        step.apply()

    # 3. Store has ADMIN_PASS but lacks KEYSTONE_DBPASS
    secrets_file.write_text(
        json.dumps({KEYSTONE_ADMIN_SECRET_NAME: TEST_ADMIN_PASS}),
        encoding="utf-8",
    )
    os.chmod(secrets_file, 0o600)

    assert step.check() is False
    assert host.calls == []
    with pytest.raises(PreconditionError, match="KEYSTONE_DBPASS"):
        step.apply()

    # 4. Store has both secrets, but mode is 0644 (overly permissive)
    secrets_file.write_text(
        json.dumps(
            {
                KEYSTONE_DB_SECRET_NAME: TEST_KEYSTONE_DBPASS,
                KEYSTONE_ADMIN_SECRET_NAME: TEST_ADMIN_PASS,
            }
        ),
        encoding="utf-8",
    )
    os.chmod(secrets_file, 0o644)
    assert step.check() is False
    with pytest.raises(PreconditionError, match="more permissive than 0600"):
        step.apply()


# --- /etc/keystone/keystone.conf and /etc/apache2/apache2.conf edits & verifiers ------


def test_edit_and_verify_keystone_conf_redacts_password_and_is_idempotent(
    tmp_path: Path,
) -> None:
    _seed_keystone_secrets(tmp_path)
    conf_path = tmp_path / KEYSTONE_CONF_REL
    conf_path.parent.mkdir(parents=True)

    # Never creates /etc/keystone/keystone.conf from scratch
    with pytest.raises(PreconditionError, match="created when keystone is installed"):
        edit_keystone_conf(tmp_path, TEST_KEYSTONE_DBPASS, dry_run=False)
    assert not conf_path.exists()

    conf_path.write_text(DEFAULT_KEYSTONE_CONF, encoding="utf-8")
    change = edit_keystone_conf(tmp_path, TEST_KEYSTONE_DBPASS, dry_run=False)
    assert change.changed is True
    assert change.backup == f"{KEYSTONE_CONF_TARGET}.orig"
    assert TEST_KEYSTONE_DBPASS not in change.diff
    assert f"mysql+pymysql://keystone:{MASK}@controller/keystone" in change.diff
    assert "provider = fernet" in change.diff

    updated = conf_path.read_text(encoding="utf-8")
    assert keystone_db_connection(TEST_KEYSTONE_DBPASS) in updated
    assert "provider = fernet" in updated
    assert "sqlite://" not in updated
    assert verify_keystone_conf_text(updated, TEST_KEYSTONE_DBPASS) == []

    # Second run is idempotent
    before = (conf_path.read_bytes(), conf_path.stat().st_mtime_ns, conf_path.stat().st_ino)
    change2 = edit_keystone_conf(tmp_path, TEST_KEYSTONE_DBPASS, dry_run=False)
    assert (change2.changed, change2.diff) == (False, "")
    assert (conf_path.read_bytes(), conf_path.stat().st_mtime_ns, conf_path.stat().st_ino) == before


def test_verify_keystone_conf_text_masks_passwords_in_problems(tmp_path: Path) -> None:
    _seed_keystone_secrets(tmp_path)
    wrong_pass = "WrongKeystonePassword1234567890AbCd"
    problems = verify_keystone_conf_text(
        f"[database]\nconnection = mysql+pymysql://keystone:{wrong_pass}@controller/keystone\n"
        "[token]\nprovider = uuid\n",
        TEST_KEYSTONE_DBPASS,
    )
    joined = "; ".join(problems)
    assert TEST_KEYSTONE_DBPASS not in joined
    assert wrong_pass not in joined
    assert MASK in joined
    assert "connection is" in joined
    assert "provider is 'uuid'" in joined

    missing_problems = verify_keystone_conf_text("[DEFAULT]\n", TEST_KEYSTONE_DBPASS)
    assert len(missing_problems) == 2
    assert TEST_KEYSTONE_DBPASS not in "; ".join(missing_problems)


def test_edit_and_verify_apache2_conf_pristine_and_dev_machine(tmp_path: Path) -> None:
    apache_path = tmp_path / APACHE2_CONF_REL
    apache_path.parent.mkdir(parents=True)

    # Never creates /etc/apache2/apache2.conf from scratch
    with pytest.raises(PreconditionError, match="created when keystone"):
        edit_apache2_conf(tmp_path, dry_run=False)
    assert not apache_path.exists()

    # 1. Pristine apache2.conf (no ServerName line)
    apache_path.write_text(PRISTINE_APACHE2_CONF, encoding="utf-8")
    change = edit_apache2_conf(tmp_path, dry_run=False)
    assert change.changed is True
    assert change.backup == f"{APACHE2_CONF_TARGET}.orig"
    assert apache_path.read_text(encoding="utf-8") == PRISTINE_APACHE2_CONF + APACHE2_BLOCK
    assert verify_apache2_conf_text(apache_path.read_text(encoding="utf-8")) == []

    # Idempotent rerun
    change_again = edit_apache2_conf(tmp_path, dry_run=False)
    assert (change_again.changed, change_again.diff) == (False, "")

    # 2. Dev-machine apache2.conf (ServerName controller already on line 1 outside block)
    apache_path.write_text(DEV_MACHINE_APACHE2_CONF, encoding="utf-8")
    change_dev = edit_apache2_conf(tmp_path, dry_run=False)
    assert change_dev.changed is True
    updated_dev = apache_path.read_text(encoding="utf-8")
    assert updated_dev.startswith(f"{DISABLED_LINE_PREFIX}ServerName controller\n")
    assert updated_dev.endswith(APACHE2_BLOCK)
    assert verify_apache2_conf_text(updated_dev) == []


def test_verify_apache2_conf_text_problems() -> None:
    assert verify_apache2_conf_text(PRISTINE_APACHE2_CONF) == [
        f"the managed block '{APACHE2_BLOCK_NAME}' is missing from {APACHE2_CONF_TARGET}"
    ]
    assert verify_apache2_conf_text(f"# BEGIN {APACHE2_BLOCK_NAME}\n") == [
        f"the managed block '{APACHE2_BLOCK_NAME}' in {APACHE2_CONF_TARGET} has broken markers"
    ]
    wrong_block = (
        f"# BEGIN {APACHE2_BLOCK_NAME}\nServerName wrong-host\n# END {APACHE2_BLOCK_NAME}\n"
    )
    assert verify_apache2_conf_text(wrong_block) == [
        f"the managed block '{APACHE2_BLOCK_NAME}' in {APACHE2_CONF_TARGET} does not "
        f"contain exactly '{APACHE2_SERVER_NAME_LINE}'"
    ]
    extra_outside = "ServerName other\n" + APACHE2_BLOCK
    assert verify_apache2_conf_text(extra_outside) == [
        f"active ServerName line(s) outside the block in {APACHE2_CONF_TARGET}: ServerName other"
    ]


def test_verify_keystone_key_repositories_and_symlink_refusal(tmp_path: Path) -> None:
    assert verify_keystone_key_repositories(tmp_path) == [
        f"Fernet token key repository {FERNET_KEYS_TARGET} does not exist",
        f"Fernet credential key repository {CREDENTIAL_KEYS_TARGET} does not exist",
    ]

    fernet_dir = tmp_path / "etc/keystone/fernet-keys"
    cred_dir = tmp_path / "etc/keystone/credential-keys"
    fernet_dir.mkdir(parents=True)
    cred_dir.mkdir(parents=True)
    # Empty directory (no key 0) and zero-byte key 0 both fail verification
    (cred_dir / "0").write_text("", encoding="utf-8")
    assert verify_keystone_key_repositories(tmp_path) == [
        f"Fernet token key repository {FERNET_KEYS_TARGET} is not initialized (missing key '0')",
        (
            f"Fernet credential key repository {CREDENTIAL_KEYS_TARGET} "
            "is not initialized (missing key '0')"
        ),
    ]

    (fernet_dir / "0").write_text("key-0\n", encoding="utf-8")
    (cred_dir / "0").write_text("key-0\n", encoding="utf-8")
    assert verify_keystone_key_repositories(tmp_path) == []

    # Symlinked key file is refused with UnsafePathError
    (cred_dir / "0").unlink()
    (cred_dir / "0").symlink_to(fernet_dir / "0")
    with pytest.raises(UnsafePathError, match="Refusing symlinked key file"):
        verify_keystone_key_repositories(tmp_path)


# --- Fresh controller apply, ordering snapshots, and idempotent second run ------------


def test_fresh_controller_apply_verifies_and_second_run_skips(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("openstack_forge.execution.commands.is_host_root", lambda _: True)
    _seed_keystone_secrets(tmp_path)
    host = _make_host(tmp_path)
    executor, audit = _make_executor(tmp_path, host)
    step = KeystoneStep(tmp_path, {}, CONTROLLER, executor)

    state = StateStore(tmp_path / "state.json")
    engine = Engine(state)

    # First run: check() returns False cleanly without running any external command
    # because /etc/keystone/keystone.conf does not exist yet.
    report1 = engine.run([step])
    assert report1.ok, report1.failure
    assert report1.outcomes[0].outcome == Outcome.APPLIED

    # Verify the exact command sequence executed during apply() and verify():
    assert host.calls == [
        # apply():
        ("/usr/bin/mariadb", "--batch", "--skip-column-names"),
        ("/usr/bin/apt-get", "install", "--yes", "keystone"),
        ("/usr/bin/keystone-manage", "db_sync"),
        (
            "/usr/bin/keystone-manage",
            "fernet_setup",
            "--keystone-user",
            "keystone",
            "--keystone-group",
            "keystone",
        ),
        (
            "/usr/bin/keystone-manage",
            "credential_setup",
            "--keystone-user",
            "keystone",
            "--keystone-group",
            "keystone",
        ),
        (
            "/usr/bin/keystone-manage",
            "bootstrap",
            "--bootstrap-password",
            TEST_ADMIN_PASS,
            "--bootstrap-admin-url",
            "http://controller:5000/v3/",
            "--bootstrap-internal-url",
            "http://controller:5000/v3/",
            "--bootstrap-public-url",
            "http://controller:5000/v3/",
            "--bootstrap-region-id",
            "RegionOne",
        ),
        ("/usr/bin/systemctl", "restart", "apache2"),
        # verify():
        ("/usr/bin/dpkg-query", "--status", "keystone"),
        ("/usr/bin/systemctl", "is-active", "apache2"),
        (
            "/usr/bin/mariadb",
            "--batch",
            "--skip-column-names",
            "--execute",
            "SELECT 'DB', SCHEMA_NAME, '', '' FROM information_schema.SCHEMATA "
            "UNION ALL SELECT 'GRANT', User, Host, Db FROM mysql.db;",
        ),
    ]

    # Verify MariaDB state, key repositories, db_sync, and bootstrap state:
    assert host.mariadb_databases == {KEYSTONE_DB_NAME}
    assert host.mariadb_grants == {
        (KEYSTONE_DB_USER, "localhost", KEYSTONE_DB_NAME),
        (KEYSTONE_DB_USER, "%", KEYSTONE_DB_NAME),
    }
    assert host.mariadb_passwords == {
        (KEYSTONE_DB_USER, "localhost"): TEST_KEYSTONE_DBPASS,
        (KEYSTONE_DB_USER, "%"): TEST_KEYSTONE_DBPASS,
    }
    assert host.keystone_db_synced is True
    assert host.keystone_bootstrapped == {
        "password": TEST_ADMIN_PASS,
        "admin_url": "http://controller:5000/v3/",
        "internal_url": "http://controller:5000/v3/",
        "public_url": "http://controller:5000/v3/",
        "region_id": "RegionOne",
    }

    # Verify both config files were already edited BEFORE `systemctl restart apache2`:
    assert len(host.multi_snapshots) == 1
    event, captured = host.multi_snapshots[0]
    assert event == "restart"
    assert captured[KEYSTONE_CONF_REL] is not None
    assert verify_keystone_conf_text(captured[KEYSTONE_CONF_REL], TEST_KEYSTONE_DBPASS) == []
    assert captured[APACHE2_CONF_REL] is not None
    assert verify_apache2_conf_text(captured[APACHE2_CONF_REL]) == []

    # Neither KEYSTONE_DBPASS nor ADMIN_PASS appears in the audit log or state file:
    audit_text = audit.path.read_text(encoding="utf-8")
    state_text = state.path.read_text(encoding="utf-8")
    assert TEST_KEYSTONE_DBPASS not in audit_text
    assert TEST_ADMIN_PASS not in audit_text
    assert TEST_KEYSTONE_DBPASS not in state_text
    assert TEST_ADMIN_PASS not in state_text

    bootstrap_records = [r for r in audit.read() if r["command_id"] == KEYSTONE_MANAGE_BOOTSTRAP]
    assert len(bootstrap_records) == 2
    assert MASK in bootstrap_records[0]["argv"]
    assert TEST_ADMIN_PASS not in bootstrap_records[0]["argv"]

    # Second run: check() returns True and Engine skips apply() without changing anything.
    host.calls.clear()
    k_conf = tmp_path / KEYSTONE_CONF_REL
    a_conf = tmp_path / APACHE2_CONF_REL
    snap_before = (
        k_conf.read_bytes(),
        k_conf.stat().st_mtime_ns,
        a_conf.read_bytes(),
        a_conf.stat().st_mtime_ns,
        state.path.read_bytes(),
    )
    report2 = engine.run([step])
    assert report2.ok
    assert report2.outcomes[0].outcome == Outcome.SKIPPED
    assert host.calls == [
        ("/usr/bin/dpkg-query", "--status", "keystone"),
        ("/usr/bin/systemctl", "is-active", "apache2"),
        (
            "/usr/bin/mariadb",
            "--batch",
            "--skip-column-names",
            "--execute",
            "SELECT 'DB', SCHEMA_NAME, '', '' FROM information_schema.SCHEMATA "
            "UNION ALL SELECT 'GRANT', User, Host, Db FROM mysql.db;",
        ),
    ]
    snap_after = (
        k_conf.read_bytes(),
        k_conf.stat().st_mtime_ns,
        a_conf.read_bytes(),
        a_conf.stat().st_mtime_ns,
        state.path.read_bytes(),
    )
    assert snap_after == snap_before


def test_apply_fails_with_precondition_error_if_package_does_not_create_configs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("openstack_forge.execution.commands.is_host_root", lambda _: True)
    _seed_keystone_secrets(tmp_path)

    # 1. keystone.conf missing after install
    host1 = _make_host(tmp_path, creates={})
    executor1, _ = _make_executor(tmp_path, host1)
    step1 = KeystoneStep(tmp_path, {}, CONTROLLER, executor1)
    with pytest.raises(PreconditionError, match="/etc/keystone/keystone.conf does not exist"):
        step1.apply()
    assert ("/usr/bin/keystone-manage", "db_sync") not in host1.calls

    # 2. apache2.conf missing after install
    host2 = _make_host(
        tmp_path,
        creates={KEYSTONE_PACKAGE: (KEYSTONE_CONF_REL, DEFAULT_KEYSTONE_CONF)},
    )
    executor2, _ = _make_executor(tmp_path, host2)
    step2 = KeystoneStep(tmp_path, {}, CONTROLLER, executor2)
    with pytest.raises(PreconditionError, match="/etc/apache2/apache2.conf does not exist"):
        step2.apply()
    assert ("/usr/bin/keystone-manage", "db_sync") not in host2.calls


# --- check() and verify() failure branches --------------------------------------------


def _prepare_converged_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ServiceHost:
    monkeypatch.setattr("openstack_forge.execution.commands.is_host_root", lambda _: True)
    _seed_keystone_secrets(tmp_path)
    host = _make_host(tmp_path)
    executor, _ = _make_executor(tmp_path, host)
    step = KeystoneStep(tmp_path, {}, CONTROLLER, executor)
    step.apply()
    host.calls.clear()
    return host


def test_check_and_verify_detect_file_key_service_package_and_db_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    host = _prepare_converged_root(tmp_path, monkeypatch)
    executor, _ = _make_executor(tmp_path, host)
    step = KeystoneStep(tmp_path, {}, CONTROLLER, executor)

    assert step.check() is True
    assert step.verify().ok

    # 1. Drift in /etc/keystone/keystone.conf -> check() is pure and runs zero commands
    k_conf = tmp_path / KEYSTONE_CONF_REL
    good_k_conf = k_conf.read_text(encoding="utf-8")
    k_conf.write_text(DEFAULT_KEYSTONE_CONF, encoding="utf-8")
    host.calls.clear()
    assert step.check() is False
    assert host.calls == []
    ver_k = step.verify()
    assert not ver_k.ok
    assert "connection is" in ver_k.details
    assert TEST_KEYSTONE_DBPASS not in ver_k.details
    k_conf.write_text(good_k_conf, encoding="utf-8")

    # 2. Drift in /etc/apache2/apache2.conf -> check() is pure and runs zero commands
    a_conf = tmp_path / APACHE2_CONF_REL
    good_a_conf = a_conf.read_text(encoding="utf-8")
    a_conf.write_text(PRISTINE_APACHE2_CONF, encoding="utf-8")
    host.calls.clear()
    assert step.check() is False
    assert host.calls == []
    ver_a = step.verify()
    assert not ver_a.ok
    assert f"the managed block '{APACHE2_BLOCK_NAME}' is missing" in ver_a.details
    a_conf.write_text(good_a_conf, encoding="utf-8")

    # 3. Missing Fernet or credential key 0 -> check() is pure and runs zero commands
    fernet_key_0 = tmp_path / "etc/keystone/fernet-keys/0"
    fernet_key_0.unlink()
    host.calls.clear()
    assert step.check() is False
    assert host.calls == []
    ver_f = step.verify()
    assert not ver_f.ok
    assert f"Fernet token key repository {FERNET_KEYS_TARGET} is not initialized" in ver_f.details
    fernet_key_0.write_text("restored-key\n", encoding="utf-8")

    cred_key_0 = tmp_path / "etc/keystone/credential-keys/0"
    cred_key_0.unlink()
    host.calls.clear()
    assert step.check() is False
    assert host.calls == []
    ver_c = step.verify()
    assert not ver_c.ok
    assert (
        f"Fernet credential key repository {CREDENTIAL_KEYS_TARGET} is not initialized"
        in ver_c.details
    )
    cred_key_0.write_text("restored-key\n", encoding="utf-8")

    # 4. Package keystone not installed
    host.installed.remove(KEYSTONE_PACKAGE)
    assert step.check() is False
    ver_pkg = step.verify()
    assert not ver_pkg.ok
    assert "keystone is not installed" in ver_pkg.details
    host.installed.add(KEYSTONE_PACKAGE)

    # 5. apache2 service inactive
    host.active.remove(APACHE2_UNIT)
    assert step.check() is False
    ver_svc = step.verify()
    assert not ver_svc.ok
    assert "the apache2 service is not active" in ver_svc.details
    host.active.add(APACHE2_UNIT)

    # 6. MariaDB database or grant missing
    host.mariadb_grants.remove((KEYSTONE_DB_USER, "%", KEYSTONE_DB_NAME))
    assert step.check() is False
    ver_grant = step.verify()
    assert not ver_grant.ok
    assert "MariaDB grant for 'keystone'@'%' on database 'keystone' is missing" in ver_grant.details
    host.mariadb_grants.add((KEYSTONE_DB_USER, "%", KEYSTONE_DB_NAME))

    host.mariadb_databases.remove(KEYSTONE_DB_NAME)
    assert step.check() is False
    ver_db = step.verify()
    assert not ver_db.ok
    assert "MariaDB database 'keystone' does not exist" in ver_db.details
    host.mariadb_databases.add(KEYSTONE_DB_NAME)

    assert step.check() is True
    assert step.verify().ok


def test_mariadb_and_keystone_manage_failure_hints_attached_on_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("openstack_forge.execution.commands.is_host_root", lambda _: True)
    _seed_keystone_secrets(tmp_path)

    # 1. MariaDB failure during apply()
    host_db = _make_host(tmp_path)
    host_db.failures[("/usr/bin/mariadb", "--batch", "--skip-column-names")] = ProcessOutput(
        1, "", "ERROR 2002 (HY000): Can't connect to local server\n"
    )
    exec_db, _ = _make_executor(tmp_path, host_db)
    step_db = KeystoneStep(tmp_path, {}, CONTROLLER, exec_db)
    report_db = Engine(StateStore(tmp_path / "state-db.json")).run([step_db])
    assert not report_db.ok
    assert MARIADB_SQL_FAILURE_HINT in format_report(report_db)

    # 2. keystone-manage db_sync failure during apply()
    host_km = _make_host(tmp_path)
    host_km.failures[("/usr/bin/keystone-manage", "db_sync")] = ProcessOutput(
        1, "", "CRITICAL keystone [-] OperationalError\n"
    )
    exec_km, _ = _make_executor(tmp_path, host_km)
    step_km = KeystoneStep(tmp_path, {}, CONTROLLER, exec_km)
    report_km = Engine(StateStore(tmp_path / "state-km.json")).run([step_km])
    assert not report_km.ok
    assert KEYSTONE_MANAGE_FAILURE_HINT in format_report(report_km)


# --- dry-run preview & CLI integration ------------------------------------------------


def test_dry_run_preview_masks_secrets_and_includes_required_notes(tmp_path: Path) -> None:
    _seed_keystone_secrets(tmp_path)
    (tmp_path / KEYSTONE_CONF_REL).parent.mkdir(parents=True)
    (tmp_path / KEYSTONE_CONF_REL).write_text(DEFAULT_KEYSTONE_CONF, encoding="utf-8")
    (tmp_path / APACHE2_CONF_REL).parent.mkdir(parents=True)
    (tmp_path / APACHE2_CONF_REL).write_text(PRISTINE_APACHE2_CONF, encoding="utf-8")

    host = _make_host(tmp_path)
    executor, _ = _make_executor(tmp_path, host)
    step = KeystoneStep(tmp_path, {}, CONTROLLER, executor)

    preview = step.dry_run_preview()
    assert host.calls == []
    assert len(preview.file_changes) == 2
    for change in preview.file_changes:
        assert TEST_KEYSTONE_DBPASS not in change.diff
        assert TEST_ADMIN_PASS not in change.diff

    assert [c.command_id for c in preview.commands] == [
        MARIADB_EXECUTE_SQL,
        APT_GET_INSTALL,
        KEYSTONE_MANAGE_DB_SYNC,
        KEYSTONE_MANAGE_FERNET_SETUP,
        KEYSTONE_MANAGE_CREDENTIAL_SETUP,
        KEYSTONE_MANAGE_BOOTSTRAP,
        SYSTEMCTL_RESTART,
    ]
    assert [c.command_id for c in preview.read_only_commands] == [
        DPKG_QUERY_STATUS,
        SYSTEMCTL_IS_ACTIVE,
        MARIADB_QUERY_SQL,
    ]

    for cmd in (*preview.commands, *preview.read_only_commands):
        assert TEST_KEYSTONE_DBPASS not in cmd.describe(DEFAULT_REGISTRY)
        assert TEST_ADMIN_PASS not in cmd.describe(DEFAULT_REGISTRY)
        assert TEST_KEYSTONE_DBPASS not in " ".join(cmd.argv(DEFAULT_REGISTRY))
        assert TEST_ADMIN_PASS not in " ".join(cmd.argv(DEFAULT_REGISTRY))
        assert TEST_KEYSTONE_DBPASS not in repr(cmd)
        assert TEST_ADMIN_PASS not in repr(cmd)

    joined_notes = "\n".join(preview.notes)
    assert TEST_KEYSTONE_DBPASS not in joined_notes
    assert TEST_ADMIN_PASS not in joined_notes
    assert "stdin" in joined_notes
    assert "db_sync" in joined_notes
    assert "/proc/<pid>/cmdline" in joined_notes
    assert "keystone-users-ubuntu.html" in joined_notes


def test_cli_apply_keystone_dry_run_and_refusals(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # 1. Dry-run before `secrets init` and before packages/files exist
    rc_before = main(
        [
            "apply",
            "--config",
            str(EXAMPLE_CONFIG),
            "--step",
            "keystone",
            "--node",
            "forge-controller-01",
            "--root",
            str(tmp_path),
        ]
    )
    out_before = capsys.readouterr()
    assert rc_before == 0
    assert "Precondition not yet met" in out_before.out
    assert "openstack-forge secrets init" in out_before.out
    assert "/etc/keystone/keystone.conf does not exist yet" in out_before.out
    assert "/etc/apache2/apache2.conf does not exist yet" in out_before.out
    assert f"mysql+pymysql://keystone:{MASK}@controller/keystone" in out_before.out
    assert f"--bootstrap-password '{MASK}'" in out_before.out

    # 2. Dry-run after `secrets init` and with existing config files
    _seed_keystone_secrets(tmp_path)
    (tmp_path / KEYSTONE_CONF_REL).parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / KEYSTONE_CONF_REL).write_text(DEFAULT_KEYSTONE_CONF, encoding="utf-8")
    (tmp_path / APACHE2_CONF_REL).parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / APACHE2_CONF_REL).write_text(DEV_MACHINE_APACHE2_CONF, encoding="utf-8")

    rc_after = main(
        [
            "apply",
            "--config",
            str(EXAMPLE_CONFIG),
            "--step",
            "keystone",
            "--node",
            "forge-controller-01",
            "--root",
            str(tmp_path),
        ]
    )
    out_after = capsys.readouterr()
    assert rc_after == 0
    assert TEST_KEYSTONE_DBPASS not in out_after.out
    assert TEST_ADMIN_PASS not in out_after.out
    assert TEST_KEYSTONE_DBPASS not in out_after.err
    assert TEST_ADMIN_PASS not in out_after.err
    assert "Precondition not yet met" not in out_after.out
    assert "--- a/etc/keystone/keystone.conf" in out_after.out
    assert "--- a/etc/apache2/apache2.conf" in out_after.out
    assert f"+connection = mysql+pymysql://keystone:{MASK}@controller/keystone" in out_after.out
    assert f"+{DISABLED_LINE_PREFIX}ServerName controller" in out_after.out

    # 3. Refusal on non-'/' root with --yes
    rc_yes_alt_root = main(
        [
            "apply",
            "--config",
            str(EXAMPLE_CONFIG),
            "--step",
            "keystone",
            "--node",
            "forge-controller-01",
            "--root",
            str(tmp_path),
            "--yes",
        ]
    )
    err_alt_root = capsys.readouterr().err
    assert rc_yes_alt_root == 1
    assert "--root other than '/' is refused" in err_alt_root

    # 4. Refusal on wrong role (compute node)
    rc_wrong_role = main(
        [
            "apply",
            "--config",
            str(EXAMPLE_CONFIG),
            "--step",
            "keystone",
            "--node",
            "forge-compute-01",
            "--root",
            str(tmp_path),
        ]
    )
    err_wrong_role = capsys.readouterr().err
    assert rc_wrong_role == 1
    assert "does not apply to role 'compute'" in err_wrong_role
