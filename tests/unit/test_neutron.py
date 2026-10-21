"""Unit tests for the ``neutron-controller`` and ``neutron-compute`` steps (runs nothing)."""

from __future__ import annotations

import json
import os
import socket
import subprocess
from pathlib import Path
from typing import Any

import pytest

from openstack_forge.execution import (
    AUDIT_FILE_MODE,
    DEFAULT_AUDIT_TARGET,
    DEFAULT_REGISTRY,
    DEFAULT_SECRETS_TARGET,
    DEFAULT_STATE_TARGET,
    DISABLED_LINE_PREFIX,
    MASK,
    NEUTRON_COMPUTE_STEP_ID,
    NEUTRON_CONF_TARGET,
    NEUTRON_CONTROLLER_STEP_ID,
    NEUTRON_DB_MANAGE_FAILURE_HINT,
    NEUTRON_DB_MANAGE_UPGRADE_HEAD,
    NEUTRON_DB_NAME,
    NEUTRON_DHCP_AGENT_CONF_TARGET,
    NEUTRON_L3_AGENT_CONF_TARGET,
    NEUTRON_METADATA_AGENT_CONF_TARGET,
    NEUTRON_ML2_CONF_TARGET,
    NEUTRON_OVS_AGENT_CONF_TARGET,
    OVS_PROVIDER_BRIDGE,
    OVS_VSCTL_ADD_BR,
    OVS_VSCTL_ADD_PORT,
    OVS_VSCTL_BR_EXISTS,
    OVS_VSCTL_FAILURE_HINT,
    OVS_VSCTL_LIST_PORTS,
    PARAM_TYPES,
    AuditLog,
    CommandExecutor,
    Engine,
    ExecutionError,
    Outcome,
    ParameterError,
    PreconditionError,
    SecretStore,
    StateStore,
    build_step,
    clear_registered_secrets,
    edit_neutron_compute_conf,
    edit_neutron_controller_conf,
    edit_neutron_dhcp_agent_conf,
    edit_neutron_l3_agent_conf,
    edit_neutron_metadata_agent_conf,
    edit_neutron_ml2_conf,
    edit_neutron_ovs_agent_conf,
    edit_nova_controller_neutron_conf,
    ini_comment_out_keys,
    ini_set_many,
    make_neutron_compute_step,
    make_neutron_controller_step,
    neutron_compute_conf_settings,
    neutron_controller_conf_settings,
    neutron_dhcp_agent_conf_settings,
    neutron_l3_agent_conf_settings,
    neutron_metadata_agent_conf_settings,
    neutron_ml2_conf_settings,
    neutron_ovs_agent_conf_settings,
    neutron_transport_url,
    nova_compute_neutron_conf_settings,
    nova_controller_neutron_conf_settings,
    parse_ovs_ports,
    resolve_provider_interface,
    verify_neutron_compute_conf_text,
    verify_neutron_controller_conf_text,
    verify_neutron_dhcp_agent_conf_text,
    verify_neutron_l3_agent_conf_text,
    verify_neutron_metadata_agent_conf_text,
    verify_neutron_ml2_conf_text,
    verify_neutron_ovs_agent_conf_text,
    verify_nova_controller_neutron_conf_text,
)
from tests.unit._service_fakes import DEV_MACHINE_SANITIZED_IP, ServiceHost

TEST_ADMIN_PASS = "TestAdminSecretValue0123456789AB"
TEST_RABBIT_PASS = "TestRabbitSecretValue0123456789A"
TEST_NEUTRON_DBPASS = "TestNeutronDbSecretValue01234567"
TEST_NEUTRON_PASS = "TestNeutronServiceSecretValue012"
TEST_NOVA_PASS = "TestNovaServiceSecretValue012345"
TEST_METADATA_SECRET = "TestMetadataSharedSecretValue012"

CONTROLLER_NODE: dict[str, Any] = {
    "role": "controller",
    "hostname": "controller",
    "management_ip": DEV_MACHINE_SANITIZED_IP,
    "provider_interface": "eno2",
}
COMPUTE_NODE: dict[str, Any] = {
    "role": "compute",
    "hostname": "compute1",
    "management_ip": "192.0.2.21",
    "provider_interface": "enp3s0",
}
SAMPLE_CONFIG: dict[str, Any] = {
    "network": {"provider_interface": "eth1"},
    "nodes": [CONTROLLER_NODE, COMPUTE_NODE],
}

