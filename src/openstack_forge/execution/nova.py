"""The ``nova-controller`` (controller only) and ``nova-compute`` (compute only) steps.

**Unverified: neither step has ever been run on a real host.**

Sources of truth
================
Read directly from the official OpenStack 2025.1 (Epoxy) Nova installation guides for Ubuntu:

1. **Controller node**:
   - https://docs.openstack.org/nova/2025.1/install/controller-install-ubuntu.html
     (raw source: ``doc/source/install/controller-install-ubuntu.rst`` on ``stable/2025.1`` in
     ``openstack/nova``).
2. **Compute node**:
   - https://docs.openstack.org/nova/2025.1/install/compute-install-ubuntu.html
     (raw source: ``doc/source/install/compute-install-ubuntu.rst`` on ``stable/2025.1`` in
     ``openstack/nova``).

What the official controller page prescribes
--------------------------------------------
1. **Prerequisites — SQL databases** (``mysql`` as ``root``):
   - ``CREATE DATABASE nova_api;``
   - ``CREATE DATABASE nova;``
   - ``CREATE DATABASE nova_cell0;``
   - ``GRANT ALL PRIVILEGES ON nova_api.* TO 'nova'@'localhost' IDENTIFIED BY 'NOVA_DBPASS';``
   - ``GRANT ALL PRIVILEGES ON nova_api.* TO 'nova'@'%' IDENTIFIED BY 'NOVA_DBPASS';``
   - ``GRANT ALL PRIVILEGES ON nova.* TO 'nova'@'localhost' IDENTIFIED BY 'NOVA_DBPASS';``
   - ``GRANT ALL PRIVILEGES ON nova.* TO 'nova'@'%' IDENTIFIED BY 'NOVA_DBPASS';``
   - ``GRANT ALL PRIVILEGES ON nova_cell0.* TO 'nova'@'localhost' IDENTIFIED BY 'NOVA_DBPASS';``
   - ``GRANT ALL PRIVILEGES ON nova_cell0.* TO 'nova'@'%' IDENTIFIED BY 'NOVA_DBPASS';``
2. **Prerequisites — Keystone service credentials and API endpoints** (after ``. admin-openrc``):
   - ``openstack user create --domain default --password-prompt nova`` (``NOVA_PASS``)
   - ``openstack role add --project service --user nova admin``
   - ``openstack service create --name nova --description "OpenStack Compute" compute``
   - ``openstack endpoint create --region RegionOne compute public http://controller:8774/v2.1``
   - ``openstack endpoint create --region RegionOne compute internal http://controller:8774/v2.1``
   - ``openstack endpoint create --region RegionOne compute admin http://controller:8774/v2.1``
3. **Install the packages**:
   - ``apt install nova-api nova-conductor nova-novncproxy nova-scheduler``
4. **Edit ``/etc/nova/nova.conf``**:
   - ``[api_database]``:
     ``connection = mysql+pymysql://nova:NOVA_DBPASS@controller/nova_api``
   - ``[database]``:
     ``connection = mysql+pymysql://nova:NOVA_DBPASS@controller/nova``
   - ``[DEFAULT]``:
     ``transport_url = rabbit://openstack:RABBIT_PASS@controller:5672/`` and
     ``my_ip = <controller management IP>``
   - ``[api]``:
     ``auth_strategy = keystone``
   - ``[keystone_authtoken]``:
     ``www_authenticate_uri = http://controller:5000/``,
     ``auth_url = http://controller:5000/``,
     ``memcached_servers = controller:11211``,
     ``auth_type = password``,
     ``project_domain_name = Default``,
     ``user_domain_name = Default``,
     ``project_name = service``,
     ``username = nova``,
     ``password = NOVA_PASS``
   - ``[service_user]``:
     ``send_service_user_token = true``,
     ``auth_url = https://controller/identity`` (see deviation #1 below: set to
     ``http://controller:5000/`` in OpenStack Forge to match the HTTP Keystone endpoint),
     ``auth_strategy = keystone``,
     ``auth_type = password``,
     ``project_domain_name = Default``,
     ``project_name = service``,
     ``user_domain_name = Default``,
     ``username = nova``,
     ``password = NOVA_PASS``
   - ``[vnc]``:
     ``enabled = true``,
     ``server_listen = $my_ip``,
     ``server_proxyclient_address = $my_ip``
   - ``[glance]``:
     ``api_servers = http://controller:9292``
   - ``[oslo_concurrency]``:
     ``lock_path = /var/lib/nova/tmp``
   - ``[placement]``:
     ``region_name = RegionOne``,
     ``project_domain_name = Default``,
     ``project_name = service``,
     ``auth_type = password``,
     ``user_domain_name = Default``,
     ``auth_url = http://controller:5000/v3``,
     ``username = placement``,
     ``password = PLACEMENT_PASS``
   - ``[scheduler]`` (from ``compute-install-ubuntu.rst`` "Add the compute node to the cell
     database"):
     ``discover_hosts_in_cells_interval = 300``
5. **Populate databases and register cells v2**:
   - ``su -s /bin/sh -c "nova-manage api_db sync" nova``
   - ``su -s /bin/sh -c "nova-manage cell_v2 map_cell0" nova``
   - ``su -s /bin/sh -c "nova-manage cell_v2 create_cell --name=cell1 --verbose" nova``
   - ``su -s /bin/sh -c "nova-manage db sync" nova``
   - ``su -s /bin/sh -c "nova-manage cell_v2 list_cells" nova``
6. **Finalize installation**:
   - ``service nova-api restart``
   - ``service nova-scheduler restart``
   - ``service nova-conductor restart``
   - ``service nova-novncproxy restart``

What the official compute page prescribes
-----------------------------------------
1. **Install the package**:
   - ``apt install nova-compute``
2. **Edit ``/etc/nova/nova.conf``**:
   - ``[DEFAULT]``:
     ``transport_url = rabbit://openstack:RABBIT_PASS@controller`` (normalized to
     ``rabbit://openstack:RABBIT_PASS@controller:5672/``; see deviation #2 below) and
     ``my_ip = <compute node management IP>``
   - ``[api]``, ``[keystone_authtoken]``, ``[service_user]``, ``[glance]``,
     ``[oslo_concurrency]``, ``[placement]``: same as controller.
   - ``[vnc]``:
     ``enabled = true``,
     ``server_listen = 0.0.0.0``,
     ``server_proxyclient_address = $my_ip``,
     ``novncproxy_base_url = http://controller:6080/vnc_auto.html``
3. **Determine hypervisor acceleration and edit ``/etc/nova/nova-compute.conf``**:
   - ``egrep -c '(vmx|svm)' /proc/cpuinfo``: if ``>= 1``, KVM is supported (``virt_type = kvm``);
     if ``0``, set ``[libvirt] virt_type = qemu`` in ``/etc/nova/nova-compute.conf``.
4. **Finalize installation**:
   - ``service nova-compute restart``

Ubuntu 24.04 + Epoxy Cloud Archive package facts (established read-only on the dev machine)
-------------------------------------------------------------------------------------------
- ``apt-cache policy nova-api nova-conductor nova-novncproxy nova-scheduler nova-compute
  nova-common python3-nova``:
  ``3:31.0.0-0ubuntu1.2~cloud3`` (installed ``3:31.0.0-0ubuntu1.2~cloud1``) in
  ``noble-updates/epoxy/main`` (Ubuntu Cloud Archive for OpenStack 2025.1 Epoxy) and
  ``3:29.2.0-0ubuntu1.8`` in ``noble-updates/main`` (``nova-novncproxy`` in ``noble/universe``
  for the base archive and ``noble-updates/epoxy/main`` in the Cloud Archive).
- ``apt-cache show``:
  - ``nova-api``, ``nova-conductor``, ``nova-novncproxy``, ``nova-scheduler`` all depend on
    ``python3-nova``, which depends on ``nova-common``;
  - ``nova-compute`` depends on ``bridge-utils, nova-compute-kvm | nova-compute-hypervisor,
    python3-nova``; ``nova-compute-kvm`` depends on ``nova-compute-libvirt, qemu-kvm |
    qemu-system`` and provides ``/etc/nova/nova-compute.conf``.
- ``dpkg -S`` and ``dpkg-query -W -f='${Conffiles}\\n' nova-common`` confirm:
  - ``python3-nova: /usr/bin/nova-manage``;
  - ``nova-common: /etc/nova/nova.conf`` (conffile MD5 ``9127d52a0e8b6812f4159720ef1eed7a``;
    ``nova-common.postinst`` creates system user/group ``nova`` UID/GID ``64060``, sets
    ``/etc/nova`` to ``0750`` ``root:nova`` and files in ``/etc/nova`` to ``0640`` ``root:nova``,
    and sets ``/var/log/nova`` to ``0750`` ``nova:adm``);
  - ``/usr/lib/systemd/system/nova-api.service``, ``nova-scheduler.service``,
    ``nova-conductor.service``, and ``nova-novncproxy.service`` run as ``User=nova``,
    ``Group=nova`` via ``/etc/init.d/nova-<daemon> systemd-start`` (which passes
    ``--config-file=/etc/nova/nova.conf --log-file=/var/log/nova/nova-<daemon>.log``).
- ``/usr/lib/python3/dist-packages/nova/cmd/manage.py``:
  - ``CATEGORIES`` maps ``'api_db': ApiDbCommands``, ``'cell_v2': CellV2Commands``, and
    ``'db': DbCommands``;
  - ``CellV2Commands.map_cell0`` (lines 816–832) catches ``db_exc.DBDuplicateEntry``, prints
    ``'Cell0 is already setup'``, and returns ``0`` (natively idempotent);
  - ``CellV2Commands.create_cell`` (lines 1183–1207) checks
    ``_non_unique_transport_url_database_connection_checker()`` and returns exit code ``2`` if a
    cell with the same ``transport_url`` and ``database_connection`` already exists (not
    idempotent unless ``list_cells`` is checked first);
  - ``CellV2Commands.list_cells`` (lines 1211–1239) prints a ``prettytable.PrettyTable`` with
    columns ``Name | UUID | Transport URL | Database Connection | Disabled`` and masks passwords
    by default.

Deviations, trade-offs, and scope boundary
------------------------------------------
1. **``[service_user] auth_url = http://controller:5000/`` instead of ``https://controller/identity``**:
   Both ``controller-install-ubuntu.rst`` (line 251) and ``compute-install-ubuntu.rst``
   (line 470) show ``auth_url = https://controller/identity`` in ``[service_user]`` (a leftover
   from DevStack's TLS ``/identity`` proxy), while ``[keystone_authtoken]`` and ``[placement]``
   on the very same pages use ``http://controller:5000/`` and ``http://controller:5000/v3``.
   Because ``keystone-install-ubuntu.html`` configures Keystone on HTTP port ``5000`` without TLS
   or an ``/identity`` prefix, OpenStack Forge sets ``[service_user] auth_url =
   http://controller:5000/`` on both controller and compute nodes so service-user tokens reach
   Keystone.
2. **``[DEFAULT] transport_url = rabbit://openstack:RABBIT_PASS@controller:5672/`` on both roles**:
   ``controller-install-ubuntu.rst`` writes ``rabbit://openstack:RABBIT_PASS@controller:5672/``
   while ``compute-install-ubuntu.rst`` omits ``:5672/``. OpenStack Forge uses the explicit
   ``rabbit://openstack:RABBIT_PASS@controller:5672/`` form on both controller and compute nodes.
3. **``[scheduler] discover_hosts_in_cells_interval = 300`` on the controller**:
   Recommended by ``compute-install-ubuntu.rst`` (lines 643–651) as the declarative alternative
   to manually re-running ``nova-manage cell_v2 discover_hosts`` on the controller every time a
   compute node finishes starting.
4. **Idempotent ``cell1`` creation via ``nova-manage cell_v2 list_cells``**:
   Because ``nova-manage cell_v2 create_cell --name=cell1 --verbose`` exits with code ``2`` when
   ``cell1`` already exists, ``NovaControllerStep.apply()`` runs ``nova-manage cell_v2
   list_cells`` first and invokes ``create_cell`` only when ``cell1`` is absent from the parsed
   table.
5. **Pure ``/proc/cpuinfo`` inspection instead of ``egrep -c '(vmx|svm)' /proc/cpuinfo``**:
   ``NovaComputeStep`` reads ``<root>/proc/cpuinfo`` in pure Python via
   :func:`detect_node_libvirt_virt_type` / :func:`detect_libvirt_virt_type` and always sets
   ``[libvirt] virt_type = kvm`` (when ``vmx`` or ``svm`` is present in a ``flags``/``features``
   line) or ``[libvirt] virt_type = qemu`` (otherwise, or if ``/proc/cpuinfo`` is absent) in
   ``/etc/nova/nova-compute.conf``.
6. **Least-privilege secrets on compute nodes**:
   ``NovaComputeStep`` loads only ``RABBIT_PASS``, ``NOVA_PASS``, and ``PLACEMENT_PASS`` from
   :class:`SecretStore`; ``NOVA_DBPASS`` and ``ADMIN_PASS`` are controller-only secrets and are
   never required on compute nodes.
7. **``nova-manage`` invoked directly as ``root`` instead of ``su``**:
   ``CommandSpec`` forbids ``su`` and ``sudo``. Editing ``/etc/nova/nova.conf`` before running
   ``nova-manage`` ensures ``api_db sync``, ``map_cell0``, ``create_cell``, and ``db sync``
   connect to MariaDB rather than SQLite.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from ipaddress import IPv4Address
from pathlib import Path
from typing import Any

from openstack_forge.execution.commands import (
    APT_GET_INSTALL,
    NOVA_MANAGE_API_DB_SYNC,
    NOVA_MANAGE_CREATE_CELL1,
    NOVA_MANAGE_DB_SYNC,
    NOVA_MANAGE_LIST_CELLS,
    NOVA_MANAGE_MAP_CELL0,
    OPENSTACK_ENDPOINT_CREATE,
    OPENSTACK_ENDPOINT_LIST,
    OPENSTACK_ROLE_ADD_USER_PROJECT,
    OPENSTACK_ROLE_ASSIGNMENT_LIST,
    OPENSTACK_SERVICE_CREATE,
    OPENSTACK_SERVICE_LIST,
    OPENSTACK_USER_CREATE,
    OPENSTACK_USER_LIST,
    SYSTEMCTL_RESTART,
    CommandExecutor,
)
from openstack_forge.execution.engine import VerifyResult
from openstack_forge.execution.errors import (
    ExecutionError,
    FileFormatError,
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

NOVA_CONTROLLER_STEP_ID = "nova-controller"
NOVA_COMPUTE_STEP_ID = "nova-compute"

NOVA_CONTROLLER_PACKAGES: tuple[str, ...] = (
    "nova-api",
    "nova-conductor",
    "nova-novncproxy",
    "nova-scheduler",
)
# Official guide restart order (controller-install-ubuntu.rst lines 381-384):
NOVA_CONTROLLER_UNITS: tuple[str, ...] = (
    "nova-api",
    "nova-scheduler",
    "nova-conductor",
    "nova-novncproxy",
)

NOVA_COMPUTE_PACKAGE = "nova-compute"
NOVA_COMPUTE_UNIT = "nova-compute"

NOVA_API_DB_NAME = "nova_api"
NOVA_MAIN_DB_NAME = "nova"
NOVA_CELL0_DB_NAME = "nova_cell0"
NOVA_DB_NAMES: tuple[str, ...] = (
    NOVA_API_DB_NAME,
    NOVA_MAIN_DB_NAME,
    NOVA_CELL0_DB_NAME,
)
NOVA_DB_USER = "nova"

NOVA_ADMIN_SECRET_NAME = "ADMIN_PASS"
NOVA_RABBIT_SECRET_NAME = "RABBIT_PASS"
NOVA_DB_SECRET_NAME = "NOVA_DBPASS"
NOVA_SERVICE_SECRET_NAME = "NOVA_PASS"
NOVA_PLACEMENT_SECRET_NAME = "PLACEMENT_PASS"

NOVA_SERVICE_USER = "nova"
NOVA_SERVICE_NAME = "nova"
NOVA_SERVICE_TYPE = "compute"
NOVA_ENDPOINT_URL = "http://controller:8774/v2.1"

NOVA_CONF_TARGET = "/etc/nova/nova.conf"
NOVA_COMPUTE_CONF_TARGET = "/etc/nova/nova-compute.conf"
PROC_CPUINFO_TARGET = "/proc/cpuinfo"

NOVA_LOCK_PATH = "/var/lib/nova/tmp"
NOVA_NOVNCPROXY_BASE_URL = "http://controller:6080/vnc_auto.html"
NOVA_DISCOVER_HOSTS_INTERVAL = "300"

_DRY_RUN_PLACEHOLDER_SECRET = "A" * DEFAULT_SECRET_LENGTH

INSTALL_NOVA_CONTROLLER_PACKAGES: tuple[PlannedCommand, ...] = tuple(
    PlannedCommand(APT_GET_INSTALL, {"package": pkg}) for pkg in NOVA_CONTROLLER_PACKAGES
)
RESTART_NOVA_CONTROLLER_UNITS: tuple[PlannedCommand, ...] = tuple(
    PlannedCommand(SYSTEMCTL_RESTART, {"unit": unit}) for unit in NOVA_CONTROLLER_UNITS
)
QUERY_NOVA_CONTROLLER_PACKAGES: tuple[PlannedCommand, ...] = tuple(
    query_package(pkg) for pkg in NOVA_CONTROLLER_PACKAGES
)
QUERY_NOVA_CONTROLLER_ACTIVE: tuple[PlannedCommand, ...] = tuple(
    query_active(unit) for unit in NOVA_CONTROLLER_UNITS
)

RUN_NOVA_API_DB_SYNC = PlannedCommand(NOVA_MANAGE_API_DB_SYNC)
RUN_NOVA_MAP_CELL0 = PlannedCommand(NOVA_MANAGE_MAP_CELL0)
QUERY_NOVA_LIST_CELLS = PlannedCommand(NOVA_MANAGE_LIST_CELLS)
RUN_NOVA_CREATE_CELL1 = PlannedCommand(NOVA_MANAGE_CREATE_CELL1)
RUN_NOVA_DB_SYNC = PlannedCommand(NOVA_MANAGE_DB_SYNC)

INSTALL_NOVA_COMPUTE_PACKAGE = PlannedCommand(APT_GET_INSTALL, {"package": NOVA_COMPUTE_PACKAGE})
RESTART_NOVA_COMPUTE_UNIT = PlannedCommand(SYSTEMCTL_RESTART, {"unit": NOVA_COMPUTE_UNIT})
QUERY_NOVA_COMPUTE_PACKAGE = query_package(NOVA_COMPUTE_PACKAGE)
QUERY_NOVA_COMPUTE_ACTIVE = query_active(NOVA_COMPUTE_UNIT)

CONTROLLER_SQL_STDIN_NOTE = (
    "MariaDB provisioning: 'CREATE DATABASE IF NOT EXISTS' and grants for 'nova'@'localhost' "
    "and 'nova'@'%' on `nova_api`, `nova`, and `nova_cell0` are sent to '/usr/bin/mariadb "
    "--batch --skip-column-names' over stdin (stdin_param='sql') so NOVA_DBPASS never appears "
    "in argv or /proc/<pid>/cmdline (ps)."
)
CONTROLLER_KEYSTONE_NOTE = (
    "Keystone provisioning: /usr/bin/openstack receives ADMIN_PASS via OS_PASSWORD "
    "(env_params, never in argv) and idempotently ensures the 'nova' user (with 'admin' role "
    "on project 'service'), the 'nova' ('compute') service, and its public/internal/admin "
    "endpoints (http://controller:8774/v2.1 in RegionOne)."
)
SERVICE_USER_AUTH_URL_NOTE = (
    "Deviation from guide: [service_user] auth_url is set to 'http://controller:5000/' instead "
    "of the upstream guide's 'https://controller/identity' (a DevStack TLS URL) so service-user "
    "tokens reach the HTTP port 5000 Keystone endpoint configured by the keystone step."
)
CELL_V2_NOTE = (
    "Cells v2 provisioning: runs 'nova-manage api_db sync', 'nova-manage cell_v2 map_cell0', "
    "checks 'nova-manage cell_v2 list_cells' and runs 'nova-manage cell_v2 create_cell "
    "--name=cell1 --verbose' only if cell1 is not yet registered (create_cell exits with code 2 "
    "on duplicate cells), then runs 'nova-manage db sync'. Also sets [scheduler] "
    "discover_hosts_in_cells_interval = 300 so newly started compute nodes are discovered "
    "automatically."
)
CONTROLLER_NOT_CHECKED = (
    "Not checked: no live HTTP request to http://controller:8774/v2.1 or AMQP connection to "
    "controller:5672 is performed."
)
COMPUTE_LEAST_PRIVILEGE_NOTE = (
    "Least-privilege secrets: nova-compute loads only RABBIT_PASS, NOVA_PASS, and "
    "PLACEMENT_PASS from /etc/openstack-forge/secrets.json; NOVA_DBPASS and ADMIN_PASS are "
    "never read on compute nodes."
)
COMPUTE_VIRT_TYPE_NOTE = (
    "Hypervisor acceleration: reads <root>/proc/cpuinfo in pure Python (instead of running "
    "egrep) and sets [libvirt] virt_type = 'kvm' when vmx or svm is present, or 'qemu' "
    "otherwise, in /etc/nova/nova-compute.conf."
)
COMPUTE_NOT_CHECKED = (
    "Not checked: no live AMQP connection to controller:5672 or hypervisor registration in "
    "'openstack compute service list' is performed."
)


def nova_api_db_connection(db_password: str) -> str:
    """Return the ``[api_database] connection`` URL for ``/etc/nova/nova.conf``."""
    return f"mysql+pymysql://{NOVA_DB_USER}:{db_password}@controller/{NOVA_API_DB_NAME}"


def nova_db_connection(db_password: str) -> str:
    """Return the ``[database] connection`` URL for ``/etc/nova/nova.conf``."""
    return f"mysql+pymysql://{NOVA_DB_USER}:{db_password}@controller/{NOVA_MAIN_DB_NAME}"


def nova_transport_url(rabbit_password: str) -> str:
    """Return the ``[DEFAULT] transport_url`` for ``/etc/nova/nova.conf``."""
    return f"rabbit://openstack:{rabbit_password}@controller:5672/"


def _management_ip(local_node: Mapping[str, Any]) -> str:
    return str(IPv4Address(local_node["management_ip"]))


def _common_auth_and_service_settings(
    nova_password: str, placement_password: str
) -> tuple[tuple[str, str, str], ...]:
    """Shared ``[api]``, ``[keystone_authtoken]``, ``[service_user]``, ``[glance]``,
    ``[oslo_concurrency]``, and ``[placement]`` settings for controller and compute."""
    return (
        ("api", "auth_strategy", "keystone"),
        ("keystone_authtoken", "www_authenticate_uri", "http://controller:5000/"),
        ("keystone_authtoken", "auth_url", "http://controller:5000/"),
        ("keystone_authtoken", "memcached_servers", "controller:11211"),
        ("keystone_authtoken", "auth_type", "password"),
        ("keystone_authtoken", "project_domain_name", "Default"),
        ("keystone_authtoken", "user_domain_name", "Default"),
        ("keystone_authtoken", "project_name", SERVICE_PROJECT_NAME),
        ("keystone_authtoken", "username", NOVA_SERVICE_USER),
        ("keystone_authtoken", "password", nova_password),
        ("service_user", "send_service_user_token", "true"),
        ("service_user", "auth_url", "http://controller:5000/"),
        ("service_user", "auth_strategy", "keystone"),
        ("service_user", "auth_type", "password"),
        ("service_user", "project_domain_name", "Default"),
        ("service_user", "project_name", SERVICE_PROJECT_NAME),
        ("service_user", "user_domain_name", "Default"),
        ("service_user", "username", NOVA_SERVICE_USER),
        ("service_user", "password", nova_password),
    )


def _placement_settings(placement_password: str) -> tuple[tuple[str, str, str], ...]:
    return (
        ("placement", "region_name", "RegionOne"),
        ("placement", "project_domain_name", "Default"),
        ("placement", "project_name", SERVICE_PROJECT_NAME),
        ("placement", "auth_type", "password"),
        ("placement", "user_domain_name", "Default"),
        ("placement", "auth_url", "http://controller:5000/v3"),
        ("placement", "username", "placement"),
        ("placement", "password", placement_password),
    )


def nova_controller_conf_settings(
    local_node: Mapping[str, Any],
    db_password: str,
    rabbit_password: str,
    nova_password: str,
    placement_password: str,
) -> tuple[tuple[str, str, str], ...]:
    """Return the 37 ``(section, key, value)`` settings for controller ``/etc/nova/nova.conf``."""
    address = _management_ip(local_node)
    return (
        ("api_database", "connection", nova_api_db_connection(db_password)),
        ("database", "connection", nova_db_connection(db_password)),
        ("DEFAULT", "transport_url", nova_transport_url(rabbit_password)),
        ("DEFAULT", "my_ip", address),
        *_common_auth_and_service_settings(nova_password, placement_password),
        ("vnc", "enabled", "true"),
        ("vnc", "server_listen", "$my_ip"),
        ("vnc", "server_proxyclient_address", "$my_ip"),
        ("glance", "api_servers", "http://controller:9292"),
        ("oslo_concurrency", "lock_path", NOVA_LOCK_PATH),
        *_placement_settings(placement_password),
        ("scheduler", "discover_hosts_in_cells_interval", NOVA_DISCOVER_HOSTS_INTERVAL),
    )


build_nova_controller_ini_entries = nova_controller_conf_settings


def nova_compute_conf_settings(
    local_node: Mapping[str, Any],
    rabbit_password: str,
    nova_password: str,
    placement_password: str,
) -> tuple[tuple[str, str, str], ...]:
    """Return the 35 ``(section, key, value)`` settings for compute ``/etc/nova/nova.conf``."""
    address = _management_ip(local_node)
    return (
        ("DEFAULT", "transport_url", nova_transport_url(rabbit_password)),
        ("DEFAULT", "my_ip", address),
        *_common_auth_and_service_settings(nova_password, placement_password),
        ("vnc", "enabled", "true"),
        ("vnc", "server_listen", "0.0.0.0"),
        ("vnc", "server_proxyclient_address", "$my_ip"),
        ("vnc", "novncproxy_base_url", NOVA_NOVNCPROXY_BASE_URL),
        ("glance", "api_servers", "http://controller:9292"),
        ("oslo_concurrency", "lock_path", NOVA_LOCK_PATH),
        *_placement_settings(placement_password),
    )


build_nova_compute_ini_entries = nova_compute_conf_settings


def detect_libvirt_virt_type(cpuinfo_text: str) -> str:
    """Return ``'kvm'`` if ``cpuinfo_text`` advertises ``vmx`` or ``svm`` flags, else ``'qemu'``."""
    for raw_line in cpuinfo_text.splitlines():
        key, sep, value = raw_line.partition(":")
        if not sep:
            continue
        if key.strip().lower() in {"flags", "features"}:
            tokens = {token.lower() for token in value.split()}
            if "vmx" in tokens or "svm" in tokens:
                return "kvm"
    return "qemu"


def detect_node_libvirt_virt_type(root: Path) -> str:
    """Read ``<root>/proc/cpuinfo`` if present and return ``'kvm'`` or ``'qemu'``."""
    path, display = resolve_target(root, PROC_CPUINFO_TARGET)
    text = _read_existing_conf(path, display)
    if text is None:
        return "qemu"
    return detect_libvirt_virt_type(text)


def nova_compute_hypervisor_conf_settings(
    virt_type: str,
) -> tuple[tuple[str, str, str], ...]:
    """Return the ``(section, key, value)`` setting for ``/etc/nova/nova-compute.conf``."""
    if virt_type not in {"kvm", "qemu"}:
        raise ExecutionError(
            f"Unsupported libvirt virt_type {virt_type!r}; expected 'kvm' or 'qemu'"
        )
    return (("libvirt", "virt_type", virt_type),)


def parse_cell_names(output: str) -> frozenset[str]:
    """Extract cell names from ``nova-manage cell_v2 list_cells`` PrettyTable or plain output."""
    names: set[str] = set()
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("+") or line.startswith("-"):
            continue
        if line.startswith("|") and line.endswith("|"):
            cols = [col.strip() for col in line.strip("|").split("|")]
            if len(cols) < 2:
                continue
            if cols[0].lower() == "name" and cols[1].lower() == "uuid":
                continue
            if cols[0]:
                names.add(cols[0])
            continue
        parts = line.split()
        if len(parts) >= 2 and not (parts[0].lower() == "name" and parts[1].lower() == "uuid"):
            names.add(parts[0])
    return frozenset(names)


def cell_exists_in_list_output(output: str, cell_name: str = "cell1") -> bool:
    """Return ``True`` if ``cell_name`` appears in ``nova-manage cell_v2 list_cells`` output."""
    return cell_name in parse_cell_names(output)


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


def _verify_ini_settings(
    text: str,
    target_display: str,
    settings: tuple[tuple[str, str, str], ...],
    *secrets: str,
) -> list[str]:
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

    problems: list[str] = []
    for section_name, key, expected in settings:
        found = values.get((section_name, key), [])
        is_secret_key = key == "password"
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


def edit_nova_controller_conf(
    root: Path,
    local_node: Mapping[str, Any],
    db_password: str,
    rabbit_password: str,
    nova_password: str,
    placement_password: str,
    *,
    dry_run: bool,
) -> FileChange:
    """Apply the 2025.1 Ubuntu controller settings to ``/etc/nova/nova.conf``."""
    path, display = resolve_target(root, NOVA_CONF_TARGET)
    if _read_existing_conf(path, display) is None:
        raise PreconditionError(
            f"{display} does not exist; it is created when {NOVA_CONTROLLER_PACKAGES[0]} "
            "is installed"
        )
    return ini_set_many(
        root,
        NOVA_CONF_TARGET,
        nova_controller_conf_settings(
            local_node, db_password, rabbit_password, nova_password, placement_password
        ),
        dry_run=dry_run,
    )


def verify_nova_controller_conf_text(
    text: str,
    local_node: Mapping[str, Any],
    db_password: str,
    rabbit_password: str,
    nova_password: str,
    placement_password: str,
) -> list[str]:
    """Pure check: all expected controller ``/etc/nova/nova.conf`` keys match expected values."""
    return _verify_ini_settings(
        text,
        NOVA_CONF_TARGET,
        nova_controller_conf_settings(
            local_node, db_password, rabbit_password, nova_password, placement_password
        ),
        db_password,
        rabbit_password,
        nova_password,
        placement_password,
    )


def edit_nova_compute_conf(
    root: Path,
    local_node: Mapping[str, Any],
    rabbit_password: str,
    nova_password: str,
    placement_password: str,
    *,
    dry_run: bool,
) -> FileChange:
    """Apply the 2025.1 Ubuntu compute settings to ``/etc/nova/nova.conf``."""
    path, display = resolve_target(root, NOVA_CONF_TARGET)
    if _read_existing_conf(path, display) is None:
        raise PreconditionError(
            f"{display} does not exist; it is created when {NOVA_COMPUTE_PACKAGE} is installed"
        )
    return ini_set_many(
        root,
        NOVA_CONF_TARGET,
        nova_compute_conf_settings(local_node, rabbit_password, nova_password, placement_password),
        dry_run=dry_run,
    )


def verify_nova_compute_conf_text(
    text: str,
    local_node: Mapping[str, Any],
    rabbit_password: str,
    nova_password: str,
    placement_password: str,
) -> list[str]:
    """Pure check: all expected compute ``/etc/nova/nova.conf`` keys match expected values."""
    return _verify_ini_settings(
        text,
        NOVA_CONF_TARGET,
        nova_compute_conf_settings(local_node, rabbit_password, nova_password, placement_password),
        rabbit_password,
        nova_password,
        placement_password,
    )


def edit_nova_compute_hypervisor_conf(root: Path, virt_type: str, *, dry_run: bool) -> FileChange:
    """Set ``[libvirt] virt_type`` in ``/etc/nova/nova-compute.conf``."""
    path, display = resolve_target(root, NOVA_COMPUTE_CONF_TARGET)
    if _read_existing_conf(path, display) is None:
        raise PreconditionError(
            f"{display} does not exist; it is created when {NOVA_COMPUTE_PACKAGE} is installed"
        )
    return ini_set_many(
        root,
        NOVA_COMPUTE_CONF_TARGET,
        nova_compute_hypervisor_conf_settings(virt_type),
        dry_run=dry_run,
    )


def verify_nova_compute_hypervisor_conf_text(text: str, virt_type: str) -> list[str]:
    """Pure check: ``[libvirt] virt_type`` in ``/etc/nova/nova-compute.conf`` matches."""
    return _verify_ini_settings(
        text,
        NOVA_COMPUTE_CONF_TARGET,
        nova_compute_hypervisor_conf_settings(virt_type),
    )


def _load_nova_controller_secrets(root: Path) -> tuple[str, str, str, str, str]:
    """Load ``(ADMIN_PASS, RABBIT_PASS, NOVA_DBPASS, NOVA_PASS, PLACEMENT_PASS)``."""
    store = SecretStore(root)
    try:
        admin_password = store.get(NOVA_ADMIN_SECRET_NAME)
        rabbit_password = store.get(NOVA_RABBIT_SECRET_NAME)
        db_password = store.get(NOVA_DB_SECRET_NAME)
        nova_password = store.get(NOVA_SERVICE_SECRET_NAME)
        placement_password = store.get(NOVA_PLACEMENT_SECRET_NAME)
    except (SecretError, UnsafePathError) as exc:
        raise PreconditionError(
            f"{NOVA_CONTROLLER_STEP_ID} requires {NOVA_ADMIN_SECRET_NAME}, "
            f"{NOVA_RABBIT_SECRET_NAME}, {NOVA_DB_SECRET_NAME}, "
            f"{NOVA_SERVICE_SECRET_NAME}, and {NOVA_PLACEMENT_SECRET_NAME} in "
            f"{DEFAULT_SECRETS_TARGET}: {exc}. Run 'openstack-forge secrets init' first "
            "(the step never generates secrets itself)."
        ) from exc
    return admin_password, rabbit_password, db_password, nova_password, placement_password


def _load_nova_compute_secrets(root: Path) -> tuple[str, str, str]:
    """Load ``(RABBIT_PASS, NOVA_PASS, PLACEMENT_PASS)`` for a compute node (least privilege)."""
    store = SecretStore(root)
    try:
        rabbit_password = store.get(NOVA_RABBIT_SECRET_NAME)
        nova_password = store.get(NOVA_SERVICE_SECRET_NAME)
        placement_password = store.get(NOVA_PLACEMENT_SECRET_NAME)
    except (SecretError, UnsafePathError) as exc:
        raise PreconditionError(
            f"{NOVA_COMPUTE_STEP_ID} requires {NOVA_RABBIT_SECRET_NAME}, "
            f"{NOVA_SERVICE_SECRET_NAME}, and {NOVA_PLACEMENT_SECRET_NAME} in "
            f"{DEFAULT_SECRETS_TARGET}: {exc}. Run 'openstack-forge secrets init' on the "
            "controller and copy the required secrets to this node (the step never generates "
            "secrets itself)."
        ) from exc
    return rabbit_password, nova_password, placement_password


def _cells_ready(executor: CommandExecutor) -> bool:
    """Return ``True`` if ``nova-manage cell_v2 list_cells`` lists both ``cell0`` and ``cell1``."""
    try:
        result = executor.run(NOVA_MANAGE_LIST_CELLS)
    except UnexpectedExitError:
        return False
    names = parse_cell_names(result.stdout)
    return "cell0" in names and "cell1" in names


def _cells_problems(executor: CommandExecutor) -> list[str]:
    """Return human-readable problems if ``cell0`` or ``cell1`` is missing in ``list_cells``."""
    try:
        result = executor.run(NOVA_MANAGE_LIST_CELLS)
    except UnexpectedExitError as exc:
        return [f"nova-manage cell_v2 list_cells failed: {exc}"]
    names = parse_cell_names(result.stdout)
    problems: list[str] = []
    for expected_cell in ("cell0", "cell1"):
        if expected_cell not in names:
            problems.append(
                f"Nova cell {expected_cell!r} is not registered in 'nova-manage cell_v2 list_cells'"
            )
    return problems


@dataclass(frozen=True)
class NovaControllerStep:
    """``check`` / ``apply`` / ``verify`` for the ``nova-controller`` catalog step."""

    root: Path
    config: Mapping[str, Any]
    local_node: Mapping[str, Any]
    executor: CommandExecutor
    id: str = NOVA_CONTROLLER_STEP_ID
    declares_commands: bool = True

    def __post_init__(self) -> None:
        role = self.local_node.get("role")
        if role != "controller":
            raise ExecutionError(
                f"Step {self.id!r} applies only to the 'controller' role, got {role!r}"
            )
        _management_ip(self.local_node)

    def dry_run_preview(self) -> StepPreview:
        """File diff + commands ``apply()`` would run, with all secrets masked."""
        notes: list[str] = []
        try:
            (
                admin_password,
                rabbit_password,
                db_password,
                nova_password,
                placement_password,
            ) = _load_nova_controller_secrets(self.root)
        except PreconditionError as exc:
            admin_password = _DRY_RUN_PLACEHOLDER_SECRET
            rabbit_password = _DRY_RUN_PLACEHOLDER_SECRET
            db_password = _DRY_RUN_PLACEHOLDER_SECRET
            nova_password = _DRY_RUN_PLACEHOLDER_SECRET
            placement_password = _DRY_RUN_PLACEHOLDER_SECRET
            notes.append(f"Precondition (checked by apply()): {exc}")

        file_changes: list[FileChange] = []
        path, display = resolve_target(self.root, NOVA_CONF_TARGET)
        if _read_existing_conf(path, display) is None:
            secrets_to_mask = (db_password, rabbit_password, nova_password, placement_password)
            rendered = "\n".join(
                f"[{sec}] {k} = {_mask_secrets(v, *secrets_to_mask)}"
                for sec, k, v in nova_controller_conf_settings(
                    self.local_node,
                    db_password,
                    rabbit_password,
                    nova_password,
                    placement_password,
                )
            )
            notes.append(
                f"{display} does not exist yet (created when {NOVA_CONTROLLER_PACKAGES[0]} is "
                f"installed); apply() will install {', '.join(NOVA_CONTROLLER_PACKAGES)} first "
                f"and then set:\n{rendered}"
            )
        else:
            file_changes.append(
                edit_nova_controller_conf(
                    self.root,
                    self.local_node,
                    db_password,
                    rabbit_password,
                    nova_password,
                    placement_password,
                    dry_run=True,
                )
            )

        notes.extend(
            (
                CONTROLLER_SQL_STDIN_NOTE,
                CONTROLLER_KEYSTONE_NOTE,
                SERVICE_USER_AUTH_URL_NOTE,
                CELL_V2_NOTE,
                CONTROLLER_NOT_CHECKED,
            )
        )
        commands = (
            plan_service_database_sql(NOVA_DB_NAMES, NOVA_DB_USER, db_password),
            PlannedCommand(
                OPENSTACK_USER_CREATE,
                {
                    "admin_password": admin_password,
                    "user": NOVA_SERVICE_USER,
                    "user_password": nova_password,
                },
            ),
            PlannedCommand(
                OPENSTACK_ROLE_ADD_USER_PROJECT,
                {
                    "admin_password": admin_password,
                    "project": SERVICE_PROJECT_NAME,
                    "user": NOVA_SERVICE_USER,
                    "role": ADMIN_ROLE_NAME,
                },
            ),
            PlannedCommand(
                OPENSTACK_SERVICE_CREATE,
                {
                    "admin_password": admin_password,
                    "service_name": NOVA_SERVICE_NAME,
                    "service_type": NOVA_SERVICE_TYPE,
                },
            ),
            *(
                PlannedCommand(
                    OPENSTACK_ENDPOINT_CREATE,
                    {
                        "admin_password": admin_password,
                        "service_name": NOVA_SERVICE_NAME,
                        "interface": iface,
                        "url": NOVA_ENDPOINT_URL,
                    },
                )
                for iface in ENDPOINT_INTERFACES
            ),
            *INSTALL_NOVA_CONTROLLER_PACKAGES,
            RUN_NOVA_API_DB_SYNC,
            RUN_NOVA_MAP_CELL0,
            RUN_NOVA_CREATE_CELL1,
            RUN_NOVA_DB_SYNC,
            *RESTART_NOVA_CONTROLLER_UNITS,
        )
        read_only_commands = (
            *QUERY_NOVA_CONTROLLER_PACKAGES,
            *QUERY_NOVA_CONTROLLER_ACTIVE,
            QUERY_MARIADB_STATE,
            PlannedCommand(OPENSTACK_USER_LIST, {"admin_password": admin_password}),
            PlannedCommand(OPENSTACK_ROLE_ASSIGNMENT_LIST, {"admin_password": admin_password}),
            PlannedCommand(OPENSTACK_SERVICE_LIST, {"admin_password": admin_password}),
            PlannedCommand(OPENSTACK_ENDPOINT_LIST, {"admin_password": admin_password}),
            QUERY_NOVA_LIST_CELLS,
        )
        return StepPreview(
            file_changes=tuple(file_changes),
            commands=commands,
            read_only_commands=read_only_commands,
            notes=tuple(notes),
        )

    def check(self) -> bool:
        """True if secrets, nova.conf, packages, units, MariaDB, Keystone, and cells are ready."""
        try:
            (
                admin_password,
                rabbit_password,
                db_password,
                nova_password,
                placement_password,
            ) = _load_nova_controller_secrets(self.root)
        except PreconditionError:
            return False
        try:
            if edit_nova_controller_conf(
                self.root,
                self.local_node,
                db_password,
                rabbit_password,
                nova_password,
                placement_password,
                dry_run=True,
            ).changed:
                return False
        except (PreconditionError, UnsafePathError, FileFormatError):
            return False
        for pkg in NOVA_CONTROLLER_PACKAGES:
            if not package_installed(self.executor, pkg):
                return False
        for unit in NOVA_CONTROLLER_UNITS:
            if unit_active_problems(self.executor, unit):
                return False
        if not service_database_ready(self.executor, NOVA_DB_NAMES, NOVA_DB_USER):
            return False
        if not service_user_ready(self.executor, admin_password, NOVA_SERVICE_USER):
            return False
        if not service_catalog_ready(
            self.executor,
            admin_password,
            NOVA_SERVICE_NAME,
            NOVA_SERVICE_TYPE,
            NOVA_ENDPOINT_URL,
        ):
            return False
        return _cells_ready(self.executor)

    def apply(self) -> None:
        """Provision MariaDB & Keystone, install Nova packages, edit nova.conf, sync DBs &
        cells v2, and restart Nova controller services."""
        (
            admin_password,
            rabbit_password,
            db_password,
            nova_password,
            placement_password,
        ) = _load_nova_controller_secrets(self.root)
        provision_service_database(self.executor, NOVA_DB_NAMES, NOVA_DB_USER, db_password)
        ensure_service_user(self.executor, admin_password, NOVA_SERVICE_USER, nova_password)
        ensure_service_catalog(
            self.executor,
            admin_password,
            NOVA_SERVICE_NAME,
            NOVA_SERVICE_TYPE,
            NOVA_ENDPOINT_URL,
        )
        for cmd in INSTALL_NOVA_CONTROLLER_PACKAGES:
            self.executor.run(cmd.command_id, cmd.params)
        edit_nova_controller_conf(
            self.root,
            self.local_node,
            db_password,
            rabbit_password,
            nova_password,
            placement_password,
            dry_run=False,
        )
        self.executor.run(RUN_NOVA_API_DB_SYNC.command_id, RUN_NOVA_API_DB_SYNC.params)
        self.executor.run(RUN_NOVA_MAP_CELL0.command_id, RUN_NOVA_MAP_CELL0.params)
        list_cells_res = self.executor.run(
            QUERY_NOVA_LIST_CELLS.command_id, QUERY_NOVA_LIST_CELLS.params
        )
        if not cell_exists_in_list_output(list_cells_res.stdout, "cell1"):
            self.executor.run(RUN_NOVA_CREATE_CELL1.command_id, RUN_NOVA_CREATE_CELL1.params)
        self.executor.run(RUN_NOVA_DB_SYNC.command_id, RUN_NOVA_DB_SYNC.params)
        for cmd in RESTART_NOVA_CONTROLLER_UNITS:
            self.executor.run(cmd.command_id, cmd.params)

    def verify(self) -> VerifyResult:
        """Verify secrets, nova.conf, packages, units, MariaDB, Keystone, and cells v2."""
        problems: list[str] = []
        admin_password: str | None = None
        rabbit_password: str | None = None
        db_password: str | None = None
        nova_password: str | None = None
        placement_password: str | None = None
        try:
            (
                admin_password,
                rabbit_password,
                db_password,
                nova_password,
                placement_password,
            ) = _load_nova_controller_secrets(self.root)
        except PreconditionError as exc:
            problems.append(str(exc))

        conf_path, conf_display = resolve_target(self.root, NOVA_CONF_TARGET)
        try:
            conf_text = _read_existing_conf(conf_path, conf_display)
        except (UnsafePathError, FileFormatError) as exc:
            return VerifyResult(False, f"{exc}. {CONTROLLER_NOT_CHECKED}")
        if conf_text is None:
            problems.append(f"{conf_display} does not exist")
        elif (
            db_password is not None
            and rabbit_password is not None
            and nova_password is not None
            and placement_password is not None
        ):
            problems += verify_nova_controller_conf_text(
                conf_text,
                self.local_node,
                db_password,
                rabbit_password,
                nova_password,
                placement_password,
            )

        for pkg in NOVA_CONTROLLER_PACKAGES:
            if not package_installed(self.executor, pkg):
                problems.append(f"package {pkg!r} is not installed")
        for unit in NOVA_CONTROLLER_UNITS:
            problems += unit_active_problems(self.executor, unit)
        problems += service_database_problems(self.executor, NOVA_DB_NAMES, NOVA_DB_USER)
        if admin_password is not None:
            problems += service_user_problems(self.executor, admin_password, NOVA_SERVICE_USER)
            problems += service_catalog_problems(
                self.executor,
                admin_password,
                NOVA_SERVICE_NAME,
                NOVA_SERVICE_TYPE,
                NOVA_ENDPOINT_URL,
            )
        problems += _cells_problems(self.executor)

        if problems:
            return VerifyResult(False, f"{'; '.join(problems)}. {CONTROLLER_NOT_CHECKED}")
        return VerifyResult(True, CONTROLLER_NOT_CHECKED)


@dataclass(frozen=True)
class NovaComputeStep:
    """``check`` / ``apply`` / ``verify`` for the ``nova-compute`` catalog step."""

    root: Path
    config: Mapping[str, Any]
    local_node: Mapping[str, Any]
    executor: CommandExecutor
    id: str = NOVA_COMPUTE_STEP_ID
    declares_commands: bool = True

    def __post_init__(self) -> None:
        role = self.local_node.get("role")
        if role != "compute":
            raise ExecutionError(
                f"Step {self.id!r} applies only to the 'compute' role, got {role!r}"
            )
        _management_ip(self.local_node)

    def dry_run_preview(self) -> StepPreview:
        """File diffs + commands ``apply()`` would run, with all secrets masked."""
        notes: list[str] = []
        try:
            rabbit_password, nova_password, placement_password = _load_nova_compute_secrets(
                self.root
            )
        except PreconditionError as exc:
            rabbit_password = _DRY_RUN_PLACEHOLDER_SECRET
            nova_password = _DRY_RUN_PLACEHOLDER_SECRET
            placement_password = _DRY_RUN_PLACEHOLDER_SECRET
            notes.append(f"Precondition (checked by apply()): {exc}")

        virt_type = detect_node_libvirt_virt_type(self.root)
        file_changes: list[FileChange] = []

        nova_path, nova_display = resolve_target(self.root, NOVA_CONF_TARGET)
        if _read_existing_conf(nova_path, nova_display) is None:
            rendered = "\n".join(
                f"[{sec}] {k} = "
                f"{_mask_secrets(v, rabbit_password, nova_password, placement_password)}"
                for sec, k, v in nova_compute_conf_settings(
                    self.local_node, rabbit_password, nova_password, placement_password
                )
            )
            notes.append(
                f"{nova_display} does not exist yet (created when {NOVA_COMPUTE_PACKAGE} is "
                f"installed); apply() will install {NOVA_COMPUTE_PACKAGE} first and then "
                f"set:\n{rendered}"
            )
        else:
            file_changes.append(
                edit_nova_compute_conf(
                    self.root,
                    self.local_node,
                    rabbit_password,
                    nova_password,
                    placement_password,
                    dry_run=True,
                )
            )

        hypervisor_path, hypervisor_display = resolve_target(self.root, NOVA_COMPUTE_CONF_TARGET)
        if _read_existing_conf(hypervisor_path, hypervisor_display) is None:
            notes.append(
                f"{hypervisor_display} does not exist yet (created when {NOVA_COMPUTE_PACKAGE} "
                f"is installed); apply() will set [libvirt] virt_type = {virt_type}"
            )
        else:
            file_changes.append(
                edit_nova_compute_hypervisor_conf(self.root, virt_type, dry_run=True)
            )

        notes.extend(
            (
                COMPUTE_LEAST_PRIVILEGE_NOTE,
                SERVICE_USER_AUTH_URL_NOTE,
                COMPUTE_VIRT_TYPE_NOTE,
                COMPUTE_NOT_CHECKED,
            )
        )
        return StepPreview(
            file_changes=tuple(file_changes),
            commands=(INSTALL_NOVA_COMPUTE_PACKAGE, RESTART_NOVA_COMPUTE_UNIT),
            read_only_commands=(QUERY_NOVA_COMPUTE_PACKAGE, QUERY_NOVA_COMPUTE_ACTIVE),
            notes=tuple(notes),
        )

    def check(self) -> bool:
        """True if secrets, nova.conf, nova-compute.conf, package, and unit are ready."""
        try:
            rabbit_password, nova_password, placement_password = _load_nova_compute_secrets(
                self.root
            )
        except PreconditionError:
            return False
        try:
            virt_type = detect_node_libvirt_virt_type(self.root)
            if edit_nova_compute_conf(
                self.root,
                self.local_node,
                rabbit_password,
                nova_password,
                placement_password,
                dry_run=True,
            ).changed:
                return False
            if edit_nova_compute_hypervisor_conf(self.root, virt_type, dry_run=True).changed:
                return False
        except (PreconditionError, UnsafePathError, FileFormatError):
            return False
        if not package_installed(self.executor, NOVA_COMPUTE_PACKAGE):
            return False
        return not unit_active_problems(self.executor, NOVA_COMPUTE_UNIT)

    def apply(self) -> None:
        """Install nova-compute, edit nova.conf and nova-compute.conf, and restart nova-compute."""
        rabbit_password, nova_password, placement_password = _load_nova_compute_secrets(self.root)
        virt_type = detect_node_libvirt_virt_type(self.root)
        self.executor.run(
            INSTALL_NOVA_COMPUTE_PACKAGE.command_id, INSTALL_NOVA_COMPUTE_PACKAGE.params
        )
        edit_nova_compute_conf(
            self.root,
            self.local_node,
            rabbit_password,
            nova_password,
            placement_password,
            dry_run=False,
        )
        edit_nova_compute_hypervisor_conf(self.root, virt_type, dry_run=False)
        self.executor.run(RESTART_NOVA_COMPUTE_UNIT.command_id, RESTART_NOVA_COMPUTE_UNIT.params)

    def verify(self) -> VerifyResult:
        """Verify secrets, nova.conf, nova-compute.conf, package, and nova-compute unit."""
        problems: list[str] = []
        rabbit_password: str | None = None
        nova_password: str | None = None
        placement_password: str | None = None
        try:
            rabbit_password, nova_password, placement_password = _load_nova_compute_secrets(
                self.root
            )
        except PreconditionError as exc:
            problems.append(str(exc))

        try:
            virt_type = detect_node_libvirt_virt_type(self.root)
        except (UnsafePathError, FileFormatError) as exc:
            return VerifyResult(False, f"{exc}. {COMPUTE_NOT_CHECKED}")

        nova_path, nova_display = resolve_target(self.root, NOVA_CONF_TARGET)
        try:
            nova_text = _read_existing_conf(nova_path, nova_display)
        except (UnsafePathError, FileFormatError) as exc:
            return VerifyResult(False, f"{exc}. {COMPUTE_NOT_CHECKED}")
        if nova_text is None:
            problems.append(f"{nova_display} does not exist")
        elif (
            rabbit_password is not None
            and nova_password is not None
            and placement_password is not None
        ):
            problems += verify_nova_compute_conf_text(
                nova_text,
                self.local_node,
                rabbit_password,
                nova_password,
                placement_password,
            )

        hypervisor_path, hypervisor_display = resolve_target(self.root, NOVA_COMPUTE_CONF_TARGET)
        try:
            hypervisor_text = _read_existing_conf(hypervisor_path, hypervisor_display)
        except (UnsafePathError, FileFormatError) as exc:
            return VerifyResult(False, f"{exc}. {COMPUTE_NOT_CHECKED}")
        if hypervisor_text is None:
            problems.append(f"{hypervisor_display} does not exist")
        else:
            problems += verify_nova_compute_hypervisor_conf_text(hypervisor_text, virt_type)

        if not package_installed(self.executor, NOVA_COMPUTE_PACKAGE):
            problems.append(f"package {NOVA_COMPUTE_PACKAGE!r} is not installed")
        problems += unit_active_problems(self.executor, NOVA_COMPUTE_UNIT)

        if problems:
            return VerifyResult(False, f"{'; '.join(problems)}. {COMPUTE_NOT_CHECKED}")
        return VerifyResult(True, COMPUTE_NOT_CHECKED)


def make_nova_controller_step(
    root: Path,
    config: Mapping[str, Any],
    local_node: Mapping[str, Any],
    *,
    executor: CommandExecutor,
) -> NovaControllerStep:
    """Construct a :class:`NovaControllerStep` for ``openstack-forge apply``."""
    return NovaControllerStep(root=root, config=config, local_node=local_node, executor=executor)


def make_nova_compute_step(
    root: Path,
    config: Mapping[str, Any],
    local_node: Mapping[str, Any],
    *,
    executor: CommandExecutor,
) -> NovaComputeStep:
    """Construct a :class:`NovaComputeStep` for ``openstack-forge apply``."""
    return NovaComputeStep(root=root, config=config, local_node=local_node, executor=executor)
