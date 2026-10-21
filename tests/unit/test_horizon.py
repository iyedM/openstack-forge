"""Unit tests for the ``horizon`` controller step (runs nothing)."""

from __future__ import annotations

import socket
import subprocess
from pathlib import Path
from typing import Any

import pytest

from openstack_forge.execution import (
    DEFAULT_AUDIT_TARGET,
    DEFAULT_REGISTRY,
    DEFAULT_STATE_TARGET,
    HORIZON_APACHE_CONF_TARGET,
    HORIZON_BLOCK_MARKER,
    HORIZON_CACHE_BACKEND,
    HORIZON_DEFAULT_DOMAIN,
    HORIZON_DEFAULT_ROLE,
    HORIZON_KEYSTONE_URL_EXPR,
    HORIZON_LOCAL_SETTINGS_TARGET,
    HORIZON_MEMCACHED_LOCATION,
    HORIZON_OPENSTACK_HOST,
    HORIZON_PACKAGE,
    HORIZON_SESSION_ENGINE,
    HORIZON_STEP_ID,
    HORIZON_TIME_ZONE,
    HORIZON_WSGI_APPLICATION_GROUP_LINE,
    HORIZON_WSGI_BLOCK_MARKER,
    SYSTEMCTL_RELOAD,
    AuditLog,
    CommandExecutor,
    Engine,
    ExecutionError,
    HorizonStep,
    Outcome,
    PreconditionError,
    ProcessOutput,
    StateStore,
    build_horizon_local_settings_block,
    build_step,
    edit_horizon_apache_conf,
    edit_horizon_local_settings,
    has_wsgi_application_group_global,
    horizon_allowed_hosts,
    make_horizon_step,
    resolve_target,
    verify_horizon_apache_conf_text,
    verify_horizon_local_settings_text,
)
from openstack_forge.execution.horizon import (
    APACHE_UNIT,
    HORIZON_KEYSTONE_URL_NOTE,
    HORIZON_NOT_CHECKED,
    INSTALL_HORIZON_PACKAGE,
    QUERY_APACHE_ACTIVE,
    QUERY_HORIZON_PACKAGE,
    RELOAD_APACHE_UNIT,
)
from tests.unit._service_fakes import DEV_MACHINE_SANITIZED_IP, ServiceHost

CONTROLLER_NODE: dict[str, Any] = {
    "role": "controller",
    "hostname": "controller",
    "fqdn": "controller.example.test",
    "management_ip": DEV_MACHINE_SANITIZED_IP,
}
COMPUTE_NODE: dict[str, Any] = {
    "role": "compute",
    "hostname": "compute1",
    "fqdn": "compute1.example.test",
    "management_ip": "192.0.2.31",
}
SAMPLE_CONFIG: dict[str, Any] = {
    "nodes": [CONTROLLER_NODE, COMPUTE_NODE],
}

UBUNTU_DEFAULT_LOCAL_SETTINGS = """\
# -*- coding: utf-8 -*-

import os

from django.utils.translation import gettext_lazy as _
from horizon.utils import secret_key
from openstack_dashboard.settings import HORIZON_CONFIG

DEBUG = False

OPENSTACK_HOST = "127.0.0.1"
OPENSTACK_KEYSTONE_URL = "http://%s:5000/identity/v3" % OPENSTACK_HOST
OPENSTACK_KEYSTONE_DEFAULT_ROLE = "member"

#CACHES = {
#    'default': {
#        'BACKEND': 'django.core.cache.backends.memcached.PyMemcacheCache',
#        'LOCATION': '127.0.0.1:11211',
#    },
#}

OPENSTACK_NEUTRON_NETWORK = {
    'enable_router': True,
    'enable_quotas': True,
    'enable_ipv6': False,
    'enable_distributed_router': False,
    'enable_ha_router': False,
    'enable_fip_topology_check': True,
}

TIME_ZONE = "UTC"

###############################################################################
# Ubuntu Settings
###############################################################################

# Allow all hosts by default
ALLOWED_HOSTS = ['*']
"""