UBUNTU_DEFAULT_NEUTRON_CONF = """\
[DEFAULT]
#core_plugin =
#service_plugins =
#transport_url = <None>
#auth_strategy = keystone

[agent]
root_helper = "sudo /usr/bin/neutron-rootwrap /etc/neutron/rootwrap.conf"

[database]
connection = sqlite:////var/lib/neutron/neutron.sqlite

[keystone_authtoken]
#www_authenticate_uri = <None>

[nova]
#auth_url = <None>

[oslo_concurrency]
lock_path = $state_path/lock
"""

UBUNTU_DEFAULT_ML2_CONF = """\
[DEFAULT]

[ml2]
#type_drivers = local,flat,vlan,gre,vxlan,geneve
#tenant_network_types = local
#mechanism_drivers =
#extension_drivers =

[ml2_type_flat]
#flat_networks = *

[ml2_type_vxlan]
#vni_ranges =

[securitygroup]
#enable_security_group = true
"""

UBUNTU_DEFAULT_OVS_AGENT_CONF = """\
[DEFAULT]

[agent]
#tunnel_types =
#l2_population = false

[ovs]
#bridge_mappings =
#local_ip = <None>

[securitygroup]
#firewall_driver = <None>
#enable_security_group = true
"""

UBUNTU_DEFAULT_L3_AGENT_CONF = """\
[DEFAULT]
#interface_driver = <None>
"""

UBUNTU_DEFAULT_DHCP_AGENT_CONF = """\
[DEFAULT]
#interface_driver = <None>
#dhcp_driver = neutron.agent.linux.dhcp.Dnsmasq
#enable_isolated_metadata = false
"""

UBUNTU_DEFAULT_METADATA_AGENT_CONF = """\
[DEFAULT]
#nova_metadata_host = 127.0.0.1
#metadata_proxy_shared_secret =
"""

UBUNTU_DEFAULT_NOVA_CONF = """\
[DEFAULT]
my_ip = 192.0.2.170

[neutron]
#auth_url = <None>
"""


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
                "NEUTRON_DBPASS": TEST_NEUTRON_DBPASS,
                "NEUTRON_PASS": TEST_NEUTRON_PASS,
                "NOVA_PASS": TEST_NOVA_PASS,
                "METADATA_SECRET": TEST_METADATA_SECRET,
            }
        ),
        encoding="utf-8",
    )
    os.chmod(secrets_file, 0o600)
    store = SecretStore(root)
    store.ensure()
    return store


def _seed_compute_secrets_only(root: Path) -> SecretStore:
    secrets_dir = root / "etc" / "openstack-forge"
    secrets_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(secrets_dir, 0o700)
    secrets_file = secrets_dir / "secrets.json"
    secrets_file.write_text(
        json.dumps(
            {
                "RABBIT_PASS": TEST_RABBIT_PASS,
                "NEUTRON_PASS": TEST_NEUTRON_PASS,
            }
        ),
        encoding="utf-8",
    )
    os.chmod(secrets_file, 0o600)
    return SecretStore(root)


def _controller_package_creates() -> dict[str, tuple[str, str]]:
    return {
        "neutron-server": ("etc/neutron/neutron.conf", UBUNTU_DEFAULT_NEUTRON_CONF),
        "neutron-plugin-ml2": (
            "etc/neutron/plugins/ml2/ml2_conf.ini",
            UBUNTU_DEFAULT_ML2_CONF,
        ),
        "neutron-openvswitch-agent": (
            "etc/neutron/plugins/ml2/openvswitch_agent.ini",
            UBUNTU_DEFAULT_OVS_AGENT_CONF,
        ),
        "neutron-l3-agent": ("etc/neutron/l3_agent.ini", UBUNTU_DEFAULT_L3_AGENT_CONF),
        "neutron-dhcp-agent": (
            "etc/neutron/dhcp_agent.ini",
            UBUNTU_DEFAULT_DHCP_AGENT_CONF,
        ),
        "neutron-metadata-agent": (
            "etc/neutron/metadata_agent.ini",
            UBUNTU_DEFAULT_METADATA_AGENT_CONF,
        ),
    }


