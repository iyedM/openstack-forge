"""Unit tests for the ``rabbitmq`` execution step (Milestone 14)."""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from _service_fakes import ServiceHost

from openstack_forge.cli import main
from openstack_forge.execution import (
    APT_GET_INSTALL,
    DEFAULT_REGISTRY,
    DPKG_QUERY_STATUS,
    MASK,
    RABBITMQ_PACKAGE,
    RABBITMQ_PERMISSIONS,
    RABBITMQ_PORT,
    RABBITMQ_SECRET_NAME,
    RABBITMQ_STEP_ID,
    RABBITMQ_UNIT,
    RABBITMQ_USER,
    RABBITMQ_VHOST,
    RABBITMQCTL_ADD_USER,
    RABBITMQCTL_CHANGE_PASSWORD,
    RABBITMQCTL_FAILURE_HINT,
    RABBITMQCTL_LIST_USER_PERMISSIONS,
    RABBITMQCTL_LIST_USERS,
    RABBITMQCTL_SET_PERMISSIONS,
    SYSTEMCTL_IS_ACTIVE,
    AuditLog,
    CommandExecutor,
    Engine,
    ExecutionError,
    Outcome,
    PreconditionError,
    ProcessOutput,
    RabbitMQStep,
    SecretStore,
    StateStore,
    build_step,
    clear_registered_secrets,
    format_report,
    make_rabbitmq_step,
    parse_rabbitmq_user_permissions,
    parse_rabbitmq_users,
    verify_rabbitmq_permissions_text,
)
from openstack_forge.orchestration import load_catalog

REPO_ROOT = Path(__file__).resolve().parents[2]
EXAMPLE_CONFIG = REPO_ROOT / "examples" / "cluster.example.yaml"
CONTROLLER = {"hostname": "ctl-01", "role": "controller", "management_ip": "192.0.2.11"}
COMPUTE = {"hostname": "cmp-01", "role": "compute", "management_ip": "192.0.2.21"}
TEST_RABBIT_PASS = "RabbitSecretPassword1234567890AbCdEf"


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


def _make_host(root: Path, **kwargs: object) -> ServiceHost:
    return ServiceHost(root, starts={RABBITMQ_PACKAGE}, **kwargs)  # type: ignore[arg-type]


@pytest.fixture(autouse=True)
def _clean_redaction() -> Iterator[None]:
    clear_registered_secrets()
    yield
    clear_registered_secrets()


def _seed_rabbit_secret(root: Path, password: str = TEST_RABBIT_PASS) -> SecretStore:
    """Create a valid 0600 secrets.json under ``root`` with a known RABBIT_PASS."""
    secrets_dir = root / "etc" / "openstack-forge"
    secrets_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(secrets_dir, 0o700)
    secrets_file = secrets_dir / "secrets.json"
    secrets_file.write_text(json.dumps({RABBITMQ_SECRET_NAME: password}), encoding="utf-8")
    os.chmod(secrets_file, 0o600)
    store = SecretStore(root)
    store.ensure()
    return store


# --- catalog & role checks ------------------------------------------------------------


def test_rabbitmq_catalog_entry_remains_unverified() -> None:
    step = load_catalog().step(RABBITMQ_STEP_ID)
    assert step.roles == ("controller",)
    assert step.depends_on == ("openstack-packages",)
    assert step.files_modified == ()
    assert step.status == "unverified"
    assert (
        step.doc_url == "https://docs.openstack.org/install-guide/environment-messaging-ubuntu.html"
    )


@pytest.mark.parametrize("role", ["compute", "block", "object"])
def test_rabbitmq_step_rejects_non_controller_roles(tmp_path: Path, role: str) -> None:
    host = _make_host(tmp_path)
    executor, _ = _make_executor(tmp_path, host)
    node = {**COMPUTE, "hostname": f"forge-{role}-01", "role": role}
    with pytest.raises(ExecutionError, match="only runs on the controller"):
        make_rabbitmq_step(tmp_path, {}, node, executor=executor)
    with pytest.raises(ExecutionError, match="only runs on the controller"):
        build_step(RABBITMQ_STEP_ID, tmp_path, {}, node, executor=executor)


# --- parsers for rabbitmqctl output ---------------------------------------------------