UBUNTU_DEFAULT_APACHE_CONF = (
    "WSGIScriptAlias /horizon "
    "/usr/share/openstack-dashboard/openstack_dashboard/wsgi.py process-group=horizon\n"
    "WSGIDaemonProcess horizon user=horizon group=horizon processes=3 threads=10 "
    "display-name=%{GROUP}\n"
    "WSGIProcessGroup horizon\n"
    "WSGIApplicationGroup %{GLOBAL}\n\n"
    "Alias /static /var/lib/openstack-dashboard/static/\n"
    "Alias /horizon/static /var/lib/openstack-dashboard/static/\n\n"
    "<Directory /usr/share/openstack-dashboard/openstack_dashboard>\n"
    "  Require all granted\n"
    "</Directory>\n\n"
    "<Directory /var/lib/openstack-dashboard/static>\n"
    "  Require all granted\n"
    "</Directory>\n"
)

APACHE_CONF_WITHOUT_GLOBAL = (
    "WSGIScriptAlias /horizon "
    "/usr/share/openstack-dashboard/openstack_dashboard/wsgi.py process-group=horizon\n"
    "WSGIDaemonProcess horizon user=horizon group=horizon processes=3 threads=10 "
    "display-name=%{GROUP}\n"
    "WSGIProcessGroup horizon\n\n"
    "<Directory /usr/share/openstack-dashboard/openstack_dashboard>\n"
    "  Require all granted\n"
    "</Directory>\n"
)


@pytest.fixture(autouse=True)
def _forbid_real_subprocess_and_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(*args: object, **kwargs: object) -> None:
        raise AssertionError(f"real subprocess/socket call is forbidden: {args!r}")

    monkeypatch.setattr(subprocess, "Popen", _boom)
    monkeypatch.setattr(subprocess, "run", _boom)
    monkeypatch.setattr(socket, "create_connection", _boom)
    monkeypatch.setattr("openstack_forge.execution.commands.is_host_root", lambda _: True)


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


def _make_engine(tmp_path: Path) -> Engine:
    state_path = tmp_path / DEFAULT_STATE_TARGET.lstrip("/")
    state_path.parent.mkdir(parents=True, exist_ok=True)
    return Engine(StateStore(state_path))


def _exec_local_settings(text: str) -> dict[str, Any]:
    """Execute a rendered local_settings.py snippet in a stubbed namespace."""
    namespace: dict[str, Any] = {}
    filtered_lines = [
        line
        for line in text.splitlines()
        if not line.startswith("from ") and not line.startswith("import ")
    ]
    exec("\n".join(filtered_lines), namespace)
    return namespace


def test_horizon_allowed_hosts_deduplicates_and_validates_fields() -> None:
    assert horizon_allowed_hosts(CONTROLLER_NODE) == (
        "controller",
        "controller.example.test",
        DEV_MACHINE_SANITIZED_IP,
        "localhost",
        "127.0.0.1",
    )
    custom_node = {
        "role": "controller",
        "hostname": "ctrl01",
        "fqdn": "ctrl01.lab.internal",
        "management_ip": "192.0.2.11",
    }
    assert horizon_allowed_hosts(custom_node) == (
        "controller",
        "ctrl01",
        "ctrl01.lab.internal",
        "192.0.2.11",
        "localhost",
        "127.0.0.1",
    )
    with pytest.raises(PreconditionError, match="hostname"):
        horizon_allowed_hosts({"fqdn": "a.test", "management_ip": "192.0.2.11"})
    with pytest.raises(PreconditionError, match="fqdn"):
        horizon_allowed_hosts({"hostname": "a", "management_ip": "192.0.2.11"})
    with pytest.raises(PreconditionError, match="management_ip"):
        horizon_allowed_hosts({"hostname": "a", "fqdn": "a.test", "management_ip": "not-an-ip"})