def _compute_package_creates() -> dict[str, tuple[tuple[str, str], ...]]:
    return {
        "neutron-openvswitch-agent": (
            ("etc/neutron/neutron.conf", UBUNTU_DEFAULT_NEUTRON_CONF),
            (
                "etc/neutron/plugins/ml2/openvswitch_agent.ini",
                UBUNTU_DEFAULT_OVS_AGENT_CONF,
            ),
        ),
    }


def _seed_nova_conf(root: Path) -> None:
    nova_path = root / "etc/nova/nova.conf"
    nova_path.parent.mkdir(parents=True, exist_ok=True)
    nova_path.write_text(UBUNTU_DEFAULT_NOVA_CONF, encoding="utf-8")


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


def test_neutron_and_ovs_commands_are_registered_in_default_registry() -> None:
    spec_db = DEFAULT_REGISTRY.get(NEUTRON_DB_MANAGE_UPGRADE_HEAD)
    assert spec_db.argv == (
        "/usr/bin/neutron-db-manage",
        "--config-file",
        "/etc/neutron/neutron.conf",
        "--config-file",
        "/etc/neutron/plugins/ml2/ml2_conf.ini",
        "upgrade",
        "head",
    )
    assert spec_db.mutates is True
    assert spec_db.failure_hint == NEUTRON_DB_MANAGE_FAILURE_HINT

    spec_add_br = DEFAULT_REGISTRY.get(OVS_VSCTL_ADD_BR)
    assert spec_add_br.build_argv({"bridge": "br-provider"}) == (
        "/usr/bin/ovs-vsctl",
        "--may-exist",
        "add-br",
        "br-provider",
    )
    assert spec_add_br.mutates is True
    assert spec_add_br.failure_hint == OVS_VSCTL_FAILURE_HINT

    spec_add_port = DEFAULT_REGISTRY.get(OVS_VSCTL_ADD_PORT)
    assert spec_add_port.build_argv({"bridge": "br-provider", "interface": "eno2"}) == (
        "/usr/bin/ovs-vsctl",
        "--may-exist",
        "add-port",
        "br-provider",
        "eno2",
    )
    assert spec_add_port.mutates is True

    spec_br_exists = DEFAULT_REGISTRY.get(OVS_VSCTL_BR_EXISTS)
    assert spec_br_exists.build_argv({"bridge": "br-provider"}) == (
        "/usr/bin/ovs-vsctl",
        "br-exists",
        "br-provider",
    )
    assert spec_br_exists.mutates is False
    assert spec_br_exists.allowed_exit_codes == frozenset({0, 2})

    spec_list_ports = DEFAULT_REGISTRY.get(OVS_VSCTL_LIST_PORTS)
    assert spec_list_ports.build_argv({"bridge": "br-provider"}) == (
        "/usr/bin/ovs-vsctl",
        "list-ports",
        "br-provider",
    )
    assert spec_list_ports.mutates is False


@pytest.mark.parametrize(
    "valid_iface",
    ["eth1", "eno2", "enp3s0", "ens5", "br-provider", "bond0.100", "enx001122334455"],
)
def test_interface_name_param_type_accepts_valid_linux_ifnames(valid_iface: str) -> None:
    assert PARAM_TYPES["interface_name"].is_valid(valid_iface) is True


@pytest.mark.parametrize(
    "invalid_iface",
    ["", "-eth0", ".eth0", "eth0;rm", "eth0/1", "too_long_interface_name_16"],
)
def test_interface_name_param_type_rejects_invalid_ifnames(invalid_iface: str) -> None:
    spec = DEFAULT_REGISTRY.get(OVS_VSCTL_ADD_BR)
    with pytest.raises(ParameterError):
        spec.build_argv({"bridge": invalid_iface})


# --- ini_comment_out_keys tests ---------------------------------------------------------