def test_parse_rabbitmq_users_handles_banners_headers_and_rows() -> None:
    with_banner_and_header = (
        "Listing users ...\nuser\ttags\nguest\t[administrator]\nopenstack\t[]\n"
    )
    assert parse_rabbitmq_users(with_banner_and_header) == {"guest", "openstack"}

    no_headers = "guest\t[administrator]\n"
    assert parse_rabbitmq_users(no_headers) == {"guest"}
    assert parse_rabbitmq_users("") == set()
    assert parse_rabbitmq_users("Listing users ...\nuser\n") == set()


def test_parse_and_verify_rabbitmq_user_permissions() -> None:
    good_output = (
        'Listing permissions for user "openstack" ...\n'
        "vhost\tconfigure\twrite\tread\n"
        "/\t.*\t.*\t.*\n"
    )
    assert parse_rabbitmq_user_permissions(good_output) == {"/": (".*", ".*", ".*")}
    assert verify_rabbitmq_permissions_text(good_output) == []

    # Missing vhost "/"
    empty_output = 'Listing permissions for user "openstack" ...\nvhost\tconfigure\twrite\tread\n'
    assert verify_rabbitmq_permissions_text(empty_output) == [
        "user 'openstack' has no permissions in vhost '/'"
    ]

    # Wrong regexes in vhost "/"
    partial_output = "/\t^openstack-.*\t.*\t.*\n"
    assert verify_rabbitmq_permissions_text(partial_output) == [
        "user 'openstack' permissions in vhost '/' are '^openstack-.* .* .*', expected '.* .* .*'"
    ]


# --- Preconditions on RABBIT_PASS in SecretStore --------------------------------------