def test_edit_horizon_local_settings_creates_valid_python_block_and_is_idempotent(
    tmp_path: Path,
) -> None:
    ls_path, _ = resolve_target(tmp_path, HORIZON_LOCAL_SETTINGS_TARGET)
    ls_path.parent.mkdir(parents=True, exist_ok=True)
    ls_path.write_text(UBUNTU_DEFAULT_LOCAL_SETTINGS, encoding="utf-8")

    change1 = edit_horizon_local_settings(tmp_path, CONTROLLER_NODE, dry_run=False)
    assert change1.changed is True
    edited = ls_path.read_text(encoding="utf-8")
    assert f"# BEGIN {HORIZON_BLOCK_MARKER}" in edited
    assert f"# END {HORIZON_BLOCK_MARKER}" in edited
    assert verify_horizon_local_settings_text(edited, CONTROLLER_NODE) == []

    ns = _exec_local_settings(edited)
    assert ns["OPENSTACK_HOST"] == HORIZON_OPENSTACK_HOST
    assert ns["SESSION_ENGINE"] == HORIZON_SESSION_ENGINE
    assert ns["CACHES"] == {
        "default": {
            "BACKEND": HORIZON_CACHE_BACKEND,
            "LOCATION": HORIZON_MEMCACHED_LOCATION,
        }
    }
    assert ns["OPENSTACK_KEYSTONE_URL"] == "http://controller:5000/v3"
    assert ns["OPENSTACK_KEYSTONE_MULTIDOMAIN_SUPPORT"] is True
    assert ns["OPENSTACK_API_VERSIONS"] == {"identity": 3, "image": 2, "volume": 3}
    assert ns["OPENSTACK_KEYSTONE_DEFAULT_DOMAIN"] == HORIZON_DEFAULT_DOMAIN
    assert ns["OPENSTACK_KEYSTONE_DEFAULT_ROLE"] == HORIZON_DEFAULT_ROLE
    assert ns["TIME_ZONE"] == HORIZON_TIME_ZONE
    assert ns["ALLOWED_HOSTS"] == [
        "controller",
        "controller.example.test",
        DEV_MACHINE_SANITIZED_IP,
        "localhost",
        "127.0.0.1",
    ]

    change2 = edit_horizon_local_settings(tmp_path, CONTROLLER_NODE, dry_run=False)
    assert change2.changed is False
    assert ls_path.read_text(encoding="utf-8") == edited


def test_verify_horizon_local_settings_text_detects_missing_or_drifted_or_overridden_block(
    tmp_path: Path,
) -> None:
    assert verify_horizon_local_settings_text(UBUNTU_DEFAULT_LOCAL_SETTINGS, CONTROLLER_NODE) != []

    ls_path, _ = resolve_target(tmp_path, HORIZON_LOCAL_SETTINGS_TARGET)
    ls_path.parent.mkdir(parents=True, exist_ok=True)
    ls_path.write_text(UBUNTU_DEFAULT_LOCAL_SETTINGS, encoding="utf-8")
    edit_horizon_local_settings(tmp_path, CONTROLLER_NODE, dry_run=False)
    edited = ls_path.read_text(encoding="utf-8")
    assert verify_horizon_local_settings_text(edited, CONTROLLER_NODE) == []

    drifted = edited.replace('"identity": 3', '"identity": 2')
    assert verify_horizon_local_settings_text(drifted, CONTROLLER_NODE) != []

    overridden = edited + "\nALLOWED_HOSTS = ['*']\n"
    problems = verify_horizon_local_settings_text(overridden, CONTROLLER_NODE)
    assert any("after '# END horizon'" in p for p in problems)


def test_edit_and_verify_horizon_apache_conf_handles_ubuntu_default_and_missing_directive(
    tmp_path: Path,
) -> None:
    ap_path, _ = resolve_target(tmp_path, HORIZON_APACHE_CONF_TARGET)
    ap_path.parent.mkdir(parents=True, exist_ok=True)
    ap_path.write_text(UBUNTU_DEFAULT_APACHE_CONF, encoding="utf-8")

    assert has_wsgi_application_group_global(UBUNTU_DEFAULT_APACHE_CONF) is True
    change1 = edit_horizon_apache_conf(tmp_path, dry_run=False)
    assert change1.changed is False
    assert ap_path.read_text(encoding="utf-8") == UBUNTU_DEFAULT_APACHE_CONF
    assert verify_horizon_apache_conf_text(UBUNTU_DEFAULT_APACHE_CONF) == []

    assert has_wsgi_application_group_global(APACHE_CONF_WITHOUT_GLOBAL) is False
    assert verify_horizon_apache_conf_text(APACHE_CONF_WITHOUT_GLOBAL) != []
    ap_path.write_text(APACHE_CONF_WITHOUT_GLOBAL, encoding="utf-8")
    change2 = edit_horizon_apache_conf(tmp_path, dry_run=False)
    assert change2.changed is True
    updated = ap_path.read_text(encoding="utf-8")
    assert f"# BEGIN {HORIZON_WSGI_BLOCK_MARKER}" in updated
    assert HORIZON_WSGI_APPLICATION_GROUP_LINE in updated
    assert verify_horizon_apache_conf_text(updated) == []

    change3 = edit_horizon_apache_conf(tmp_path, dry_run=False)
    assert change3.changed is False
    assert ap_path.read_text(encoding="utf-8") == updated