def test_ini_comment_out_keys_comments_active_key_and_is_idempotent(tmp_path: Path) -> None:
    root = tmp_path / "root"
    conf = root / "etc/neutron/neutron.conf"
    conf.parent.mkdir(parents=True, exist_ok=True)
    conf.write_text(UBUNTU_DEFAULT_NEUTRON_CONF, encoding="utf-8")

    change1 = ini_comment_out_keys(root, NEUTRON_CONF_TARGET, (("database", "connection"),))
    assert change1.changed is True
    updated = conf.read_text(encoding="utf-8")
    assert (
        f"{DISABLED_LINE_PREFIX}connection = sqlite:////var/lib/neutron/neutron.sqlite" in updated
    )
    assert "\nconnection = " not in updated

    change2 = ini_comment_out_keys(root, NEUTRON_CONF_TARGET, (("database", "connection"),))
    assert change2.changed is False


def test_ini_set_many_combines_comment_out_and_entries_in_single_edit(
    tmp_path: Path,
) -> None:
    root = tmp_path / "root"
    conf = root / "etc/neutron/neutron.conf"
    conf.parent.mkdir(parents=True, exist_ok=True)
    conf.write_text(UBUNTU_DEFAULT_NEUTRON_CONF, encoding="utf-8")

    change = ini_set_many(
        root,
        NEUTRON_CONF_TARGET,
        (("DEFAULT", "transport_url", "rabbit://openstack:pw@controller:5672/"),),
        comment_out=(("database", "connection"),),
    )
    assert change.changed is True
    assert "-connection = sqlite:////var/lib/neutron/neutron.sqlite" in change.diff
    assert (
        f"+{DISABLED_LINE_PREFIX}connection = sqlite:////var/lib/neutron/neutron.sqlite"
        in change.diff
    )
    assert f"+transport_url = rabbit://openstack:{MASK}@controller:5672/" in change.diff


# --- resolve_provider_interface tests ---------------------------------------------------


def test_resolve_provider_interface_precedence_and_validation() -> None:
    # 1. local_node["provider_interface"] takes precedence over config
    assert resolve_provider_interface(CONTROLLER_NODE, SAMPLE_CONFIG) == "eno2"
    # 2. falls back to config["network"]["provider_interface"]
    node_without_override = {"role": "controller", "management_ip": DEV_MACHINE_SANITIZED_IP}
    assert resolve_provider_interface(node_without_override, SAMPLE_CONFIG) == "eth1"
    # 3. supports cluster.example.yaml structure (interfaces.provider & networks.provider.interface)
    assert (
        resolve_provider_interface(
            {"role": "compute", "interfaces": {"provider": "ens5"}},
            {},
        )
        == "ens5"
    )
    assert (
        resolve_provider_interface(
            {"role": "compute"},
            {"networks": {"provider": {"interface": "ens6"}}},
        )
        == "ens6"
    )

    # 4. missing provider interface raises PreconditionError (subclass of ExecutionError)
    with pytest.raises(PreconditionError, match="network.provider_interface"):
        resolve_provider_interface({"role": "controller"}, {})

    # 5. invalid or empty interface raises PreconditionError
    with pytest.raises(PreconditionError, match="provider_interface"):
        resolve_provider_interface({"provider_interface": ""}, SAMPLE_CONFIG)
    with pytest.raises(PreconditionError, match="provider_interface"):
        resolve_provider_interface({"provider_interface": "bad iface!"}, SAMPLE_CONFIG)


# --- Pure INI helper and verification tests --------------------------------------------


