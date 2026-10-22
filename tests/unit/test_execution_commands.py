"""Tests for allowlisted command specs, parameter validation and the command executor.

Every test uses a fake backend except the two marked REAL below, which run harmless
``sys.executable`` one-liners. The fake specs point at non-existent paths under
/opt/forge-test/bin, so even an accidental real run could not do anything.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from collections.abc import Mapping
from pathlib import Path

import pytest

from openstack_forge.execution import (
    DEFAULT_REGISTRY,
    MASK,
    OPENSTACK_ADMIN_AUTH_ARGS,
    PARAM_TYPES,
    SANITIZED_ENV,
    AuditLog,
    CommandError,
    CommandExecutor,
    CommandNotFoundError,
    CommandRegistry,
    CommandSpec,
    CommandSpecError,
    CommandTimeoutError,
    ParameterError,
    ProcessOutput,
    SubprocessBackend,
    UnexpectedExitError,
    UnknownCommandError,
    command_succeeds,
    validate_parameter,
)

TOOL = "/opt/forge-test/bin/unit-tool"
PKG = "/opt/forge-test/bin/pkg-tool"


class FakeBackend:
    """Records every argv it is asked to run and returns canned output; runs nothing."""

    def __init__(self, default: ProcessOutput | Exception | None = None) -> None:
        self.default = default or ProcessOutput(0, "", "")
        self.responses: dict[tuple[str, ...], ProcessOutput | Exception] = {}
        self.calls: list[tuple[tuple[str, ...], dict[str, str], float]] = []

    def run(
        self, argv: tuple[str, ...], *, env: Mapping[str, str], timeout: float
    ) -> ProcessOutput:
        self.calls.append((argv, dict(env), timeout))
        response = self.responses.get(argv, self.default)
        if isinstance(response, Exception):
            raise response
        return response


def _registry() -> CommandRegistry:
    return CommandRegistry(
        [
            CommandSpec(
                "test.restart-unit",
                (TOOL, "restart", "{unit}"),
                {"unit": "systemd_unit"},
                timeout=30,
                mutates=True,
            ),
            CommandSpec(
                "test.install-package",
                (PKG, "install", "--yes", "{package}"),
                {"package": "package_name"},
                timeout=600,
                mutates=True,
            ),
            CommandSpec(
                "test.unit-active",
                (TOOL, "is-active", "{unit}"),
                {"unit": "systemd_unit"},
                timeout=10,
                allowed_exit_codes={0, 3},
                mutates=False,
            ),
            CommandSpec("test.status", (TOOL, "status"), timeout=5, mutates=False),
        ]
    )


@pytest.fixture
def backend() -> FakeBackend:
    return FakeBackend()


@pytest.fixture
def audit(tmp_path: Path) -> AuditLog:
    return AuditLog(tmp_path / "commands.log")


@pytest.fixture
def executor(tmp_path: Path, backend: FakeBackend, audit: AuditLog) -> CommandExecutor:
    """Executor against a temporary root: read-only commands only."""
    return CommandExecutor(_registry(), root=tmp_path, audit_log=audit, backend=backend)


# --- parameter types ---------------------------------------------------------------

VALID = {
    "systemd_unit": [
        "chrony",
        "chrony.service",
        "openstack-nova-api.service",
        "getty@tty1.service",
    ],
    "package_name": ["chrony", "python3-openstackclient", "libstdc++6", "mariadb-server"],
    "cloud_archive_pocket": ["cloud-archive:epoxy", "cloud-archive:epoxy-updates"],
    "hostname": ["ctl-01", "forge-compute-02", "a"],
    "ipv4_address": ["192.0.2.11", "198.51.100.1", "255.255.255.255"],
    "absolute_path": ["/etc/hosts", "/etc/chrony/chrony.conf", "/var/lib/openstack-forge"],
    "secret": ["A" * 32, "aB3x9K" * 6, "9" * 64],
    "openstack_name": ["service", "admin", "glance", "placement", "nova", "cinderv3", "volumev3"],
    "endpoint_interface": ["public", "internal", "admin"],
    "endpoint_url": [
        "http://controller:5000/v3/",
        "http://controller:9292",
        "http://controller:8778",
        "http://controller:8774/v2.1",
        "https://controller.example.test:9696",
        "http://controller:8776/v3/%(project_id)s",
    ],
    "device_path": [
        "/dev/sdb",
        "/dev/vdb",
        "/dev/nvme0n1",
    ],
}

# Shell metacharacters and option injection, rejected for every parameter type.
INJECTIONS = [
    "x; rm -rf /",
    "x;id",
    "x|cat",
    "x||true",
    "x&",
    "x&&id",
    "$(id)",
    "${HOME}",
    "$HOME",
    "`id`",
    "x\nid",
    "x\rid",
    "x id",
    "x\tid",
    "'x'",
    '"x"',
    "-x",
    "--help",
    "x\x00",
    "x>/etc/passwd",
    "x<in",
    "x*",
    "x\\n",
    "",
]

INVALID_FORMAT = {
    "systemd_unit": ["chrony/../x", ".hidden", "a" * 300],
    "package_name": ["Python3", "a", "pkg_name", "pkg:amd64", "pkg=1.0"],
    "cloud_archive_pocket": ["cloud-archive:Epoxy", "ppa:owner/name", "cloud-archive:", "epoxy"],
    "hostname": ["ctl_01", "ctl.example.com", "ctl-", "a" * 64],
    "ipv4_address": ["256.1.1.1", "1.2.3", "01.2.3.4.5", "1.2.3.4/24", "::1"],
    "absolute_path": ["etc/hosts", "/etc/../shadow", "/etc/./hosts", "/", "/etc//hosts"],
    "secret": ["A" * 31, "A" * 257, "a" * 31 + "_", "a" * 31 + "-"],
    "openstack_name": ["Service", "_bad", "0bad", "a" * 64, "bad.name"],
    "endpoint_interface": ["Public", "external", "private", "v3"],
    "endpoint_url": [
        "ftp://controller:9292",
        "http://controller",
        "http://controller:0",
        "http://controller:70000",
        "http://controller:9292/../x",
        "http://controller:9292/./x",
        "http://-bad:9292",
        "http://bad..host:9292",
        "http://controller:8776/v3/%(other)s",
        "http://controller:8776/v3/%s",
    ],
    "device_path": [
        "sdb",
        "/dev/",
        "/etc/passwd",
        "/dev/../etc/passwd",
        "/dev//sdb",
    ],
}


@pytest.mark.parametrize(("type_name", "value"), [(t, v) for t, vs in VALID.items() for v in vs])
def test_valid_parameters(type_name: str, value: str) -> None:
    assert validate_parameter(type_name, "p", value) == value


@pytest.mark.parametrize("type_name", sorted(set(PARAM_TYPES) - {"sql_stdin"}))
@pytest.mark.parametrize("value", INJECTIONS)
def test_injection_attempts_are_rejected_for_every_type(type_name: str, value: str) -> None:
    with pytest.raises(ParameterError):
        validate_parameter(type_name, "p", value)


@pytest.mark.parametrize(
    ("type_name", "value"), [(t, v) for t, vs in INVALID_FORMAT.items() for v in vs]
)
def test_values_of_the_wrong_format_are_rejected(type_name: str, value: str) -> None:
    with pytest.raises(ParameterError):
        validate_parameter(type_name, "p", value)


@pytest.mark.parametrize("value", [123, None, ["chrony"], b"chrony"])
def test_non_string_parameters_are_rejected(value: object) -> None:
    with pytest.raises(ParameterError, match="must be a string"):
        validate_parameter("systemd_unit", "unit", value)


def test_parameter_error_messages_name_the_problem() -> None:
    with pytest.raises(ParameterError, match="must not start with '-'"):
        validate_parameter("package_name", "package", "--force-yes")
    with pytest.raises(ParameterError, match=r"forbidden character\(s\) ';'"):
        validate_parameter("package_name", "package", "chrony;id")
    with pytest.raises(ParameterError, match="whitespace or control"):
        validate_parameter("package_name", "package", "chrony id")


# --- specifications and registry ------------------------------------------------------


@pytest.mark.parametrize(
    ("argv", "params", "match"),
    [
        ((), {}, "empty argv"),
        (("unit-tool", "status"), {}, "literal absolute path"),
        (("{tool}", "status"), {"tool": "absolute_path"}, "literal absolute path"),
        (("/bin/sh", "-c", "id"), {}, "not allowed"),
        (("/usr/bin/bash", "-c", "id"), {}, "not allowed"),
        (("/usr/bin/env", "id"), {}, "not allowed"),
        (("/usr/bin/sudo", "id"), {}, "not allowed"),
        (("/usr/bin/xargs", "id"), {}, "not allowed"),
        ((TOOL, "/usr/bin/sudo", "id"), {}, "sudo or su"),
        ((TOOL, "--unit={unit}"), {"unit": "systemd_unit"}, "whole arguments"),
        ((TOOL, "{unit}"), {}, "exactly once"),
        ((TOOL, "status"), {"unit": "systemd_unit"}, "exactly once"),
        ((TOOL, "{unit}", "{unit}"), {"unit": "systemd_unit"}, "exactly once"),
        ((TOOL, "{unit}"), {"unit": "free_text"}, "unknown parameter type"),
        ((TOOL, "a\x00b"), {}, "NUL"),
    ],
)
def test_invalid_specs_are_rejected(argv, params, match: str) -> None:
    with pytest.raises(CommandSpecError, match=match):
        CommandSpec("test.bad", argv, params)


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"id": "Bad Id"}, "Invalid command id"),
        ({"timeout": 0}, "timeout"),
        ({"allowed_exit_codes": set()}, "exit codes"),
        ({"allowed_exit_codes": {256}}, "exit codes"),
    ],
)
def test_invalid_spec_settings(kwargs: dict, match: str) -> None:
    settings = {"id": "test.ok", "argv": (TOOL, "status"), **kwargs}
    with pytest.raises(CommandSpecError, match=match):
        CommandSpec(**settings)


def test_unknown_command_id(executor: CommandExecutor, backend: FakeBackend, audit) -> None:
    with pytest.raises(UnknownCommandError, match="'apt-get' is not in the allowlist"):
        executor.run("apt-get", {"package": "chrony"})
    assert backend.calls == []
    assert audit.read() == []


def test_duplicate_registration_is_rejected() -> None:
    registry = _registry()
    with pytest.raises(CommandSpecError, match="already registered"):
        registry.register(CommandSpec("test.status", (TOOL, "status")))


def test_default_registry_contains_exactly_the_reviewed_commands() -> None:
    # Deliberately updated in milestone 8, then in milestone 9 (apt-get update,
    # add-apt-repository --cloud, apt-cache policy), milestone 12 (systemctl enable,
    # systemctl is-enabled, for the etcd page), milestone 14 (five /usr/sbin/rabbitmqctl
    # specs for the rabbitmq page), and milestone 15 (two /usr/bin/mariadb specs and four
    # /usr/bin/keystone-manage specs for the keystone page): these are the only real
    # commands, and any new one must be added here on purpose after review.
    specs = {spec_id: DEFAULT_REGISTRY.get(spec_id) for spec_id in DEFAULT_REGISTRY.ids()}
    assert {
        spec_id: (spec.argv, dict(spec.params), spec.mutates, spec.allowed_exit_codes)
        for spec_id, spec in specs.items()
    } == {
        "apt-get.install": (
            ("/usr/bin/apt-get", "install", "--yes", "{package}"),
            {"package": "package_name"},
            True,
            frozenset({0}),
        ),
        "apt-get.update": (
            ("/usr/bin/apt-get", "update"),
            {},
            True,
            frozenset({0}),
        ),
        "add-apt-repository.cloud-archive": (
            ("/usr/bin/add-apt-repository", "--yes", "--cloud", "{pocket}"),
            {"pocket": "cloud_archive_pocket"},
            True,
            frozenset({0}),
        ),
        "apt-cache.policy": (
            ("/usr/bin/apt-cache", "policy", "{package}"),
            {"package": "package_name"},
            False,
            frozenset({0}),
        ),
        "systemctl.restart": (
            ("/usr/bin/systemctl", "restart", "{unit}"),
            {"unit": "systemd_unit"},
            True,
            frozenset({0}),
        ),
        "systemctl.reload": (
            ("/usr/bin/systemctl", "reload", "{unit}"),
            {"unit": "systemd_unit"},
            True,
            frozenset({0}),
        ),
        "systemctl.is-active": (
            ("/usr/bin/systemctl", "is-active", "{unit}"),
            {"unit": "systemd_unit"},
            False,
            frozenset({0, 1, 2, 3, 4}),
        ),
        "systemctl.enable": (
            ("/usr/bin/systemctl", "enable", "{unit}"),
            {"unit": "systemd_unit"},
            True,
            frozenset({0}),
        ),
        "systemctl.is-enabled": (
            ("/usr/bin/systemctl", "is-enabled", "{unit}"),
            {"unit": "systemd_unit"},
            False,
            frozenset({0, 1, 2, 3, 4}),
        ),
        "dpkg-query.status": (
            ("/usr/bin/dpkg-query", "--status", "{package}"),
            {"package": "package_name"},
            False,
            frozenset({0, 1}),
        ),
        "rabbitmqctl.add-user": (
            ("/usr/sbin/rabbitmqctl", "add_user", "openstack", "{password}"),
            {"password": "secret"},
            True,
            frozenset({0}),
        ),
        "rabbitmqctl.change-password": (
            ("/usr/sbin/rabbitmqctl", "change_password", "openstack", "{password}"),
            {"password": "secret"},
            True,
            frozenset({0}),
        ),
        "rabbitmqctl.set-permissions": (
            ("/usr/sbin/rabbitmqctl", "set_permissions", "openstack", ".*", ".*", ".*"),
            {},
            True,
            frozenset({0}),
        ),
        "rabbitmqctl.list-users": (
            ("/usr/sbin/rabbitmqctl", "list_users"),
            {},
            False,
            frozenset({0}),
        ),
        "rabbitmqctl.list-user-permissions": (
            ("/usr/sbin/rabbitmqctl", "list_user_permissions", "openstack"),
            {},
            False,
            frozenset({0}),
        ),
        "mariadb.execute-sql": (
            ("/usr/bin/mariadb", "--batch", "--skip-column-names"),
            {"sql": "sql_stdin"},
            True,
            frozenset({0}),
        ),
        "mariadb.query-sql": (
            (
                "/usr/bin/mariadb",
                "--batch",
                "--skip-column-names",
                "--execute",
                "SELECT 'DB', SCHEMA_NAME, '', '' FROM information_schema.SCHEMATA "
                "UNION ALL "
                "SELECT 'GRANT', User, Host, Db FROM mysql.db;",
            ),
            {},
            False,
            frozenset({0}),
        ),
        "keystone-manage.db-sync": (
            ("/usr/bin/keystone-manage", "db_sync"),
            {},
            True,
            frozenset({0}),
        ),
        "keystone-manage.fernet-setup": (
            (
                "/usr/bin/keystone-manage",
                "fernet_setup",
                "--keystone-user",
                "keystone",
                "--keystone-group",
                "keystone",
            ),
            {},
            True,
            frozenset({0}),
        ),
        "keystone-manage.credential-setup": (
            (
                "/usr/bin/keystone-manage",
                "credential_setup",
                "--keystone-user",
                "keystone",
                "--keystone-group",
                "keystone",
            ),
            {},
            True,
            frozenset({0}),
        ),
        "keystone-manage.bootstrap": (
            (
                "/usr/bin/keystone-manage",
                "bootstrap",
                "--bootstrap-admin-url",
                "http://controller:5000/v3/",
                "--bootstrap-internal-url",
                "http://controller:5000/v3/",
                "--bootstrap-public-url",
                "http://controller:5000/v3/",
                "--bootstrap-region-id",
                "RegionOne",
            ),
            {"password": "secret"},
            True,
            frozenset({0}),
        ),
        "openstack.project-list": (
            (*OPENSTACK_ADMIN_AUTH_ARGS, "project", "list", "--domain", "default", "-f", "json"),
            {"admin_password": "secret"},
            False,
            frozenset({0}),
        ),
        "openstack.project-create-service": (
            (
                *OPENSTACK_ADMIN_AUTH_ARGS,
                "project",
                "create",
                "--domain",
                "default",
                "--description",
                "Service Project",
                "service",
                "-f",
                "json",
            ),
            {"admin_password": "secret"},
            True,
            frozenset({0}),
        ),
        "openstack.user-list": (
            (*OPENSTACK_ADMIN_AUTH_ARGS, "user", "list", "--domain", "default", "-f", "json"),
            {"admin_password": "secret"},
            False,
            frozenset({0}),
        ),
        "openstack.user-create": (
            (
                *OPENSTACK_ADMIN_AUTH_ARGS,
                "user",
                "create",
                "--domain",
                "default",
                "--password",
                "{user_password}",
                "{user}",
                "-f",
                "json",
            ),
            {"admin_password": "secret", "user": "openstack_name", "user_password": "secret"},
            True,
            frozenset({0}),
        ),
        "openstack.user-set-password": (
            (
                *OPENSTACK_ADMIN_AUTH_ARGS,
                "user",
                "set",
                "--domain",
                "default",
                "--password",
                "{user_password}",
                "{user}",
            ),
            {"admin_password": "secret", "user": "openstack_name", "user_password": "secret"},
            True,
            frozenset({0}),
        ),
        "openstack.role-assignment-list": (
            (
                *OPENSTACK_ADMIN_AUTH_ARGS,
                "role",
                "assignment",
                "list",
                "--names",
                "-f",
                "json",
            ),
            {"admin_password": "secret"},
            False,
            frozenset({0}),
        ),
        "openstack.role-add-user-project": (
            (
                *OPENSTACK_ADMIN_AUTH_ARGS,
                "role",
                "add",
                "--project",
                "{project}",
                "--user",
                "{user}",
                "{role}",
            ),
            {
                "admin_password": "secret",
                "project": "openstack_name",
                "user": "openstack_name",
                "role": "openstack_name",
            },
            True,
            frozenset({0}),
        ),
        "openstack.service-list": (
            (*OPENSTACK_ADMIN_AUTH_ARGS, "service", "list", "-f", "json"),
            {"admin_password": "secret"},
            False,
            frozenset({0}),
        ),
        "openstack.service-create": (
            (
                *OPENSTACK_ADMIN_AUTH_ARGS,
                "service",
                "create",
                "--name",
                "{service_name}",
                "{service_type}",
                "-f",
                "json",
            ),
            {
                "admin_password": "secret",
                "service_name": "openstack_name",
                "service_type": "openstack_name",
            },
            True,
            frozenset({0}),
        ),
        "openstack.endpoint-list": (
            (*OPENSTACK_ADMIN_AUTH_ARGS, "endpoint", "list", "-f", "json"),
            {"admin_password": "secret"},
            False,
            frozenset({0}),
        ),
        "openstack.endpoint-create": (
            (
                *OPENSTACK_ADMIN_AUTH_ARGS,
                "endpoint",
                "create",
                "--region",
                "RegionOne",
                "{service_name}",
                "{interface}",
                "{url}",
                "-f",
                "json",
            ),
            {
                "admin_password": "secret",
                "service_name": "openstack_name",
                "interface": "endpoint_interface",
                "url": "endpoint_url",
            },
            True,
            frozenset({0}),
        ),
        "glance-manage.db-sync": (
            ("/usr/bin/glance-manage", "db_sync"),
            {},
            True,
            frozenset({0}),
        ),
        "placement-manage.db-sync": (
            ("/usr/bin/placement-manage", "db", "sync"),
            {},
            True,
            frozenset({0}),
        ),
        "nova-manage.api-db-sync": (
            ("/usr/bin/nova-manage", "api_db", "sync"),
            {},
            True,
            frozenset({0}),
        ),
        "nova-manage.cell-v2-map-cell0": (
            ("/usr/bin/nova-manage", "cell_v2", "map_cell0"),
            {},
            True,
            frozenset({0}),
        ),
        "nova-manage.cell-v2-list-cells": (
            ("/usr/bin/nova-manage", "cell_v2", "list_cells"),
            {},
            False,
            frozenset({0}),
        ),
        "nova-manage.cell-v2-create-cell1": (
            ("/usr/bin/nova-manage", "cell_v2", "create_cell", "--name=cell1", "--verbose"),
            {},
            True,
            frozenset({0}),
        ),
        "nova-manage.db-sync": (
            ("/usr/bin/nova-manage", "db", "sync"),
            {},
            True,
            frozenset({0}),
        ),
        "neutron-db-manage.upgrade-head": (
            (
                "/usr/bin/neutron-db-manage",
                "--config-file",
                "/etc/neutron/neutron.conf",
                "--config-file",
                "/etc/neutron/plugins/ml2/ml2_conf.ini",
                "upgrade",
                "head",
            ),
            {},
            True,
            frozenset({0}),
        ),
        "ovs-vsctl.add-br": (
            ("/usr/bin/ovs-vsctl", "--may-exist", "add-br", "{bridge}"),
            {"bridge": "interface_name"},
            True,
            frozenset({0}),
        ),
        "ovs-vsctl.add-port": (
            ("/usr/bin/ovs-vsctl", "--may-exist", "add-port", "{bridge}", "{interface}"),
            {"bridge": "interface_name", "interface": "interface_name"},
            True,
            frozenset({0}),
        ),
        "ovs-vsctl.br-exists": (
            ("/usr/bin/ovs-vsctl", "br-exists", "{bridge}"),
            {"bridge": "interface_name"},
            False,
            frozenset({0, 2}),
        ),
        "ovs-vsctl.list-ports": (
            ("/usr/bin/ovs-vsctl", "list-ports", "{bridge}"),
            {"bridge": "interface_name"},
            False,
            frozenset({0}),
        ),
        "cinder-manage.db-sync": (
            ("/usr/bin/cinder-manage", "db", "sync"),
            {},
            True,
            frozenset({0}),
        ),
        "lvm.pvs": (
            ("/usr/sbin/pvs", "--noheadings", "-o", "pv_name"),
            {},
            False,
            frozenset({0}),
        ),
        "lvm.pvcreate": (
            ("/usr/sbin/pvcreate", "{device}"),
            {"device": "device_path"},
            True,
            frozenset({0}),
        ),
        "lvm.vgs": (
            ("/usr/sbin/vgs", "--noheadings", "-o", "vg_name"),
            {},
            False,
            frozenset({0}),
        ),
        "lvm.vgcreate-cinder-volumes": (
            ("/usr/sbin/vgcreate", "cinder-volumes", "{device}"),
            {"device": "device_path"},
            True,
            frozenset({0}),
        ),
    }
    assert all(spec.timeout > 0 for spec in specs.values())
    assert specs["keystone-manage.bootstrap"].env_params == {"password": "OS_BOOTSTRAP_PASSWORD"}
    openstack_ids = {spec_id for spec_id in specs if spec_id.startswith("openstack.")}
    assert len(openstack_ids) == 11
    for spec_id in openstack_ids:
        assert specs[spec_id].env_params == {"admin_password": "OS_PASSWORD"}
        assert "admin_password" in specs[spec_id].sensitive_params
    for spec_id, spec in specs.items():
        if spec_id != "keystone-manage.bootstrap" and spec_id not in openstack_ids:
            assert spec.env_params == {}
    assert specs["rabbitmqctl.add-user"].sensitive_params == frozenset({"password"})
    assert specs["rabbitmqctl.change-password"].sensitive_params == frozenset({"password"})
    assert specs["mariadb.execute-sql"].stdin_param == "sql"
    assert specs["mariadb.execute-sql"].sensitive_params == frozenset({"sql"})
    assert specs["keystone-manage.bootstrap"].sensitive_params == frozenset({"password"})
    assert specs["openstack.user-create"].sensitive_params == frozenset(
        {"admin_password", "user_password"}
    )
    assert specs["openstack.user-set-password"].sensitive_params == frozenset(
        {"admin_password", "user_password"}
    )
    assert "--yes" in specs["apt-get.install"].argv
    assert specs["apt-get.install"].failure_hint
    # add-apt-repository must stay non-interactive and must keep its own update.
    assert "--yes" in specs["add-apt-repository.cloud-archive"].argv
    assert "--no-update" not in specs["add-apt-repository.cloud-archive"].argv
    assert specs["add-apt-repository.cloud-archive"].failure_hint
    assert "network" in specs["apt-get.update"].failure_hint
    assert "network" in specs["apt-get.update"].description
    # No apt upgrade, dist-upgrade or reboot can be expressed with the registry
    # (only neutron-db-manage uses Alembic's 'upgrade head' subcommand).
    non_neutron_words = {
        word
        for spec_id, spec in specs.items()
        if spec_id != "neutron-db-manage.upgrade-head"
        for word in spec.argv
    }
    assert not non_neutron_words & {
        "upgrade",
        "dist-upgrade",
        "full-upgrade",
        "reboot",
        "--no-update",
    }


@pytest.mark.parametrize(
    "pocket", ["ppa:owner/name", "cloud-archive:epoxy;id", "--remove", "cloud-archive:"]
)
def test_add_apt_repository_only_accepts_a_cloud_archive_pocket(pocket: str) -> None:
    with pytest.raises(ParameterError):
        DEFAULT_REGISTRY.get("add-apt-repository.cloud-archive").build_argv({"pocket": pocket})


def test_executor_has_no_free_form_entry_point() -> None:
    # The only way to run something is run(command_id, params); a command string is
    # just an unknown id.
    executor = CommandExecutor(
        _registry(), root=Path("/nonexistent"), audit_log=AuditLog(Path("/nonexistent/log"))
    )
    with pytest.raises(UnknownCommandError):
        executor.run("/opt/forge-test/bin/unit-tool restart chrony")


# --- executor with a fake backend -----------------------------------------------------


def test_argv_env_and_timeout_passed_to_the_backend(
    executor: CommandExecutor, backend: FakeBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OS_PASSWORD", "leaked")
    monkeypatch.setenv("LD_PRELOAD", "/tmp/evil.so")
    monkeypatch.setenv("PATH", "/tmp/evil:/usr/bin")
    result = executor.run("test.unit-active", {"unit": "chrony.service"})
    ((argv, env, timeout),) = backend.calls
    assert argv == (TOOL, "is-active", "chrony.service")
    assert (
        env
        == dict(SANITIZED_ENV)
        == {
            "PATH": "/usr/sbin:/usr/bin:/sbin:/bin",
            "LC_ALL": "C",
            "LANG": "C",
            "DEBIAN_FRONTEND": "noninteractive",
        }
    )
    assert timeout == 10
    assert result.ok and result.argv == argv and not result.dry_run


@pytest.mark.parametrize("value", ["chrony; reboot", "$(reboot)", "-reboot", "a b"])
def test_injection_is_rejected_before_anything_runs(
    executor: CommandExecutor, backend: FakeBackend, audit: AuditLog, value: str
) -> None:
    with pytest.raises(ParameterError):
        executor.run("test.unit-active", {"unit": value})
    assert backend.calls == []
    assert audit.read() == []


@pytest.mark.parametrize(
    "params", [{}, {"unit": "chrony", "extra": "x"}, {"service": "chrony"}, None]
)
def test_missing_or_unexpected_parameters(executor: CommandExecutor, backend, params) -> None:
    with pytest.raises(ParameterError, match="missing parameter"):
        executor.run("test.unit-active", params)
    assert backend.calls == []


def test_unexpected_exit_code(executor: CommandExecutor, backend: FakeBackend, audit) -> None:
    backend.default = ProcessOutput(1, "", "Failed: token=abc123 rejected\n")
    with pytest.raises(UnexpectedExitError) as excinfo:
        executor.run("test.status")
    assert excinfo.value.returncode == 1
    assert "abc123" not in str(excinfo.value)
    assert f"token={MASK}" in excinfo.value.stderr
    assert [e["outcome"] for e in audit.read() if e["event"] == "finish"] == ["unexpected_exit"]


def test_allowed_nonzero_exit_code(executor: CommandExecutor, backend: FakeBackend) -> None:
    backend.default = ProcessOutput(3, "inactive\n", "")
    result = executor.run("test.unit-active", {"unit": "chrony"})
    assert (result.returncode, result.ok, result.stdout) == (3, False, "inactive\n")
    assert command_succeeds(executor, "test.unit-active", {"unit": "chrony"}) is False


def test_timeout(executor: CommandExecutor, backend: FakeBackend, audit: AuditLog) -> None:
    backend.default = CommandTimeoutError("unit-tool timed out after 5s")
    with pytest.raises(CommandTimeoutError):
        executor.run("test.status")
    start, finish = audit.read()
    assert (start["event"], finish["event"], finish["outcome"]) == ("start", "finish", "timeout")
    assert finish["exit_code"] is None


def test_missing_binary(executor: CommandExecutor, backend: FakeBackend, audit: AuditLog) -> None:
    backend.default = CommandNotFoundError(f"Executable not found: {TOOL}")
    with pytest.raises(CommandNotFoundError, match="Executable not found"):
        executor.run("test.status")
    assert audit.read()[-1]["outcome"] == "not_found"


def test_other_backend_errors_are_wrapped(executor: CommandExecutor, backend, audit) -> None:
    backend.default = OSError("Too many open files")
    with pytest.raises(CommandError, match="could not be run: Too many open files"):
        executor.run("test.status")
    assert audit.read()[-1]["outcome"] == "error"


def test_output_is_truncated_to_the_limit(tmp_path: Path, backend: FakeBackend, audit) -> None:
    executor = CommandExecutor(
        _registry(), root=tmp_path, audit_log=audit, backend=backend, output_limit=100
    )
    backend.default = ProcessOutput(0, "a" * 5000 + "END", "short")
    result = executor.run("test.status")
    assert result.stdout_truncated and not result.stderr_truncated
    assert result.stdout.startswith("[... output truncated: 4903 earlier characters removed ...]")
    assert result.stdout.endswith("a" * 97 + "END")
    assert result.stderr == "short"


def test_secrets_are_redacted_before_truncation(tmp_path: Path, backend, audit) -> None:
    executor = CommandExecutor(
        _registry(), root=tmp_path, audit_log=audit, backend=backend, output_limit=40
    )
    # Truncating first would keep only the tail of the value, without its key.
    backend.default = ProcessOutput(0, "x" * 100 + "\nadmin_password=" + "S3CRET" * 20, "")
    result = executor.run("test.status")
    assert "S3CRET" not in result.stdout
    assert MASK in result.stdout


def test_secrets_in_output_are_redacted(executor: CommandExecutor, backend: FakeBackend) -> None:
    backend.default = ProcessOutput(
        0,
        "connection = mysql+pymysql://nova:DBPASS@ctl/nova\nok\n",
        "warning: OS_PASSWORD=pw123 is set\n",
    )
    result = executor.run("test.status")
    assert "DBPASS" not in result.stdout and "pw123" not in result.stderr
    assert f"nova:{MASK}@ctl" in result.stdout


# --- dry run ---------------------------------------------------------------------------


def test_dry_run_prints_the_exact_argv_and_runs_nothing(
    tmp_path: Path, backend: FakeBackend, audit: AuditLog
) -> None:
    printed: list[str] = []
    executor = CommandExecutor(
        _registry(), root=tmp_path, audit_log=audit, backend=backend, dry_run=True,
        echo=printed.append,
    )  # fmt: skip
    result = executor.run("test.install-package", {"package": "python3-openstackclient"})
    assert printed == [
        f"DRY RUN [test.install-package]: {PKG} install --yes python3-openstackclient"
    ]
    assert result.dry_run and result.returncode is None
    assert result.argv == (PKG, "install", "--yes", "python3-openstackclient")
    assert backend.calls == []
    assert not audit.path.exists()


def test_dry_run_still_validates_parameters(tmp_path: Path, backend, audit) -> None:
    printed: list[str] = []
    executor = CommandExecutor(
        _registry(), root=tmp_path, audit_log=audit, backend=backend, dry_run=True,
        echo=printed.append,
    )  # fmt: skip
    with pytest.raises(ParameterError):
        executor.run("test.restart-unit", {"unit": "chrony;reboot"})
    assert printed == []


# --- the subprocess backend ---------------------------------------------------------------


def test_subprocess_backend_never_uses_a_shell(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict = {}

    class RecordingPopen:
        def __init__(self, args, **kwargs):
            seen["args"], seen["kwargs"] = args, kwargs
            self.returncode = 0
            self.pid = 0

        def communicate(self, timeout=None):
            seen["timeout"] = timeout
            return b"out", b"err"

    monkeypatch.setattr(subprocess, "Popen", RecordingPopen)
    output = SubprocessBackend().run((TOOL, "status"), env={"PATH": "/usr/bin"}, timeout=7)
    assert output == ProcessOutput(0, "out", "err")
    assert seen["args"] == [TOOL, "status"]
    kwargs = seen["kwargs"]
    assert kwargs["shell"] is False
    assert kwargs["env"] == {"PATH": "/usr/bin"}
    assert kwargs["stdin"] is subprocess.DEVNULL
    assert kwargs["start_new_session"] is True
    assert kwargs["cwd"] == "/"
    assert seen["timeout"] == 7


@pytest.mark.parametrize(
    ("error", "match"),
    [(FileNotFoundError, "not found"), (PermissionError, "not executable")],
)
def test_subprocess_backend_maps_launch_errors(
    monkeypatch: pytest.MonkeyPatch, error: type[OSError], match: str
) -> None:
    def fail(*args, **kwargs):
        raise error

    monkeypatch.setattr(subprocess, "Popen", fail)
    with pytest.raises(CommandNotFoundError, match=match):
        SubprocessBackend().run((TOOL,), env={}, timeout=1)


# --- REAL subprocess tests (harmless sys.executable one-liners) -----------------------------

_REAL_PRINT_ENV = (
    "import json, os, sys; "
    "print(json.dumps(dict(os.environ))); "
    "print('db_password=hunter2'); "
    "sys.stderr.write('e' * 5000 + 'TAIL')"
)


def test_real_command_capture_env_redaction_and_truncation(tmp_path: Path) -> None:
    registry = CommandRegistry(
        [
            CommandSpec(
                "test.python-env",
                (sys.executable, "-I", "-c", _REAL_PRINT_ENV),
                timeout=30,
                mutates=False,
            )
        ]  # fmt: skip
    )
    audit = AuditLog(tmp_path / "commands.log")
    executor = CommandExecutor(registry, root=tmp_path, audit_log=audit, output_limit=1000)

    result = executor.run("test.python-env")

    env_line, secret_line = result.stdout.splitlines()
    assert json.loads(env_line) == dict(SANITIZED_ENV)
    assert secret_line == f"db_password={MASK}"
    assert result.stderr_truncated and result.stderr.endswith("TAIL")
    assert len(result.stderr) < 1100
    start, finish = audit.read()
    assert (finish["outcome"], finish["exit_code"]) == ("ok", 0)
    assert "hunter2" not in audit.path.read_text()


def test_real_command_timeout_kills_the_process(tmp_path: Path) -> None:
    registry = CommandRegistry(
        [
            CommandSpec(
                "test.python-sleep",
                (sys.executable, "-I", "-c", "import time; time.sleep(30)"),
                timeout=0.5,
                mutates=False,
            )
        ]  # fmt: skip
    )
    audit = AuditLog(tmp_path / "commands.log")
    executor = CommandExecutor(registry, root=tmp_path, audit_log=audit)

    with pytest.raises(CommandTimeoutError, match="timed out after 0.5s"):
        executor.run("test.python-sleep")

    finish = audit.read()[-1]
    assert finish["outcome"] == "timeout"
    assert finish["duration_s"] < 10


# --- REAL: SIGTERM, grace period, then SIGKILL (harmless sys.executable one-liners) ----------


def _sleeper_registry(code: str, timeout: float) -> CommandRegistry:
    return CommandRegistry(
        [
            CommandSpec(
                "test.python-sleeper",
                (sys.executable, "-I", "-c", code),
                timeout=timeout,
                mutates=False,
            )
        ]  # fmt: skip
    )


def test_real_timeout_sends_sigterm_first(tmp_path: Path) -> None:
    marker = tmp_path / "got-sigterm"
    code = (
        "import signal, sys, time\n"
        "def stop(signum, frame):\n"
        f"    open({str(marker)!r}, 'w').write('TERM')\n"
        "    sys.exit(0)\n"
        "signal.signal(signal.SIGTERM, stop)\n"
        "time.sleep(30)\n"
    )
    executor = CommandExecutor(
        _sleeper_registry(code, timeout=1.0), root=tmp_path,
        audit_log=AuditLog(tmp_path / "commands.log"),
        backend=SubprocessBackend(term_grace_period=10.0),
    )  # fmt: skip
    started = time.monotonic()
    with pytest.raises(CommandTimeoutError, match=r"terminated \(SIGTERM\)") as excinfo:
        executor.run("test.python-sleeper")
    elapsed = time.monotonic() - started
    assert excinfo.value.killed is False
    assert marker.read_text() == "TERM"  # the process handled SIGTERM itself
    assert elapsed < 6  # it did not wait for the 10s grace period


def test_real_timeout_kills_a_process_that_ignores_sigterm(tmp_path: Path) -> None:
    code = "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(30)"
    executor = CommandExecutor(
        _sleeper_registry(code, timeout=1.0), root=tmp_path,
        audit_log=AuditLog(tmp_path / "commands.log"),
        backend=SubprocessBackend(term_grace_period=0.5),
    )  # fmt: skip
    started = time.monotonic()
    with pytest.raises(CommandTimeoutError, match="killed \\(SIGKILL\\) after ignoring SIGTERM"):
        executor.run("test.python-sleeper")
    elapsed = time.monotonic() - started
    assert 1.4 <= elapsed < 10  # timeout plus the grace period, then SIGKILL
    assert executor.audit_log.read()[-1]["outcome"] == "timeout"


def test_term_grace_period_must_not_be_negative() -> None:
    with pytest.raises(ValueError):
        SubprocessBackend(term_grace_period=-1)


def test_executor_attaches_the_spec_failure_hint(tmp_path: Path, backend: FakeBackend) -> None:
    registry = CommandRegistry(
        [
            CommandSpec(
                "test.hinted",
                (TOOL, "go"),
                timeout=5,
                mutates=False,
                failure_hint="Check the thing by hand.",
            )
        ]  # fmt: skip
    )
    backend.default = ProcessOutput(2, "", "boom")
    executor = CommandExecutor(
        registry, root=tmp_path, audit_log=AuditLog(tmp_path / "log"), backend=backend
    )
    with pytest.raises(UnexpectedExitError) as excinfo:
        executor.run("test.hinted")
    assert excinfo.value.hint == "Check the thing by hand."


@pytest.mark.parametrize(
    ("command_id", "params"),
    [
        ("dpkg-query.status", {"package": "chrony"}),
        ("systemctl.is-active", {"unit": "chrony.service"}),
        ("mariadb.query-sql", {}),
        ("openstack.user-list", {"admin_password": "A" * 32}),
        ("ovs-vsctl.br-exists", {"bridge": "br-provider"}),
        ("lvm.pvs", {}),
    ],
)
def test_read_only_executor_runs_non_mutating_default_registry_commands(
    tmp_path: Path,
    command_id: str,
    params: Mapping[str, str],
) -> None:
    from tests.unit._service_fakes import ServiceHost

    host = ServiceHost(
        tmp_path,
        installed={"chrony"},
        active={"chrony.service"},
        ovs_bridges={"br-provider": ()},
        lvm_pvs={"/dev/sdb"},
    )
    audit = AuditLog(tmp_path / "audit.log")
    executor = CommandExecutor(
        DEFAULT_REGISTRY,
        root=tmp_path,
        audit_log=audit,
        backend=host,
        read_only=True,
    )
    result = executor.run(command_id, params)
    assert result.returncode == 0
    assert len(host.calls) == 1
    assert not audit.path.exists()


@pytest.mark.parametrize(
    ("command_id", "params"),
    [
        ("apt-get.install", {"package": "chrony"}),
        ("systemctl.restart", {"unit": "chrony.service"}),
        ("mariadb.execute-sql", {"sql_stdin": "CREATE DATABASE keystone;\n"}),
    ],
)
def test_read_only_executor_refuses_mutating_commands_before_invoking_backend(
    tmp_path: Path,
    backend: FakeBackend,
    command_id: str,
    params: Mapping[str, str],
) -> None:
    executor = CommandExecutor(
        DEFAULT_REGISTRY,
        root=tmp_path,
        backend=backend,
        dry_run=False,
        read_only=True,
    )
    with pytest.raises(
        CommandError,
        match=rf"Command {command_id!r} mutates host state and cannot run in read-only mode",
    ):
        executor.run(command_id, params)
    assert backend.calls == []


def test_read_only_executor_refuses_every_mutating_spec_in_default_registry(
    tmp_path: Path,
    backend: FakeBackend,
) -> None:
    executor = CommandExecutor(
        DEFAULT_REGISTRY,
        root=tmp_path,
        backend=backend,
        dry_run=False,
        read_only=True,
    )
    mutating_specs = [
        DEFAULT_REGISTRY.get(cmd_id)
        for cmd_id in DEFAULT_REGISTRY.ids()
        if DEFAULT_REGISTRY.get(cmd_id).mutates
    ]
    assert len(mutating_specs) >= 20
    for spec in mutating_specs:
        with pytest.raises(
            CommandError,
            match=rf"Command {spec.id!r} mutates host state and cannot run in read-only mode",
        ):
            executor.run(spec.id)
    assert backend.calls == []