def test_horizon_step_rejects_non_controller_role(tmp_path: Path) -> None:
    host = ServiceHost(tmp_path)
    executor = _make_executor(tmp_path, host)
    with pytest.raises(ExecutionError, match="controller"):
        make_horizon_step(tmp_path, SAMPLE_CONFIG, COMPUTE_NODE, executor=executor)


def test_horizon_step_dry_run_preview_before_and_after_package_files_exist(
    tmp_path: Path,
) -> None:
    host = ServiceHost(tmp_path)
    executor = _make_executor(tmp_path, host, dry_run=True)
    step = make_horizon_step(tmp_path, SAMPLE_CONFIG, CONTROLLER_NODE, executor=executor)

    preview_before = step.dry_run_preview()
    assert preview_before.commands == (INSTALL_HORIZON_PACKAGE, RELOAD_APACHE_UNIT)
    assert preview_before.read_only_commands == (QUERY_HORIZON_PACKAGE, QUERY_APACHE_ACTIVE)
    assert preview_before.file_changes == ()
    assert any("local_settings.py does not exist yet" in n for n in preview_before.notes)
    assert any("openstack-dashboard.conf does not exist yet" in n for n in preview_before.notes)
    assert HORIZON_KEYSTONE_URL_NOTE in preview_before.notes
    assert HORIZON_NOT_CHECKED in preview_before.notes

    ls_path, _ = resolve_target(tmp_path, HORIZON_LOCAL_SETTINGS_TARGET)
    ap_path, _ = resolve_target(tmp_path, HORIZON_APACHE_CONF_TARGET)
    ls_path.parent.mkdir(parents=True, exist_ok=True)
    ap_path.parent.mkdir(parents=True, exist_ok=True)
    ls_path.write_text(UBUNTU_DEFAULT_LOCAL_SETTINGS, encoding="utf-8")
    ap_path.write_text(UBUNTU_DEFAULT_APACHE_CONF, encoding="utf-8")

    preview_after = step.dry_run_preview()
    assert len(preview_after.file_changes) == 2
    assert preview_after.file_changes[0].changed is True
    assert preview_after.file_changes[1].changed is False


def test_horizon_step_end_to_end_and_second_run_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **kw: (_ for _ in ()).throw(AssertionError("real subprocess.run called")),
    )
    host = ServiceHost(
        tmp_path,
        active={APACHE_UNIT},
        enabled={APACHE_UNIT},
        creates={
            HORIZON_PACKAGE: (
                ("etc/openstack-dashboard/local_settings.py", UBUNTU_DEFAULT_LOCAL_SETTINGS),
                ("etc/apache2/conf-available/openstack-dashboard.conf", UBUNTU_DEFAULT_APACHE_CONF),
            )
        },
        watch_files=(
            "etc/openstack-dashboard/local_settings.py",
            "etc/apache2/conf-available/openstack-dashboard.conf",
        ),
    )
    engine = _make_engine(tmp_path)
    executor = _make_executor(tmp_path, host)
    step = build_step(
        HORIZON_STEP_ID,
        tmp_path,
        SAMPLE_CONFIG,
        CONTROLLER_NODE,
        executor=executor,
    )
    assert isinstance(step, HorizonStep)

    report1 = engine.run([step])
    assert report1.ok
    assert report1.outcomes[0].outcome is Outcome.APPLIED
    assert HORIZON_PACKAGE in host.installed
    assert host.reloaded == [APACHE_UNIT]
    assert APACHE_UNIT in host.active

    # Config files were written before systemctl reload apache2 ran.
    reload_snapshots = [snap for event, snap in host.multi_snapshots if event == "reload"]
    assert len(reload_snapshots) == 1
    assert (
        verify_horizon_local_settings_text(
            reload_snapshots[0]["etc/openstack-dashboard/local_settings.py"] or "",
            CONTROLLER_NODE,
        )
        == []
    )

    # Ubuntu 24.04 default openstack-dashboard.conf already has WSGIApplicationGroup %{GLOBAL},
    # so only local_settings.py was rewritten.
    ap_path, _ = resolve_target(tmp_path, HORIZON_APACHE_CONF_TARGET)
    assert ap_path.read_text(encoding="utf-8") == UBUNTU_DEFAULT_APACHE_CONF

    # Second run is a clean no-op (Outcome.SKIPPED) with zero mutating calls.
    calls_before = len(host.calls)
    report2 = engine.run([step])
    assert report2.ok
    assert report2.outcomes[0].outcome is Outcome.SKIPPED
    new_calls = host.calls[calls_before:]
    assert not any(
        call[:2] in {("/usr/bin/apt-get", "install"), ("/usr/bin/systemctl", "reload")}
        for call in new_calls
    )


