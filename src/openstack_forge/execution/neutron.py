"""The ``neutron-controller`` (controller only) and ``neutron-compute`` (compute only) steps.

**Unverified: neither step has ever been run on a real host.**

Sources of truth
================
Read directly from the official OpenStack 2025.1 (Epoxy) Neutron installation guides for Ubuntu
(Networking Option 2: Self-service networks with Open vSwitch):

1. **Controller node**:
   - https://docs.openstack.org/neutron/2025.1/install/controller-install-ubuntu.html
     (raw source: ``doc/source/install/controller-install-ubuntu.rst`` on ``stable/2025.1`` in
     ``openstack/neutron``).
   - https://docs.openstack.org/neutron/2025.1/install/controller-install-option2-ubuntu.html
     (raw source: ``doc/source/install/controller-install-option2-ubuntu.rst`` on
     ``stable/2025.1`` in ``openstack/neutron``).
2. **Compute node**:
   - https://docs.openstack.org/neutron/2025.1/install/compute-install-ubuntu.html
     (raw source: ``doc/source/install/compute-install-ubuntu.rst`` on ``stable/2025.1`` in
     ``openstack/neutron``).
   - https://docs.openstack.org/neutron/2025.1/install/compute-install-option2-ubuntu.html
     (raw source: ``doc/source/install/compute-install-option2-ubuntu.rst`` on ``stable/2025.1``
     in ``openstack/neutron``).

What the official controller pages prescribe
--------------------------------------------
1. **Prerequisites — SQL database** (``mysql`` as ``root``):
   - ``CREATE DATABASE neutron;``
   - ``GRANT ALL PRIVILEGES ON neutron.* TO 'neutron'@'localhost' IDENTIFIED BY 'NEUTRON_DBPASS';``
   - ``GRANT ALL PRIVILEGES ON neutron.* TO 'neutron'@'%' IDENTIFIED BY 'NEUTRON_DBPASS';``
2. **Prerequisites — Keystone service credentials and API endpoints** (after ``. admin-openrc``):
   - ``openstack user create --domain default --password-prompt neutron`` (``NEUTRON_PASS``)
   - ``openstack role add --project service --user neutron admin``
   - ``openstack service create --name neutron --description "OpenStack Networking" network``
   - ``openstack endpoint create --region RegionOne network public http://controller:9696``
   - ``openstack endpoint create --region RegionOne network internal http://controller:9696``
   - ``openstack endpoint create --region RegionOne network admin http://controller:9696``
3. **Networking Option 2 (Self-service networks with Open vSwitch) — Install the packages**:
   - ``apt install neutron-server neutron-plugin-ml2 neutron-openvswitch-agent
     neutron-l3-agent neutron-dhcp-agent neutron-metadata-agent``
4. **Configure the server component (``/etc/neutron/neutron.conf``)**:
   - ``[database]``:
     ``connection = mysql+pymysql://neutron:NEUTRON_DBPASS@controller/neutron``
   - ``[DEFAULT]``:
     ``core_plugin = ml2``,
     ``service_plugins = router``,
     ``transport_url = rabbit://openstack:RABBIT_PASS@controller`` (normalized to
     ``rabbit://openstack:RABBIT_PASS@controller:5672/``; see deviation #3 below),
     ``auth_strategy = keystone``,
     ``notify_nova_on_port_status_changes = true``,
     ``notify_nova_on_port_data_changes = true``
   - ``[keystone_authtoken]``:
     ``www_authenticate_uri = http://controller:5000``,
     ``auth_url = http://controller:5000``,
     ``memcached_servers = controller:11211``,
     ``auth_type = password``,
     ``project_domain_name = Default``,
     ``user_domain_name = Default``,
     ``project_name = service``,
     ``username = neutron``,
     ``password = NEUTRON_PASS``
   - ``[nova]``:
     ``auth_url = http://controller:5000``,
     ``auth_type = password``,
     ``project_domain_name = Default``,
     ``user_domain_name = Default``,
     ``region_name = RegionOne``,
     ``project_name = service``,
     ``username = nova``,
     ``password = NOVA_PASS``
   - ``[oslo_concurrency]``:
     ``lock_path = /var/lib/neutron/tmp``
5. **Configure the Modular Layer 2 (ML2) plug-in (``/etc/neutron/plugins/ml2/ml2_conf.ini``)**:
   - ``[ml2]``:
     ``type_drivers = flat,vlan,vxlan``,
     ``tenant_network_types = vxlan``,
     ``mechanism_drivers = openvswitch,l2population``,
     ``extension_drivers = port_security``
   - ``[ml2_type_flat]``:
     ``flat_networks = provider``
   - ``[ml2_type_vxlan]``:
     ``vni_ranges = 1:1000``
6. **Configure the Open vSwitch agent (``/etc/neutron/plugins/ml2/openvswitch_agent.ini``)**:
   - ``[ovs]``:
     ``bridge_mappings = provider:br-provider``,
     ``local_ip = <controller management/overlay IP>``
   - ``[agent]``:
     ``tunnel_types = vxlan``,
     ``l2_population = true``
   - ``[securitygroup]``:
     ``enable_security_group = true``,
     ``firewall_driver = openvswitch``
7. **Configure the layer-3 agent (``/etc/neutron/l3_agent.ini``)**:
   - ``[DEFAULT]``:
     ``interface_driver = openvswitch``
8. **Configure the DHCP agent (``/etc/neutron/dhcp_agent.ini``)**:
   - ``[DEFAULT]``:
     ``interface_driver = openvswitch``,
     ``dhcp_driver = neutron.agent.linux.dhcp.Dnsmasq``,
     ``enable_isolated_metadata = true``
9. **Configure the Open vSwitch provider bridge and attach the provider physical interface**:
   - ``service openvswitch-switch restart``
   - ``ovs-vsctl add-br br-provider``
   - ``ovs-vsctl add-port br-provider PROVIDER_INTERFACE_NAME``
10. **Configure the metadata agent (``/etc/neutron/metadata_agent.ini``)**:
    - ``[DEFAULT]``:
      ``nova_metadata_host = controller``,
      ``metadata_proxy_shared_secret = METADATA_SECRET``
11. **Configure Compute to use Networking (``/etc/nova/nova.conf``)**:
    - ``[neutron]``:
      ``auth_url = http://controller:5000``,
      ``auth_type = password``,
      ``project_domain_name = Default``,
      ``user_domain_name = Default``,
      ``region_name = RegionOne``,
      ``project_name = service``,
      ``username = neutron``,
      ``password = NEUTRON_PASS``,
      ``service_metadata_proxy = true``,
      ``metadata_proxy_shared_secret = METADATA_SECRET``
12. **Finalize installation**:
    - ``su -s /bin/sh -c "neutron-db-manage --config-file /etc/neutron/neutron.conf
      --config-file /etc/neutron/plugins/ml2/ml2_conf.ini upgrade head" neutron``
    - ``service nova-api restart``
    - ``service neutron-server restart``
    - ``service neutron-openvswitch-agent restart``
    - ``service neutron-dhcp-agent restart``
    - ``service neutron-metadata-agent restart``
    - ``service neutron-l3-agent restart``

What the official compute pages prescribe
-----------------------------------------
1. **Install the Open vSwitch agent package**:
   - ``apt install neutron-openvswitch-agent``
2. **Configure the common component (``/etc/neutron/neutron.conf``)**:
   - In ``[database]``, comment out any ``connection`` options because compute nodes do not
     directly access the database.
   - ``[DEFAULT]``:
     ``transport_url = rabbit://openstack:RABBIT_PASS@controller`` (normalized to
     ``rabbit://openstack:RABBIT_PASS@controller:5672/``; see deviation #3 below)
   - ``[oslo_concurrency]``:
     ``lock_path = /var/lib/neutron/tmp``
3. **Configure the Open vSwitch agent (``/etc/neutron/plugins/ml2/openvswitch_agent.ini``)**:
   - ``[ovs]``:
     ``bridge_mappings = provider:br-provider``,
     ``local_ip = <compute node management/overlay IP>``
   - ``[agent]``:
     ``tunnel_types = vxlan``,
     ``l2_population = true``
   - ``[securitygroup]``:
     ``enable_security_group = true``,
     ``firewall_driver = openvswitch``
4. **Configure the Open vSwitch provider bridge and attach the provider physical interface**:
   - ``service openvswitch-switch restart``
   - ``ovs-vsctl add-br br-provider``
   - ``ovs-vsctl add-port br-provider PROVIDER_INTERFACE_NAME``
5. **Configure the Compute service to use the Networking service (``/etc/nova/nova.conf``)**:
   - ``[neutron]``:
     ``auth_url = http://controller:5000``,
     ``auth_type = password``,
     ``project_domain_name = Default``,
     ``user_domain_name = Default``,
     ``region_name = RegionOne``,
     ``project_name = service``,
     ``username = neutron``,
     ``password = NEUTRON_PASS``
6. **Finalize installation**:
   - ``service nova-compute restart``
   - ``service neutron-openvswitch-agent restart``

Ubuntu 24.04 + Epoxy Cloud Archive package facts (established read-only on the dev machine)
-------------------------------------------------------------------------------------------
- ``apt-cache policy neutron-server neutron-plugin-ml2 neutron-openvswitch-agent
  neutron-l3-agent neutron-dhcp-agent neutron-metadata-agent neutron-common python3-neutron
  openvswitch-switch``:
  - ``neutron-*`` and ``python3-neutron``: ``2:26.0.0-0ubuntu1~cloud1`` in
    ``noble-updates/epoxy/main`` (Ubuntu Cloud Archive for OpenStack 2025.1 Epoxy) and
    ``2:24.1.0-0ubuntu1.2`` in ``noble-updates/main`` (``2:24.0.0-0ubuntu1`` in ``noble/main``);
  - ``openvswitch-switch``: ``3.5.1-0ubuntu0.1~cloud0`` in ``noble-updates/epoxy/main`` and
    ``3.3.9-0ubuntu0.24.04.1`` in ``noble-updates/main`` (``3.3.0-1ubuntu3`` in ``noble/main``).
- ``apt-cache show``:
  - ``neutron-server``, ``neutron-plugin-ml2``, ``neutron-openvswitch-agent``,
    ``neutron-l3-agent``, ``neutron-dhcp-agent``, and ``neutron-metadata-agent`` all depend on
    ``neutron-common`` (which depends on ``python3-neutron``);
  - ``neutron-openvswitch-agent`` depends on ``openvswitch-switch``.
- ``dpkg -S`` and ``dpkg-query -W -f='${Conffiles}\\n' neutron-common`` confirm:
  - ``python3-neutron: /usr/bin/neutron-db-manage``;
  - ``openvswitch-switch: /usr/bin/ovs-vsctl``;
  - ``neutron-common`` ships all 6 conffiles:
    - ``/etc/neutron/neutron.conf`` (ships an active ``connection =
      sqlite:////var/lib/neutron/neutron.sqlite`` under ``[database]`` and ``lock_path =
      $state_path/lock`` under ``[oslo_concurrency]``);
    - ``/etc/neutron/plugins/ml2/ml2_conf.ini`` (includes ``[ml2]``, ``[ml2_type_flat]``,
      ``[ml2_type_vxlan]``, and ``[securitygroup]`` sections);
    - ``/etc/neutron/plugins/ml2/openvswitch_agent.ini`` (includes ``[ovs]``, ``[agent]``, and
      ``[securitygroup]`` sections);
    - ``/etc/neutron/l3_agent.ini`` (includes ``[DEFAULT]``);
    - ``/etc/neutron/dhcp_agent.ini`` (includes ``[DEFAULT]``);
    - ``/etc/neutron/metadata_agent.ini`` (includes ``[DEFAULT]``);
  - ``neutron-common.postinst`` creates system user/group ``neutron`` (UID/GID ``64065``), sets
    ``/etc/neutron`` to ``0750`` ``root:neutron`` and conffiles to ``0640`` ``root:neutron``,
    and creates ``/var/log/neutron`` (``0750`` ``neutron:adm``).
- Systemd service units:
  - ``/usr/lib/systemd/system/neutron-server.service``,
    ``neutron-openvswitch-agent.service``, ``neutron-l3-agent.service``,
    ``neutron-dhcp-agent.service``, ``neutron-metadata-agent.service``, and
    ``openvswitch-switch.service``.
- ``/usr/lib/python3/dist-packages/neutron/db/migration/cli.py``:
  - ``add_command_parsers()`` registers ``upgrade`` (``--revision`` positional ``head``);
    ``--config-file /etc/neutron/neutron.conf --config-file
    /etc/neutron/plugins/ml2/ml2_conf.ini upgrade head`` is natively idempotent when the
    Alembic schema is already at ``head``.
- ``ovs-vsctl(8)``:
  - ``[--may-exist] add-br bridge`` and ``[--may-exist] add-port bridge port`` succeed
    idempotently when ``--may-exist`` is passed; without ``--may-exist`` they fail if the bridge
    or port already exists;
  - ``br-exists bridge`` exits ``0`` if ``bridge`` exists and ``2`` if ``bridge`` does not exist;
  - ``list-ports bridge`` prints attached ports one per line on stdout (excluding the bridge's
    own local port).

Deviations, trade-offs, and scope boundary
------------------------------------------
1. **Open vSwitch (``openvswitch``) in 2025.1 Option 2 (and residual "Linux bridge" prose)**:
   In the live 2025.1 Ubuntu Option 2 pages (``controller-install-option2-ubuntu.html`` and
   ``compute-install-option2-ubuntu.html``), all command and configuration ``<pre>`` blocks
   already use Open vSwitch (``apt install ... neutron-openvswitch-agent``,
   ``mechanism_drivers = openvswitch,l2population``, ``openvswitch_agent.ini``,
   ``firewall_driver = openvswitch``, ``interface_driver = openvswitch``, and
   ``ovs-vsctl add-br br-provider`` / ``ovs-vsctl add-port br-provider PROVIDER_INTERFACE_NAME``),
   although several surrounding prose paragraphs on both pages still refer to "Linux bridge" /
   "Linux bridge agent". Ubuntu 24.04 + Epoxy Cloud Archive ships ``neutron-openvswitch-agent``
   (``2:26.0.0-0ubuntu1~cloud1``) and ``openvswitch-switch`` (``3.5.1-0ubuntu0.1~cloud0``).
   OpenStack Forge follows the Open vSwitch commands and configuration blocks from the 2025.1
   Option 2 pages.
2. **Idempotent ``ovs-vsctl --may-exist add-br`` and ``ovs-vsctl --may-exist add-port``**:
   ``ovs-vsctl(8)`` specifies that ``add-br`` and ``add-port`` fail on re-runs unless
   ``--may-exist`` is supplied. OpenStack Forge passes ``--may-exist`` to both commands and
   verifies bridge existence (``ovs-vsctl br-exists br-provider``, allowed exit codes ``0`` and
   ``2``) and port attachment (``ovs-vsctl list-ports br-provider``) in ``check()`` and
   ``verify()``.
3. **``[DEFAULT] transport_url = rabbit://openstack:RABBIT_PASS@controller:5672/`` on both roles**:
   The Neutron Ubuntu guides write ``rabbit://openstack:RABBIT_PASS@controller`` without
   ``:5672/``. Consistent with ``glance.py`` and ``nova.py``, OpenStack Forge uses the explicit
   ``rabbit://openstack:RABBIT_PASS@controller:5672/`` URL on both controller and compute nodes.
4. **Explicitly commenting out ``[database] connection`` on compute nodes**:
   ``compute-install-ubuntu.rst`` instructs operators to comment out any ``connection`` options
   in ``[database]`` of ``/etc/neutron/neutron.conf`` on compute nodes, and Ubuntu's
   ``neutron-common`` package ships an active ``connection =
   sqlite:////var/lib/neutron/neutron.sqlite`` line. ``NeutronComputeStep`` uses
   ``ini_set_many(..., comment_out=(("database", "connection"),))`` and verifies that no active
   ``connection`` key remains in ``[database]``.
5. **Least-privilege secrets on compute nodes**:
   ``NeutronComputeStep`` loads only ``RABBIT_PASS`` and ``NEUTRON_PASS`` from
   :class:`SecretStore`; ``ADMIN_PASS``, ``NEUTRON_DBPASS``, ``NOVA_PASS``, and
   ``METADATA_SECRET`` are controller-only secrets and are never required on compute nodes.
6. **No kernel sysctl (``net.bridge.bridge-nf-call-iptables``) mutation**:
   The ``br_netfilter`` sysctl instructions in the guide were specific to the deprecated
   ``iptables_hybrid`` / ``linuxbridge`` firewall path; native ``firewall_driver = openvswitch``
   uses OpenFlow rules in OVS rather than Linux bridge netfilter.
7. **``neutron-db-manage`` invoked directly as ``root`` instead of ``su``**:
   ``CommandSpec`` forbids ``su`` and ``sudo``. Editing ``/etc/neutron/neutron.conf`` and
   ``/etc/neutron/plugins/ml2/ml2_conf.ini`` before running ``/usr/bin/neutron-db-manage``
   ensures schema migrations run against MariaDB rather than SQLite.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from ipaddress import IPv4Address
from pathlib import Path
from typing import Any

import yaml

from openstack_forge.execution.commands import (
    APT_GET_INSTALL,
    NEUTRON_DB_MANAGE_UPGRADE_HEAD,
    OPENSTACK_ENDPOINT_CREATE,
    OPENSTACK_ENDPOINT_LIST,
    OPENSTACK_ROLE_ADD_USER_PROJECT,
    OPENSTACK_ROLE_ASSIGNMENT_LIST,
    OPENSTACK_SERVICE_CREATE,
    OPENSTACK_SERVICE_LIST,
    OPENSTACK_USER_CREATE,
    OPENSTACK_USER_LIST,
    OVS_VSCTL_ADD_BR,
    OVS_VSCTL_ADD_PORT,
    OVS_VSCTL_BR_EXISTS,
    OVS_VSCTL_LIST_PORTS,
    SYSTEMCTL_RESTART,
    CommandExecutor,
    validate_parameter,
)
from openstack_forge.execution.engine import VerifyResult
from openstack_forge.execution.errors import (
    ExecutionError,
    FileFormatError,
    ParameterError,
    PreconditionError,
    SecretError,
    UnexpectedExitError,
    UnsafePathError,
)
from openstack_forge.execution.files import FileChange, ini_set_many
from openstack_forge.execution.fsutil import read_text_file, resolve_target
from openstack_forge.execution.openstack_cli import (
    ADMIN_ROLE_NAME,
    ENDPOINT_INTERFACES,
    SERVICE_PROJECT_NAME,
    ensure_service_catalog,
    ensure_service_user,
    service_catalog_problems,
    service_catalog_ready,
    service_user_problems,
    service_user_ready,
)
from openstack_forge.execution.preview import PlannedCommand, StepPreview
from openstack_forge.execution.redaction import MASK, redact_text
from openstack_forge.execution.secrets import (
    DEFAULT_SECRET_LENGTH,
    DEFAULT_SECRETS_TARGET,
    SecretStore,
)
from openstack_forge.execution.service_checks import (
    package_installed,
    query_active,
    query_package,
    unit_active_problems,
)
from openstack_forge.execution.sql import (
    QUERY_MARIADB_STATE,
    plan_service_database_sql,
    provision_service_database,
    service_database_problems,
    service_database_ready,
)

NEUTRON_CONTROLLER_STEP_ID = "neutron-controller"
NEUTRON_COMPUTE_STEP_ID = "neutron-compute"

NEUTRON_CONTROLLER_PACKAGES: tuple[str, ...] = (
    "neutron-server",
    "neutron-plugin-ml2",
    "neutron-openvswitch-agent",
    "neutron-l3-agent",
    "neutron-dhcp-agent",
    "neutron-metadata-agent",
)
# Official guide finalize restart order (controller-install-ubuntu.rst):
NEUTRON_CONTROLLER_UNITS: tuple[str, ...] = (
    "nova-api",
    "neutron-server",
    "neutron-openvswitch-agent",
    "neutron-dhcp-agent",
    "neutron-metadata-agent",
    "neutron-l3-agent",
)

NEUTRON_COMPUTE_PACKAGE = "neutron-openvswitch-agent"
# Official guide finalize restart order (compute-install-ubuntu.rst lines 187-188):
NEUTRON_COMPUTE_UNITS: tuple[str, ...] = (
    "nova-compute",
    "neutron-openvswitch-agent",
)

OVS_SWITCH_UNIT = "openvswitch-switch"
OVS_PROVIDER_BRIDGE = "br-provider"
ALLOW_SHARED_PROVIDER_INTERFACE_KEY = "allow_shared_provider_interface"
NETPLAN_DIR_TARGET = "/etc/netplan"

NEUTRON_DB_NAME = "neutron"
NEUTRON_DB_USER = "neutron"

NEUTRON_ADMIN_SECRET_NAME = "ADMIN_PASS"
NEUTRON_RABBIT_SECRET_NAME = "RABBIT_PASS"
NEUTRON_DB_SECRET_NAME = "NEUTRON_DBPASS"
NEUTRON_SERVICE_SECRET_NAME = "NEUTRON_PASS"
NEUTRON_NOVA_SECRET_NAME = "NOVA_PASS"
NEUTRON_METADATA_SECRET_NAME = "METADATA_SECRET"

NEUTRON_SERVICE_USER = "neutron"
NEUTRON_SERVICE_NAME = "neutron"
NEUTRON_SERVICE_TYPE = "network"
NEUTRON_ENDPOINT_URL = "http://controller:9696"

NEUTRON_CONF_TARGET = "/etc/neutron/neutron.conf"
NEUTRON_ML2_CONF_TARGET = "/etc/neutron/plugins/ml2/ml2_conf.ini"
NEUTRON_OVS_AGENT_CONF_TARGET = "/etc/neutron/plugins/ml2/openvswitch_agent.ini"
NEUTRON_L3_AGENT_CONF_TARGET = "/etc/neutron/l3_agent.ini"
NEUTRON_DHCP_AGENT_CONF_TARGET = "/etc/neutron/dhcp_agent.ini"
NEUTRON_METADATA_AGENT_CONF_TARGET = "/etc/neutron/metadata_agent.ini"
NOVA_CONF_TARGET = "/etc/nova/nova.conf"

NEUTRON_LOCK_PATH = "/var/lib/neutron/tmp"
NEUTRON_DHCP_DRIVER = "neutron.agent.linux.dhcp.Dnsmasq"
NEUTRON_COMPUTE_COMMENTED_OUT_KEYS: tuple[tuple[str, str], ...] = (("database", "connection"),)

_DRY_RUN_PLACEHOLDER_SECRET = "A" * DEFAULT_SECRET_LENGTH

INSTALL_NEUTRON_CONTROLLER_PACKAGES: tuple[PlannedCommand, ...] = tuple(
    PlannedCommand(APT_GET_INSTALL, {"package": pkg}) for pkg in NEUTRON_CONTROLLER_PACKAGES
)
RESTART_OVS_SWITCH_UNIT = PlannedCommand(SYSTEMCTL_RESTART, {"unit": OVS_SWITCH_UNIT})
RUN_OVS_ADD_PROVIDER_BRIDGE = PlannedCommand(OVS_VSCTL_ADD_BR, {"bridge": OVS_PROVIDER_BRIDGE})
QUERY_OVS_PROVIDER_BRIDGE_EXISTS = PlannedCommand(
    OVS_VSCTL_BR_EXISTS, {"bridge": OVS_PROVIDER_BRIDGE}
)
QUERY_OVS_PROVIDER_BRIDGE_PORTS = PlannedCommand(
    OVS_VSCTL_LIST_PORTS, {"bridge": OVS_PROVIDER_BRIDGE}
)
RUN_NEUTRON_DB_UPGRADE_HEAD = PlannedCommand(NEUTRON_DB_MANAGE_UPGRADE_HEAD)

RESTART_NEUTRON_CONTROLLER_UNITS: tuple[PlannedCommand, ...] = tuple(
    PlannedCommand(SYSTEMCTL_RESTART, {"unit": unit}) for unit in NEUTRON_CONTROLLER_UNITS
)
QUERY_NEUTRON_CONTROLLER_PACKAGES: tuple[PlannedCommand, ...] = tuple(
    query_package(pkg) for pkg in NEUTRON_CONTROLLER_PACKAGES
)
QUERY_NEUTRON_CONTROLLER_ACTIVE: tuple[PlannedCommand, ...] = (
    query_active(OVS_SWITCH_UNIT),
    *(query_active(unit) for unit in NEUTRON_CONTROLLER_UNITS),
)

INSTALL_NEUTRON_COMPUTE_PACKAGE = PlannedCommand(
    APT_GET_INSTALL, {"package": NEUTRON_COMPUTE_PACKAGE}
)
RESTART_NEUTRON_COMPUTE_UNITS: tuple[PlannedCommand, ...] = tuple(
    PlannedCommand(SYSTEMCTL_RESTART, {"unit": unit}) for unit in NEUTRON_COMPUTE_UNITS
)
QUERY_NEUTRON_COMPUTE_PACKAGE = query_package(NEUTRON_COMPUTE_PACKAGE)
QUERY_NEUTRON_COMPUTE_ACTIVE: tuple[PlannedCommand, ...] = (
    query_active(OVS_SWITCH_UNIT),
    *(query_active(unit) for unit in NEUTRON_COMPUTE_UNITS),
)

CONTROLLER_SQL_STDIN_NOTE = (
    "MariaDB provisioning: 'CREATE DATABASE IF NOT EXISTS `neutron`' and grants for "
    "'neutron'@'localhost' and 'neutron'@'%' are sent to '/usr/bin/mariadb --batch "
    "--skip-column-names' over stdin (stdin_param='sql') so NEUTRON_DBPASS never appears in "
    "argv or /proc/<pid>/cmdline (ps)."
)
CONTROLLER_KEYSTONE_NOTE = (
    "Keystone provisioning: /usr/bin/openstack receives ADMIN_PASS via OS_PASSWORD "
    "(env_params, never in argv) and idempotently ensures the 'neutron' user (with 'admin' "
    "role on project 'service'), the 'neutron' ('network') service, and its "
    "public/internal/admin endpoints (http://controller:9696 in RegionOne)."
)
OVS_OPTION2_NOTE = (
    "Networking Option 2 (Self-service networks with Open vSwitch): configures ML2 with "
    "mechanism_drivers = openvswitch,l2population, VXLAN tenant networks (vni_ranges = 1:1000), "
    "flat provider networks (bridge_mappings = provider:br-provider), and openvswitch security "
    "group firewall driver, then idempotently creates br-provider and attaches the provider "
    "interface via 'ovs-vsctl --may-exist'."
)
CONTROLLER_NOT_CHECKED = (
    "Not checked: no live HTTP request to http://controller:9696 or AMQP connection to "
    "controller:5672 is performed."
)
COMPUTE_LEAST_PRIVILEGE_NOTE = (
    "Least-privilege secrets: neutron-compute loads only RABBIT_PASS and NEUTRON_PASS from "
    "/etc/openstack-forge/secrets.json; ADMIN_PASS, NEUTRON_DBPASS, NOVA_PASS, and "
    "METADATA_SECRET are never read on compute nodes."
)
COMPUTE_DATABASE_COMMENT_NOTE = (
    "Compute database isolation: [database] connection in /etc/neutron/neutron.conf is "
    "explicitly commented out so compute nodes never open a direct SQL connection or use "
    "the default SQLite database."
)
COMPUTE_NOT_CHECKED = (
    "Not checked: no live AMQP connection to controller:5672 or agent registration in "
    "'openstack network agent list' is performed."
)


def _management_ip(local_node: Mapping[str, Any]) -> str:
    return str(IPv4Address(local_node["management_ip"]))


def resolve_provider_interface(
    local_node: Mapping[str, Any],
    config: Mapping[str, Any] | None = None,
) -> str:
    """Resolve and validate the Open vSwitch provider physical interface name.

    Precedence:
    1. ``local_node["provider_interface"]``
    2. ``local_node["interfaces"]["provider"]``
    3. ``config["network"]["provider_interface"]``
    4. ``config["networks"]["provider"]["interface"]``
    """
    candidate: Any = None
    found = False
    if "provider_interface" in local_node and local_node["provider_interface"] is not None:
        candidate = local_node["provider_interface"]
        found = True
    else:
        node_interfaces = local_node.get("interfaces")
        if (
            isinstance(node_interfaces, Mapping)
            and "provider" in node_interfaces
            and node_interfaces["provider"] is not None
        ):
            candidate = node_interfaces["provider"]
            found = True
        elif config is not None:
            network_cfg = config.get("network")
            if (
                isinstance(network_cfg, Mapping)
                and "provider_interface" in network_cfg
                and network_cfg["provider_interface"] is not None
            ):
                candidate = network_cfg["provider_interface"]
                found = True
            else:
                networks_cfg = config.get("networks")
                if isinstance(networks_cfg, Mapping):
                    provider_cfg = networks_cfg.get("provider")
                    if (
                        isinstance(provider_cfg, Mapping)
                        and "interface" in provider_cfg
                        and provider_cfg["interface"] is not None
                    ):
                        candidate = provider_cfg["interface"]
                        found = True
    if not found:
        raise PreconditionError(
            "Provider network interface is not configured: set 'provider_interface' on the "
            "node or 'network.provider_interface' (or 'networks.provider.interface') in the "
            "cluster configuration"
        )
    if not isinstance(candidate, str) or not candidate:
        raise PreconditionError(
            f"Invalid provider_interface {candidate!r} (network.provider_interface): "
            "must be a non-empty interface name"
        )
    try:
        return validate_parameter("interface_name", "provider_interface", candidate)
    except ParameterError as exc:
        raise PreconditionError(
            f"Invalid provider_interface {candidate!r} (network.provider_interface): {exc}"
        ) from exc


def resolve_management_interface(
    local_node: Mapping[str, Any],
    config: Mapping[str, Any] | None = None,
) -> str | None:
    """Resolve the node's management interface name if configured, else ``None``.

    Precedence:
    1. ``local_node["management_interface"]``
    2. ``local_node["interfaces"]["management"]``
    3. ``config["network"]["management_interface"]``
    4. ``config["networks"]["management"]["interface"]``
    """
    if "management_interface" in local_node and isinstance(local_node["management_interface"], str):
        val = local_node["management_interface"].strip()
        if val:
            return val
    node_interfaces = local_node.get("interfaces")
    if isinstance(node_interfaces, Mapping):
        mgmt = node_interfaces.get("management")
        if isinstance(mgmt, str) and mgmt.strip():
            return mgmt.strip()
    if config is not None:
        network_cfg = config.get("network")
        if isinstance(network_cfg, Mapping):
            mgmt = network_cfg.get("management_interface")
            if isinstance(mgmt, str) and mgmt.strip():
                return mgmt.strip()
        networks_cfg = config.get("networks")
        if isinstance(networks_cfg, Mapping):
            mgmt_cfg = networks_cfg.get("management")
            if isinstance(mgmt_cfg, Mapping):
                mgmt = mgmt_cfg.get("interface")
                if isinstance(mgmt, str) and mgmt.strip():
                    return mgmt.strip()
    return None


def _shared_provider_interface_allowed(
    local_node: Mapping[str, Any],
    config: Mapping[str, Any] | None = None,
) -> bool:
    """True only when the operator explicitly opted into a shared provider interface."""
    if local_node.get(ALLOW_SHARED_PROVIDER_INTERFACE_KEY) is True:
        return True
    tags = local_node.get("tags")
    if isinstance(tags, (list, tuple, set, frozenset)) and (
        ALLOW_SHARED_PROVIDER_INTERFACE_KEY in tags
    ):
        return True
    if config is not None:
        if config.get(ALLOW_SHARED_PROVIDER_INTERFACE_KEY) is True:
            return True
        network_cfg = config.get("network")
        if (
            isinstance(network_cfg, Mapping)
            and network_cfg.get(ALLOW_SHARED_PROVIDER_INTERFACE_KEY) is True
        ):
            return True
        networks_cfg = config.get("networks")
        if isinstance(networks_cfg, Mapping):
            provider_cfg = networks_cfg.get("provider")
            if (
                isinstance(provider_cfg, Mapping)
                and provider_cfg.get(ALLOW_SHARED_PROVIDER_INTERFACE_KEY) is True
            ):
                return True
    return False


def _strip_cidr(addr: str) -> str:
    return addr.split("/", 1)[0].strip()


def _extract_interface_ips_from_entry(entry: Any) -> list[str]:
    ips: list[str] = []
    if isinstance(entry, str):
        ips.append(_strip_cidr(entry))
    elif isinstance(entry, Sequence):
        for item in entry:
            if isinstance(item, str):
                ips.append(_strip_cidr(item))
            elif isinstance(item, Mapping):
                local = item.get("local") or item.get("address")
                if isinstance(local, str):
                    ips.append(_strip_cidr(local))
    return ips


def _provider_interface_carries_management_ip(
    provider_iface: str,
    mgmt_ip: str,
    local_node: Mapping[str, Any],
    *,
    root: Path | None = None,
    host_interfaces: Sequence[Any] | None = None,
) -> bool:
    """Return True if ``provider_iface`` is known to carry ``mgmt_ip``."""
    # 1. Explicit interface address mappings on local_node
    for key in ("interface_ips", "interface_addresses"):
        mapping = local_node.get(key)
        if isinstance(mapping, Mapping) and provider_iface in mapping:
            if mgmt_ip in _extract_interface_ips_from_entry(mapping[provider_iface]):
                return True

    # 2. Sequence of NetworkInterface objects or `ip -j addr` dicts
    iface_lists: list[Sequence[Any]] = []
    if host_interfaces is not None:
        iface_lists.append(host_interfaces)
    node_host_ifaces = local_node.get("host_interfaces")
    if isinstance(node_host_ifaces, Sequence) and not isinstance(node_host_ifaces, (str, bytes)):
        iface_lists.append(node_host_ifaces)
    for iface_list in iface_lists:
        for item in iface_list:
            if isinstance(item, Mapping):
                name = item.get("ifname") or item.get("name")
                if name != provider_iface:
                    continue
                addrs = _extract_interface_ips_from_entry(
                    item.get("addresses") or item.get("addr_info") or ()
                )
                if mgmt_ip in addrs:
                    return True
            else:
                name = getattr(item, "name", None)
                if name != provider_iface:
                    continue
                addrs = _extract_interface_ips_from_entry(getattr(item, "addresses", ()))
                if mgmt_ip in addrs:
                    return True

    # 3. Netplan YAML files under <root>/etc/netplan/*.yaml|*.yml
    if root is not None:
        try:
            netplan_dir, _ = resolve_target(root, NETPLAN_DIR_TARGET)
        except UnsafePathError:
            netplan_dir = None
        if netplan_dir is not None and not netplan_dir.is_symlink() and netplan_dir.is_dir():
            for candidate in sorted(netplan_dir.iterdir()):
                if candidate.suffix not in {".yaml", ".yml"}:
                    continue
                if candidate.is_symlink() or not candidate.is_file():
                    continue
                try:
                    raw = yaml.safe_load(candidate.read_text(encoding="utf-8"))
                except (OSError, UnicodeDecodeError, yaml.YAMLError):
                    continue
                if not isinstance(raw, Mapping):
                    continue
                net = raw.get("network")
                if not isinstance(net, Mapping):
                    continue
                ethernets = net.get("ethernets")
                if not isinstance(ethernets, Mapping):
                    continue
                iface_cfg = ethernets.get(provider_iface)
                if not isinstance(iface_cfg, Mapping):
                    continue
                addrs = _extract_interface_ips_from_entry(iface_cfg.get("addresses", ()))
                if mgmt_ip in addrs:
                    return True

    return False


def check_provider_interface_safety(
    local_node: Mapping[str, Any],
    config: Mapping[str, Any] | None = None,
    *,
    root: Path | None = None,
    host_interfaces: Sequence[Any] | None = None,
) -> str:
    """Resolve ``provider_interface`` and refuse if it shares the management interface or IP.

    Attaching the management interface (or an interface carrying ``management_ip``) to OVS
    bridge ``br-provider`` via ``ovs-vsctl add-port br-provider <iface>`` moves layer-2
    frames into the OVS datapath without moving the host IP onto ``br-provider``, which
    immediately cuts management connectivity to the node.

    Operators who have pre-configured OVS bridge IP migration out-of-band can explicitly opt
    in by setting ``allow_shared_provider_interface: true`` on the node (or in ``tags``).
    """
    provider_iface = resolve_provider_interface(local_node, config)
    if _shared_provider_interface_allowed(local_node, config):
        return provider_iface

    hostname = str(local_node.get("hostname", local_node.get("role", "node")))
    mgmt_iface = resolve_management_interface(local_node, config)
    if mgmt_iface is not None and provider_iface == mgmt_iface:
        raise PreconditionError(
            f"Refusing to attach provider_interface {provider_iface!r} to OVS bridge "
            f"{OVS_PROVIDER_BRIDGE!r} on node {hostname!r}: {provider_iface!r} is also the "
            f"management interface, so running 'ovs-vsctl add-port {OVS_PROVIDER_BRIDGE} "
            f"{provider_iface}' would cut management connectivity. Configure a dedicated "
            f"provider interface, or set '{ALLOW_SHARED_PROVIDER_INTERFACE_KEY}: true' on the "
            f"node to override this safety check."
        )

    mgmt_ip = _management_ip(local_node)
    if _provider_interface_carries_management_ip(
        provider_iface,
        mgmt_ip,
        local_node,
        root=root,
        host_interfaces=host_interfaces,
    ):
        raise PreconditionError(
            f"Refusing to attach provider_interface {provider_iface!r} to OVS bridge "
            f"{OVS_PROVIDER_BRIDGE!r} on node {hostname!r}: {provider_iface!r} carries the "
            f"node's management_ip ({mgmt_ip}), so running 'ovs-vsctl add-port "
            f"{OVS_PROVIDER_BRIDGE} {provider_iface}' would cut management connectivity. "
            f"Move the management IP off {provider_iface!r} and use a dedicated unnumbered "
            f"provider interface, or set '{ALLOW_SHARED_PROVIDER_INTERFACE_KEY}: true' on the "
            f"node to override this safety check."
        )

    return provider_iface


def neutron_db_connection(db_password: str) -> str:
    """Return the ``[database] connection`` URL for controller ``/etc/neutron/neutron.conf``."""
    return f"mysql+pymysql://{NEUTRON_DB_USER}:{db_password}@controller/{NEUTRON_DB_NAME}"


def neutron_transport_url(rabbit_password: str) -> str:
    """Return the ``[DEFAULT] transport_url`` for ``/etc/neutron/neutron.conf``."""
    return f"rabbit://openstack:{rabbit_password}@controller:5672/"


def neutron_controller_conf_settings(
    db_password: str,
    rabbit_password: str,
    neutron_password: str,
    nova_password: str,
) -> tuple[tuple[str, str, str], ...]:
    """Return the 25 ``(section, key, value)`` settings for controller ``neutron.conf``."""
    return (
        ("database", "connection", neutron_db_connection(db_password)),
        ("DEFAULT", "core_plugin", "ml2"),
        ("DEFAULT", "service_plugins", "router"),
        ("DEFAULT", "transport_url", neutron_transport_url(rabbit_password)),
        ("DEFAULT", "auth_strategy", "keystone"),
        ("DEFAULT", "notify_nova_on_port_status_changes", "true"),
        ("DEFAULT", "notify_nova_on_port_data_changes", "true"),
        ("keystone_authtoken", "www_authenticate_uri", "http://controller:5000"),
        ("keystone_authtoken", "auth_url", "http://controller:5000"),
        ("keystone_authtoken", "memcached_servers", "controller:11211"),
        ("keystone_authtoken", "auth_type", "password"),
        ("keystone_authtoken", "project_domain_name", "Default"),
        ("keystone_authtoken", "user_domain_name", "Default"),
        ("keystone_authtoken", "project_name", SERVICE_PROJECT_NAME),
        ("keystone_authtoken", "username", NEUTRON_SERVICE_USER),
        ("keystone_authtoken", "password", neutron_password),
        ("nova", "auth_url", "http://controller:5000"),
        ("nova", "auth_type", "password"),
        ("nova", "project_domain_name", "Default"),
        ("nova", "user_domain_name", "Default"),
        ("nova", "region_name", "RegionOne"),
        ("nova", "project_name", SERVICE_PROJECT_NAME),
        ("nova", "username", "nova"),
        ("nova", "password", nova_password),
        ("oslo_concurrency", "lock_path", NEUTRON_LOCK_PATH),
    )


build_neutron_controller_ini_entries = neutron_controller_conf_settings


def neutron_ml2_conf_settings() -> tuple[tuple[str, str, str], ...]:
    """Return the 6 ``(section, key, value)`` settings for ``ml2_conf.ini``."""
    return (
        ("ml2", "type_drivers", "flat,vlan,vxlan"),
        ("ml2", "tenant_network_types", "vxlan"),
        ("ml2", "mechanism_drivers", "openvswitch,l2population"),
        ("ml2", "extension_drivers", "port_security"),
        ("ml2_type_flat", "flat_networks", "provider"),
        ("ml2_type_vxlan", "vni_ranges", "1:1000"),
    )


build_neutron_ml2_ini_entries = neutron_ml2_conf_settings


def neutron_ovs_agent_conf_settings(
    local_node: Mapping[str, Any],
) -> tuple[tuple[str, str, str], ...]:
    """Return the 6 ``(section, key, value)`` settings for ``openvswitch_agent.ini``."""
    address = _management_ip(local_node)
    return (
        ("ovs", "bridge_mappings", f"provider:{OVS_PROVIDER_BRIDGE}"),
        ("ovs", "local_ip", address),
        ("agent", "tunnel_types", "vxlan"),
        ("agent", "l2_population", "true"),
        ("securitygroup", "enable_security_group", "true"),
        ("securitygroup", "firewall_driver", "openvswitch"),
    )


build_neutron_ovs_agent_ini_entries = neutron_ovs_agent_conf_settings


def neutron_l3_agent_conf_settings() -> tuple[tuple[str, str, str], ...]:
    """Return the ``(section, key, value)`` setting for ``/etc/neutron/l3_agent.ini``."""
    return (("DEFAULT", "interface_driver", "openvswitch"),)


build_neutron_l3_agent_ini_entries = neutron_l3_agent_conf_settings


def neutron_dhcp_agent_conf_settings() -> tuple[tuple[str, str, str], ...]:
    """Return the 3 ``(section, key, value)`` settings for ``/etc/neutron/dhcp_agent.ini``."""
    return (
        ("DEFAULT", "interface_driver", "openvswitch"),
        ("DEFAULT", "dhcp_driver", NEUTRON_DHCP_DRIVER),
        ("DEFAULT", "enable_isolated_metadata", "true"),
    )


build_neutron_dhcp_agent_ini_entries = neutron_dhcp_agent_conf_settings


def neutron_metadata_agent_conf_settings(
    metadata_secret: str,
) -> tuple[tuple[str, str, str], ...]:
    """Return the 2 ``(section, key, value)`` settings for ``/etc/neutron/metadata_agent.ini``."""
    return (
        ("DEFAULT", "nova_metadata_host", "controller"),
        ("DEFAULT", "metadata_proxy_shared_secret", metadata_secret),
    )


build_neutron_metadata_agent_ini_entries = neutron_metadata_agent_conf_settings


def _common_nova_neutron_settings(
    neutron_password: str,
) -> tuple[tuple[str, str, str], ...]:
    return (
        ("neutron", "auth_url", "http://controller:5000"),
        ("neutron", "auth_type", "password"),
        ("neutron", "project_domain_name", "Default"),
        ("neutron", "user_domain_name", "Default"),
        ("neutron", "region_name", "RegionOne"),
        ("neutron", "project_name", SERVICE_PROJECT_NAME),
        ("neutron", "username", NEUTRON_SERVICE_USER),
        ("neutron", "password", neutron_password),
    )


def nova_controller_neutron_conf_settings(
    neutron_password: str,
    metadata_secret: str,
) -> tuple[tuple[str, str, str], ...]:
    """Return the 10 ``[neutron]`` settings for controller ``/etc/nova/nova.conf``."""
    return (
        *_common_nova_neutron_settings(neutron_password),
        ("neutron", "service_metadata_proxy", "true"),
        ("neutron", "metadata_proxy_shared_secret", metadata_secret),
    )


build_nova_controller_neutron_ini_entries = nova_controller_neutron_conf_settings


def neutron_compute_conf_settings(
    rabbit_password: str,
) -> tuple[tuple[str, str, str], ...]:
    """Return the 2 ``(section, key, value)`` settings for compute ``/etc/neutron/neutron.conf``."""
    return (
        ("DEFAULT", "transport_url", neutron_transport_url(rabbit_password)),
        ("oslo_concurrency", "lock_path", NEUTRON_LOCK_PATH),
    )


build_neutron_compute_ini_entries = neutron_compute_conf_settings


def nova_compute_neutron_conf_settings(
    neutron_password: str,
) -> tuple[tuple[str, str, str], ...]:
    """Return the 8 ``[neutron]`` settings for compute ``/etc/nova/nova.conf``."""
    return _common_nova_neutron_settings(neutron_password)


build_nova_compute_neutron_ini_entries = nova_compute_neutron_conf_settings


def parse_ovs_ports(output: str) -> frozenset[str]:
    """Parse ``ovs-vsctl list-ports <bridge>`` stdout (one port name per line)."""
    return frozenset(line.strip() for line in output.splitlines() if line.strip())


def ovs_bridge_and_port_ready(
    executor: CommandExecutor,
    bridge: str,
    interface: str,
) -> bool:
    """Return ``True`` if ``bridge`` exists and ``interface`` is listed in ``list-ports``."""
    try:
        br_res = executor.run(OVS_VSCTL_BR_EXISTS, {"bridge": bridge})
    except UnexpectedExitError:
        return False
    if br_res.returncode != 0:
        return False
    try:
        ports_res = executor.run(OVS_VSCTL_LIST_PORTS, {"bridge": bridge})
    except UnexpectedExitError:
        return False
    return interface in parse_ovs_ports(ports_res.stdout)


def ovs_bridge_and_port_problems(
    executor: CommandExecutor,
    bridge: str,
    interface: str,
) -> list[str]:
    """Return human-readable problems if ``bridge`` is missing or ``interface`` is not attached."""
    try:
        br_res = executor.run(OVS_VSCTL_BR_EXISTS, {"bridge": bridge})
    except UnexpectedExitError as exc:
        return [f"ovs-vsctl br-exists {bridge} failed: {exc}"]
    if br_res.returncode != 0:
        return [f"Open vSwitch bridge {bridge!r} does not exist"]
    try:
        ports_res = executor.run(OVS_VSCTL_LIST_PORTS, {"bridge": bridge})
    except UnexpectedExitError as exc:
        return [f"ovs-vsctl list-ports {bridge} failed: {exc}"]
    ports = parse_ovs_ports(ports_res.stdout)
    if interface not in ports:
        return [
            f"provider interface {interface!r} is not attached to Open vSwitch bridge {bridge!r}"
        ]
    return []


def _read_existing_conf(path: Path, display: str) -> str | None:
    """Read an existing config file, returning ``None`` if neither it nor its parent dir exists."""
    if not path.is_symlink() and not path.exists():
        return None
    return read_text_file(path, display)


def _mask_secrets(text: str, *secrets: str) -> str:
    masked = redact_text(text)
    for secret in secrets:
        if secret:
            masked = masked.replace(secret, MASK)
    return masked


_SECRET_INI_KEYS: frozenset[str] = frozenset({"password", "metadata_proxy_shared_secret"})


def _parse_ini_values(text: str) -> dict[tuple[str, str], list[str]]:
    values: dict[tuple[str, str], list[str]] = {}
    section: str | None = None
    for raw in text.split("\n"):
        line = raw.strip()
        if not line or line[0] in "#;":
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1].strip()
            continue
        if section is None or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values.setdefault((section, key.strip()), []).append(value.strip())
    return values


def _verify_ini_settings(
    text: str,
    target_display: str,
    settings: tuple[tuple[str, str, str], ...],
    *secrets: str,
    commented_out: tuple[tuple[str, str], ...] = (),
) -> list[str]:
    values = _parse_ini_values(text)
    problems: list[str] = []
    for section_name, key in commented_out:
        found = values.get((section_name, key), [])
        for actual in dict.fromkeys(found):
            shown_actual = MASK if key in _SECRET_INI_KEYS else _mask_secrets(actual, *secrets)
            problems.append(
                f"{key} must be commented out in [{section_name}] of {target_display} "
                f"(found {shown_actual!r})"
            )
    for section_name, key, expected in settings:
        found = values.get((section_name, key), [])
        is_secret_key = key in _SECRET_INI_KEYS
        shown_expected = MASK if is_secret_key else _mask_secrets(expected, *secrets)
        if not found:
            problems.append(
                f"{key} is not set in [{section_name}] of {target_display} "
                f"(expected {shown_expected!r})"
            )
            continue
        for actual in dict.fromkeys(found):
            if actual != expected:
                shown_actual = MASK if is_secret_key else _mask_secrets(actual, *secrets)
                problems.append(
                    f"{key} is {shown_actual!r} in [{section_name}] of "
                    f"{target_display}, expected {shown_expected!r}"
                )
    return problems


def _require_conf_exists(root: Path, target: str, package_hint: str) -> None:
    path, display = resolve_target(root, target)
    if _read_existing_conf(path, display) is None:
        raise PreconditionError(
            f"{display} does not exist; it is created when {package_hint} is installed"
        )


def edit_neutron_controller_conf(
    root: Path,
    db_password: str,
    rabbit_password: str,
    neutron_password: str,
    nova_password: str,
    *,
    dry_run: bool,
) -> FileChange:
    """Apply the 2025.1 Ubuntu controller settings to ``/etc/neutron/neutron.conf``."""
    _require_conf_exists(root, NEUTRON_CONF_TARGET, NEUTRON_CONTROLLER_PACKAGES[0])
    return ini_set_many(
        root,
        NEUTRON_CONF_TARGET,
        neutron_controller_conf_settings(
            db_password, rabbit_password, neutron_password, nova_password
        ),
        dry_run=dry_run,
    )


def verify_neutron_controller_conf_text(
    text: str,
    db_password: str,
    rabbit_password: str,
    neutron_password: str,
    nova_password: str,
) -> list[str]:
    """Pure check: all expected controller ``/etc/neutron/neutron.conf`` keys match."""
    return _verify_ini_settings(
        text,
        NEUTRON_CONF_TARGET,
        neutron_controller_conf_settings(
            db_password, rabbit_password, neutron_password, nova_password
        ),
        db_password,
        rabbit_password,
        neutron_password,
        nova_password,
    )


def edit_neutron_ml2_conf(root: Path, *, dry_run: bool) -> FileChange:
    """Apply the 2025.1 Ubuntu Option 2 settings to ``/etc/neutron/plugins/ml2/ml2_conf.ini``."""
    _require_conf_exists(root, NEUTRON_ML2_CONF_TARGET, "neutron-plugin-ml2")
    return ini_set_many(
        root,
        NEUTRON_ML2_CONF_TARGET,
        neutron_ml2_conf_settings(),
        dry_run=dry_run,
    )


def verify_neutron_ml2_conf_text(text: str) -> list[str]:
    """Pure check: all expected ``/etc/neutron/plugins/ml2/ml2_conf.ini`` keys match."""
    return _verify_ini_settings(text, NEUTRON_ML2_CONF_TARGET, neutron_ml2_conf_settings())


def edit_neutron_ovs_agent_conf(
    root: Path,
    local_node: Mapping[str, Any],
    *,
    dry_run: bool,
) -> FileChange:
    """Apply the Option 2 OVS settings to ``/etc/neutron/plugins/ml2/openvswitch_agent.ini``."""
    _require_conf_exists(root, NEUTRON_OVS_AGENT_CONF_TARGET, NEUTRON_COMPUTE_PACKAGE)
    return ini_set_many(
        root,
        NEUTRON_OVS_AGENT_CONF_TARGET,
        neutron_ovs_agent_conf_settings(local_node),
        dry_run=dry_run,
    )


def verify_neutron_ovs_agent_conf_text(
    text: str,
    local_node: Mapping[str, Any],
) -> list[str]:
    """Pure check: all expected ``openvswitch_agent.ini`` keys match."""
    return _verify_ini_settings(
        text,
        NEUTRON_OVS_AGENT_CONF_TARGET,
        neutron_ovs_agent_conf_settings(local_node),
    )


def edit_neutron_l3_agent_conf(root: Path, *, dry_run: bool) -> FileChange:
    """Apply the Option 2 settings to ``/etc/neutron/l3_agent.ini``."""
    _require_conf_exists(root, NEUTRON_L3_AGENT_CONF_TARGET, "neutron-l3-agent")
    return ini_set_many(
        root,
        NEUTRON_L3_AGENT_CONF_TARGET,
        neutron_l3_agent_conf_settings(),
        dry_run=dry_run,
    )


def verify_neutron_l3_agent_conf_text(text: str) -> list[str]:
    """Pure check: all expected ``/etc/neutron/l3_agent.ini`` keys match."""
    return _verify_ini_settings(
        text, NEUTRON_L3_AGENT_CONF_TARGET, neutron_l3_agent_conf_settings()
    )


def edit_neutron_dhcp_agent_conf(root: Path, *, dry_run: bool) -> FileChange:
    """Apply the Option 2 settings to ``/etc/neutron/dhcp_agent.ini``."""
    _require_conf_exists(root, NEUTRON_DHCP_AGENT_CONF_TARGET, "neutron-dhcp-agent")
    return ini_set_many(
        root,
        NEUTRON_DHCP_AGENT_CONF_TARGET,
        neutron_dhcp_agent_conf_settings(),
        dry_run=dry_run,
    )


def verify_neutron_dhcp_agent_conf_text(text: str) -> list[str]:
    """Pure check: all expected ``/etc/neutron/dhcp_agent.ini`` keys match."""
    return _verify_ini_settings(
        text, NEUTRON_DHCP_AGENT_CONF_TARGET, neutron_dhcp_agent_conf_settings()
    )


def edit_neutron_metadata_agent_conf(
    root: Path,
    metadata_secret: str,
    *,
    dry_run: bool,
) -> FileChange:
    """Apply the settings to ``/etc/neutron/metadata_agent.ini``."""
    _require_conf_exists(root, NEUTRON_METADATA_AGENT_CONF_TARGET, "neutron-metadata-agent")
    return ini_set_many(
        root,
        NEUTRON_METADATA_AGENT_CONF_TARGET,
        neutron_metadata_agent_conf_settings(metadata_secret),
        dry_run=dry_run,
    )


def verify_neutron_metadata_agent_conf_text(
    text: str,
    metadata_secret: str,
) -> list[str]:
    """Pure check: all expected ``/etc/neutron/metadata_agent.ini`` keys match."""
    return _verify_ini_settings(
        text,
        NEUTRON_METADATA_AGENT_CONF_TARGET,
        neutron_metadata_agent_conf_settings(metadata_secret),
        metadata_secret,
    )


def edit_nova_controller_neutron_conf(
    root: Path,
    neutron_password: str,
    metadata_secret: str,
    *,
    dry_run: bool,
) -> FileChange:
    """Apply the ``[neutron]`` section settings to controller ``/etc/nova/nova.conf``."""
    _require_conf_exists(root, NOVA_CONF_TARGET, "nova-api (apply 'nova-controller' first)")
    return ini_set_many(
        root,
        NOVA_CONF_TARGET,
        nova_controller_neutron_conf_settings(neutron_password, metadata_secret),
        dry_run=dry_run,
    )


def verify_nova_controller_neutron_conf_text(
    text: str,
    neutron_password: str,
    metadata_secret: str,
) -> list[str]:
    """Pure check: all expected ``[neutron]`` keys in controller ``/etc/nova/nova.conf`` match."""
    return _verify_ini_settings(
        text,
        NOVA_CONF_TARGET,
        nova_controller_neutron_conf_settings(neutron_password, metadata_secret),
        neutron_password,
        metadata_secret,
    )


def edit_neutron_compute_conf(
    root: Path,
    rabbit_password: str,
    *,
    dry_run: bool,
) -> FileChange:
    """Comment out ``[database] connection`` and apply compute settings to ``neutron.conf``."""
    _require_conf_exists(root, NEUTRON_CONF_TARGET, NEUTRON_COMPUTE_PACKAGE)
    return ini_set_many(
        root,
        NEUTRON_CONF_TARGET,
        neutron_compute_conf_settings(rabbit_password),
        comment_out=NEUTRON_COMPUTE_COMMENTED_OUT_KEYS,
        dry_run=dry_run,
    )


def verify_neutron_compute_conf_text(
    text: str,
    rabbit_password: str,
) -> list[str]:
    """Pure check: compute ``/etc/neutron/neutron.conf`` has ``[database] connection`` commented
    out and expected ``[DEFAULT]`` / ``[oslo_concurrency]`` keys set."""
    return _verify_ini_settings(
        text,
        NEUTRON_CONF_TARGET,
        neutron_compute_conf_settings(rabbit_password),
        rabbit_password,
        commented_out=NEUTRON_COMPUTE_COMMENTED_OUT_KEYS,
    )


def edit_nova_compute_neutron_conf(
    root: Path,
    neutron_password: str,
    *,
    dry_run: bool,
) -> FileChange:
    """Apply the ``[neutron]`` section settings to compute ``/etc/nova/nova.conf``."""
    _require_conf_exists(root, NOVA_CONF_TARGET, "nova-compute (apply 'nova-compute' first)")
    return ini_set_many(
        root,
        NOVA_CONF_TARGET,
        nova_compute_neutron_conf_settings(neutron_password),
        dry_run=dry_run,
    )


def verify_nova_compute_neutron_conf_text(
    text: str,
    neutron_password: str,
) -> list[str]:
    """Pure check: all expected ``[neutron]`` keys in compute ``/etc/nova/nova.conf`` match."""
    return _verify_ini_settings(
        text,
        NOVA_CONF_TARGET,
        nova_compute_neutron_conf_settings(neutron_password),
        neutron_password,
    )


def _load_neutron_controller_secrets(
    root: Path,
) -> tuple[str, str, str, str, str, str]:
    """Load the 6 required controller secrets from :class:`SecretStore`."""
    store = SecretStore(root)
    try:
        admin_password = store.get(NEUTRON_ADMIN_SECRET_NAME)
        rabbit_password = store.get(NEUTRON_RABBIT_SECRET_NAME)
        db_password = store.get(NEUTRON_DB_SECRET_NAME)
        neutron_password = store.get(NEUTRON_SERVICE_SECRET_NAME)
        nova_password = store.get(NEUTRON_NOVA_SECRET_NAME)
        metadata_secret = store.get(NEUTRON_METADATA_SECRET_NAME)
    except (SecretError, UnsafePathError) as exc:
        raise PreconditionError(
            f"{NEUTRON_CONTROLLER_STEP_ID} requires {NEUTRON_ADMIN_SECRET_NAME}, "
            f"{NEUTRON_RABBIT_SECRET_NAME}, {NEUTRON_DB_SECRET_NAME}, "
            f"{NEUTRON_SERVICE_SECRET_NAME}, {NEUTRON_NOVA_SECRET_NAME}, and "
            f"{NEUTRON_METADATA_SECRET_NAME} in {DEFAULT_SECRETS_TARGET}: {exc}. "
            "Run 'openstack-forge secrets init' first (the step never generates secrets itself)."
        ) from exc
    return (
        admin_password,
        rabbit_password,
        db_password,
        neutron_password,
        nova_password,
        metadata_secret,
    )


def _load_neutron_compute_secrets(root: Path) -> tuple[str, str]:
    """Load ``(RABBIT_PASS, NEUTRON_PASS)`` for a compute node (least privilege)."""
    store = SecretStore(root)
    try:
        rabbit_password = store.get(NEUTRON_RABBIT_SECRET_NAME)
        neutron_password = store.get(NEUTRON_SERVICE_SECRET_NAME)
    except (SecretError, UnsafePathError) as exc:
        raise PreconditionError(
            f"{NEUTRON_COMPUTE_STEP_ID} requires {NEUTRON_RABBIT_SECRET_NAME} and "
            f"{NEUTRON_SERVICE_SECRET_NAME} in {DEFAULT_SECRETS_TARGET}: {exc}. "
            "Run 'openstack-forge secrets init' on the controller and copy the required "
            "secrets to this node (the step never generates secrets itself)."
        ) from exc
    return rabbit_password, neutron_password


def _preview_or_note_conf(
    root: Path,
    target: str,
    package_hint: str,
    settings: tuple[tuple[str, str, str], ...],
    edit_fn: Any,
    secrets_to_mask: tuple[str, ...],
    file_changes: list[FileChange],
    notes: list[str],
    *,
    commented_out: tuple[tuple[str, str], ...] = (),
) -> None:
    path, display = resolve_target(root, target)
    if _read_existing_conf(path, display) is None:
        lines = [f"[{sec}] # comment out {k}" for sec, k in commented_out] + [
            f"[{sec}] {k} = {_mask_secrets(v, *secrets_to_mask)}" for sec, k, v in settings
        ]
        rendered = "\n".join(lines)
        notes.append(
            f"{display} does not exist yet (created when {package_hint} is installed); "
            f"apply() will set:\n{rendered}"
        )
    else:
        file_changes.append(edit_fn())


@dataclass(frozen=True)
class NeutronControllerStep:
    """``check`` / ``apply`` / ``verify`` for the ``neutron-controller`` catalog step."""

    root: Path
    config: Mapping[str, Any]
    local_node: Mapping[str, Any]
    executor: CommandExecutor
    id: str = NEUTRON_CONTROLLER_STEP_ID
    declares_commands: bool = True

    def __post_init__(self) -> None:
        role = self.local_node.get("role")
        if role != "controller":
            raise ExecutionError(
                f"Step {self.id!r} applies only to the 'controller' role, got {role!r}"
            )
        _management_ip(self.local_node)
        resolve_provider_interface(self.local_node, self.config)

    @property
    def provider_interface(self) -> str:
        return resolve_provider_interface(self.local_node, self.config)

    def dry_run_preview(self) -> StepPreview:
        """File diffs + commands ``apply()`` would run, with all secrets masked."""
        notes: list[str] = []
        try:
            (
                admin_password,
                rabbit_password,
                db_password,
                neutron_password,
                nova_password,
                metadata_secret,
            ) = _load_neutron_controller_secrets(self.root)
        except PreconditionError as exc:
            admin_password = _DRY_RUN_PLACEHOLDER_SECRET
            rabbit_password = _DRY_RUN_PLACEHOLDER_SECRET
            db_password = _DRY_RUN_PLACEHOLDER_SECRET
            neutron_password = _DRY_RUN_PLACEHOLDER_SECRET
            nova_password = _DRY_RUN_PLACEHOLDER_SECRET
            metadata_secret = _DRY_RUN_PLACEHOLDER_SECRET
            notes.append(f"Precondition (checked by apply()): {exc}")

        provider_iface = self.provider_interface
        try:
            check_provider_interface_safety(self.local_node, self.config, root=self.root)
        except PreconditionError as exc:
            notes.append(f"Precondition (checked by apply()): {exc}")
        secrets_to_mask = (
            admin_password,
            rabbit_password,
            db_password,
            neutron_password,
            nova_password,
            metadata_secret,
        )
        file_changes: list[FileChange] = []

        _preview_or_note_conf(
            self.root,
            NEUTRON_CONF_TARGET,
            NEUTRON_CONTROLLER_PACKAGES[0],
            neutron_controller_conf_settings(
                db_password, rabbit_password, neutron_password, nova_password
            ),
            lambda: edit_neutron_controller_conf(
                self.root,
                db_password,
                rabbit_password,
                neutron_password,
                nova_password,
                dry_run=True,
            ),
            secrets_to_mask,
            file_changes,
            notes,
        )
        _preview_or_note_conf(
            self.root,
            NEUTRON_ML2_CONF_TARGET,
            "neutron-plugin-ml2",
            neutron_ml2_conf_settings(),
            lambda: edit_neutron_ml2_conf(self.root, dry_run=True),
            secrets_to_mask,
            file_changes,
            notes,
        )
        _preview_or_note_conf(
            self.root,
            NEUTRON_OVS_AGENT_CONF_TARGET,
            NEUTRON_COMPUTE_PACKAGE,
            neutron_ovs_agent_conf_settings(self.local_node),
            lambda: edit_neutron_ovs_agent_conf(self.root, self.local_node, dry_run=True),
            secrets_to_mask,
            file_changes,
            notes,
        )
        _preview_or_note_conf(
            self.root,
            NEUTRON_L3_AGENT_CONF_TARGET,
            "neutron-l3-agent",
            neutron_l3_agent_conf_settings(),
            lambda: edit_neutron_l3_agent_conf(self.root, dry_run=True),
            secrets_to_mask,
            file_changes,
            notes,
        )
        _preview_or_note_conf(
            self.root,
            NEUTRON_DHCP_AGENT_CONF_TARGET,
            "neutron-dhcp-agent",
            neutron_dhcp_agent_conf_settings(),
            lambda: edit_neutron_dhcp_agent_conf(self.root, dry_run=True),
            secrets_to_mask,
            file_changes,
            notes,
        )
        _preview_or_note_conf(
            self.root,
            NEUTRON_METADATA_AGENT_CONF_TARGET,
            "neutron-metadata-agent",
            neutron_metadata_agent_conf_settings(metadata_secret),
            lambda: edit_neutron_metadata_agent_conf(self.root, metadata_secret, dry_run=True),
            secrets_to_mask,
            file_changes,
            notes,
        )
        _preview_or_note_conf(
            self.root,
            NOVA_CONF_TARGET,
            "nova-api",
            nova_controller_neutron_conf_settings(neutron_password, metadata_secret),
            lambda: edit_nova_controller_neutron_conf(
                self.root, neutron_password, metadata_secret, dry_run=True
            ),
            secrets_to_mask,
            file_changes,
            notes,
        )

        notes.extend(
            (
                CONTROLLER_SQL_STDIN_NOTE,
                CONTROLLER_KEYSTONE_NOTE,
                OVS_OPTION2_NOTE,
                CONTROLLER_NOT_CHECKED,
            )
        )
        commands = (
            plan_service_database_sql((NEUTRON_DB_NAME,), NEUTRON_DB_USER, db_password),
            PlannedCommand(
                OPENSTACK_USER_CREATE,
                {
                    "admin_password": admin_password,
                    "user": NEUTRON_SERVICE_USER,
                    "user_password": neutron_password,
                },
            ),
            PlannedCommand(
                OPENSTACK_ROLE_ADD_USER_PROJECT,
                {
                    "admin_password": admin_password,
                    "project": SERVICE_PROJECT_NAME,
                    "user": NEUTRON_SERVICE_USER,
                    "role": ADMIN_ROLE_NAME,
                },
            ),
            PlannedCommand(
                OPENSTACK_SERVICE_CREATE,
                {
                    "admin_password": admin_password,
                    "service_name": NEUTRON_SERVICE_NAME,
                    "service_type": NEUTRON_SERVICE_TYPE,
                },
            ),
            *(
                PlannedCommand(
                    OPENSTACK_ENDPOINT_CREATE,
                    {
                        "admin_password": admin_password,
                        "service_name": NEUTRON_SERVICE_NAME,
                        "interface": iface,
                        "url": NEUTRON_ENDPOINT_URL,
                    },
                )
                for iface in ENDPOINT_INTERFACES
            ),
            *INSTALL_NEUTRON_CONTROLLER_PACKAGES,
            RESTART_OVS_SWITCH_UNIT,
            RUN_OVS_ADD_PROVIDER_BRIDGE,
            PlannedCommand(
                OVS_VSCTL_ADD_PORT,
                {"bridge": OVS_PROVIDER_BRIDGE, "interface": provider_iface},
            ),
            RUN_NEUTRON_DB_UPGRADE_HEAD,
            *RESTART_NEUTRON_CONTROLLER_UNITS,
        )
        read_only_commands = (
            *QUERY_NEUTRON_CONTROLLER_PACKAGES,
            *QUERY_NEUTRON_CONTROLLER_ACTIVE,
            QUERY_MARIADB_STATE,
            PlannedCommand(OPENSTACK_USER_LIST, {"admin_password": admin_password}),
            PlannedCommand(OPENSTACK_ROLE_ASSIGNMENT_LIST, {"admin_password": admin_password}),
            PlannedCommand(OPENSTACK_SERVICE_LIST, {"admin_password": admin_password}),
            PlannedCommand(OPENSTACK_ENDPOINT_LIST, {"admin_password": admin_password}),
            QUERY_OVS_PROVIDER_BRIDGE_EXISTS,
            QUERY_OVS_PROVIDER_BRIDGE_PORTS,
        )
        return StepPreview(
            file_changes=tuple(file_changes),
            commands=commands,
            read_only_commands=read_only_commands,
            notes=tuple(notes),
        )

    def check(self) -> bool:
        """True if secrets, 7 configs, packages, units, MariaDB, Keystone, and OVS are ready."""
        try:
            check_provider_interface_safety(self.local_node, self.config, root=self.root)
            (
                admin_password,
                rabbit_password,
                db_password,
                neutron_password,
                nova_password,
                metadata_secret,
            ) = _load_neutron_controller_secrets(self.root)
        except PreconditionError:
            return False
        try:
            if edit_neutron_controller_conf(
                self.root,
                db_password,
                rabbit_password,
                neutron_password,
                nova_password,
                dry_run=True,
            ).changed:
                return False
            if edit_neutron_ml2_conf(self.root, dry_run=True).changed:
                return False
            if edit_neutron_ovs_agent_conf(self.root, self.local_node, dry_run=True).changed:
                return False
            if edit_neutron_l3_agent_conf(self.root, dry_run=True).changed:
                return False
            if edit_neutron_dhcp_agent_conf(self.root, dry_run=True).changed:
                return False
            if edit_neutron_metadata_agent_conf(self.root, metadata_secret, dry_run=True).changed:
                return False
            if edit_nova_controller_neutron_conf(
                self.root, neutron_password, metadata_secret, dry_run=True
            ).changed:
                return False
        except (PreconditionError, UnsafePathError, FileFormatError):
            return False
        for pkg in NEUTRON_CONTROLLER_PACKAGES:
            if not package_installed(self.executor, pkg):
                return False
        for unit in (OVS_SWITCH_UNIT, *NEUTRON_CONTROLLER_UNITS):
            if unit_active_problems(self.executor, unit):
                return False
        if not service_database_ready(self.executor, (NEUTRON_DB_NAME,), NEUTRON_DB_USER):
            return False
        if not service_user_ready(self.executor, admin_password, NEUTRON_SERVICE_USER):
            return False
        if not service_catalog_ready(
            self.executor,
            admin_password,
            NEUTRON_SERVICE_NAME,
            NEUTRON_SERVICE_TYPE,
            NEUTRON_ENDPOINT_URL,
        ):
            return False
        return ovs_bridge_and_port_ready(
            self.executor, OVS_PROVIDER_BRIDGE, self.provider_interface
        )

    def apply(self) -> None:
        """Provision MariaDB & Keystone, install Neutron controller packages, edit 7 config files,
        configure OVS ``br-provider``, run ``neutron-db-manage upgrade head``, and restart units."""
        provider_iface = check_provider_interface_safety(
            self.local_node, self.config, root=self.root
        )
        (
            admin_password,
            rabbit_password,
            db_password,
            neutron_password,
            nova_password,
            metadata_secret,
        ) = _load_neutron_controller_secrets(self.root)
        # Ensure /etc/nova/nova.conf already exists (from nova-controller) before mutating:
        _require_conf_exists(
            self.root, NOVA_CONF_TARGET, "nova-api (apply 'nova-controller' first)"
        )

        provision_service_database(self.executor, (NEUTRON_DB_NAME,), NEUTRON_DB_USER, db_password)
        ensure_service_user(self.executor, admin_password, NEUTRON_SERVICE_USER, neutron_password)
        ensure_service_catalog(
            self.executor,
            admin_password,
            NEUTRON_SERVICE_NAME,
            NEUTRON_SERVICE_TYPE,
            NEUTRON_ENDPOINT_URL,
        )
        for cmd in INSTALL_NEUTRON_CONTROLLER_PACKAGES:
            self.executor.run(cmd.command_id, cmd.params)

        edit_neutron_controller_conf(
            self.root,
            db_password,
            rabbit_password,
            neutron_password,
            nova_password,
            dry_run=False,
        )
        edit_neutron_ml2_conf(self.root, dry_run=False)
        edit_neutron_ovs_agent_conf(self.root, self.local_node, dry_run=False)
        edit_neutron_l3_agent_conf(self.root, dry_run=False)
        edit_neutron_dhcp_agent_conf(self.root, dry_run=False)
        edit_neutron_metadata_agent_conf(self.root, metadata_secret, dry_run=False)
        edit_nova_controller_neutron_conf(
            self.root, neutron_password, metadata_secret, dry_run=False
        )

        self.executor.run(RESTART_OVS_SWITCH_UNIT.command_id, RESTART_OVS_SWITCH_UNIT.params)
        self.executor.run(
            RUN_OVS_ADD_PROVIDER_BRIDGE.command_id, RUN_OVS_ADD_PROVIDER_BRIDGE.params
        )
        self.executor.run(
            OVS_VSCTL_ADD_PORT,
            {"bridge": OVS_PROVIDER_BRIDGE, "interface": provider_iface},
        )
        self.executor.run(
            RUN_NEUTRON_DB_UPGRADE_HEAD.command_id, RUN_NEUTRON_DB_UPGRADE_HEAD.params
        )
        for cmd in RESTART_NEUTRON_CONTROLLER_UNITS:
            self.executor.run(cmd.command_id, cmd.params)

    def verify(self) -> VerifyResult:
        """Verify secrets, 7 config files, packages, units, MariaDB, Keystone, and OVS bridge."""
        problems: list[str] = []
        admin_password: str | None = None
        rabbit_password: str | None = None
        db_password: str | None = None
        neutron_password: str | None = None
        nova_password: str | None = None
        metadata_secret: str | None = None
        try:
            check_provider_interface_safety(self.local_node, self.config, root=self.root)
        except PreconditionError as exc:
            problems.append(str(exc))
        try:
            (
                admin_password,
                rabbit_password,
                db_password,
                neutron_password,
                nova_password,
                metadata_secret,
            ) = _load_neutron_controller_secrets(self.root)
        except PreconditionError as exc:
            problems.append(str(exc))

        # 1. /etc/neutron/neutron.conf
        n_path, n_display = resolve_target(self.root, NEUTRON_CONF_TARGET)
        try:
            n_text = _read_existing_conf(n_path, n_display)
        except (UnsafePathError, FileFormatError) as exc:
            return VerifyResult(False, f"{exc}. {CONTROLLER_NOT_CHECKED}")
        if n_text is None:
            problems.append(f"{n_display} does not exist")
        elif (
            db_password is not None
            and rabbit_password is not None
            and neutron_password is not None
            and nova_password is not None
        ):
            problems += verify_neutron_controller_conf_text(
                n_text, db_password, rabbit_password, neutron_password, nova_password
            )

        # 2. /etc/neutron/plugins/ml2/ml2_conf.ini
        ml2_path, ml2_display = resolve_target(self.root, NEUTRON_ML2_CONF_TARGET)
        try:
            ml2_text = _read_existing_conf(ml2_path, ml2_display)
        except (UnsafePathError, FileFormatError) as exc:
            return VerifyResult(False, f"{exc}. {CONTROLLER_NOT_CHECKED}")
        if ml2_text is None:
            problems.append(f"{ml2_display} does not exist")
        else:
            problems += verify_neutron_ml2_conf_text(ml2_text)

        # 3. /etc/neutron/plugins/ml2/openvswitch_agent.ini
        ovs_path, ovs_display = resolve_target(self.root, NEUTRON_OVS_AGENT_CONF_TARGET)
        try:
            ovs_text = _read_existing_conf(ovs_path, ovs_display)
        except (UnsafePathError, FileFormatError) as exc:
            return VerifyResult(False, f"{exc}. {CONTROLLER_NOT_CHECKED}")
        if ovs_text is None:
            problems.append(f"{ovs_display} does not exist")
        else:
            problems += verify_neutron_ovs_agent_conf_text(ovs_text, self.local_node)

        # 4. /etc/neutron/l3_agent.ini
        l3_path, l3_display = resolve_target(self.root, NEUTRON_L3_AGENT_CONF_TARGET)
        try:
            l3_text = _read_existing_conf(l3_path, l3_display)
        except (UnsafePathError, FileFormatError) as exc:
            return VerifyResult(False, f"{exc}. {CONTROLLER_NOT_CHECKED}")
        if l3_text is None:
            problems.append(f"{l3_display} does not exist")
        else:
            problems += verify_neutron_l3_agent_conf_text(l3_text)

        # 5. /etc/neutron/dhcp_agent.ini
        dhcp_path, dhcp_display = resolve_target(self.root, NEUTRON_DHCP_AGENT_CONF_TARGET)
        try:
            dhcp_text = _read_existing_conf(dhcp_path, dhcp_display)
        except (UnsafePathError, FileFormatError) as exc:
            return VerifyResult(False, f"{exc}. {CONTROLLER_NOT_CHECKED}")
        if dhcp_text is None:
            problems.append(f"{dhcp_display} does not exist")
        else:
            problems += verify_neutron_dhcp_agent_conf_text(dhcp_text)

        # 6. /etc/neutron/metadata_agent.ini
        meta_path, meta_display = resolve_target(self.root, NEUTRON_METADATA_AGENT_CONF_TARGET)
        try:
            meta_text = _read_existing_conf(meta_path, meta_display)
        except (UnsafePathError, FileFormatError) as exc:
            return VerifyResult(False, f"{exc}. {CONTROLLER_NOT_CHECKED}")
        if meta_text is None:
            problems.append(f"{meta_display} does not exist")
        elif metadata_secret is not None:
            problems += verify_neutron_metadata_agent_conf_text(meta_text, metadata_secret)

        # 7. /etc/nova/nova.conf ([neutron] section)
        nova_path, nova_display = resolve_target(self.root, NOVA_CONF_TARGET)
        try:
            nova_text = _read_existing_conf(nova_path, nova_display)
        except (UnsafePathError, FileFormatError) as exc:
            return VerifyResult(False, f"{exc}. {CONTROLLER_NOT_CHECKED}")
        if nova_text is None:
            problems.append(f"{nova_display} does not exist")
        elif neutron_password is not None and metadata_secret is not None:
            problems += verify_nova_controller_neutron_conf_text(
                nova_text, neutron_password, metadata_secret
            )

        for pkg in NEUTRON_CONTROLLER_PACKAGES:
            if not package_installed(self.executor, pkg):
                problems.append(f"package {pkg!r} is not installed")
        for unit in (OVS_SWITCH_UNIT, *NEUTRON_CONTROLLER_UNITS):
            problems += unit_active_problems(self.executor, unit)
        problems += service_database_problems(self.executor, (NEUTRON_DB_NAME,), NEUTRON_DB_USER)
        if admin_password is not None:
            problems += service_user_problems(self.executor, admin_password, NEUTRON_SERVICE_USER)
            problems += service_catalog_problems(
                self.executor,
                admin_password,
                NEUTRON_SERVICE_NAME,
                NEUTRON_SERVICE_TYPE,
                NEUTRON_ENDPOINT_URL,
            )
        problems += ovs_bridge_and_port_problems(
            self.executor, OVS_PROVIDER_BRIDGE, self.provider_interface
        )

        if problems:
            return VerifyResult(False, f"{'; '.join(problems)}. {CONTROLLER_NOT_CHECKED}")
        return VerifyResult(True, CONTROLLER_NOT_CHECKED)


@dataclass(frozen=True)
class NeutronComputeStep:
    """``check`` / ``apply`` / ``verify`` for the ``neutron-compute`` catalog step."""

    root: Path
    config: Mapping[str, Any]
    local_node: Mapping[str, Any]
    executor: CommandExecutor
    id: str = NEUTRON_COMPUTE_STEP_ID
    declares_commands: bool = True

    def __post_init__(self) -> None:
        role = self.local_node.get("role")
        if role != "compute":
            raise ExecutionError(
                f"Step {self.id!r} applies only to the 'compute' role, got {role!r}"
            )
        _management_ip(self.local_node)
        resolve_provider_interface(self.local_node, self.config)

    @property
    def provider_interface(self) -> str:
        return resolve_provider_interface(self.local_node, self.config)

    def dry_run_preview(self) -> StepPreview:
        """File diffs + commands ``apply()`` would run, with all secrets masked."""
        notes: list[str] = []
        try:
            rabbit_password, neutron_password = _load_neutron_compute_secrets(self.root)
        except PreconditionError as exc:
            rabbit_password = _DRY_RUN_PLACEHOLDER_SECRET
            neutron_password = _DRY_RUN_PLACEHOLDER_SECRET
            notes.append(f"Precondition (checked by apply()): {exc}")

        provider_iface = self.provider_interface
        try:
            check_provider_interface_safety(self.local_node, self.config, root=self.root)
        except PreconditionError as exc:
            notes.append(f"Precondition (checked by apply()): {exc}")
        secrets_to_mask = (rabbit_password, neutron_password)
        file_changes: list[FileChange] = []

        _preview_or_note_conf(
            self.root,
            NEUTRON_CONF_TARGET,
            NEUTRON_COMPUTE_PACKAGE,
            neutron_compute_conf_settings(rabbit_password),
            lambda: edit_neutron_compute_conf(self.root, rabbit_password, dry_run=True),
            secrets_to_mask,
            file_changes,
            notes,
            commented_out=NEUTRON_COMPUTE_COMMENTED_OUT_KEYS,
        )
        _preview_or_note_conf(
            self.root,
            NEUTRON_OVS_AGENT_CONF_TARGET,
            NEUTRON_COMPUTE_PACKAGE,
            neutron_ovs_agent_conf_settings(self.local_node),
            lambda: edit_neutron_ovs_agent_conf(self.root, self.local_node, dry_run=True),
            secrets_to_mask,
            file_changes,
            notes,
        )
        _preview_or_note_conf(
            self.root,
            NOVA_CONF_TARGET,
            "nova-compute",
            nova_compute_neutron_conf_settings(neutron_password),
            lambda: edit_nova_compute_neutron_conf(self.root, neutron_password, dry_run=True),
            secrets_to_mask,
            file_changes,
            notes,
        )

        notes.extend(
            (
                COMPUTE_LEAST_PRIVILEGE_NOTE,
                COMPUTE_DATABASE_COMMENT_NOTE,
                OVS_OPTION2_NOTE,
                COMPUTE_NOT_CHECKED,
            )
        )
        commands = (
            INSTALL_NEUTRON_COMPUTE_PACKAGE,
            RESTART_OVS_SWITCH_UNIT,
            RUN_OVS_ADD_PROVIDER_BRIDGE,
            PlannedCommand(
                OVS_VSCTL_ADD_PORT,
                {"bridge": OVS_PROVIDER_BRIDGE, "interface": provider_iface},
            ),
            *RESTART_NEUTRON_COMPUTE_UNITS,
        )
        read_only_commands = (
            QUERY_NEUTRON_COMPUTE_PACKAGE,
            *QUERY_NEUTRON_COMPUTE_ACTIVE,
            QUERY_OVS_PROVIDER_BRIDGE_EXISTS,
            QUERY_OVS_PROVIDER_BRIDGE_PORTS,
        )
        return StepPreview(
            file_changes=tuple(file_changes),
            commands=commands,
            read_only_commands=read_only_commands,
            notes=tuple(notes),
        )

    def check(self) -> bool:
        """True if secrets, 3 config files, package, units, and OVS bridge/port are ready."""
        try:
            check_provider_interface_safety(self.local_node, self.config, root=self.root)
            rabbit_password, neutron_password = _load_neutron_compute_secrets(self.root)
        except PreconditionError:
            return False
        try:
            if edit_neutron_compute_conf(self.root, rabbit_password, dry_run=True).changed:
                return False
            if edit_neutron_ovs_agent_conf(self.root, self.local_node, dry_run=True).changed:
                return False
            if edit_nova_compute_neutron_conf(self.root, neutron_password, dry_run=True).changed:
                return False
        except (PreconditionError, UnsafePathError, FileFormatError):
            return False
        if not package_installed(self.executor, NEUTRON_COMPUTE_PACKAGE):
            return False
        for unit in (OVS_SWITCH_UNIT, *NEUTRON_COMPUTE_UNITS):
            if unit_active_problems(self.executor, unit):
                return False
        return ovs_bridge_and_port_ready(
            self.executor, OVS_PROVIDER_BRIDGE, self.provider_interface
        )

    def apply(self) -> None:
        """Install ``neutron-openvswitch-agent``, edit 3 config files, configure OVS
        ``br-provider``, and restart ``nova-compute`` and ``neutron-openvswitch-agent``."""
        provider_iface = check_provider_interface_safety(
            self.local_node, self.config, root=self.root
        )
        rabbit_password, neutron_password = _load_neutron_compute_secrets(self.root)
        # Ensure /etc/nova/nova.conf already exists (from nova-compute) before mutating:
        _require_conf_exists(
            self.root, NOVA_CONF_TARGET, "nova-compute (apply 'nova-compute' first)"
        )

        self.executor.run(
            INSTALL_NEUTRON_COMPUTE_PACKAGE.command_id, INSTALL_NEUTRON_COMPUTE_PACKAGE.params
        )
        edit_neutron_compute_conf(self.root, rabbit_password, dry_run=False)
        edit_neutron_ovs_agent_conf(self.root, self.local_node, dry_run=False)
        edit_nova_compute_neutron_conf(self.root, neutron_password, dry_run=False)

        self.executor.run(RESTART_OVS_SWITCH_UNIT.command_id, RESTART_OVS_SWITCH_UNIT.params)
        self.executor.run(
            RUN_OVS_ADD_PROVIDER_BRIDGE.command_id, RUN_OVS_ADD_PROVIDER_BRIDGE.params
        )
        self.executor.run(
            OVS_VSCTL_ADD_PORT,
            {"bridge": OVS_PROVIDER_BRIDGE, "interface": provider_iface},
        )
        for cmd in RESTART_NEUTRON_COMPUTE_UNITS:
            self.executor.run(cmd.command_id, cmd.params)

    def verify(self) -> VerifyResult:
        """Verify secrets, 3 config files, package, units, and OVS bridge/port."""
        problems: list[str] = []
        rabbit_password: str | None = None
        neutron_password: str | None = None
        try:
            check_provider_interface_safety(self.local_node, self.config, root=self.root)
        except PreconditionError as exc:
            problems.append(str(exc))
        try:
            rabbit_password, neutron_password = _load_neutron_compute_secrets(self.root)
        except PreconditionError as exc:
            problems.append(str(exc))

        # 1. /etc/neutron/neutron.conf
        n_path, n_display = resolve_target(self.root, NEUTRON_CONF_TARGET)
        try:
            n_text = _read_existing_conf(n_path, n_display)
        except (UnsafePathError, FileFormatError) as exc:
            return VerifyResult(False, f"{exc}. {COMPUTE_NOT_CHECKED}")
        if n_text is None:
            problems.append(f"{n_display} does not exist")
        elif rabbit_password is not None:
            problems += verify_neutron_compute_conf_text(n_text, rabbit_password)

        # 2. /etc/neutron/plugins/ml2/openvswitch_agent.ini
        ovs_path, ovs_display = resolve_target(self.root, NEUTRON_OVS_AGENT_CONF_TARGET)
        try:
            ovs_text = _read_existing_conf(ovs_path, ovs_display)
        except (UnsafePathError, FileFormatError) as exc:
            return VerifyResult(False, f"{exc}. {COMPUTE_NOT_CHECKED}")
        if ovs_text is None:
            problems.append(f"{ovs_display} does not exist")
        else:
            problems += verify_neutron_ovs_agent_conf_text(ovs_text, self.local_node)

        # 3. /etc/nova/nova.conf ([neutron] section)
        nova_path, nova_display = resolve_target(self.root, NOVA_CONF_TARGET)
        try:
            nova_text = _read_existing_conf(nova_path, nova_display)
        except (UnsafePathError, FileFormatError) as exc:
            return VerifyResult(False, f"{exc}. {COMPUTE_NOT_CHECKED}")
        if nova_text is None:
            problems.append(f"{nova_display} does not exist")
        elif neutron_password is not None:
            problems += verify_nova_compute_neutron_conf_text(nova_text, neutron_password)

        if not package_installed(self.executor, NEUTRON_COMPUTE_PACKAGE):
            problems.append(f"package {NEUTRON_COMPUTE_PACKAGE!r} is not installed")
        for unit in (OVS_SWITCH_UNIT, *NEUTRON_COMPUTE_UNITS):
            problems += unit_active_problems(self.executor, unit)
        problems += ovs_bridge_and_port_problems(
            self.executor, OVS_PROVIDER_BRIDGE, self.provider_interface
        )

        if problems:
            return VerifyResult(False, f"{'; '.join(problems)}. {COMPUTE_NOT_CHECKED}")
        return VerifyResult(True, COMPUTE_NOT_CHECKED)


def make_neutron_controller_step(
    root: Path,
    config: Mapping[str, Any],
    local_node: Mapping[str, Any],
    *,
    executor: CommandExecutor,
) -> NeutronControllerStep:
    """Construct a :class:`NeutronControllerStep` for ``openstack-forge apply``."""
    return NeutronControllerStep(root=root, config=config, local_node=local_node, executor=executor)


def make_neutron_compute_step(
    root: Path,
    config: Mapping[str, Any],
    local_node: Mapping[str, Any],
    *,
    executor: CommandExecutor,
) -> NeutronComputeStep:
    """Construct a :class:`NeutronComputeStep` for ``openstack-forge apply``."""
    return NeutronComputeStep(root=root, config=config, local_node=local_node, executor=executor)
