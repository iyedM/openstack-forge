"""Unit tests for the reusable ``/usr/bin/openstack`` CLI brick (`openstack_cli.py`)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from openstack_forge.execution import (
    DEFAULT_REGISTRY,
    MASK,
    OPENSTACK_ADMIN_AUTH_ARGS,
    OPENSTACK_CLI_FAILURE_HINT,
    OPENSTACK_ENDPOINT_CREATE,
    OPENSTACK_ENDPOINT_LIST,
    OPENSTACK_PROJECT_CREATE_SERVICE,
    OPENSTACK_PROJECT_LIST,
    OPENSTACK_ROLE_ADD_USER_PROJECT,
    OPENSTACK_ROLE_ASSIGNMENT_LIST,
    OPENSTACK_SERVICE_CREATE,
    OPENSTACK_SERVICE_LIST,
    OPENSTACK_USER_CREATE,
    OPENSTACK_USER_LIST,
    OPENSTACK_USER_SET_PASSWORD,
    AuditLog,
    CommandExecutor,
    EndpointRecord,
    ExecutionError,
    ParameterError,
    PreconditionError,
    ProcessOutput,
    ProjectRecord,
    RoleAssignmentRecord,
    ServiceRecord,
    UnexpectedExitError,
    UserRecord,
    clear_registered_secrets,
    ensure_service_catalog,
    ensure_service_project,
    ensure_service_user,
    parse_endpoint_list,
    parse_project_list,
    parse_role_assignment_list,
    parse_service_list,
    parse_user_list,
    plan_create_service_project,
    plan_project_list,
    register_secret,
    service_catalog_problems,
    service_catalog_ready,
    service_project_problems,
    service_project_ready,
    service_user_problems,
    service_user_ready,
    verify_project_present,
    verify_service_and_endpoints,
    verify_service_user_and_role,
)
from tests.unit._service_fakes import ServiceHost

ADMIN_PASS = "AdminSecretPass1234567890ABCDEFGH"
GLANCE_PASS = "GlanceSecretPass1234567890ABCDEFG"
GLANCE_PASS_2 = "GlanceRotatedPass1234567890ABCDEF"


@pytest.fixture(autouse=True)
def _clean_redaction_registry():
    clear_registered_secrets()
    yield
    clear_registered_secrets()


def _host_executor(
    tmp_path: Path, host: ServiceHost, *, dry_run: bool = False
) -> tuple[CommandExecutor, AuditLog]:
    (tmp_path / "var/lib/openstack-forge").mkdir(parents=True, exist_ok=True)
    audit = AuditLog(tmp_path / "var/lib/openstack-forge/commands.log")
    executor = CommandExecutor(
        DEFAULT_REGISTRY,
        root=tmp_path,
        audit_log=audit,
        backend=host,
        dry_run=dry_run,
        echo=lambda _msg: None,
        euid=lambda: 0,
    )
    executor._refuse_unsafe_mutation = lambda _spec: None  # type: ignore[method-assign]
    return executor, audit


# --- CommandSpec env_params and argv masking -------------------------------------------


def test_keystone_bootstrap_and_openstack_specs_pass_admin_password_via_env_only() -> None:
    bootstrap = DEFAULT_REGISTRY.get("keystone-manage.bootstrap")
    assert bootstrap.env_params == {"password": "OS_BOOTSTRAP_PASSWORD"}
    assert "--bootstrap-password" not in bootstrap.argv
    argv = bootstrap.build_argv({"password": ADMIN_PASS})
    assert ADMIN_PASS not in " ".join(argv)
    assert bootstrap.build_env({"password": ADMIN_PASS})["OS_BOOTSTRAP_PASSWORD"] == ADMIN_PASS
    assert bootstrap.build_shown_env({"password": ADMIN_PASS})["OS_BOOTSTRAP_PASSWORD"] == MASK

    for spec_id in (
        OPENSTACK_PROJECT_LIST,
        OPENSTACK_PROJECT_CREATE_SERVICE,
        OPENSTACK_USER_LIST,
        OPENSTACK_USER_CREATE,
        OPENSTACK_USER_SET_PASSWORD,
        OPENSTACK_ROLE_ASSIGNMENT_LIST,
        OPENSTACK_ROLE_ADD_USER_PROJECT,
        OPENSTACK_SERVICE_LIST,
        OPENSTACK_SERVICE_CREATE,
        OPENSTACK_ENDPOINT_LIST,
        OPENSTACK_ENDPOINT_CREATE,
    ):
        spec = DEFAULT_REGISTRY.get(spec_id)
        assert spec.argv[: len(OPENSTACK_ADMIN_AUTH_ARGS)] == OPENSTACK_ADMIN_AUTH_ARGS
        assert spec.env_params == {"admin_password": "OS_PASSWORD"}
        assert spec.failure_hint == OPENSTACK_CLI_FAILURE_HINT


def test_openstack_user_specs_mask_both_admin_and_user_passwords() -> None:
    for spec_id in (OPENSTACK_USER_CREATE, OPENSTACK_USER_SET_PASSWORD):
        spec = DEFAULT_REGISTRY.get(spec_id)
        params = {
            "admin_password": ADMIN_PASS,
            "user": "glance",
            "user_password": GLANCE_PASS,
        }
        real_argv = spec.build_argv(params)
        shown_argv = spec.build_shown_argv(params)
        env = spec.build_env(params)
        shown_env = spec.build_shown_env(params)

        assert ADMIN_PASS not in " ".join(real_argv)
        assert GLANCE_PASS in real_argv
        assert GLANCE_PASS not in " ".join(shown_argv)
        assert ADMIN_PASS not in " ".join(shown_argv)
        assert MASK in shown_argv
        assert env["OS_PASSWORD"] == ADMIN_PASS
        assert shown_env["OS_PASSWORD"] == MASK


def test_planned_commands_mask_admin_password() -> None:
    p_list = plan_project_list(ADMIN_PASS)
    p_create = plan_create_service_project(ADMIN_PASS)
    for cmd in (p_list, p_create):
        assert ADMIN_PASS not in repr(cmd)
        assert ADMIN_PASS not in cmd.describe(DEFAULT_REGISTRY)


# --- Pure JSON parsers -----------------------------------------------------------------


def test_parse_project_and_user_lists_accept_upper_and_lowercase_keys() -> None:
    projects = parse_project_list(
        json.dumps([{"ID": "p1", "Name": "admin"}, {"id": "p2", "name": "service"}])
    )
    assert projects == (
        ProjectRecord(id="p1", name="admin"),
        ProjectRecord(id="p2", name="service"),
    )

    users = parse_user_list(
        json.dumps([{"ID": "u1", "Name": "admin"}, {"id": "u2", "name": "glance"}])
    )
    assert users == (
        UserRecord(id="u1", name="admin"),
        UserRecord(id="u2", name="glance"),
    )


def test_parse_role_assignment_list_normalizes_domain_suffixes() -> None:
    raw = json.dumps(
        [
            {
                "Role": "admin",
                "User": "glance@Default",
                "Group": "",
                "Project": "service@Default",
                "Domain": "",
                "System": "",
                "Inherited": False,
            },
            {
                "role": "admin",
                "user": "placement",
                "project": "service",
            },
            {
                "Role": "member",
                "User": "alice@Example",
                "Project": "demo@Example",
            },
        ]
    )
    records = parse_role_assignment_list(raw)
    assert records == (
        RoleAssignmentRecord(
            role="admin",
            user="glance",
            project="service",
            user_domain="default",
            project_domain="default",
        ),
        RoleAssignmentRecord(
            role="admin",
            user="placement",
            project="service",
            user_domain="default",
            project_domain="default",
        ),
        RoleAssignmentRecord(
            role="member",
            user="alice",
            project="demo",
            user_domain="example",
            project_domain="example",
        ),
    )


def test_parse_service_and_endpoint_lists() -> None:
    services = parse_service_list(
        json.dumps(
            [
                {"ID": "s1", "Name": "keystone", "Type": "identity"},
                {"id": "s2", "name": "glance", "type": "image"},
                {"ID": "s3", "Name": None, "Type": "compute"},
            ]
        )
    )
    assert services == (
        ServiceRecord(id="s1", name="keystone", type="identity"),
        ServiceRecord(id="s2", name="glance", type="image"),
        ServiceRecord(id="s3", name="", type="compute"),
    )

    endpoints = parse_endpoint_list(
        json.dumps(
            [
                {
                    "ID": "e1",
                    "Region": "RegionOne",
                    "Service Name": "glance",
                    "Service Type": "image",
                    "Enabled": True,
                    "Interface": "Public",
                    "URL": "http://controller:9292",
                },
                {
                    "id": "e2",
                    "region_id": "RegionOne",
                    "service_name": "glance",
                    "service_type": "image",
                    "enabled": False,
                    "interface": "admin",
                    "url": "http://controller:9292",
                },
            ]
        )
    )
    assert endpoints == (
        EndpointRecord(
            id="e1",
            region="RegionOne",
            service_name="glance",
            service_type="image",
            enabled=True,
            interface="public",
            url="http://controller:9292",
        ),
        EndpointRecord(
            id="e2",
            region="RegionOne",
            service_name="glance",
            service_type="image",
            enabled=False,
            interface="admin",
            url="http://controller:9292",
        ),
    )


def test_parsers_reject_invalid_json_and_redact_secrets() -> None:
    register_secret(ADMIN_PASS)
    with pytest.raises(ExecutionError, match="Unexpected non-JSON output") as exc_info:
        parse_project_list(f"ERROR: password={ADMIN_PASS}")
    assert ADMIN_PASS not in str(exc_info.value)

    with pytest.raises(ExecutionError, match="expected a JSON list of objects"):
        parse_user_list('{"not": "a list"}')

    with pytest.raises(ExecutionError, match="Missing or invalid 'ID'"):
        parse_service_list('[{"Name": "glance", "Type": "image"}]')

    with pytest.raises(ExecutionError, match="Invalid 'Enabled' field"):
        parse_endpoint_list(
            json.dumps(
                [
                    {
                        "ID": "e1",
                        "Region": "RegionOne",
                        "Service Name": "glance",
                        "Service Type": "image",
                        "Enabled": "yes",
                        "Interface": "public",
                        "URL": "http://controller:9292",
                    }
                ]
            )
        )


# --- Pure verification helpers ---------------------------------------------------------


def test_verify_project_present_detects_missing_and_duplicate() -> None:
    assert verify_project_present((ProjectRecord("p1", "service"),)) == []
    assert verify_project_present(()) == [
        "Keystone project 'service' (domain 'default') does not exist"
    ]
    assert verify_project_present(
        (ProjectRecord("p1", "service"), ProjectRecord("p2", "service"))
    ) == ["duplicate Keystone project entries for 'service' in domain 'default' (found 2)"]


def test_verify_service_user_and_role_detects_missing_and_duplicate() -> None:
    users = (UserRecord("u1", "glance"),)
    roles = (RoleAssignmentRecord(role="admin", user="glance", project="service"),)
    assert verify_service_user_and_role(users, roles, "glance") == []

    missing_problems = verify_service_user_and_role((), (), "glance")
    assert len(missing_problems) == 2
    assert "Keystone user 'glance' (domain 'default') does not exist" in missing_problems
    assert (
        "Keystone user 'glance' does not have role 'admin' on project 'service'" in missing_problems
    )

    dup_users = (UserRecord("u1", "glance"), UserRecord("u2", "glance"))
    assert "duplicate Keystone user entries for 'glance'" in "; ".join(
        verify_service_user_and_role(dup_users, roles, "glance")
    )


def test_verify_service_and_endpoints_detects_duplicates_wrong_url_and_disabled() -> None:
    svc = ServiceRecord("s1", "glance", "image")
    eps = tuple(
        EndpointRecord(
            id=f"e-{iface}",
            region="RegionOne",
            service_name="glance",
            service_type="image",
            enabled=True,
            interface=iface,
            url="http://controller:9292",
        )
        for iface in ("public", "internal", "admin")
    )
    assert (
        verify_service_and_endpoints((svc,), eps, "glance", "image", "http://controller:9292") == []
    )

    # Duplicate service
    dup_svc_problems = verify_service_and_endpoints(
        (svc, ServiceRecord("s2", "glance", "image")),
        eps,
        "glance",
        "image",
        "http://controller:9292",
    )
    assert any("duplicate Keystone service entries for 'glance'" in p for p in dup_svc_problems)

    # Wrong service type
    wrong_type_problems = verify_service_and_endpoints(
        (ServiceRecord("s1", "glance", "compute"),),
        eps,
        "glance",
        "image",
        "http://controller:9292",
    )
    assert any("expected 'image'" in p for p in wrong_type_problems)

    # Duplicate public endpoint, disabled internal endpoint, wrong admin URL
    bad_eps = (
        eps[0],
        EndpointRecord(
            id="e-pub-2",
            region="RegionOne",
            service_name="glance",
            service_type="image",
            enabled=True,
            interface="public",
            url="http://controller:9292",
        ),
        EndpointRecord(
            id="e-internal",
            region="RegionOne",
            service_name="glance",
            service_type="image",
            enabled=False,
            interface="internal",
            url="http://controller:9292",
        ),
        EndpointRecord(
            id="e-admin",
            region="RegionOne",
            service_name="glance",
            service_type="image",
            enabled=True,
            interface="admin",
            url="http://controller:9999",
        ),
    )
    bad_ep_problems = verify_service_and_endpoints(
        (svc,), bad_eps, "glance", "image", "http://controller:9292"
    )
    joined = "; ".join(bad_ep_problems)
    assert "duplicate Keystone public endpoints for 'glance'" in joined
    assert "Keystone internal endpoint for 'glance' is disabled" in joined
    assert "Keystone admin endpoint for 'glance' has URL 'http://controller:9999'" in joined


# --- Executor-backed idempotent convergence helpers ------------------------------------


def test_ensure_service_project_is_idempotent_and_rejects_duplicates(tmp_path: Path) -> None:
    host = ServiceHost(tmp_path)
    executor, _audit = _host_executor(tmp_path, host)

    assert service_project_ready(executor, ADMIN_PASS) is False
    assert service_project_problems(executor, ADMIN_PASS) == [
        "Keystone project 'service' (domain 'default') does not exist"
    ]

    created = ensure_service_project(executor, ADMIN_PASS)
    assert created is True
    assert service_project_ready(executor, ADMIN_PASS) is True
    assert service_project_problems(executor, ADMIN_PASS) == []

    # Second call inspects project list and does NOT call project create again
    calls_before = len(host.calls)
    created_again = ensure_service_project(executor, ADMIN_PASS)
    assert created_again is False
    assert len(host.calls) == calls_before + 1
    assert host.calls[-1][-5:] == ("project", "list", "--domain", "default", "-f", "json")[1:]
    assert [p["Name"] for p in host.openstack_projects].count("service") == 1

    # Duplicate project raises PreconditionError
    host.openstack_projects.append({"ID": "proj-dup", "Name": "service"})
    with pytest.raises(PreconditionError, match="Duplicate Keystone project entries"):
        ensure_service_project(executor, ADMIN_PASS)


def test_ensure_service_user_creates_then_updates_password_idempotently(tmp_path: Path) -> None:
    host = ServiceHost(
        tmp_path,
        openstack_projects=[{"ID": "proj-service", "Name": "service"}],
    )
    executor, audit = _host_executor(tmp_path, host)

    assert service_user_ready(executor, ADMIN_PASS, "glance") is False
    assert len(service_user_problems(executor, ADMIN_PASS, "glance")) == 2

    ensure_service_user(executor, ADMIN_PASS, "glance", GLANCE_PASS)
    assert host.openstack_user_passwords["glance"] == GLANCE_PASS
    assert service_user_ready(executor, ADMIN_PASS, "glance") is True
    assert service_user_problems(executor, ADMIN_PASS, "glance") == []

    # Second run with rotated password runs `user set --password` and skips `role add`
    host.calls.clear()
    ensure_service_user(executor, ADMIN_PASS, "glance", GLANCE_PASS_2)
    assert host.openstack_user_passwords["glance"] == GLANCE_PASS_2
    sub_calls = [call[len(OPENSTACK_ADMIN_AUTH_ARGS) :] for call in host.calls]
    assert sub_calls == [
        ("user", "list", "--domain", "default", "-f", "json"),
        ("user", "set", "--domain", "default", "--password", GLANCE_PASS_2, "glance"),
        ("role", "assignment", "list", "--names", "-f", "json"),
    ]

    # Audit log masks both ADMIN_PASS and GLANCE_PASS / GLANCE_PASS_2
    log_text = audit.path.read_text(encoding="utf-8")
    assert ADMIN_PASS not in log_text
    assert GLANCE_PASS not in log_text
    assert GLANCE_PASS_2 not in log_text


def test_ensure_service_catalog_is_idempotent_and_never_creates_duplicates(
    tmp_path: Path,
) -> None:
    host = ServiceHost(tmp_path)
    executor, _audit = _host_executor(tmp_path, host)

    assert (
        service_catalog_ready(executor, ADMIN_PASS, "glance", "image", "http://controller:9292")
        is False
    )
    assert (
        len(
            service_catalog_problems(
                executor, ADMIN_PASS, "glance", "image", "http://controller:9292"
            )
        )
        == 4
    )

    ensure_service_catalog(executor, ADMIN_PASS, "glance", "image", "http://controller:9292")
    assert len(host.openstack_services) == 1
    assert len(host.openstack_endpoints) == 3
    assert (
        service_catalog_ready(executor, ADMIN_PASS, "glance", "image", "http://controller:9292")
        is True
    )
    assert (
        service_catalog_problems(executor, ADMIN_PASS, "glance", "image", "http://controller:9292")
        == []
    )

    # Second call must ONLY run service list and endpoint list — zero creates!
    host.calls.clear()
    ensure_service_catalog(executor, ADMIN_PASS, "glance", "image", "http://controller:9292")
    sub_calls = [call[len(OPENSTACK_ADMIN_AUTH_ARGS) :] for call in host.calls]
    assert sub_calls == [
        ("service", "list", "-f", "json"),
        ("endpoint", "list", "-f", "json"),
    ]
    assert len(host.openstack_services) == 1
    assert len(host.openstack_endpoints) == 3


def test_ensure_service_catalog_creates_only_missing_endpoint_and_rejects_mismatch(
    tmp_path: Path,
) -> None:
    host = ServiceHost(
        tmp_path,
        openstack_services=[{"ID": "s1", "Name": "glance", "Type": "image"}],
        openstack_endpoints=[
            {
                "ID": "ep-pub",
                "Region": "RegionOne",
                "Service Name": "glance",
                "Service Type": "image",
                "Enabled": True,
                "Interface": "public",
                "URL": "http://controller:9292",
            },
            {
                "ID": "ep-int",
                "Region": "RegionOne",
                "Service Name": "glance",
                "Service Type": "image",
                "Enabled": True,
                "Interface": "internal",
                "URL": "http://controller:9292",
            },
        ],
    )
    executor, _audit = _host_executor(tmp_path, host)

    ensure_service_catalog(executor, ADMIN_PASS, "glance", "image", "http://controller:9292")
    sub_calls = [call[len(OPENSTACK_ADMIN_AUTH_ARGS) :] for call in host.calls]
    assert sub_calls == [
        ("service", "list", "-f", "json"),
        ("endpoint", "list", "-f", "json"),
        (
            "endpoint",
            "create",
            "--region",
            "RegionOne",
            "glance",
            "admin",
            "http://controller:9292",
            "-f",
            "json",
        ),
    ]
    assert len(host.openstack_services) == 1
    assert len(host.openstack_endpoints) == 3

    # Mismatched URL on existing endpoint raises PreconditionError instead of duplicating
    with pytest.raises(PreconditionError, match="does not match expected state"):
        ensure_service_catalog(executor, ADMIN_PASS, "glance", "image", "http://controller:9999")


def test_openstack_cli_command_failure_attaches_hint_and_redacts_password(
    tmp_path: Path,
) -> None:
    register_secret(ADMIN_PASS)
    host = ServiceHost(tmp_path)
    host.failures[
        (*OPENSTACK_ADMIN_AUTH_ARGS, "project", "list", "--domain", "default", "-f", "json")
    ] = ProcessOutput(1, "", f"HTTP 500 error (OS_PASSWORD={ADMIN_PASS})\n")
    executor, _audit = _host_executor(tmp_path, host)

    assert service_project_ready(executor, ADMIN_PASS) is False
    problems = service_project_problems(executor, ADMIN_PASS)
    assert len(problems) == 1
    assert ADMIN_PASS not in problems[0]

    with pytest.raises(UnexpectedExitError) as exc_info:
        ensure_service_project(executor, ADMIN_PASS)
    assert exc_info.value.hint == OPENSTACK_CLI_FAILURE_HINT
    assert ADMIN_PASS not in str(exc_info.value)


def test_openstack_cli_validates_parameters_before_running(tmp_path: Path) -> None:
    host = ServiceHost(tmp_path)
    executor, _audit = _host_executor(tmp_path, host)

    with pytest.raises(ParameterError):
        ensure_service_user(executor, ADMIN_PASS, "InvalidUser", GLANCE_PASS)
    with pytest.raises(ParameterError):
        ensure_service_catalog(executor, ADMIN_PASS, "glance", "image", "http://controller:9292;id")
    assert host.calls == []