def test_horizon_step_fails_when_config_files_missing_after_package_install(
    tmp_path: Path,
) -> None:
    host = ServiceHost(tmp_path, installed={HORIZON_PACKAGE}, active={APACHE_UNIT})
    executor = _make_executor(tmp_path, host)
    step = make_horizon_step(tmp_path, SAMPLE_CONFIG, CONTROLLER_NODE, executor=executor)
    with pytest.raises(PreconditionError, match="local_settings.py"):
        step.apply()

    ls_path, _ = resolve_target(tmp_path, HORIZON_LOCAL_SETTINGS_TARGET)
    ls_path.parent.mkdir(parents=True, exist_ok=True)
    ls_path.write_text(UBUNTU_DEFAULT_LOCAL_SETTINGS, encoding="utf-8")
    with pytest.raises(PreconditionError, match="openstack-dashboard.conf"):
        step.apply()


def test_horizon_step_verify_reports_inactive_apache_and_missing_package(
    tmp_path: Path,
) -> None:
    ls_path, _ = resolve_target(tmp_path, HORIZON_LOCAL_SETTINGS_TARGET)
    ap_path, _ = resolve_target(tmp_path, HORIZON_APACHE_CONF_TARGET)
    ls_path.parent.mkdir(parents=True, exist_ok=True)
    ap_path.parent.mkdir(parents=True, exist_ok=True)
    ls_path.write_text(UBUNTU_DEFAULT_LOCAL_SETTINGS, encoding="utf-8")
    ap_path.write_text(UBUNTU_DEFAULT_APACHE_CONF, encoding="utf-8")
    edit_horizon_local_settings(tmp_path, CONTROLLER_NODE, dry_run=False)

    host = ServiceHost(tmp_path)
    executor = _make_executor(tmp_path, host)
    step = make_horizon_step(tmp_path, SAMPLE_CONFIG, CONTROLLER_NODE, executor=executor)

    assert step.check() is False
    vr = step.verify()
    assert vr.ok is False
    assert "openstack-dashboard" in vr.details
    assert "apache2" in vr.details


def test_horizon_step_fails_when_systemctl_reload_fails(tmp_path: Path) -> None:
    ls_path, _ = resolve_target(tmp_path, HORIZON_LOCAL_SETTINGS_TARGET)
    ap_path, _ = resolve_target(tmp_path, HORIZON_APACHE_CONF_TARGET)
    ls_path.parent.mkdir(parents=True, exist_ok=True)
    ap_path.parent.mkdir(parents=True, exist_ok=True)
    ls_path.write_text(UBUNTU_DEFAULT_LOCAL_SETTINGS, encoding="utf-8")
    ap_path.write_text(APACHE_CONF_WITHOUT_GLOBAL, encoding="utf-8")

    host = ServiceHost(
        tmp_path,
        installed={HORIZON_PACKAGE},
        active={APACHE_UNIT},
    )
    host.failures[("/usr/bin/systemctl", "reload", APACHE_UNIT)] = ProcessOutput(
        1, "", "Job for apache2.service failed.\n"
    )
    executor = _make_executor(tmp_path, host)
    step = make_horizon_step(tmp_path, SAMPLE_CONFIG, CONTROLLER_NODE, executor=executor)

    with pytest.raises(ExecutionError):
        step.apply()


def test_build_horizon_local_settings_block_contains_expected_directives() -> None:
    block = build_horizon_local_settings_block(CONTROLLER_NODE)
    assert f'OPENSTACK_HOST = "{HORIZON_OPENSTACK_HOST}"' in block
    assert f'"LOCATION": "{HORIZON_MEMCACHED_LOCATION}"' in block
    assert f"OPENSTACK_KEYSTONE_URL = {HORIZON_KEYSTONE_URL_EXPR}" in block
    assert SYSTEMCTL_RELOAD == "systemctl.reload"
    assert DEFAULT_REGISTRY.get(SYSTEMCTL_RELOAD).argv == (
        "/usr/bin/systemctl",
        "reload",
        "{unit}",
    )