def test_pure_settings_builders_and_verify_helpers(tmp_path: Path) -> None:
    root = tmp_path / "root"
    for rel, content in _controller_package_creates().values():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    _seed_nova_conf(root)

    assert (
        len(
            neutron_controller_conf_settings(
                TEST_NEUTRON_DBPASS, TEST_RABBIT_PASS, TEST_NEUTRON_PASS, TEST_NOVA_PASS
            )
        )
        == 25
    )
    assert len(neutron_ml2_conf_settings()) == 6
    assert len(neutron_ovs_agent_conf_settings(CONTROLLER_NODE)) == 6
    assert len(neutron_l3_agent_conf_settings()) == 1
    assert len(neutron_dhcp_agent_conf_settings()) == 3
    assert len(neutron_metadata_agent_conf_settings(TEST_METADATA_SECRET)) == 2
    assert len(nova_controller_neutron_conf_settings(TEST_NEUTRON_PASS, TEST_METADATA_SECRET)) == 10
    assert len(neutron_compute_conf_settings(TEST_RABBIT_PASS)) == 2
    assert len(nova_compute_neutron_conf_settings(TEST_NEUTRON_PASS)) == 8

    edit_neutron_controller_conf(
        root,
        TEST_NEUTRON_DBPASS,
        TEST_RABBIT_PASS,
        TEST_NEUTRON_PASS,
        TEST_NOVA_PASS,
        dry_run=False,
    )
    edit_neutron_ml2_conf(root, dry_run=False)
    edit_neutron_ovs_agent_conf(root, CONTROLLER_NODE, dry_run=False)
    edit_neutron_l3_agent_conf(root, dry_run=False)
    edit_neutron_dhcp_agent_conf(root, dry_run=False)
    edit_neutron_metadata_agent_conf(root, TEST_METADATA_SECRET, dry_run=False)
    edit_nova_controller_neutron_conf(root, TEST_NEUTRON_PASS, TEST_METADATA_SECRET, dry_run=False)

    assert (
        verify_neutron_controller_conf_text(
            (root / "etc/neutron/neutron.conf").read_text(encoding="utf-8"),
            TEST_NEUTRON_DBPASS,
            TEST_RABBIT_PASS,
            TEST_NEUTRON_PASS,
            TEST_NOVA_PASS,
        )
        == []
    )
    assert (
        verify_neutron_ml2_conf_text(
            (root / "etc/neutron/plugins/ml2/ml2_conf.ini").read_text(encoding="utf-8")
        )
        == []
    )
    assert (
        verify_neutron_ovs_agent_conf_text(
            (root / "etc/neutron/plugins/ml2/openvswitch_agent.ini").read_text(encoding="utf-8"),
            CONTROLLER_NODE,
        )
        == []
    )
    assert (
        verify_neutron_l3_agent_conf_text(
            (root / "etc/neutron/l3_agent.ini").read_text(encoding="utf-8")
        )
        == []
    )
    assert (
        verify_neutron_dhcp_agent_conf_text(
            (root / "etc/neutron/dhcp_agent.ini").read_text(encoding="utf-8")
        )
        == []
    )
    assert (
        verify_neutron_metadata_agent_conf_text(
            (root / "etc/neutron/metadata_agent.ini").read_text(encoding="utf-8"),
            TEST_METADATA_SECRET,
        )
        == []
    )
    assert (
        verify_nova_controller_neutron_conf_text(
            (root / "etc/nova/nova.conf").read_text(encoding="utf-8"),
            TEST_NEUTRON_PASS,
            TEST_METADATA_SECRET,
        )
        == []
    )


def test_verify_helpers_mask_all_secrets_when_drifted() -> None:
    drifted_problems = verify_neutron_controller_conf_text(
        "[database]\nconnection = mysql+pymysql://neutron:WrongSecret@controller/neutron\n",
        TEST_NEUTRON_DBPASS,
        TEST_RABBIT_PASS,
        TEST_NEUTRON_PASS,
        TEST_NOVA_PASS,
    )
    joined = "\n".join(drifted_problems)
    for secret in (
        TEST_NEUTRON_DBPASS,
        TEST_RABBIT_PASS,
        TEST_NEUTRON_PASS,
        TEST_NOVA_PASS,
        "WrongSecret",
    ):
        assert secret not in joined
    assert MASK in joined

    meta_problems = verify_neutron_metadata_agent_conf_text(
        "[DEFAULT]\nnova_metadata_host = controller\nmetadata_proxy_shared_secret = WrongMeta\n",
        TEST_METADATA_SECRET,
    )
    meta_joined = "\n".join(meta_problems)
    assert TEST_METADATA_SECRET not in meta_joined
    assert "WrongMeta" not in meta_joined
    assert MASK in meta_joined


def test_verify_neutron_compute_conf_text_rejects_active_database_connection(
    tmp_path: Path,
) -> None:
    # Default Ubuntu neutron.conf still has active [database] connection = sqlite...
    problems_before = verify_neutron_compute_conf_text(
        UBUNTU_DEFAULT_NEUTRON_CONF, TEST_RABBIT_PASS
    )
    assert any("connection must be commented out in [database]" in p for p in problems_before)

    root = tmp_path / "root"
    conf = root / "etc/neutron/neutron.conf"
    conf.parent.mkdir(parents=True, exist_ok=True)
    conf.write_text(UBUNTU_DEFAULT_NEUTRON_CONF, encoding="utf-8")

    edit_neutron_compute_conf(root, TEST_RABBIT_PASS, dry_run=False)
    assert (
        verify_neutron_compute_conf_text(conf.read_text(encoding="utf-8"), TEST_RABBIT_PASS) == []
    )


