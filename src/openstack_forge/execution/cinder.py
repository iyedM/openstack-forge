"""The ``cinder-controller`` and ``cinder-storage`` deployment steps (Block Storage service).

**Unverified and never run on a real host.** Every command goes through
:class:`CommandExecutor` and :data:`DEFAULT_REGISTRY`; the unit tests use an injectable fake
backend and a temporary root directory, so nothing runs on the development machine.

Official 2025.1 (Epoxy) Ubuntu installation guides read directly from the ``stable/2025.1``
branch of ``openstack/cinder``:

- **Controller node**:
  ``https://docs.openstack.org/cinder/2025.1/install/cinder-controller-install-ubuntu.html``
  (raw ``doc/source/install/cinder-controller-install-ubuntu.rst``)
- **Storage node (LVM + tgt)**:
  ``https://docs.openstack.org/cinder/2025.1/install/cinder-storage-install-ubuntu.html``
  (raw ``doc/source/install/cinder-storage-install-ubuntu.rst``)

Exact steps prescribed by the official pages
--------------------------------------------

### Part A — Controller node (``cinder-controller``)

1. **Prerequisites — MariaDB database**:

   - ``CREATE DATABASE cinder;``
   - ``GRANT ALL PRIVILEGES ON cinder.* TO 'cinder'@'localhost' IDENTIFIED BY 'CINDER_DBPASS';``
   - ``GRANT ALL PRIVILEGES ON cinder.* TO 'cinder'@'%' IDENTIFIED BY 'CINDER_DBPASS';``

2. **Prerequisites — Keystone service credentials and API endpoints**:

   - ``openstack user create --domain default --password-prompt cinder``
   - ``openstack role add --project service --user cinder admin``
   - ``openstack service create --name cinderv3 --description "OpenStack Block Storage" volumev3``
   - ``openstack endpoint create --region RegionOne volumev3 public http://controller:8776/v3/%(project_id)s``
   - ``openstack endpoint create --region RegionOne volumev3 internal http://controller:8776/v3/%(project_id)s``
   - ``openstack endpoint create --region RegionOne volumev3 admin http://controller:8776/v3/%(project_id)s``

3. **Install the packages**:

   - ``apt install cinder-api cinder-scheduler``

4. **Edit ``/etc/cinder/cinder.conf``**:

   - ``[database]``:
     ``connection = mysql+pymysql://cinder:CINDER_DBPASS@controller/cinder``
   - ``[DEFAULT]``:
     ``transport_url = rabbit://openstack:RABBIT_PASS@controller:5672/``,
     ``auth_strategy = keystone``, ``my_ip = <controller management IP>``
   - ``[keystone_authtoken]``:
     ``www_authenticate_uri = http://controller:5000``,
     ``auth_url = http://controller:5000``,
     ``memcached_servers = controller:11211``,
     ``auth_type = password``,
     ``project_domain_name = Default``,
     ``user_domain_name = Default``,
     ``project_name = service``,
     ``username = cinder``,
     ``password = CINDER_PASS``
   - ``[oslo_concurrency]``:
     ``lock_path = /var/lib/cinder/tmp``

5. **Populate the Block Storage database**:

   - ``su -s /bin/sh -c "cinder-manage db sync" cinder``

6. **Configure Compute to use Block Storage (``/etc/nova/nova.conf``)**:

   - ``[cinder]``:
     ``os_region_name = RegionOne``

7. **Finalize installation**:

   - ``service nova-api restart``
   - ``service cinder-scheduler restart``
   - ``service apache2 restart``

### Part B — Block storage node (``cinder-storage``)

1. **Install supporting LVM utility packages and Block Storage volume packages**:

   - ``apt install lvm2 thin-provisioning-tools``
   - ``apt install cinder-volume tgt``

2. **Prepare the LVM physical volume and ``cinder-volumes`` volume group**:

   - ``pvcreate <disk>`` (for example ``/dev/sdb``)
   - ``vgcreate cinder-volumes <disk>``

3. **Edit ``/etc/lvm/lvm.conf``**:

   - In the ``devices { ... }`` section:
     ``filter = [ "a/<rel_disk>/", "r/.*/"]`` (for example ``filter = [ "a/sdb/", "r/.*/"]``)
   - The official Cinder 2025.1 Ubuntu storage node installation guide
     (``https://docs.openstack.org/cinder/2025.1/install/cinder-storage-install-ubuntu.html``)
     explicitly warns:
     *"Warning: If your storage nodes use LVM on the operating system disk, you must also add
     the associated device to the filter. For example, if the ``/dev/sda`` device contains the
     operating system: ``filter = [ "a/sda/", "a/sdb/", "r/.*/"]``"*
   - :func:`check_cinder_lvm_root_safety` inspects ``<root>/proc/mounts`` and
     ``<root>/etc/fstab`` (plus optional ``local_node["root_device"]`` /
     ``local_node["root_on_lvm"]``) and refuses with :class:`PreconditionError` when the root
     filesystem ``/`` is backed by LVM and would be hidden by the single-device LVM filter,
     unless ``allow_lvm_root_filter_override: true`` is set.

4. **Edit ``/etc/cinder/cinder.conf``**:

   - ``[database]``:
     ``connection = mysql+pymysql://cinder:CINDER_DBPASS@controller/cinder``
   - ``[DEFAULT]``:
     ``transport_url = rabbit://openstack:RABBIT_PASS@controller:5672/``,
     ``auth_strategy = keystone``,
     ``my_ip = <block node management IP>``,
     ``enabled_backends = lvm``,
     ``glance_api_servers = http://controller:9292``
   - ``[keystone_authtoken]``:
     ``www_authenticate_uri = http://controller:5000``,
     ``auth_url = http://controller:5000``,
     ``memcached_servers = controller:11211``,
     ``auth_type = password``,
     ``project_domain_name = Default``,
     ``user_domain_name = Default``,
     ``project_name = service``,
     ``username = cinder``,
     ``password = CINDER_PASS``
   - ``[lvm]``:
     ``volume_driver = cinder.volume.drivers.lvm.LVMVolumeDriver``,
     ``volume_group = cinder-volumes``,
     ``target_protocol = iscsi``,
     ``target_helper = tgtadm``
   - ``[oslo_concurrency]``:
     ``lock_path = /var/lib/cinder/tmp``

5. **Create ``/etc/tgt/conf.d/cinder.conf``**:

   - ``include /var/lib/cinder/volumes/*``

6. **Finalize installation**:

   - ``service tgt restart``
   - ``service cinder-volume restart``

Ubuntu 24.04 + Cloud Archive (Epoxy) package and conffile facts
---------------------------------------------------------------

Inspected read-only on the Ubuntu 24.04 development machine:

- ``apt-cache policy cinder-api cinder-scheduler cinder-volume cinder-common python3-cinder``:
  all ``2:26.0.0-0ubuntu1.2~cloud1`` in ``noble-updates/epoxy/main`` (``2:24.2.0-0ubuntu2.1``
  in ``noble-updates/main``).
- ``apt-cache policy lvm2 thin-provisioning-tools tgt``:
  ``lvm2`` ``2.03.16-3ubuntu3.2`` (``noble-updates/main``), ``thin-provisioning-tools``
  ``0.9.0-2ubuntu5.1`` (``noble-updates/main``), ``tgt`` ``1:1.0.85-1.1ubuntu6``
  (``noble/universe``).
- ``cinder-api`` runs under Apache (``apache2.service``) on Ubuntu 24.04 via ``cinder-wsgi``,
  which is why the official guide restarts ``apache2`` alongside ``cinder-scheduler`` and
  ``nova-api`` on the controller.
- ``/etc/lvm/lvm.conf`` has a top-level ``devices { ... }`` section (lines 52–405 in Ubuntu
  24.04's default conffile) containing commented ``# filter = [ "a|.*|" ]`` examples.

Deviations from the guide and design decisions
----------------------------------------------

1. **``[DEFAULT] transport_url = rabbit://openstack:RABBIT_PASS@controller:5672/`` and
   ``project_domain_name = Default`` / ``user_domain_name = Default``**:
   The Cinder Ubuntu guides write ``rabbit://openstack:RABBIT_PASS@controller`` without
   ``:5672/`` and lowercase ``default`` for ``project_domain_name`` / ``user_domain_name``.
   Consistent with ``glance.py``, ``placement.py``, ``nova.py``, and ``neutron.py``, OpenStack
   Forge uses the explicit ``rabbit://openstack:RABBIT_PASS@controller:5672/`` URL and
   ``Default`` domain name on both controller and block nodes.
2. **Idempotent ``pvs`` / ``pvcreate`` and ``vgs`` / ``vgcreate`` checks**:
   Running ``pvcreate /dev/sdb`` or ``vgcreate cinder-volumes /dev/sdb`` unconditionally fails
   on re-runs once the PV or VG exists. OpenStack Forge queries ``/usr/sbin/pvs --noheadings
   -o pv_name`` and ``/usr/sbin/vgs --noheadings -o vg_name`` first and runs ``pvcreate`` /
   ``vgcreate`` only when absent.
3. **Least-privilege secrets on ``block`` nodes**:
   ``CinderStorageStep`` loads only ``RABBIT_PASS``, ``CINDER_DBPASS``, and ``CINDER_PASS``
   from :class:`SecretStore`; ``ADMIN_PASS`` is controller-only and is never required on block
   storage nodes.
4. **``cinder-manage db sync`` invoked directly as ``root`` instead of ``su``**:
   ``CommandSpec`` forbids ``su`` and ``sudo``. Editing ``/etc/cinder/cinder.conf`` before
   running ``/usr/bin/cinder-manage db sync`` ensures migrations run against MariaDB at
   ``controller``.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from ipaddress import IPv4Address
from pathlib import Path
from typing import Any

from openstack_forge.execution.commands import (
    APT_GET_INSTALL,
    CINDER_MANAGE_DB_SYNC,
    LVM_PVCREATE,
    LVM_PVS,
    LVM_VGCREATE_CINDER_VOLUMES,
    LVM_VGS,
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
from openstack_forge.execution.files import (
    DISABLED_LINE_PREFIX,
    FileChange,
    ini_set_many,
    managed_block,
)
from openstack_forge.execution.fsutil import (
    atomic_write_text,
    backup_once,
    file_mode,
    read_text_file,
    resolve_target,
    unified_diff,
)
from openstack_forge.execution.openstack_cli import (
    ADMIN_ROLE_NAME,
    DEFAULT_READINESS_INITIAL_BACKOFF,
    DEFAULT_READINESS_MAX_ATTEMPTS,
    DEFAULT_READINESS_MAX_BACKOFF,
    ENDPOINT_INTERFACES,
    SERVICE_PROJECT_NAME,
    ensure_service_catalog,
    ensure_service_user,
    service_catalog_problems,
    service_catalog_ready,
    service_user_problems,
    service_user_ready,
    wait_for_keystone_ready,
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

CINDER_CONTROLLER_STEP_ID = "cinder-controller"
CINDER_STORAGE_STEP_ID = "cinder-storage"

CINDER_CONTROLLER_PACKAGES: tuple[str, ...] = (
    "cinder-api",
    "cinder-scheduler",
)
# Official guide finalize restart order (cinder-controller-install-ubuntu.rst lines 275-282):
CINDER_CONTROLLER_UNITS: tuple[str, ...] = (
    "nova-api",
    "cinder-scheduler",
    "apache2",
)

CINDER_STORAGE_PACKAGES: tuple[str, ...] = (
    "lvm2",
    "thin-provisioning-tools",
    "cinder-volume",
    "tgt",
)
# Official guide finalize restart order (cinder-storage-install-ubuntu.rst lines 270-271):
CINDER_STORAGE_UNITS: tuple[str, ...] = (
    "tgt",
    "cinder-volume",
)

CINDER_CONF_TARGET = "/etc/cinder/cinder.conf"
NOVA_CONF_TARGET = "/etc/nova/nova.conf"
LVM_CONF_TARGET = "/etc/lvm/lvm.conf"
CINDER_TGT_CONF_TARGET = "/etc/tgt/conf.d/cinder.conf"
CINDER_TGT_BLOCK_MARKER = "cinder-tgt"
CINDER_TGT_INCLUDE_LINE = "include /var/lib/cinder/volumes/*"
PROC_MOUNTS_TARGET = "/proc/mounts"
FSTAB_TARGET = "/etc/fstab"
ALLOW_LVM_ROOT_FILTER_OVERRIDE_KEY = "allow_lvm_root_filter_override"
CINDER_STORAGE_INSTALL_DOC_URL = (
    "https://docs.openstack.org/cinder/2025.1/install/cinder-storage-install-ubuntu.html"
)
CINDER_STORAGE_LVM_FILTER_WARNING = (
    "If your storage nodes use LVM on the operating system disk, you must also add the "
    "associated device to the filter. For example, if the /dev/sda device contains the "
    'operating system: filter = [ "a/sda/", "a/sdb/", "r/.*/"]'
)

CINDER_DB_NAME = "cinder"
CINDER_DB_USER = "cinder"
CINDER_ADMIN_SECRET_NAME = "ADMIN_PASS"
CINDER_RABBIT_SECRET_NAME = "RABBIT_PASS"
CINDER_DB_SECRET_NAME = "CINDER_DBPASS"
CINDER_SERVICE_SECRET_NAME = "CINDER_PASS"

CINDER_SERVICE_USER = "cinder"
CINDER_SERVICE_NAME = "cinderv3"
CINDER_SERVICE_TYPE = "volumev3"
CINDER_ENDPOINT_URL = "http://controller:8776/v3/%(project_id)s"
CINDER_VG_NAME = "cinder-volumes"

CINDER_KEYSTONE_URI = "http://controller:5000"
CINDER_MEMCACHED_SERVERS = "controller:11211"
CINDER_GLANCE_API_SERVERS = "http://controller:9292"
CINDER_LOCK_PATH = "/var/lib/cinder/tmp"
CINDER_REGION_NAME = "RegionOne"
CINDER_VOLUME_DRIVER = "cinder.volume.drivers.lvm.LVMVolumeDriver"
CINDER_TARGET_PROTOCOL = "iscsi"
CINDER_TARGET_HELPER = "tgtadm"

_DRY_RUN_PLACEHOLDER_SECRET = "D" * DEFAULT_SECRET_LENGTH

INSTALL_CINDER_CONTROLLER_PACKAGES: tuple[PlannedCommand, ...] = tuple(
    PlannedCommand(APT_GET_INSTALL, {"package": pkg}) for pkg in CINDER_CONTROLLER_PACKAGES
)
RUN_CINDER_DB_SYNC = PlannedCommand(CINDER_MANAGE_DB_SYNC, {})
RESTART_CINDER_CONTROLLER_UNITS: tuple[PlannedCommand, ...] = tuple(
    PlannedCommand(SYSTEMCTL_RESTART, {"unit": unit}) for unit in CINDER_CONTROLLER_UNITS
)
QUERY_CINDER_CONTROLLER_PACKAGES: tuple[PlannedCommand, ...] = tuple(
    query_package(pkg) for pkg in CINDER_CONTROLLER_PACKAGES
)
QUERY_CINDER_CONTROLLER_ACTIVE: tuple[PlannedCommand, ...] = tuple(
    query_active(unit) for unit in CINDER_CONTROLLER_UNITS
)

INSTALL_CINDER_STORAGE_PACKAGES: tuple[PlannedCommand, ...] = tuple(
    PlannedCommand(APT_GET_INSTALL, {"package": pkg}) for pkg in CINDER_STORAGE_PACKAGES
)
QUERY_LVM_PVS = PlannedCommand(LVM_PVS, {})
QUERY_LVM_VGS = PlannedCommand(LVM_VGS, {})
RESTART_CINDER_STORAGE_UNITS: tuple[PlannedCommand, ...] = tuple(
    PlannedCommand(SYSTEMCTL_RESTART, {"unit": unit}) for unit in CINDER_STORAGE_UNITS
)
QUERY_CINDER_STORAGE_PACKAGES: tuple[PlannedCommand, ...] = tuple(
    query_package(pkg) for pkg in CINDER_STORAGE_PACKAGES
)
QUERY_CINDER_STORAGE_ACTIVE: tuple[PlannedCommand, ...] = tuple(
    query_active(unit) for unit in CINDER_STORAGE_UNITS
)

CONTROLLER_SQL_STDIN_NOTE = (
    "Password delivery (MariaDB): CINDER_DBPASS is sent to /usr/bin/mariadb via standard "
    "input (stdin_param='sql'), so it never appears in argv or /proc/<pid>/cmdline (ps)."
)
CONTROLLER_KEYSTONE_NOTE = (
    "Password delivery (Keystone CLI): ADMIN_PASS is passed via OS_PASSWORD in /proc/<pid>/environ "
    "(env_params). CINDER_PASS is passed via --password in argv of '/usr/bin/openstack user "
    "create' (or 'user set') because python-openstackclient's --password-prompt requires a "
    "TTY; any local user can read /proc/<pid>/cmdline (ps) while that command runs."
)
CONTROLLER_NOT_CHECKED = (
    "Not checked: no live HTTP request to http://controller:8776 or AMQP connection to "
    "controller:5672 is performed."
)
STORAGE_LEAST_PRIVILEGE_NOTE = (
    "Least-privilege secrets: cinder-storage loads only RABBIT_PASS, CINDER_DBPASS, and "
    "CINDER_PASS from /etc/openstack-forge/secrets.json; ADMIN_PASS is never read on block "
    "storage nodes."
)
STORAGE_LVM_NOTE = (
    "Idempotent LVM provisioning: queries '/usr/sbin/pvs --noheadings -o pv_name' and "
    "'/usr/sbin/vgs --noheadings -o vg_name' first and runs pvcreate / vgcreate only when "
    "the physical volume or 'cinder-volumes' volume group is not yet present."
)
STORAGE_NOT_CHECKED = (
    "Not checked: no live iSCSI target login on port 3260 or 'openstack volume service list' "
    "check is performed."
)

_INI_SECTION_RE = re.compile(r"^\s*\[(?P<name>[^\]]+)\]\s*$")
_INI_ACTIVE_KV_RE = re.compile(r"^\s*(?P<key>[^#;\[\s=][^=]*?)\s*=\s*(?P<value>.*?)\s*$")
_LVM_SECTION_OPEN_RE = re.compile(r"^\s*(?P<name>[A-Za-z_][A-Za-z0-9_]*)\s*\{\s*(?:#.*)?$")
_LVM_ACTIVE_FILTER_RE = re.compile(r"^(?P<indent>\s*)filter\s*=\s*(?P<value>.+?)\s*$")
_LVM_COMMENTED_FILTER_RE = re.compile(r"^(?P<indent>\s*)#\s*filter\s*=")


def _management_ip(local_node: Mapping[str, Any]) -> str:
    return str(IPv4Address(local_node["management_ip"]))


def resolve_block_disk(local_node: Mapping[str, Any]) -> str:
    """Resolve and validate ``local_node["disk"]`` as a ``device_path``."""
    raw = local_node.get("disk")
    if not isinstance(raw, str) or not raw.strip():
        raise PreconditionError(
            "Block storage node is missing a 'disk' device path (expected e.g. '/dev/sdb')"
        )
    candidate = raw.strip()
    try:
        return validate_parameter("device_path", "disk", candidate)
    except ParameterError as exc:
        raise PreconditionError(f"Invalid block device path {candidate!r}: {exc}") from exc


def lvm_filter_for_disk(disk: str) -> str:
    """Build the LVM ``devices/filter`` value for ``disk`` (stripping leading ``/dev/``)."""
    validated = validate_parameter("device_path", "disk", disk)
    rel_disk = validated.removeprefix("/dev/")
    return f'[ "a/{rel_disk}/", "r/.*/"]'


_NON_VG_DEV_SUBDIRS = frozenset(
    {
        "block",
        "bsg",
        "bus",
        "char",
        "cpu",
        "disk",
        "dri",
        "fd",
        "hugepages",
        "input",
        "lightnvm",
        "loop",
        "mapper",
        "md",
        "mqueue",
        "net",
        "pts",
        "raw",
        "shm",
        "snd",
        "usb",
        "vfio",
        "vhost-net",
        "vhost-vsock",
    }
)


def _allow_lvm_root_filter_override(
    local_node: Mapping[str, Any],
    config: Mapping[str, Any] | None = None,
) -> bool:
    if local_node.get(ALLOW_LVM_ROOT_FILTER_OVERRIDE_KEY) is True:
        return True
    tags = local_node.get("tags")
    if isinstance(tags, Mapping) and tags.get(ALLOW_LVM_ROOT_FILTER_OVERRIDE_KEY) is True:
        return True
    if (
        isinstance(tags, (list, tuple, set, frozenset))
        and ALLOW_LVM_ROOT_FILTER_OVERRIDE_KEY in tags
    ):
        return True
    return isinstance(config, Mapping) and config.get(ALLOW_LVM_ROOT_FILTER_OVERRIDE_KEY) is True


def _is_lvm_root_device(device: str) -> bool:
    dev = device.strip()
    if not dev:
        return False
    if dev.startswith("/dev/mapper/") and dev != "/dev/mapper/control":
        return True
    if re.match(r"^/dev/dm-[0-9]+$", dev):
        return True
    if dev.startswith("/dev/disk/by-id/dm-"):
        return True
    if dev.startswith("/dev/"):
        parts = [part for part in dev.removeprefix("/dev/").split("/") if part]
        if len(parts) == 2 and parts[0] not in _NON_VG_DEV_SUBDIRS:
            return True
    return False


def _parse_mount_table_for_root(text: str) -> list[str]:
    devices: list[str] = []
    for raw_line in text.splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line:
            continue
        fields = line.split()
        if len(fields) >= 2 and fields[1] == "/":
            devices.append(fields[0])
    return devices


def _detect_lvm_root_sources(root: Path, local_node: Mapping[str, Any]) -> list[str]:
    found: list[str] = []
    if local_node.get("root_on_lvm") is True:
        raw_root_dev = local_node.get("root_device")
        label = (
            raw_root_dev.strip()
            if isinstance(raw_root_dev, str) and raw_root_dev.strip()
            else "/dev/mapper/root"
        )
        found.append(f"{label} (local_node.root_on_lvm=true)")

    raw_root_dev = local_node.get("root_device")
    if isinstance(raw_root_dev, str) and _is_lvm_root_device(raw_root_dev):
        entry = f"{raw_root_dev.strip()} (local_node.root_device)"
        if entry not in found:
            found.append(entry)

    for target in (PROC_MOUNTS_TARGET, FSTAB_TARGET):
        try:
            path, display = resolve_target(root, target)
            text = _read_existing_conf(path, display)
        except (UnsafePathError, FileFormatError):
            continue
        if text is None:
            continue
        for dev in _parse_mount_table_for_root(text):
            if _is_lvm_root_device(dev):
                entry = f"{dev} ({target})"
                if entry not in found:
                    found.append(entry)
    return found


def check_cinder_lvm_root_safety(
    root: Path,
    local_node: Mapping[str, Any],
    config: Mapping[str, Any] | None = None,
    *,
    disk: str | None = None,
) -> None:
    """Refuse when the root filesystem ``/`` is on LVM and would be hidden by ``lvm.conf``.

    The official Cinder 2025.1 Ubuntu storage node installation guide
    (:data:`CINDER_STORAGE_INSTALL_DOC_URL`) instructs operators to set
    ``filter = [ "a/sdb/", "r/.*/"]`` in ``/etc/lvm/lvm.conf`` and warns:

        *"Warning: If your storage nodes use LVM on the operating system disk, you must also
        add the associated device to the filter. For example, if the /dev/sda device contains
        the operating system: filter = [ \\"a/sda/\\", \\"a/sdb/\\", \\"r/.*/\\"]"*

    Because ``cinder-storage`` writes a single-device filter for ``local_node["disk"]``,
    applying that filter on a host whose root filesystem ``/`` resides on an LVM logical
    volume on another physical volume would hide the root volume group on reboot. Operators
    who manage ``/etc/lvm/lvm.conf`` externally or intentionally override this check may set
    ``allow_lvm_root_filter_override: true`` on the node (or in ``node.tags`` / top-level
    config).
    """
    resolved_disk = disk if disk is not None else resolve_block_disk(local_node)
    if _allow_lvm_root_filter_override(local_node, config):
        return
    lvm_root_sources = _detect_lvm_root_sources(root, local_node)
    if not lvm_root_sources:
        return
    proposed_filter = lvm_filter_for_disk(resolved_disk)
    joined_sources = ", ".join(lvm_root_sources)
    raise PreconditionError(
        f"Refusing to write single-disk LVM filter {proposed_filter} to {LVM_CONF_TARGET} "
        f"for Cinder disk {resolved_disk!r}: root filesystem '/' is backed by LVM "
        f"({joined_sources}) and would be hidden by 'r/.*/' on reboot. "
        f"Per {CINDER_STORAGE_INSTALL_DOC_URL}: "
        f"{CINDER_STORAGE_LVM_FILTER_WARNING!r}. "
        f"Use a block node whose root filesystem is not on LVM, or set "
        f"{ALLOW_LVM_ROOT_FILTER_OVERRIDE_KEY}: true on the node to override this safety check."
    )


def parse_lvm_names(output: str) -> frozenset[str]:
    """Parse ``pvs --noheadings -o pv_name`` or ``vgs --noheadings -o vg_name`` output."""
    return frozenset(line.strip() for line in output.splitlines() if line.strip())


def lvm_pv_and_vg_ready(executor: CommandExecutor, disk: str) -> bool:
    """Return ``True`` if ``disk`` is an LVM PV and ``cinder-volumes`` VG exists."""
    return not lvm_pv_and_vg_problems(executor, disk)


def lvm_pv_and_vg_problems(executor: CommandExecutor, disk: str) -> list[str]:
    """Return human-readable problems if ``disk`` PV or ``cinder-volumes`` VG is missing."""
    problems: list[str] = []
    try:
        pvs_res = executor.run(LVM_PVS, {})
        pvs = parse_lvm_names(pvs_res.stdout)
        if disk not in pvs:
            problems.append(f"LVM physical volume {disk!r} does not exist")
    except UnexpectedExitError as exc:
        problems.append(f"pvs failed: {exc}")

    try:
        vgs_res = executor.run(LVM_VGS, {})
        vgs = parse_lvm_names(vgs_res.stdout)
        if CINDER_VG_NAME not in vgs:
            problems.append(f"LVM volume group {CINDER_VG_NAME!r} does not exist")
    except UnexpectedExitError as exc:
        problems.append(f"vgs failed: {exc}")
    return problems


def ensure_lvm_pv_and_vg(executor: CommandExecutor, disk: str) -> None:
    """Idempotently create the LVM PV ``disk`` and VG ``cinder-volumes`` when absent."""
    pvs_res = executor.run(LVM_PVS, {})
    if disk not in parse_lvm_names(pvs_res.stdout):
        executor.run(LVM_PVCREATE, {"device": disk})
    vgs_res = executor.run(LVM_VGS, {})
    if CINDER_VG_NAME not in parse_lvm_names(vgs_res.stdout):
        executor.run(LVM_VGCREATE_CINDER_VOLUMES, {"device": disk})


def cinder_db_connection(db_password: str) -> str:
    """Build the ``[database] connection`` URL for ``/etc/cinder/cinder.conf``."""
    return f"mysql+pymysql://{CINDER_DB_USER}:{db_password}@controller/{CINDER_DB_NAME}"


def cinder_transport_url(rabbit_password: str) -> str:
    """Build the ``[DEFAULT] transport_url`` URL for ``/etc/cinder/cinder.conf``."""
    return f"rabbit://openstack:{rabbit_password}@controller:5672/"


def _cinder_keystone_authtoken_settings(
    cinder_password: str,
) -> tuple[tuple[str, str, str], ...]:
    return (
        ("keystone_authtoken", "www_authenticate_uri", CINDER_KEYSTONE_URI),
        ("keystone_authtoken", "auth_url", CINDER_KEYSTONE_URI),
        ("keystone_authtoken", "memcached_servers", CINDER_MEMCACHED_SERVERS),
        ("keystone_authtoken", "auth_type", "password"),
        ("keystone_authtoken", "project_domain_name", "Default"),
        ("keystone_authtoken", "user_domain_name", "Default"),
        ("keystone_authtoken", "project_name", SERVICE_PROJECT_NAME),
        ("keystone_authtoken", "username", CINDER_SERVICE_USER),
        ("keystone_authtoken", "password", cinder_password),
    )


def cinder_controller_conf_settings(
    local_node: Mapping[str, Any],
    db_password: str,
    rabbit_password: str,
    cinder_password: str,
) -> tuple[tuple[str, str, str], ...]:
    """Return the 14 ``(section, key, value)`` settings for controller ``cinder.conf``."""
    mgmt_ip = _management_ip(local_node)
    return (
        ("database", "connection", cinder_db_connection(db_password)),
        ("DEFAULT", "transport_url", cinder_transport_url(rabbit_password)),
        ("DEFAULT", "auth_strategy", "keystone"),
        ("DEFAULT", "my_ip", mgmt_ip),
        *_cinder_keystone_authtoken_settings(cinder_password),
        ("oslo_concurrency", "lock_path", CINDER_LOCK_PATH),
    )


def nova_controller_cinder_conf_settings() -> tuple[tuple[str, str, str], ...]:
    """Return the ``[cinder]`` setting for controller ``/etc/nova/nova.conf``."""
    return (("cinder", "os_region_name", CINDER_REGION_NAME),)


def cinder_storage_conf_settings(
    local_node: Mapping[str, Any],
    db_password: str,
    rabbit_password: str,
    cinder_password: str,
) -> tuple[tuple[str, str, str], ...]:
    """Return the 20 ``(section, key, value)`` settings for block storage ``cinder.conf``."""
    mgmt_ip = _management_ip(local_node)
    return (
        ("database", "connection", cinder_db_connection(db_password)),
        ("DEFAULT", "transport_url", cinder_transport_url(rabbit_password)),
        ("DEFAULT", "auth_strategy", "keystone"),
        ("DEFAULT", "my_ip", mgmt_ip),
        ("DEFAULT", "enabled_backends", "lvm"),
        ("DEFAULT", "glance_api_servers", CINDER_GLANCE_API_SERVERS),
        *_cinder_keystone_authtoken_settings(cinder_password),
        ("lvm", "volume_driver", CINDER_VOLUME_DRIVER),
        ("lvm", "volume_group", CINDER_VG_NAME),
        ("lvm", "target_protocol", CINDER_TARGET_PROTOCOL),
        ("lvm", "target_helper", CINDER_TARGET_HELPER),
        ("oslo_concurrency", "lock_path", CINDER_LOCK_PATH),
    )


def _read_existing_conf(path: Path, display: str) -> str | None:
    if not path.is_symlink() and not path.exists():
        return None
    return read_text_file(path, display)


def _require_conf_exists(root: Path, target: str, package_hint: str) -> None:
    path, display = resolve_target(root, target)
    if _read_existing_conf(path, display) is None:
        raise PreconditionError(
            f"{display} does not exist (expected from package {package_hint}; "
            "OpenStack Forge will not create a stub configuration file)"
        )


def _mask_secrets(text: str, *secrets: str) -> str:
    masked = redact_text(text)
    for secret in secrets:
        if secret:
            masked = masked.replace(secret, MASK)
    return masked


def _parse_ini_values(text: str) -> dict[tuple[str, str], list[str]]:
    values: dict[tuple[str, str], list[str]] = {}
    current_section: str | None = None
    for raw_line in text.split("\n"):
        sec_match = _INI_SECTION_RE.match(raw_line)
        if sec_match is not None:
            current_section = sec_match["name"].strip()
            continue
        if current_section is None:
            continue
        kv_match = _INI_ACTIVE_KV_RE.match(raw_line)
        if kv_match is not None:
            key = kv_match["key"].strip()
            val = kv_match["value"].strip()
            values.setdefault((current_section, key), []).append(val)
    return values


def _verify_ini_settings(
    text: str,
    target_display: str,
    expected_settings: tuple[tuple[str, str, str], ...],
    *secrets: str,
) -> list[str]:
    if "\r" in text:
        return [f"{target_display} contains carriage returns (CRLF)"]
    found = _parse_ini_values(text)
    problems: list[str] = []
    for section, key, expected in expected_settings:
        entries = found.get((section, key), [])
        shown_expected = _mask_secrets(expected, *secrets)
        if not entries:
            problems.append(
                f"{target_display}: {key} is missing in [{section}] (expected {shown_expected!r})"
            )
        elif len(entries) > 1 and len(set(entries)) > 1:
            shown_entries = [_mask_secrets(v, *secrets) for v in entries]
            problems.append(
                f"{target_display}: duplicate {key} in [{section}] with conflicting values "
                f"{shown_entries!r}"
            )
        elif entries[-1] != expected:
            shown_actual = _mask_secrets(entries[-1], *secrets)
            problems.append(
                f"{target_display}: [{section}] {key} is {shown_actual!r}, "
                f"expected {shown_expected!r}"
            )
    return problems


def edit_cinder_controller_conf(
    root: Path,
    local_node: Mapping[str, Any],
    db_password: str,
    rabbit_password: str,
    cinder_password: str,
    *,
    dry_run: bool,
) -> FileChange:
    """Apply the 2025.1 Ubuntu controller settings to ``/etc/cinder/cinder.conf``."""
    _require_conf_exists(root, CINDER_CONF_TARGET, CINDER_CONTROLLER_PACKAGES[0])
    return ini_set_many(
        root,
        CINDER_CONF_TARGET,
        cinder_controller_conf_settings(local_node, db_password, rabbit_password, cinder_password),
        dry_run=dry_run,
    )


def verify_cinder_controller_conf_text(
    text: str,
    local_node: Mapping[str, Any],
    db_password: str,
    rabbit_password: str,
    cinder_password: str,
) -> list[str]:
    """Pure check: all 14 controller ``/etc/cinder/cinder.conf`` settings match."""
    return _verify_ini_settings(
        text,
        CINDER_CONF_TARGET,
        cinder_controller_conf_settings(local_node, db_password, rabbit_password, cinder_password),
        db_password,
        rabbit_password,
        cinder_password,
    )


def edit_nova_controller_cinder_conf(
    root: Path,
    *,
    dry_run: bool,
) -> FileChange:
    """Apply ``[cinder] os_region_name = RegionOne`` to controller ``/etc/nova/nova.conf``."""
    _require_conf_exists(root, NOVA_CONF_TARGET, "nova-api (apply 'nova-controller' first)")
    return ini_set_many(
        root,
        NOVA_CONF_TARGET,
        nova_controller_cinder_conf_settings(),
        dry_run=dry_run,
    )


def verify_nova_controller_cinder_conf_text(text: str) -> list[str]:
    """Pure check: ``[cinder] os_region_name = RegionOne`` in ``/etc/nova/nova.conf``."""
    return _verify_ini_settings(
        text,
        NOVA_CONF_TARGET,
        nova_controller_cinder_conf_settings(),
    )


def edit_cinder_storage_conf(
    root: Path,
    local_node: Mapping[str, Any],
    db_password: str,
    rabbit_password: str,
    cinder_password: str,
    *,
    dry_run: bool,
) -> FileChange:
    """Apply the 2025.1 Ubuntu block storage settings to ``/etc/cinder/cinder.conf``."""
    _require_conf_exists(root, CINDER_CONF_TARGET, "cinder-volume")
    return ini_set_many(
        root,
        CINDER_CONF_TARGET,
        cinder_storage_conf_settings(local_node, db_password, rabbit_password, cinder_password),
        dry_run=dry_run,
    )


def verify_cinder_storage_conf_text(
    text: str,
    local_node: Mapping[str, Any],
    db_password: str,
    rabbit_password: str,
    cinder_password: str,
) -> list[str]:
    """Pure check: all 20 block storage ``/etc/cinder/cinder.conf`` settings match."""
    return _verify_ini_settings(
        text,
        CINDER_CONF_TARGET,
        cinder_storage_conf_settings(local_node, db_password, rabbit_password, cinder_password),
        db_password,
        rabbit_password,
        cinder_password,
    )


def _find_lvm_devices_span(lines: list[str], display: str) -> tuple[int, int]:
    """Return ``(open_line_idx, close_line_idx)`` for ``devices { ... }`` in ``lvm.conf``."""
    open_idx: int | None = None
    depth = 0
    for idx, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        code = line.split("#", 1)[0]
        if open_idx is None:
            m = _LVM_SECTION_OPEN_RE.match(line)
            if m is not None and m["name"] == "devices":
                open_idx = idx
                depth = 1
        else:
            depth += code.count("{") - code.count("}")
            if depth == 0:
                return open_idx, idx
    raise FileFormatError(f"{display} is missing a balanced 'devices {{ ... }}' section")


def _edit_lvm_conf_text(text: str, disk: str, display: str) -> str:
    """Pure text transformer setting ``filter = [ "a/<rel_disk>/", "r/.*/"]`` in ``devices``."""
    if "\r" in text:
        raise FileFormatError(f"{display} contains carriage returns (CRLF)")
    expected_filter = lvm_filter_for_disk(disk)
    trailing_newline = text.endswith("\n")
    lines = text.split("\n")
    if trailing_newline:
        lines.pop()

    open_idx, close_idx = _find_lvm_devices_span(lines, display)

    active_matches: list[tuple[int, str, str]] = []
    last_filter_anchor: int | None = None
    anchor_indent = "\t"
    for idx in range(open_idx + 1, close_idx):
        line = lines[idx]
        m_act = _LVM_ACTIVE_FILTER_RE.match(line)
        if m_act is not None:
            active_matches.append((idx, m_act["indent"], m_act["value"].strip()))
            last_filter_anchor = idx
            anchor_indent = m_act["indent"] or "\t"
            continue
        m_com = _LVM_COMMENTED_FILTER_RE.match(line)
        if m_com is not None or line.startswith(DISABLED_LINE_PREFIX):
            if "filter" in line:
                last_filter_anchor = idx
                if m_com is not None and m_com["indent"]:
                    anchor_indent = m_com["indent"]

    # Already converged: exactly one active filter = expected_filter
    if len(active_matches) == 1 and active_matches[0][2] == expected_filter:
        return text

    new_lines = list(lines)
    kept_expected = False
    for idx, _indent, val in active_matches:
        if val == expected_filter and not kept_expected:
            kept_expected = True
        else:
            new_lines[idx] = f"{DISABLED_LINE_PREFIX}{new_lines[idx]}"

    if not kept_expected:
        target_line = f"{anchor_indent}filter = {expected_filter}"
        insert_at = (last_filter_anchor + 1) if last_filter_anchor is not None else close_idx
        new_lines.insert(insert_at, target_line)

    return "\n".join(new_lines) + ("\n" if trailing_newline or not text else "")


def edit_lvm_conf(
    root: Path,
    disk: str,
    *,
    dry_run: bool,
) -> FileChange:
    """Idempotently set ``devices/filter`` for ``disk`` in ``/etc/lvm/lvm.conf``."""
    _require_conf_exists(root, LVM_CONF_TARGET, "lvm2")
    path, display = resolve_target(root, LVM_CONF_TARGET)
    before = read_text_file(path, display)
    after = _edit_lvm_conf_text(before, disk, display)
    if after == before:
        return FileChange(path=display, changed=False, diff="", backup=None, created=False)
    diff = unified_diff(before, after, display)
    backup: str | None = None
    if not dry_run:
        mode = file_mode(path, 0o644)
        if backup_once(path) is not None:
            backup = f"{display}.orig"
        atomic_write_text(path, after, mode=mode)
    return FileChange(path=display, changed=True, diff=diff, backup=backup, created=False)


def verify_lvm_conf_text(text: str, disk: str) -> list[str]:
    """Pure check: ``/etc/lvm/lvm.conf`` has ``filter = [ "a/<rel_disk>/", "r/.*/"]`` in
    ``devices { ... }``."""
    if "\r" in text:
        return [f"{LVM_CONF_TARGET} contains carriage returns (CRLF)"]
    expected_filter = lvm_filter_for_disk(disk)
    lines = text.split("\n")
    try:
        open_idx, close_idx = _find_lvm_devices_span(lines, LVM_CONF_TARGET)
    except FileFormatError as exc:
        return [str(exc)]
    active_values = [
        m["value"].strip()
        for idx in range(open_idx + 1, close_idx)
        if (m := _LVM_ACTIVE_FILTER_RE.match(lines[idx])) is not None
    ]
    if not active_values:
        return [
            f"{LVM_CONF_TARGET}: filter is missing in devices section "
            f"(expected {expected_filter!r})"
        ]
    if len(active_values) > 1:
        return [
            f"{LVM_CONF_TARGET}: multiple active filter lines in devices section: {active_values!r}"
        ]
    if active_values[0] != expected_filter:
        return [
            f"{LVM_CONF_TARGET}: devices/filter is {active_values[0]!r}, "
            f"expected {expected_filter!r}"
        ]
    return []


def edit_cinder_tgt_conf(
    root: Path,
    *,
    dry_run: bool,
) -> FileChange:
    """Create or update ``/etc/tgt/conf.d/cinder.conf`` with
    ``include /var/lib/cinder/volumes/*``."""
    path, display = resolve_target(root, CINDER_TGT_CONF_TARGET)
    if not path.parent.exists():
        raise PreconditionError(
            f"Parent directory of {display} does not exist (expected from package 'tgt')"
        )
    if path.exists() and not path.is_symlink():
        text = read_text_file(path, display)
        begin_marker = f"# BEGIN {CINDER_TGT_BLOCK_MARKER}"
        if begin_marker not in text and not verify_cinder_tgt_conf_text(text):
            return FileChange(path=display, changed=False, diff="", backup=None, created=False)
    return managed_block(
        root,
        CINDER_TGT_CONF_TARGET,
        CINDER_TGT_BLOCK_MARKER,
        f"{CINDER_TGT_INCLUDE_LINE}\n",
        dry_run=dry_run,
    )


def verify_cinder_tgt_conf_text(text: str) -> list[str]:
    """Pure check: ``/etc/tgt/conf.d/cinder.conf`` includes ``/var/lib/cinder/volumes/*``."""
    if "\r" in text:
        return [f"{CINDER_TGT_CONF_TARGET} contains carriage returns (CRLF)"]
    for raw_line in text.splitlines():
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped == CINDER_TGT_INCLUDE_LINE:
            return []
    return [f"{CINDER_TGT_CONF_TARGET}: missing active {CINDER_TGT_INCLUDE_LINE!r} line"]


def _load_cinder_controller_secrets(root: Path) -> tuple[str, str, str, str]:
    """Load ``(ADMIN_PASS, RABBIT_PASS, CINDER_DBPASS, CINDER_PASS)`` from :class:`SecretStore`."""
    store = SecretStore(root)
    try:
        admin_password = store.get(CINDER_ADMIN_SECRET_NAME)
        rabbit_password = store.get(CINDER_RABBIT_SECRET_NAME)
        db_password = store.get(CINDER_DB_SECRET_NAME)
        cinder_password = store.get(CINDER_SERVICE_SECRET_NAME)
    except (SecretError, UnsafePathError, KeyError) as exc:
        raise PreconditionError(
            f"{CINDER_ADMIN_SECRET_NAME}, {CINDER_RABBIT_SECRET_NAME}, "
            f"{CINDER_DB_SECRET_NAME}, and {CINDER_SERVICE_SECRET_NAME} must be initialized "
            f"in {DEFAULT_SECRETS_TARGET} before applying the {CINDER_CONTROLLER_STEP_ID!r} "
            f"step: {exc} (run 'openstack-forge secrets init' first)"
        ) from exc
    return admin_password, rabbit_password, db_password, cinder_password


def _load_cinder_storage_secrets(root: Path) -> tuple[str, str, str]:
    """Load only ``(RABBIT_PASS, CINDER_DBPASS, CINDER_PASS)`` on block storage nodes."""
    store = SecretStore(root)
    try:
        rabbit_password = store.get(CINDER_RABBIT_SECRET_NAME)
        db_password = store.get(CINDER_DB_SECRET_NAME)
        cinder_password = store.get(CINDER_SERVICE_SECRET_NAME)
    except (SecretError, UnsafePathError, KeyError) as exc:
        raise PreconditionError(
            f"{CINDER_RABBIT_SECRET_NAME}, {CINDER_DB_SECRET_NAME}, and "
            f"{CINDER_SERVICE_SECRET_NAME} must be initialized in {DEFAULT_SECRETS_TARGET} "
            f"before applying the {CINDER_STORAGE_STEP_ID!r} step: {exc} "
            "(run 'openstack-forge secrets init' first)"
        ) from exc
    return rabbit_password, db_password, cinder_password


def _preview_or_note_ini_conf(
    root: Path,
    target: str,
    package_hint: str,
    settings: tuple[tuple[str, str, str], ...],
    edit_fn: Callable[[], FileChange],
    secrets_to_mask: tuple[str, ...],
    file_changes: list[FileChange],
    notes: list[str],
) -> None:
    path, display = resolve_target(root, target)
    if _read_existing_conf(path, display) is None:
        rendered = "\n".join(
            f"[{sec}] {k} = {_mask_secrets(v, *secrets_to_mask)}" for sec, k, v in settings
        )
        notes.append(
            f"{display} does not exist yet (created when {package_hint} is installed); "
            f"apply() will set:\n{rendered}"
        )
    else:
        file_changes.append(edit_fn())


@dataclass(frozen=True)
class CinderControllerStep:
    """``check`` / ``apply`` / ``verify`` for the ``cinder-controller`` catalog step."""

    root: Path
    config: Mapping[str, Any]
    local_node: Mapping[str, Any]
    executor: CommandExecutor
    id: str = CINDER_CONTROLLER_STEP_ID
    sleep: Callable[[float], None] = time.sleep
    readiness_max_attempts: int = DEFAULT_READINESS_MAX_ATTEMPTS
    readiness_initial_backoff: float = DEFAULT_READINESS_INITIAL_BACKOFF
    readiness_max_backoff: float = DEFAULT_READINESS_MAX_BACKOFF
    declares_commands: bool = True

    def __post_init__(self) -> None:
        role = self.local_node.get("role")
        if role != "controller":
            raise ExecutionError(
                f"Step {self.id!r} applies only to the 'controller' role, got {role!r}"
            )
        _management_ip(self.local_node)

    def dry_run_preview(self) -> StepPreview:
        """File diffs + commands ``apply()`` would run, with all secrets masked."""
        notes: list[str] = []
        try:
            (
                admin_password,
                rabbit_password,
                db_password,
                cinder_password,
            ) = _load_cinder_controller_secrets(self.root)
        except PreconditionError as exc:
            admin_password = _DRY_RUN_PLACEHOLDER_SECRET
            rabbit_password = _DRY_RUN_PLACEHOLDER_SECRET
            db_password = _DRY_RUN_PLACEHOLDER_SECRET
            cinder_password = _DRY_RUN_PLACEHOLDER_SECRET
            notes.append(f"Precondition (checked by apply()): {exc}")

        secrets_to_mask = (admin_password, rabbit_password, db_password, cinder_password)
        file_changes: list[FileChange] = []

        _preview_or_note_ini_conf(
            self.root,
            CINDER_CONF_TARGET,
            CINDER_CONTROLLER_PACKAGES[0],
            cinder_controller_conf_settings(
                self.local_node, db_password, rabbit_password, cinder_password
            ),
            lambda: edit_cinder_controller_conf(
                self.root,
                self.local_node,
                db_password,
                rabbit_password,
                cinder_password,
                dry_run=True,
            ),
            secrets_to_mask,
            file_changes,
            notes,
        )
        _preview_or_note_ini_conf(
            self.root,
            NOVA_CONF_TARGET,
            "nova-api (apply 'nova-controller' first)",
            nova_controller_cinder_conf_settings(),
            lambda: edit_nova_controller_cinder_conf(self.root, dry_run=True),
            secrets_to_mask,
            file_changes,
            notes,
        )

        notes.extend((CONTROLLER_SQL_STDIN_NOTE, CONTROLLER_KEYSTONE_NOTE, CONTROLLER_NOT_CHECKED))
        commands = (
            plan_service_database_sql((CINDER_DB_NAME,), CINDER_DB_USER, db_password),
            PlannedCommand(
                OPENSTACK_USER_CREATE,
                {
                    "admin_password": admin_password,
                    "user": CINDER_SERVICE_USER,
                    "user_password": cinder_password,
                },
            ),
            PlannedCommand(
                OPENSTACK_ROLE_ADD_USER_PROJECT,
                {
                    "admin_password": admin_password,
                    "project": SERVICE_PROJECT_NAME,
                    "user": CINDER_SERVICE_USER,
                    "role": ADMIN_ROLE_NAME,
                },
            ),
            PlannedCommand(
                OPENSTACK_SERVICE_CREATE,
                {
                    "admin_password": admin_password,
                    "service_name": CINDER_SERVICE_NAME,
                    "service_type": CINDER_SERVICE_TYPE,
                },
            ),
            *(
                PlannedCommand(
                    OPENSTACK_ENDPOINT_CREATE,
                    {
                        "admin_password": admin_password,
                        "service_name": CINDER_SERVICE_NAME,
                        "interface": iface,
                        "url": CINDER_ENDPOINT_URL,
                    },
                )
                for iface in ENDPOINT_INTERFACES
            ),
            *INSTALL_CINDER_CONTROLLER_PACKAGES,
            RUN_CINDER_DB_SYNC,
            *RESTART_CINDER_CONTROLLER_UNITS,
        )
        read_only_commands = (
            *QUERY_CINDER_CONTROLLER_PACKAGES,
            *QUERY_CINDER_CONTROLLER_ACTIVE,
            QUERY_MARIADB_STATE,
            PlannedCommand(OPENSTACK_USER_LIST, {"admin_password": admin_password}),
            PlannedCommand(OPENSTACK_ROLE_ASSIGNMENT_LIST, {"admin_password": admin_password}),
            PlannedCommand(OPENSTACK_SERVICE_LIST, {"admin_password": admin_password}),
            PlannedCommand(OPENSTACK_ENDPOINT_LIST, {"admin_password": admin_password}),
        )
        return StepPreview(
            file_changes=tuple(file_changes),
            commands=commands,
            read_only_commands=read_only_commands,
            notes=tuple(notes),
        )

    def check(self) -> bool:
        """True if secrets, configs, packages, units, MariaDB, and Keystone are ready."""
        try:
            (
                admin_password,
                rabbit_password,
                db_password,
                cinder_password,
            ) = _load_cinder_controller_secrets(self.root)
        except PreconditionError:
            return False
        try:
            if edit_cinder_controller_conf(
                self.root,
                self.local_node,
                db_password,
                rabbit_password,
                cinder_password,
                dry_run=True,
            ).changed:
                return False
            if edit_nova_controller_cinder_conf(self.root, dry_run=True).changed:
                return False
        except (PreconditionError, UnsafePathError, FileFormatError):
            return False
        for pkg in CINDER_CONTROLLER_PACKAGES:
            if not package_installed(self.executor, pkg):
                return False
        for unit in CINDER_CONTROLLER_UNITS:
            if unit_active_problems(self.executor, unit):
                return False
        if not service_database_ready(self.executor, (CINDER_DB_NAME,), CINDER_DB_USER):
            return False
        if not service_user_ready(self.executor, admin_password, CINDER_SERVICE_USER):
            return False
        return service_catalog_ready(
            self.executor,
            admin_password,
            CINDER_SERVICE_NAME,
            CINDER_SERVICE_TYPE,
            CINDER_ENDPOINT_URL,
        )

    def apply(self) -> None:
        """Provision MariaDB & Keystone, install ``cinder-api`` and ``cinder-scheduler``,
        edit ``cinder.conf`` and ``nova.conf``, run ``cinder-manage db sync``, and restart units."""
        (
            admin_password,
            rabbit_password,
            db_password,
            cinder_password,
        ) = _load_cinder_controller_secrets(self.root)
        _require_conf_exists(
            self.root, NOVA_CONF_TARGET, "nova-api (apply 'nova-controller' first)"
        )

        provision_service_database(self.executor, (CINDER_DB_NAME,), CINDER_DB_USER, db_password)
        wait_for_keystone_ready(
            self.executor,
            admin_password,
            max_attempts=self.readiness_max_attempts,
            initial_backoff=self.readiness_initial_backoff,
            max_backoff=self.readiness_max_backoff,
            sleep=self.sleep,
        )
        ensure_service_user(self.executor, admin_password, CINDER_SERVICE_USER, cinder_password)
        ensure_service_catalog(
            self.executor,
            admin_password,
            CINDER_SERVICE_NAME,
            CINDER_SERVICE_TYPE,
            CINDER_ENDPOINT_URL,
        )
        for cmd in INSTALL_CINDER_CONTROLLER_PACKAGES:
            self.executor.run(cmd.command_id, cmd.params)
        edit_cinder_controller_conf(
            self.root,
            self.local_node,
            db_password,
            rabbit_password,
            cinder_password,
            dry_run=False,
        )
        self.executor.run(RUN_CINDER_DB_SYNC.command_id, RUN_CINDER_DB_SYNC.params)
        edit_nova_controller_cinder_conf(self.root, dry_run=False)
        for cmd in RESTART_CINDER_CONTROLLER_UNITS:
            self.executor.run(cmd.command_id, cmd.params)
        wait_for_keystone_ready(
            self.executor,
            admin_password,
            max_attempts=self.readiness_max_attempts,
            initial_backoff=self.readiness_initial_backoff,
            max_backoff=self.readiness_max_backoff,
            sleep=self.sleep,
        )

    def verify(self) -> VerifyResult:
        """Verify secrets, ``cinder.conf``, ``nova.conf``, packages, units, MariaDB, & Keystone."""
        problems: list[str] = []
        admin_password: str | None = None
        rabbit_password: str | None = None
        db_password: str | None = None
        cinder_password: str | None = None
        try:
            (
                admin_password,
                rabbit_password,
                db_password,
                cinder_password,
            ) = _load_cinder_controller_secrets(self.root)
        except PreconditionError as exc:
            problems.append(str(exc))

        c_path, c_display = resolve_target(self.root, CINDER_CONF_TARGET)
        try:
            c_text = _read_existing_conf(c_path, c_display)
        except (UnsafePathError, FileFormatError) as exc:
            return VerifyResult(False, f"{exc}. {CONTROLLER_NOT_CHECKED}")
        if c_text is None:
            problems.append(f"{c_display} does not exist")
        elif (
            db_password is not None and rabbit_password is not None and cinder_password is not None
        ):
            problems += verify_cinder_controller_conf_text(
                c_text, self.local_node, db_password, rabbit_password, cinder_password
            )

        n_path, n_display = resolve_target(self.root, NOVA_CONF_TARGET)
        try:
            n_text = _read_existing_conf(n_path, n_display)
        except (UnsafePathError, FileFormatError) as exc:
            return VerifyResult(False, f"{exc}. {CONTROLLER_NOT_CHECKED}")
        if n_text is None:
            problems.append(f"{n_display} does not exist")
        else:
            problems += verify_nova_controller_cinder_conf_text(n_text)

        for pkg in CINDER_CONTROLLER_PACKAGES:
            if not package_installed(self.executor, pkg):
                problems.append(f"package {pkg!r} is not installed")
        for unit in CINDER_CONTROLLER_UNITS:
            problems += unit_active_problems(self.executor, unit)
        problems += service_database_problems(self.executor, (CINDER_DB_NAME,), CINDER_DB_USER)
        if admin_password is not None:
            problems += service_user_problems(self.executor, admin_password, CINDER_SERVICE_USER)
            problems += service_catalog_problems(
                self.executor,
                admin_password,
                CINDER_SERVICE_NAME,
                CINDER_SERVICE_TYPE,
                CINDER_ENDPOINT_URL,
            )

        if problems:
            return VerifyResult(False, f"{'; '.join(problems)}. {CONTROLLER_NOT_CHECKED}")
        return VerifyResult(
            True,
            f"{CINDER_CONF_TARGET} and {NOVA_CONF_TARGET} match, "
            f"{', '.join(CINDER_CONTROLLER_PACKAGES)} are installed, "
            f"{', '.join(CINDER_CONTROLLER_UNITS)} are active, MariaDB database "
            f"{CINDER_DB_NAME!r} and Keystone service {CINDER_SERVICE_NAME!r} "
            f"({CINDER_SERVICE_TYPE!r}) are provisioned. {CONTROLLER_NOT_CHECKED}",
        )


@dataclass(frozen=True)
class CinderStorageStep:
    """``check`` / ``apply`` / ``verify`` for the ``cinder-storage`` catalog step."""

    root: Path
    config: Mapping[str, Any]
    local_node: Mapping[str, Any]
    executor: CommandExecutor
    id: str = CINDER_STORAGE_STEP_ID
    declares_commands: bool = True

    def __post_init__(self) -> None:
        role = self.local_node.get("role")
        if role != "block":
            raise ExecutionError(f"Step {self.id!r} applies only to the 'block' role, got {role!r}")
        _management_ip(self.local_node)
        resolve_block_disk(self.local_node)

    @property
    def disk(self) -> str:
        return resolve_block_disk(self.local_node)

    def dry_run_preview(self) -> StepPreview:
        """File diffs + commands ``apply()`` would run, with all secrets masked."""
        notes: list[str] = []
        try:
            rabbit_password, db_password, cinder_password = _load_cinder_storage_secrets(self.root)
        except PreconditionError as exc:
            rabbit_password = _DRY_RUN_PLACEHOLDER_SECRET
            db_password = _DRY_RUN_PLACEHOLDER_SECRET
            cinder_password = _DRY_RUN_PLACEHOLDER_SECRET
            notes.append(f"Precondition (checked by apply()): {exc}")

        disk = self.disk
        try:
            check_cinder_lvm_root_safety(self.root, self.local_node, self.config, disk=disk)
        except PreconditionError as exc:
            notes.append(f"Precondition (checked by apply()): {exc}")
        secrets_to_mask = (rabbit_password, db_password, cinder_password)
        file_changes: list[FileChange] = []

        lvm_path, lvm_display = resolve_target(self.root, LVM_CONF_TARGET)
        if _read_existing_conf(lvm_path, lvm_display) is None:
            notes.append(
                f"{lvm_display} does not exist yet (created when lvm2 is installed); "
                f"apply() will set devices/filter = {lvm_filter_for_disk(disk)}"
            )
        else:
            file_changes.append(edit_lvm_conf(self.root, disk, dry_run=True))

        _preview_or_note_ini_conf(
            self.root,
            CINDER_CONF_TARGET,
            "cinder-volume",
            cinder_storage_conf_settings(
                self.local_node, db_password, rabbit_password, cinder_password
            ),
            lambda: edit_cinder_storage_conf(
                self.root,
                self.local_node,
                db_password,
                rabbit_password,
                cinder_password,
                dry_run=True,
            ),
            secrets_to_mask,
            file_changes,
            notes,
        )

        tgt_path, tgt_display = resolve_target(self.root, CINDER_TGT_CONF_TARGET)
        if not tgt_path.parent.exists():
            notes.append(
                f"{tgt_display} parent directory does not exist yet (created when tgt is "
                f"installed); apply() will write:\n{CINDER_TGT_INCLUDE_LINE}"
            )
        else:
            file_changes.append(edit_cinder_tgt_conf(self.root, dry_run=True))

        notes.extend((STORAGE_LEAST_PRIVILEGE_NOTE, STORAGE_LVM_NOTE, STORAGE_NOT_CHECKED))
        commands = (
            *INSTALL_CINDER_STORAGE_PACKAGES,
            PlannedCommand(LVM_PVCREATE, {"device": disk}),
            PlannedCommand(LVM_VGCREATE_CINDER_VOLUMES, {"device": disk}),
            *RESTART_CINDER_STORAGE_UNITS,
        )
        read_only_commands = (
            *QUERY_CINDER_STORAGE_PACKAGES,
            *QUERY_CINDER_STORAGE_ACTIVE,
            QUERY_LVM_PVS,
            QUERY_LVM_VGS,
        )
        return StepPreview(
            file_changes=tuple(file_changes),
            commands=commands,
            read_only_commands=read_only_commands,
            notes=tuple(notes),
        )

    def check(self) -> bool:
        """True if secrets, 3 configs, packages, units, and LVM PV/VG are ready."""
        try:
            rabbit_password, db_password, cinder_password = _load_cinder_storage_secrets(self.root)
        except PreconditionError:
            return False
        disk = self.disk
        try:
            check_cinder_lvm_root_safety(self.root, self.local_node, self.config, disk=disk)
            if edit_lvm_conf(self.root, disk, dry_run=True).changed:
                return False
            if edit_cinder_storage_conf(
                self.root,
                self.local_node,
                db_password,
                rabbit_password,
                cinder_password,
                dry_run=True,
            ).changed:
                return False
            if edit_cinder_tgt_conf(self.root, dry_run=True).changed:
                return False
        except (PreconditionError, UnsafePathError, FileFormatError):
            return False
        for pkg in CINDER_STORAGE_PACKAGES:
            if not package_installed(self.executor, pkg):
                return False
        for unit in CINDER_STORAGE_UNITS:
            if unit_active_problems(self.executor, unit):
                return False
        return lvm_pv_and_vg_ready(self.executor, disk)

    def apply(self) -> None:
        """Install block storage packages, provision LVM PV and ``cinder-volumes`` VG,
        edit ``lvm.conf``, ``cinder.conf``, and ``/etc/tgt/conf.d/cinder.conf``, and
        restart units."""
        rabbit_password, db_password, cinder_password = _load_cinder_storage_secrets(self.root)
        disk = self.disk
        check_cinder_lvm_root_safety(self.root, self.local_node, self.config, disk=disk)

        for cmd in INSTALL_CINDER_STORAGE_PACKAGES:
            self.executor.run(cmd.command_id, cmd.params)
        ensure_lvm_pv_and_vg(self.executor, disk)
        edit_lvm_conf(self.root, disk, dry_run=False)
        edit_cinder_storage_conf(
            self.root,
            self.local_node,
            db_password,
            rabbit_password,
            cinder_password,
            dry_run=False,
        )
        edit_cinder_tgt_conf(self.root, dry_run=False)
        for cmd in RESTART_CINDER_STORAGE_UNITS:
            self.executor.run(cmd.command_id, cmd.params)

    def verify(self) -> VerifyResult:
        """Verify secrets, ``lvm.conf``, ``cinder.conf``, ``tgt/conf.d/cinder.conf``,
        packages, units, and LVM PV/VG."""
        problems: list[str] = []
        rabbit_password: str | None = None
        db_password: str | None = None
        cinder_password: str | None = None
        try:
            rabbit_password, db_password, cinder_password = _load_cinder_storage_secrets(self.root)
        except PreconditionError as exc:
            problems.append(str(exc))

        disk = self.disk
        try:
            check_cinder_lvm_root_safety(self.root, self.local_node, self.config, disk=disk)
        except PreconditionError as exc:
            problems.append(str(exc))

        lvm_path, lvm_display = resolve_target(self.root, LVM_CONF_TARGET)
        try:
            lvm_text = _read_existing_conf(lvm_path, lvm_display)
        except (UnsafePathError, FileFormatError) as exc:
            return VerifyResult(False, f"{exc}. {STORAGE_NOT_CHECKED}")
        if lvm_text is None:
            problems.append(f"{lvm_display} does not exist")
        else:
            problems += verify_lvm_conf_text(lvm_text, disk)

        c_path, c_display = resolve_target(self.root, CINDER_CONF_TARGET)
        try:
            c_text = _read_existing_conf(c_path, c_display)
        except (UnsafePathError, FileFormatError) as exc:
            return VerifyResult(False, f"{exc}. {STORAGE_NOT_CHECKED}")
        if c_text is None:
            problems.append(f"{c_display} does not exist")
        elif (
            db_password is not None and rabbit_password is not None and cinder_password is not None
        ):
            problems += verify_cinder_storage_conf_text(
                c_text, self.local_node, db_password, rabbit_password, cinder_password
            )

        tgt_path, tgt_display = resolve_target(self.root, CINDER_TGT_CONF_TARGET)
        try:
            tgt_text = _read_existing_conf(tgt_path, tgt_display)
        except (UnsafePathError, FileFormatError) as exc:
            return VerifyResult(False, f"{exc}. {STORAGE_NOT_CHECKED}")
        if tgt_text is None:
            problems.append(f"{tgt_display} does not exist")
        else:
            problems += verify_cinder_tgt_conf_text(tgt_text)

        for pkg in CINDER_STORAGE_PACKAGES:
            if not package_installed(self.executor, pkg):
                problems.append(f"package {pkg!r} is not installed")
        for unit in CINDER_STORAGE_UNITS:
            problems += unit_active_problems(self.executor, unit)
        problems += lvm_pv_and_vg_problems(self.executor, disk)

        if problems:
            return VerifyResult(False, f"{'; '.join(problems)}. {STORAGE_NOT_CHECKED}")
        return VerifyResult(
            True,
            f"{LVM_CONF_TARGET}, {CINDER_CONF_TARGET}, and {CINDER_TGT_CONF_TARGET} match, "
            f"{', '.join(CINDER_STORAGE_PACKAGES)} are installed, "
            f"{', '.join(CINDER_STORAGE_UNITS)} are active, and LVM physical volume "
            f"{disk!r} + volume group {CINDER_VG_NAME!r} exist. {STORAGE_NOT_CHECKED}",
        )


def make_cinder_controller_step(
    root: Path,
    config: Mapping[str, Any],
    local_node: Mapping[str, Any],
    *,
    executor: CommandExecutor,
) -> CinderControllerStep:
    """Construct a :class:`CinderControllerStep` for ``openstack-forge apply``."""
    return CinderControllerStep(root=root, config=config, local_node=local_node, executor=executor)


def make_cinder_storage_step(
    root: Path,
    config: Mapping[str, Any],
    local_node: Mapping[str, Any],
    *,
    executor: CommandExecutor,
) -> CinderStorageStep:
    """Construct a :class:`CinderStorageStep` for ``openstack-forge apply``."""
    return CinderStorageStep(root=root, config=config, local_node=local_node, executor=executor)