def test_missing_or_incomplete_secret_store_precondition(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("openstack_forge.execution.commands.is_host_root", lambda _: True)
    host = _make_host(tmp_path)
    executor, _ = _make_executor(tmp_path, host)
    step = RabbitMQStep(tmp_path, {}, CONTROLLER, executor)

    # 1. Store does not exist yet: check() is pure and runs zero commands.
    assert step.check() is False
    assert host.calls == []

    # apply() fails with an actionable PreconditionError and never generates secrets itself.
    with pytest.raises(PreconditionError, match="openstack-forge secrets init"):
        step.apply()
    assert host.calls == []
    assert not (tmp_path / "etc" / "openstack-forge" / "secrets.json").exists()

    # verify() reports the missing store.
    ver = step.verify()
    assert not ver.ok
    assert "openstack-forge secrets init" in ver.details
    assert f"port {RABBITMQ_PORT}" in ver.details

    # 2. Store exists with 0600 mode, but RABBIT_PASS is missing.
    secrets_dir = tmp_path / "etc" / "openstack-forge"
    secrets_dir.mkdir(parents=True, mode=0o700)
    os.chmod(secrets_dir, 0o700)
    secrets_file = secrets_dir / "secrets.json"
    secrets_file.write_text(json.dumps({"ADMIN_PASS": "A" * 32}), encoding="utf-8")
    os.chmod(secrets_file, 0o600)

    assert step.check() is False
    with pytest.raises(PreconditionError, match="RABBIT_PASS"):
        step.apply()

    # 3. Store has overly permissive mode (0644): refused by check() and apply().
    secrets_file.write_text(json.dumps({RABBITMQ_SECRET_NAME: TEST_RABBIT_PASS}), encoding="utf-8")
    os.chmod(secrets_file, 0o644)
    assert step.check() is False
    with pytest.raises(PreconditionError, match="more permissive than 0600"):
        step.apply()


# --- Fresh install, idempotence, and existing user convergence ------------------------


def test_fresh_controller_apply_verifies_and_second_run_skips(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("openstack_forge.execution.commands.is_host_root", lambda _: True)
    _seed_rabbit_secret(tmp_path)
    host = _make_host(tmp_path)
    executor, audit = _make_executor(tmp_path, host)
    step = RabbitMQStep(tmp_path, {}, CONTROLLER, executor)

    state = StateStore(tmp_path / "state.json")
    engine = Engine(state)

    # First run: installs rabbitmq-server, lists users (only 'guest'), adds 'openstack',
    # sets permissions, and verifies.
    report1 = engine.run([step])
    assert report1.ok
    assert report1.outcomes[0].outcome == Outcome.APPLIED
    assert host.installed == {RABBITMQ_PACKAGE}
    assert host.active == {RABBITMQ_UNIT}
    assert RABBITMQ_USER in host.rabbit_users
    assert host.rabbit_passwords == {RABBITMQ_USER: TEST_RABBIT_PASS}
    assert host.rabbit_permissions[RABBITMQ_USER] == {RABBITMQ_VHOST: RABBITMQ_PERMISSIONS}

    # Verify exact command sequence of the first run (no systemctl restart is run):
    assert host.calls == [
        # check() stops as soon as dpkg-query sees rabbitmq-server is not installed:
        ("/usr/bin/dpkg-query", "--status", "rabbitmq-server"),
        # apply():
        ("/usr/bin/apt-get", "install", "--yes", "rabbitmq-server"),
        ("/usr/sbin/rabbitmqctl", "list_users"),
        ("/usr/sbin/rabbitmqctl", "add_user", "openstack", TEST_RABBIT_PASS),
        ("/usr/sbin/rabbitmqctl", "set_permissions", "openstack", ".*", ".*", ".*"),
        # verify():
        ("/usr/bin/dpkg-query", "--status", "rabbitmq-server"),
        ("/usr/bin/systemctl", "is-active", "rabbitmq-server"),
        ("/usr/sbin/rabbitmqctl", "list_users"),
        ("/usr/sbin/rabbitmqctl", "list_user_permissions", "openstack"),
    ]

    # Audit log masks TEST_RABBIT_PASS with MASK:
    audit_text = audit.path.read_text(encoding="utf-8")
    assert TEST_RABBIT_PASS not in audit_text
    records = audit.read()
    add_user_records = [r for r in records if r["command_id"] == RABBITMQCTL_ADD_USER]
    assert len(add_user_records) == 2  # start and finish
    assert add_user_records[0]["argv"] == [
        "/usr/sbin/rabbitmqctl",
        "add_user",
        "openstack",
        MASK,
    ]

    # Second run: check() returns True using read-only commands only; apply() is skipped.
    host.calls.clear()
    report2 = engine.run([step])
    assert report2.ok
    assert report2.outcomes[0].outcome == Outcome.SKIPPED
    assert host.calls == [
        ("/usr/bin/dpkg-query", "--status", "rabbitmq-server"),
        ("/usr/bin/systemctl", "is-active", "rabbitmq-server"),
        ("/usr/sbin/rabbitmqctl", "list_users"),
        ("/usr/sbin/rabbitmqctl", "list_user_permissions", "openstack"),
    ]


def test_apply_converges_existing_user_with_change_password_instead_of_add_user(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When 'openstack' already exists, apply() must run change_password (add_user would fail)."""
    monkeypatch.setattr("openstack_forge.execution.commands.is_host_root", lambda _: True)
    _seed_rabbit_secret(tmp_path)
    host = _make_host(
        tmp_path,
        installed={RABBITMQ_PACKAGE},
        active={RABBITMQ_UNIT},
        rabbit_users={"guest": "[administrator]", RABBITMQ_USER: "[]"},
        # Permissions missing (for example a previous run failed before set_permissions):
        rabbit_permissions={"guest": {"/": (".*", ".*", ".*")}},
    )
    executor, audit = _make_executor(tmp_path, host)
    step = RabbitMQStep(tmp_path, {}, CONTROLLER, executor)

    assert step.check() is False
    host.calls.clear()

    step.apply()
    assert host.rabbit_passwords == {RABBITMQ_USER: TEST_RABBIT_PASS}
    assert host.rabbit_permissions[RABBITMQ_USER] == {RABBITMQ_VHOST: RABBITMQ_PERMISSIONS}
    assert host.calls == [
        ("/usr/bin/apt-get", "install", "--yes", "rabbitmq-server"),
        ("/usr/sbin/rabbitmqctl", "list_users"),
        ("/usr/sbin/rabbitmqctl", "change_password", "openstack", TEST_RABBIT_PASS),
        ("/usr/sbin/rabbitmqctl", "set_permissions", "openstack", ".*", ".*", ".*"),
    ]
    assert step.verify().ok
    assert step.check() is True

    # Audit log masks the password on change_password too:
    audit_text = audit.path.read_text(encoding="utf-8")
    assert TEST_RABBIT_PASS not in audit_text
    change_records = [r for r in audit.read() if r["command_id"] == RABBITMQCTL_CHANGE_PASSWORD]
    assert change_records[0]["argv"] == [
        "/usr/sbin/rabbitmqctl",
        "change_password",
        "openstack",
        MASK,
    ]


# --- check() and verify() failure branches --------------------------------------------


def test_check_and_verify_detect_each_missing_or_broken_component(tmp_path: Path) -> None:
    _seed_rabbit_secret(tmp_path)

    # 1. Package installed, but service inactive
    host_inactive = _make_host(
        tmp_path,
        installed={RABBITMQ_PACKAGE},
        active=set(),
        rabbit_users={"guest": "[administrator]", RABBITMQ_USER: "[]"},
        rabbit_permissions={RABBITMQ_USER: {"/": RABBITMQ_PERMISSIONS}},
    )
    exec_inactive, _ = _make_executor(tmp_path, host_inactive)
    step_inactive = RabbitMQStep(tmp_path, {}, CONTROLLER, exec_inactive)
    assert step_inactive.check() is False
    ver_inactive = step_inactive.verify()
    assert not ver_inactive.ok
    assert "the rabbitmq-server service is not active" in ver_inactive.details

    # 2. Service active, but user 'openstack' missing
    host_no_user = _make_host(
        tmp_path,
        installed={RABBITMQ_PACKAGE},
        active={RABBITMQ_UNIT},
        rabbit_users={"guest": "[administrator]"},
    )
    exec_no_user, _ = _make_executor(tmp_path, host_no_user)
    step_no_user = RabbitMQStep(tmp_path, {}, CONTROLLER, exec_no_user)
    assert step_no_user.check() is False
    ver_no_user = step_no_user.verify()
    assert not ver_no_user.ok
    assert "RabbitMQ user 'openstack' does not exist" in ver_no_user.details

    # 3. User exists, but permissions wrong
    host_bad_perms = _make_host(
        tmp_path,
        installed={RABBITMQ_PACKAGE},
        active={RABBITMQ_UNIT},
        rabbit_users={"guest": "[administrator]", RABBITMQ_USER: "[]"},
        rabbit_permissions={RABBITMQ_USER: {"/": (".*", ".*", "^read-only$")}},
    )
    exec_bad_perms, _ = _make_executor(tmp_path, host_bad_perms)
    step_bad_perms = RabbitMQStep(tmp_path, {}, CONTROLLER, exec_bad_perms)
    assert step_bad_perms.check() is False
    ver_bad_perms = step_bad_perms.verify()
    assert not ver_bad_perms.ok
    assert "expected '.* .* .*'" in ver_bad_perms.details

    # 4. rabbitmqctl list_users exits non-zero (e.g. broker unreachable due to hostname error)
    host_ctl_fail = _make_host(
        tmp_path,
        installed={RABBITMQ_PACKAGE},
        active={RABBITMQ_UNIT},
    )
    host_ctl_fail.failures[("/usr/sbin/rabbitmqctl", "list_users")] = ProcessOutput(
        69, "", "Error: unable to perform an operation on node 'rabbit@controller'."
    )
    exec_ctl_fail, _ = _make_executor(tmp_path, host_ctl_fail)
    step_ctl_fail = RabbitMQStep(tmp_path, {}, CONTROLLER, exec_ctl_fail)
    assert step_ctl_fail.check() is False
    ver_ctl_fail = step_ctl_fail.verify()
    assert not ver_ctl_fail.ok
    assert "rabbitmqctl list_users failed" in ver_ctl_fail.details
    assert "hosts-file" in ver_ctl_fail.details

    # 5. rabbitmqctl list_user_permissions exits non-zero
    host_perm_fail = _make_host(
        tmp_path,
        installed={RABBITMQ_PACKAGE},
        active={RABBITMQ_UNIT},
        rabbit_users={"guest": "[administrator]", RABBITMQ_USER: "[]"},
    )
    host_perm_fail.failures[("/usr/sbin/rabbitmqctl", "list_user_permissions", "openstack")] = (
        ProcessOutput(69, "", "Error: node unreachable")
    )
    exec_perm_fail, _ = _make_executor(tmp_path, host_perm_fail)
    step_perm_fail = RabbitMQStep(tmp_path, {}, CONTROLLER, exec_perm_fail)
    assert step_perm_fail.check() is False
    ver_perm_fail = step_perm_fail.verify()
    assert not ver_perm_fail.ok
    assert "rabbitmqctl list_user_permissions failed" in ver_perm_fail.details


def test_rabbitmqctl_failure_attaches_hostname_and_service_hint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("openstack_forge.execution.commands.is_host_root", lambda _: True)
    _seed_rabbit_secret(tmp_path)
    host = _make_host(tmp_path)
    host.failures[("/usr/sbin/rabbitmqctl", "list_users")] = ProcessOutput(
        69, "", "Error: unable to connect to node rabbit@controller: nodedown"
    )
    executor, _ = _make_executor(tmp_path, host)
    step = RabbitMQStep(tmp_path, {}, CONTROLLER, executor)
    state = StateStore(tmp_path / "state.json")
    report = Engine(state).run([step])
    assert not report.ok
    text = format_report(report)
    assert "hosts-file" in text
    assert RABBITMQCTL_FAILURE_HINT in text


# --- dry-run preview & CLI integration ------------------------------------------------


def test_dry_run_preview_masks_secret_and_includes_required_notes(tmp_path: Path) -> None:
    _seed_rabbit_secret(tmp_path)
    host = _make_host(tmp_path)
    executor, _ = _make_executor(tmp_path, host)
    step = RabbitMQStep(tmp_path, {}, CONTROLLER, executor)

    preview = step.dry_run_preview()
    assert host.calls == []  # dry_run_preview never runs commands
    assert preview.file_changes == ()
    assert [c.command_id for c in preview.commands] == [
        APT_GET_INSTALL,
        RABBITMQCTL_ADD_USER,
        RABBITMQCTL_SET_PERMISSIONS,
    ]
    assert [c.command_id for c in preview.read_only_commands] == [
        DPKG_QUERY_STATUS,
        SYSTEMCTL_IS_ACTIVE,
        RABBITMQCTL_LIST_USERS,
        RABBITMQCTL_LIST_USER_PERMISSIONS,
    ]

    # Password is masked in argv and describe()
    add_cmd = preview.commands[1]
    assert add_cmd.argv(DEFAULT_REGISTRY) == (
        "/usr/sbin/rabbitmqctl",
        "add_user",
        "openstack",
        MASK,
    )
    assert TEST_RABBIT_PASS not in add_cmd.describe(DEFAULT_REGISTRY)
    assert TEST_RABBIT_PASS not in repr(add_cmd)

    # Required notes in dry-run preview:
    joined_notes = "\n".join(preview.notes)
    assert TEST_RABBIT_PASS not in joined_notes
    assert "/proc/<pid>/cmdline" in joined_notes and "ps" in joined_notes
    assert "hosts-file" in joined_notes
    assert "change_password" in joined_notes
    assert "5672" in joined_notes and "without TLS" in joined_notes
    assert "unconfirmed on a live broker" in joined_notes


def test_cli_apply_rabbitmq_dry_run_and_refusals(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # 1. Dry-run before `secrets init` still succeeds and prints a precondition note + masked argv
    rc_before = main(
        [
            "apply",
            "--config",
            str(EXAMPLE_CONFIG),
            "--step",
            "rabbitmq",
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
    assert f"/usr/sbin/rabbitmqctl add_user openstack '{MASK}'" in out_before.out
    assert "/usr/sbin/rabbitmqctl set_permissions openstack '.*' '.*' '.*'" in out_before.out

    # 2. Dry-run after `secrets init` masks RABBIT_PASS completely
    _seed_rabbit_secret(tmp_path)
    rc_after = main(
        [
            "apply",
            "--config",
            str(EXAMPLE_CONFIG),
            "--step",
            "rabbitmq",
            "--node",
            "forge-controller-01",
            "--root",
            str(tmp_path),
        ]
    )
    out_after = capsys.readouterr()
    assert rc_after == 0
    assert TEST_RABBIT_PASS not in out_after.out
    assert TEST_RABBIT_PASS not in out_after.err
    assert "Precondition not yet met" not in out_after.out
    assert f"/usr/sbin/rabbitmqctl add_user openstack '{MASK}'" in out_after.out
    assert "/proc/<pid>/cmdline" in out_after.out
    assert "hosts-file" in out_after.out

    # 3. Refusal on non-'/' root with --yes (command-declaring step)
    rc_yes_alt_root = main(
        [
            "apply",
            "--config",
            str(EXAMPLE_CONFIG),
            "--step",
            "rabbitmq",
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
            "rabbitmq",
            "--node",
            "forge-compute-01",
            "--root",
            str(tmp_path),
        ]
    )
    err_wrong_role = capsys.readouterr().err
    assert rc_wrong_role == 1
    assert "does not apply to role 'compute'" in err_wrong_role