def test_parse_ovs_ports_extracts_port_lines() -> None:
    assert parse_ovs_ports("eno2\npatch-tun\n\n") == frozenset({"eno2", "patch-tun"})
    assert parse_ovs_ports("") == frozenset()


# --- NeutronControllerStep lifecycle tests ---------------------------------------------


def test_neutron_controller_step_rejects_non_controller_role(tmp_path: Path) -> None:
    root = tmp_path / "root"
    host = ServiceHost(root)
    executor = _make_executor(root, host)
    with pytest.raises(ExecutionError, match="controller"):
        make_neutron_controller_step(root, SAMPLE_CONFIG, COMPUTE_NODE, executor=executor)


def test_neutron_controller_dry_run_preview_masks_all_secrets(tmp_path: Path) -> None:
    root = tmp_path / "root"
    _seed_controller_secrets(root)
    for rel, content in _controller_package_creates().values():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    _seed_nova_conf(root)

    host = ServiceHost(root)
    executor = _make_executor(root, host, dry_run=True)
    step = make_neutron_controller_step(root, SAMPLE_CONFIG, CONTROLLER_NODE, executor=executor)
    preview = step.dry_run_preview()
    assert len(preview.file_changes) == 7

    rendered = "\n".join(
        [
            *(fc.diff for fc in preview.file_changes),
            *preview.notes,
            *(str(c.params) for c in preview.commands),
        ]
    )
    # Check that diff text and notes never contain raw secrets:
    diff_and_notes = "\n".join([*(fc.diff for fc in preview.file_changes), *preview.notes])
    for secret in (
        TEST_ADMIN_PASS,
        TEST_RABBIT_PASS,
        TEST_NEUTRON_DBPASS,
        TEST_NEUTRON_PASS,
        TEST_NOVA_PASS,
        TEST_METADATA_SECRET,
    ):
        assert secret not in diff_and_notes
    assert rendered  # non-empty


def test_neutron_controller_full_lifecycle_and_idempotence(tmp_path: Path) -> None:
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
        NEUTRON_CONTROLLER_STEP_ID,
        root,
        SAMPLE_CONFIG,
        CONTROLLER_NODE,
        executor=executor,
    )

    assert step.check() is False

    state_path = root / DEFAULT_STATE_TARGET.lstrip("/")
    state_path.parent.mkdir(parents=True, exist_ok=True)
    engine = Engine(StateStore(state_path))
    report1 = engine.run([step])
    assert report1.ok
    assert report1.outcomes[0].outcome is Outcome.APPLIED
    assert host.neutron_db_upgraded is True
    assert host.ovs_bridges == {"br-provider": {"eno2"}}
    assert [ep["URL"] for ep in host.openstack_endpoints] == [
        "http://controller:9696",
        "http://controller:9696",
        "http://controller:9696",
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
        TEST_NEUTRON_DBPASS,
        TEST_NEUTRON_PASS,
        TEST_NOVA_PASS,
        TEST_METADATA_SECRET,
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
            "/usr/bin/ovs-vsctl",
        }
        if call[0] == "/usr/bin/ovs-vsctl":
            assert call[1] in {"br-exists", "list-ports"}


def test_neutron_controller_detects_ovs_bridge_and_port_drift(tmp_path: Path) -> None:
    root = tmp_path / "root"
    _seed_controller_secrets(root)
    _seed_nova_conf(root)

    host = ServiceHost(
        root,
        creates=_controller_package_creates(),
        openstack_projects=[{"ID": "proj-service", "Name": "service"}],
    )
    executor = _make_executor(root, host)
    step = make_neutron_controller_step(root, SAMPLE_CONFIG, CONTROLLER_NODE, executor=executor)
    step.apply()
    assert step.check() is True

    # Remove port from br-provider -> check() is False, verify() reports missing port
    host.ovs_bridges[OVS_PROVIDER_BRIDGE].clear()
    assert step.check() is False
    v_port = step.verify()
    assert v_port.ok is False
    assert "provider interface 'eno2' is not attached" in v_port.details

    # Remove bridge br-provider -> check() is False, verify() reports missing bridge
    host.ovs_bridges.clear()
    assert step.check() is False
    v_br = step.verify()
    assert v_br.ok is False
    assert "Open vSwitch bridge 'br-provider' does not exist" in v_br.details


# --- NeutronComputeStep lifecycle tests ------------------------------------------------


def test_neutron_compute_step_rejects_non_compute_role(tmp_path: Path) -> None:
    root = tmp_path / "root"
    host = ServiceHost(root)
    executor = _make_executor(root, host)
    with pytest.raises(ExecutionError, match="compute"):
        make_neutron_compute_step(root, SAMPLE_CONFIG, CONTROLLER_NODE, executor=executor)


def test_neutron_compute_least_privilege_secrets_and_full_lifecycle(
    tmp_path: Path,
) -> None:
    root = tmp_path / "root"
    # Seed ONLY RABBIT_PASS and NEUTRON_PASS (no ADMIN_PASS, NEUTRON_DBPASS, NOVA_PASS,
    # or METADATA_SECRET on the compute node):
    _seed_compute_secrets_only(root)
    _seed_nova_conf(root)

    host = ServiceHost(
        root,
        creates=_compute_package_creates(),
    )
    executor = _make_executor(root, host)
    step = build_step(
        NEUTRON_COMPUTE_STEP_ID,
        root,
        SAMPLE_CONFIG,
        COMPUTE_NODE,
        executor=executor,
    )

    assert step.check() is False

    state_path = root / DEFAULT_STATE_TARGET.lstrip("/")
    state_path.parent.mkdir(parents=True, exist_ok=True)
    engine = Engine(StateStore(state_path))
    report1 = engine.run([step])
    assert report1.ok
    assert report1.outcomes[0].outcome is Outcome.APPLIED
    assert host.ovs_bridges == {OVS_PROVIDER_BRIDGE: {"enp3s0"}}
    assert step.verify().ok is True
    assert step.check() is True

    # Verify [database] connection is commented out on the compute node:
    compute_neutron_conf = (root / "etc/neutron/neutron.conf").read_text(encoding="utf-8")
    assert (
        f"{DISABLED_LINE_PREFIX}connection = sqlite:////var/lib/neutron/neutron.sqlite"
        in compute_neutron_conf
    )
    assert f"transport_url = {neutron_transport_url(TEST_RABBIT_PASS)}" in compute_neutron_conf

    # Re-running via Engine is idempotent (SKIPPED)
    report2 = engine.run([step])
    assert report2.ok
    assert report2.outcomes[0].outcome is Outcome.SKIPPED

    # Re-introducing an active [database] connection line causes check() and verify() to fail:
    (root / "etc/neutron/neutron.conf").write_text(
        compute_neutron_conf + "\n[database]\nconnection = sqlite:////tmp/bad.db\n",
        encoding="utf-8",
    )
    assert step.check() is False
    v_db = step.verify()
    assert v_db.ok is False
    assert "connection must be commented out in [database]" in v_db.details


def test_neutron_compute_fails_when_nova_conf_missing(tmp_path: Path) -> None:
    root = tmp_path / "root"
    _seed_compute_secrets_only(root)
    host = ServiceHost(root, creates=_compute_package_creates())
    executor = _make_executor(root, host)
    step = make_neutron_compute_step(root, SAMPLE_CONFIG, COMPUTE_NODE, executor=executor)
    with pytest.raises(PreconditionError, match="/etc/nova/nova.conf"):
        step.apply()


def test_neutron_steps_fail_when_secrets_missing(tmp_path: Path) -> None:
    root = tmp_path / "root"
    _seed_nova_conf(root)
    host = ServiceHost(root)
    executor = _make_executor(root, host)

    ctrl_step = make_neutron_controller_step(
        root, SAMPLE_CONFIG, CONTROLLER_NODE, executor=executor
    )
    assert ctrl_step.check() is False
    with pytest.raises(PreconditionError, match=DEFAULT_SECRETS_TARGET):
        ctrl_step.apply()

    comp_step = make_neutron_compute_step(root, SAMPLE_CONFIG, COMPUTE_NODE, executor=executor)
    assert comp_step.check() is False
    with pytest.raises(PreconditionError, match=DEFAULT_SECRETS_TARGET):
        comp_step.apply()


def test_neutron_controller_detects_package_unit_db_and_keystone_drift(
    tmp_path: Path,
) -> None:
    root = tmp_path / "root"
    _seed_controller_secrets(root)
    _seed_nova_conf(root)

    host = ServiceHost(
        root,
        creates=_controller_package_creates(),
        openstack_projects=[{"ID": "proj-service", "Name": "service"}],
    )
    executor = _make_executor(root, host)
    step = make_neutron_controller_step(root, SAMPLE_CONFIG, CONTROLLER_NODE, executor=executor)
    step.apply()
    assert step.check() is True

    # 1. Missing package
    host.installed.discard("neutron-l3-agent")
    assert step.check() is False
    assert "package 'neutron-l3-agent' is not installed" in step.verify().details
    host.installed.add("neutron-l3-agent")
    assert step.check() is True

    # 2. Inactive unit
    host.active.discard("neutron-server")
    assert step.check() is False
    assert "the neutron-server service is not active" in step.verify().details
    host.active.add("neutron-server")
    assert step.check() is True

    # 3. Missing MariaDB database
    host.mariadb_databases.discard(NEUTRON_DB_NAME)
    assert step.check() is False
    assert "MariaDB database 'neutron' does not exist" in step.verify().details
    host.mariadb_databases.add(NEUTRON_DB_NAME)
    assert step.check() is True

    # 4. Missing Keystone endpoint
    saved_endpoints = list(host.openstack_endpoints)
    host.openstack_endpoints.pop()
    assert step.check() is False
    assert (
        "Keystone admin endpoint for 'neutron' ('network') in RegionOne does not exist"
        in step.verify().details
    )
    host.openstack_endpoints = saved_endpoints
    assert step.check() is True


def test_neutron_steps_dry_run_masks_secrets_and_works_without_secrets(
    tmp_path: Path,
) -> None:
    root = tmp_path / "root"
    _seed_nova_conf(root)
    host = ServiceHost(root)
    executor = _make_executor(root, host)

    # dry_run_preview works when secrets.json does not exist yet
    ctrl_step = make_neutron_controller_step(
        root, SAMPLE_CONFIG, CONTROLLER_NODE, executor=executor
    )
    comp_step = make_neutron_compute_step(root, SAMPLE_CONFIG, COMPUTE_NODE, executor=executor)
    p_ctrl_missing = ctrl_step.dry_run_preview()
    p_comp_missing = comp_step.dry_run_preview()
    assert len(p_ctrl_missing.commands) == 23
    assert len(p_comp_missing.commands) == 6
    assert any(DEFAULT_SECRETS_TARGET in note for note in p_ctrl_missing.notes)
    assert any(DEFAULT_SECRETS_TARGET in note for note in p_comp_missing.notes)

    # Seed secrets and config files, then verify dry_run_preview diff masks all secret values
    _seed_controller_secrets(root)
    for rel in (
        NEUTRON_CONF_TARGET,
        NEUTRON_ML2_CONF_TARGET,
        NEUTRON_OVS_AGENT_CONF_TARGET,
        NEUTRON_L3_AGENT_CONF_TARGET,
        NEUTRON_DHCP_AGENT_CONF_TARGET,
        NEUTRON_METADATA_AGENT_CONF_TARGET,
    ):
        p = root / rel.lstrip("/")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("[DEFAULT]\n", encoding="utf-8")

    p_ctrl = ctrl_step.dry_run_preview()
    assert len(p_ctrl.file_changes) == 7
    combined_diff = "\n".join(fc.diff for fc in p_ctrl.file_changes)
    assert MASK in combined_diff
    for secret in (
        TEST_ADMIN_PASS,
        TEST_RABBIT_PASS,
        TEST_NEUTRON_DBPASS,
        TEST_NEUTRON_PASS,
        TEST_NOVA_PASS,
        TEST_METADATA_SECRET,
    ):
        assert secret not in combined_diff
