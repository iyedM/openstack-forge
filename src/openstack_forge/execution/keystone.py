"""Install and configure the Keystone Identity service on the controller node.

Sources of truth (read directly for this module):
1. ``https://docs.openstack.org/keystone/2025.1/install/keystone-install-ubuntu.html``
   (source: ``doc/source/install/keystone-install-ubuntu.rst`` on ``stable/2025.1`` in
   ``openstack/keystone``).
2. ``https://docs.openstack.org/keystone/2025.1/install/keystone-users-ubuntu.html``
   (source: ``doc/source/install/common/keystone-users.inc`` on ``stable/2025.1``).
3. ``https://docs.openstack.org/keystone/2025.1/install/keystone-verify-ubuntu.html``
   (source: ``doc/source/install/keystone-verify-ubuntu.rst`` on ``stable/2025.1``).
4. ``https://docs.openstack.org/keystone/2025.1/install/keystone-openrc-ubuntu.html``
   (source: ``doc/source/install/common/openrc.inc`` on ``stable/2025.1``).

What the pages prescribe for Ubuntu:
1. Prerequisites (``keystone-install-ubuntu.html``) — create the ``keystone`` database and
   grant access in MariaDB::

       CREATE DATABASE keystone;
       GRANT ALL PRIVILEGES ON keystone.* TO 'keystone'@'localhost' \\
         IDENTIFIED BY 'KEYSTONE_DBPASS';
       GRANT ALL PRIVILEGES ON keystone.* TO 'keystone'@'%' \\
         IDENTIFIED BY 'KEYSTONE_DBPASS';

2. Install the package::

       # apt install keystone

3. Edit ``/etc/keystone/keystone.conf``:
   - In ``[database]``, set::

         connection = mysql+pymysql://keystone:KEYSTONE_DBPASS@controller/keystone

     ("Comment out or remove any other ``connection`` options in the ``[database]``
     section.")
   - In ``[token]``, set::

         provider = fernet

4. Populate the Identity service database::

       # su -s /bin/sh -c "keystone-manage db_sync" keystone

5. Initialize Fernet key repositories::

       # keystone-manage fernet_setup --keystone-user keystone --keystone-group keystone
       # keystone-manage credential_setup --keystone-user keystone --keystone-group keystone

6. Bootstrap the Identity service::

       # keystone-manage bootstrap --bootstrap-password ADMIN_PASS \\
         --bootstrap-admin-url http://controller:5000/v3/ \\
         --bootstrap-internal-url http://controller:5000/v3/ \\
         --bootstrap-public-url http://controller:5000/v3/ \\
         --bootstrap-region-id RegionOne

7. Configure the Apache HTTP server:
   - Edit ``/etc/apache2/apache2.conf`` and configure the ``ServerName`` option to
     reference the controller node::

         ServerName controller

8. Finalize the installation:
   - Restart the Apache service::

         # service apache2 restart

9. Create a domain, projects, users, and roles (``keystone-users-ubuntu.html``):
   - Create the ``service`` project required by all subsequent OpenStack services::

         $ openstack project create --domain default \\
           --description "Service Project" service

   - The guide also shows tutorial examples (``example`` domain, ``myproject``, ``myuser``,
     ``myrole``); see conscious deviation 8 below.

10. Verify operation (``keystone-verify-ubuntu.html``):
    - Authenticate as ``admin`` against ``http://controller:5000/v3`` in domain ``Default``
      / project ``admin``.

11. Create OpenStack client environment scripts (``keystone-openrc-ubuntu.html``):
    - Write ``admin-openrc`` with::

          export OS_PROJECT_DOMAIN_NAME=Default
          export OS_USER_DOMAIN_NAME=Default
          export OS_PROJECT_NAME=admin
          export OS_USERNAME=admin
          export OS_PASSWORD=ADMIN_PASS
          export OS_AUTH_URL=http://controller:5000/v3
          export OS_IDENTITY_API_VERSION=3
          export OS_IMAGE_API_VERSION=2

What was established on the Ubuntu 24.04 development machine (read-only inspection):
- ``apt-cache policy keystone`` shows ``2:27.0.0-0ubuntu1.1~cloud2`` in
  ``noble-updates/epoxy/main`` (Ubuntu Cloud Archive for OpenStack 2025.1 Epoxy) and
  ``2:25.0.0-0ubuntu1.4`` in ``noble-updates/main``.
- ``apt-cache show keystone`` shows
  ``Depends: adduser, apache2 | httpd, libapache2-mod-wsgi-py3, python3-keystone
  (= 2:27.0.0-0ubuntu1.1~cloud2), ssl-cert (>= 1.0.12)``,
  and ``python3-keystone`` depends on ``keystone-common`` and ``python3-pymysql``.
- ``dpkg -S`` confirms:
  - ``python3-keystone: /usr/bin/keystone-manage``
  - ``python3-openstackclient: /usr/bin/openstack`` (``7.4.0-0ubuntu1~cloud0``)
  - ``keystone-common: /etc/keystone`` (with ``/etc/keystone/keystone.conf`` listed in
    ``/var/lib/dpkg/info/keystone-common.conffiles``; ``keystone-common.postinst`` sets
    ``/etc/keystone`` to ``0750`` ``keystone:keystone``, ``/etc/keystone/keystone.conf`` to
    ``0640`` ``keystone:keystone``, ``/etc/keystone/fernet-keys`` to ``0700``
    ``keystone:keystone``, and ``/var/log/keystone`` to ``0750`` ``keystone:adm``).
  - ``keystone: /etc/apache2/sites-available/keystone.conf`` (enabled by
    ``/var/lib/dpkg/info/keystone.postinst`` via ``apache2_invoke ensite keystone.conf``,
    configuring ``Listen 5000`` and ``<VirtualHost *:5000>`` with ``WSGIDaemonProcess
    keystone-public user=keystone group=keystone`` and ``/usr/bin/keystone-wsgi-public``).
  - ``apache2: /etc/apache2/apache2.conf`` (``2.4.58-1ubuntu8.15``) and
    ``/usr/lib/systemd/system/apache2.service``.
- Inspection of ``/usr/lib/python3/dist-packages/keystone/`` confirms:
  - ``keystone/conf/fernet_tokens.py`` defaults ``[fernet_tokens] key_repository`` to
    ``/etc/keystone/fernet-keys/``.
  - ``keystone/conf/credential.py`` defaults ``[credential] key_repository`` to
    ``/etc/keystone/credential-keys/``.
  - ``keystone/common/fernet_utils.py`` ``FernetUtils.initialize_key_repository()`` returns
    immediately with exit status 0 if key ``0`` already exists in ``key_repository``.
  - ``keystone/cmd/cli.py`` (lines 64-235) ``BootStrap.do_bootstrap()`` reads
    ``self.password = os.environ.get('OS_BOOTSTRAP_PASSWORD') or
    CONF.command.bootstrap_password``.
  - ``keystone/cmd/bootstrap.py`` ``Bootstrapper.bootstrap()`` is idempotent: it ensures
    the ``Default`` domain, ``admin`` project, default roles (``reader``, ``member``,
    ``manager``, ``admin``, ``service``), role assignments, ``RegionOne`` region, and
    ``keystone`` (``identity``) service endpoints, and creates the ``admin`` user or resets
    its password to ``OS_BOOTSTRAP_PASSWORD``.

Deviations from the guide pages:
1. Precondition on :class:`SecretStore`: both ``KEYSTONE_DBPASS`` and ``ADMIN_PASS`` must
   exist in ``/etc/openstack-forge/secrets.json`` (created by ``openstack-forge secrets
   init``). The step never generates secrets itself; if the store or either secret is
   missing or invalid, :meth:`KeystoneStep.apply` raises an actionable
   :class:`PreconditionError`.
2. Non-interactive idempotent SQL provisioning via :data:`MARIADB_EXECUTE_SQL`
   (``/usr/bin/mariadb --batch --skip-column-names`` with ``stdin_param="sql"``) using
   ``CREATE DATABASE IF NOT EXISTS `keystone`;`` and ``GRANT ALL PRIVILEGES ...`` instead
   of an interactive ``mysql`` shell. ``KEYSTONE_DBPASS`` is sent over ``stdin`` and never
   appears in ``argv`` or ``/proc/<pid>/cmdline`` (``ps``).
3. Non-interactive ``apt-get install --yes keystone`` instead of ``apt install keystone``.
4. ``/usr/bin/keystone-manage db_sync`` is invoked directly as ``root`` instead of
   ``su -s /bin/sh -c "keystone-manage db_sync" keystone`` because :class:`CommandSpec`
   forbids ``su`` and ``sudo``. Because ``/etc/keystone/keystone.conf`` is edited before
   ``db_sync`` to point ``[database] connection`` at MariaDB
   (``mysql+pymysql://keystone:...@controller/keystone``), ``db_sync`` does not create a
   root-owned SQLite database file in ``/var/lib/keystone``. Caveat: if ``log_file`` or
   ``log_dir`` has been enabled in ``/etc/keystone/keystone.conf``, running
   ``keystone-manage`` as ``root`` can create or touch a root-owned log file in
   ``/var/log/keystone`` (by default ``keystone-manage`` logs to stderr when ``log_file``
   is unset).
5. ``/usr/bin/keystone-manage bootstrap`` receives ``ADMIN_PASS`` via
   ``env_params={"password": "OS_BOOTSTRAP_PASSWORD"}`` rather than ``--bootstrap-password
   ADMIN_PASS`` in ``argv``, keeping ``ADMIN_PASS`` in ``/proc/<pid>/environ`` (mode
   ``0400``, readable only by ``root``) instead of ``/proc/<pid>/cmdline`` (``ps``).
6. ``/etc/apache2/apache2.conf`` is edited with :func:`disable_lines_and_set_block` using
   the ``# BEGIN openstack-forge keystone-apache`` managed block (``ServerName controller``)
   while commenting out any active ``ServerName`` lines outside the block.
7. ``systemctl restart apache2`` is used instead of ``service apache2 restart``.
8. ``keystone-users-ubuntu.html``: :class:`KeystoneStep` idempotently creates the required
   ``service`` project in domain ``default`` via :func:`ensure_service_project` (using
   ``openstack.project-list`` and ``openstack.project-create-service`` with
   ``env_params={"admin_password": "OS_PASSWORD"}``) and consciously omits the tutorial-only
   ``example`` domain, ``myproject`` project, ``myuser`` user, ``myrole`` role, and
   ``demo-openrc`` file so production clusters do not carry unused example accounts.
9. ``keystone-openrc-ubuntu.html``: because the guide states *"The paths of the client
   environment scripts are unrestricted... ensure that they are accessible and located in a
   secure place appropriate for your deployment, as they do contain sensitive credentials"*,
   ``admin-openrc`` is written to ``/etc/openstack-forge/admin-openrc`` inside the ``0700``
   directory ``/etc/openstack-forge`` with file mode ``0600`` (owned by the effective UID,
   refusing symlinks and overly permissive modes, never creating ``admin-openrc.orig``, and
   masking ``ADMIN_PASS`` in :attr:`FileChange.diff`).

Password delivery and ``/proc/<pid>/cmdline`` (``ps``) guarantee:
- Neither ``KEYSTONE_DBPASS`` (sent via ``stdin_param="sql"`` and written to ``0640``
  ``/etc/keystone/keystone.conf``) nor ``ADMIN_PASS`` (sent via ``env_params`` as
  ``OS_BOOTSTRAP_PASSWORD`` to ``keystone-manage bootstrap`` and ``OS_PASSWORD`` to
  ``/usr/bin/openstack``, and written to ``0600`` ``/etc/openstack-forge/admin-openrc``)
  ever appears in ``argv`` or ``/proc/<pid>/cmdline`` (``ps``) during the ``keystone`` step.
"""

from __future__ import annotations

import os
import re
import stat
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, ClassVar

from openstack_forge.execution.commands import (
    APT_GET_INSTALL,
    KEYSTONE_MANAGE_BOOTSTRAP,
    KEYSTONE_MANAGE_CREDENTIAL_SETUP,
    KEYSTONE_MANAGE_DB_SYNC,
    KEYSTONE_MANAGE_FERNET_SETUP,
    SYSTEMCTL_RESTART,
    CommandExecutor,
    validate_parameter,
)
from openstack_forge.execution.engine import VerifyResult
from openstack_forge.execution.errors import (
    ExecutionError,
    FileFormatError,
    PreconditionError,
    SecretError,
    UnsafePathError,
)
from openstack_forge.execution.files import (
    FileChange,
    _ini_set_many_editor,
    _managed_block_editor,
    apply_line_edit,
    disable_lines_and_set_block,
    ini_set_many,
)
from openstack_forge.execution.fsutil import (
    atomic_write_text,
    read_text_file,
    resolve_target,
    unified_diff,
)
from openstack_forge.execution.openstack_cli import (
    DEFAULT_READINESS_INITIAL_BACKOFF,
    DEFAULT_READINESS_MAX_ATTEMPTS,
    DEFAULT_READINESS_MAX_BACKOFF,
    SERVICE_PROJECT_NAME,
    ensure_service_project,
    plan_create_service_project,
    plan_project_list,
    service_project_problems,
    service_project_ready,
)
from openstack_forge.execution.preview import PlannedCommand, StepPreview
from openstack_forge.execution.redaction import MASK, redact_text
from openstack_forge.execution.secrets import (
    DEFAULT_SECRET_LENGTH,
    DEFAULT_SECRETS_TARGET,
    SECRETS_DIR_MODE,
    SECRETS_FILE_MODE,
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

KEYSTONE_STEP_ID = "keystone"
KEYSTONE_PACKAGE = "keystone"
APACHE2_PACKAGE = "apache2"
APACHE2_UNIT = "apache2"

KEYSTONE_DB_NAME = "keystone"
KEYSTONE_DB_USER = "keystone"
KEYSTONE_DB_SECRET_NAME = "KEYSTONE_DBPASS"
KEYSTONE_ADMIN_SECRET_NAME = "ADMIN_PASS"

KEYSTONE_CONF_TARGET = "/etc/keystone/keystone.conf"
APACHE2_CONF_TARGET = "/etc/apache2/apache2.conf"
APACHE2_BLOCK_NAME = "openstack-forge keystone-apache"
APACHE2_SERVER_NAME_LINE = "ServerName controller"

ADMIN_OPENRC_TARGET = "/etc/openstack-forge/admin-openrc"
ADMIN_OPENRC_DIR_MODE = SECRETS_DIR_MODE
ADMIN_OPENRC_MODE = SECRETS_FILE_MODE

FERNET_KEYS_TARGET = "/etc/keystone/fernet-keys"
CREDENTIAL_KEYS_TARGET = "/etc/keystone/credential-keys"
KEY_REPOSITORIES: tuple[tuple[str, str], ...] = (
    ("Fernet token key repository", FERNET_KEYS_TARGET),
    ("Fernet credential key repository", CREDENTIAL_KEYS_TARGET),
)

# Active ServerName directive outside the managed block (case-insensitive in Apache).
SERVER_NAME_LINE_RE = re.compile(r"^\s*ServerName(?:\s|$)", re.IGNORECASE)

# Placeholder secret used ONLY to build a masked preview when secrets.json has not been
# initialized yet.
_DRY_RUN_PLACEHOLDER_SECRET = "A" * DEFAULT_SECRET_LENGTH

INSTALL_KEYSTONE = PlannedCommand(APT_GET_INSTALL, {"package": KEYSTONE_PACKAGE})
RUN_DB_SYNC = PlannedCommand(KEYSTONE_MANAGE_DB_SYNC)
RUN_FERNET_SETUP = PlannedCommand(KEYSTONE_MANAGE_FERNET_SETUP)
RUN_CREDENTIAL_SETUP = PlannedCommand(KEYSTONE_MANAGE_CREDENTIAL_SETUP)
RESTART_APACHE2 = PlannedCommand(SYSTEMCTL_RESTART, {"unit": APACHE2_UNIT})

QUERY_KEYSTONE_PACKAGE = query_package(KEYSTONE_PACKAGE)
QUERY_APACHE2_ACTIVE = query_active(APACHE2_UNIT)

SQL_STDIN_NOTE = (
    "MariaDB provisioning: 'CREATE DATABASE IF NOT EXISTS `keystone`;' and grants for "
    "'keystone'@'localhost' and 'keystone'@'%' are sent to '/usr/bin/mariadb --batch "
    "--skip-column-names' over stdin (stdin_param='sql') so KEYSTONE_DBPASS never appears "
    "in argv or /proc/<pid>/cmdline (ps)."
)
DB_SYNC_NOTE = (
    "Database sync deviation: '/usr/bin/keystone-manage db_sync' is run directly as root "
    "instead of 'su -s /bin/sh -c \"keystone-manage db_sync\" keystone' (CommandSpec "
    "forbids su/sudo). Because /etc/keystone/keystone.conf is edited before db_sync to use "
    "mysql+pymysql://..., no root-owned SQLite file is created in /var/lib/keystone (note: "
    "if log_file is enabled in keystone.conf, running keystone-manage as root can create a "
    "root-owned log file in /var/log/keystone)."
)
BOOTSTRAP_ENV_NOTE = (
    "Bootstrap and CLI password delivery via env_params: 'keystone-manage bootstrap' "
    "receives ADMIN_PASS via OS_BOOTSTRAP_PASSWORD (verified in "
    "/usr/lib/python3/dist-packages/keystone/cmd/cli.py) and '/usr/bin/openstack' receives "
    "ADMIN_PASS via OS_PASSWORD. Neither KEYSTONE_DBPASS nor ADMIN_PASS ever appears in "
    "argv or /proc/<pid>/cmdline (ps); /proc/<pid>/environ is readable only by root."
)
PACKAGE_NOTE = (
    "Ubuntu 24.04 package facts (established read-only on the dev machine): keystone "
    "2:27.0.0-0ubuntu1.1~cloud2 in cloud-archive:epoxy depends on apache2, "
    "libapache2-mod-wsgi-py3, and python3-keystone (which depends on keystone-common and "
    "python3-pymysql); keystone.postinst enables /etc/apache2/sites-available/keystone.conf "
    "on port 5000."
)
USERS_AND_OPENRC_NOTE = (
    "Post-bootstrap deviation (keystone-users-ubuntu.html & keystone-openrc-ubuntu.html): "
    "creates the 'service' project in domain 'default' and writes "
    "/etc/openstack-forge/admin-openrc (mode 0600, no .orig backup); tutorial-only "
    "'example' domain, 'myproject', 'myuser', 'myrole', and 'demo-openrc' are omitted."
)
NOT_CHECKED = (
    "Not checked: the SQL schema version from keystone-manage db_sync is not queried "
    "directly (live Keystone /v3 authentication and the 'service' project are verified via "
    "openstack.project-list)."
)


def keystone_db_connection(db_password: str) -> str:
    """Return the ``[database] connection`` URL for ``/etc/keystone/keystone.conf``."""
    return f"mysql+pymysql://{KEYSTONE_DB_USER}:{db_password}@controller/{KEYSTONE_DB_NAME}"


def keystone_conf_settings(db_password: str) -> tuple[tuple[str, str, str], ...]:
    """Return the ``(section, key, value)`` settings for ``/etc/keystone/keystone.conf``."""
    return (
        ("database", "connection", keystone_db_connection(db_password)),
        ("token", "provider", "fernet"),
    )


def _read_existing_conf(path: Path, display: str) -> str | None:
    """Read an existing config file, returning ``None`` if neither it nor its parent dir exists."""
    if not path.is_symlink() and not path.exists():
        return None
    return read_text_file(path, display)


def edit_keystone_conf(root: Path, db_password: str, *, dry_run: bool) -> FileChange:
    """Set ``[database] connection`` and ``[token] provider`` in ``/etc/keystone/keystone.conf``.

    The file comes from ``keystone-common`` (installed by ``keystone``) and is never
    created from scratch.
    """
    path, display = resolve_target(root, KEYSTONE_CONF_TARGET)
    if _read_existing_conf(path, display) is None:
        raise PreconditionError(
            f"{display} does not exist; it is created when {KEYSTONE_PACKAGE} is installed"
        )
    return ini_set_many(
        root, KEYSTONE_CONF_TARGET, keystone_conf_settings(db_password), dry_run=dry_run
    )


def verify_keystone_conf_text(text: str, db_password: str) -> list[str]:
    """Pure check: ``[database] connection`` and ``[token] provider`` match expected values."""
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
    for section_name, key, expected in keystone_conf_settings(db_password):
        found = values.get((section_name, key), [])
        shown_expected = redact_text(expected).replace(db_password, MASK)
        if not found:
            problems.append(
                f"{key} is not set in [{section_name}] of {KEYSTONE_CONF_TARGET} "
                f"(expected {shown_expected!r})"
            )
            continue
        for actual in dict.fromkeys(found):
            if actual != expected:
                shown_actual = redact_text(actual).replace(db_password, MASK)
                problems.append(
                    f"{key} is {shown_actual!r} in [{section_name}] of {KEYSTONE_CONF_TARGET}, "
                    f"expected {shown_expected!r}"
                )
    return problems


def apache2_block_content() -> str:
    """Return the managed block content for ``/etc/apache2/apache2.conf``."""
    return f"{APACHE2_SERVER_NAME_LINE}\n"


def edit_apache2_conf(root: Path, *, dry_run: bool) -> FileChange:
    """Disable outside ``ServerName`` lines and set the ``keystone-apache`` managed block."""
    path, display = resolve_target(root, APACHE2_CONF_TARGET)
    if _read_existing_conf(path, display) is None:
        raise PreconditionError(
            f"{display} does not exist; it is created when {KEYSTONE_PACKAGE} "
            f"(and its dependency {APACHE2_PACKAGE}) is installed"
        )
    return disable_lines_and_set_block(
        root,
        APACHE2_CONF_TARGET,
        SERVER_NAME_LINE_RE,
        APACHE2_BLOCK_NAME,
        apache2_block_content(),
        dry_run=dry_run,
    )


def verify_apache2_conf_text(text: str) -> list[str]:
    """Pure check: the ``keystone-apache`` block sets ``ServerName controller`` and no other
    active ``ServerName`` line exists outside the block."""
    lines = text.split("\n")
    begin, end = f"# BEGIN {APACHE2_BLOCK_NAME}", f"# END {APACHE2_BLOCK_NAME}"
    begins = [i for i, line in enumerate(lines) if line.strip() == begin]
    ends = [i for i, line in enumerate(lines) if line.strip() == end]
    if not begins and not ends:
        return [f"the managed block '{APACHE2_BLOCK_NAME}' is missing from {APACHE2_CONF_TARGET}"]
    if len(begins) != 1 or len(ends) != 1 or ends[0] < begins[0]:
        return [
            f"the managed block '{APACHE2_BLOCK_NAME}' in {APACHE2_CONF_TARGET} has broken markers"
        ]
    problems: list[str] = []
    body = [line.strip() for line in lines[begins[0] + 1 : ends[0]]]
    if body != [APACHE2_SERVER_NAME_LINE]:
        problems.append(
            f"the managed block '{APACHE2_BLOCK_NAME}' in {APACHE2_CONF_TARGET} does not "
            f"contain exactly '{APACHE2_SERVER_NAME_LINE}'"
        )
    outside = [
        line.strip()
        for index, line in enumerate(lines)
        if not begins[0] <= index <= ends[0] and SERVER_NAME_LINE_RE.match(line)
    ]
    if outside:
        problems.append(
            f"active ServerName line(s) outside the block in {APACHE2_CONF_TARGET}: "
            f"{', '.join(outside)}"
        )
    return problems


def render_admin_openrc(admin_password: str) -> str:
    """Render the ``admin-openrc`` script prescribed by ``keystone-openrc-ubuntu.html``."""
    validate_parameter("secret", "admin_password", admin_password)
    return (
        "export OS_PROJECT_DOMAIN_NAME=Default\n"
        "export OS_USER_DOMAIN_NAME=Default\n"
        "export OS_PROJECT_NAME=admin\n"
        "export OS_USERNAME=admin\n"
        f"export OS_PASSWORD={admin_password}\n"
        "export OS_AUTH_URL=http://controller:5000/v3\n"
        "export OS_IDENTITY_API_VERSION=3\n"
        "export OS_IMAGE_API_VERSION=2\n"
    )


def verify_admin_openrc_text(text: str, admin_password: str) -> list[str]:
    """Pure check: ``text`` matches :func:`render_admin_openrc` without leaking the secret."""
    expected = render_admin_openrc(admin_password)
    if text == expected:
        return []
    expected_lines = expected.strip().split("\n")
    actual_lines = [line.strip() for line in text.strip().split("\n") if line.strip()]
    missing_or_wrong: list[str] = []
    for expected_line in expected_lines:
        key, _, _ = expected_line.partition("=")
        matching = [line for line in actual_lines if line.startswith(f"{key}=")]
        if not matching:
            missing_or_wrong.append(f"missing {key!r}")
        elif matching != [expected_line]:
            missing_or_wrong.append(f"mismatch for {key!r}")
    if len(actual_lines) != len(expected_lines) and not missing_or_wrong:
        missing_or_wrong.append("unexpected extra or duplicate lines")
    detail = ", ".join(missing_or_wrong) or "content does not match expected exports"
    return [f"{ADMIN_OPENRC_TARGET} content mismatch ({detail})"]


def _reject_openrc_symlinks(root: Path, path: Path, display: str) -> None:
    root_dir = Path(root).resolve(strict=True)
    display_dir = str(PurePosixPath(display).parent)
    rel_parts = path.relative_to(root_dir).parts
    current = root_dir
    for part in rel_parts[:-1]:
        current = current / part
        if current.is_symlink():
            raise UnsafePathError(
                f"Refusing to use directory {display_dir}: {current} is a symlink"
            )
    if path.is_symlink():
        raise UnsafePathError(f"Refusing to modify {display}: it is a symlink")


def _check_openrc_directory(parent: Path, display_dir: str, expected_uid: int) -> None:
    if parent.is_symlink():
        raise UnsafePathError(f"Refusing to use directory {display_dir}: it is a symlink")
    dir_stat = parent.stat(follow_symlinks=False)
    if not stat.S_ISDIR(dir_stat.st_mode):
        raise UnsafePathError(f"Parent {display_dir} is not a directory")
    if dir_stat.st_uid != expected_uid:
        raise PreconditionError(
            f"Directory {display_dir} is owned by uid {dir_stat.st_uid}, "
            f"expected current effective uid {expected_uid}"
        )
    dir_mode = stat.S_IMODE(dir_stat.st_mode)
    if dir_mode & 0o077 != 0:
        raise PreconditionError(
            f"Directory {display_dir} has mode {dir_mode:04o}, which is more permissive than "
            f"{ADMIN_OPENRC_DIR_MODE:04o} (group/other bits must be 0)"
        )


def _check_openrc_file(path: Path, display: str, expected_uid: int) -> None:
    if path.is_symlink():
        raise UnsafePathError(f"Refusing to modify {display}: it is a symlink")
    file_stat = path.stat(follow_symlinks=False)
    if not stat.S_ISREG(file_stat.st_mode):
        raise UnsafePathError(f"Refusing to modify {display}: it is not a regular file")
    if file_stat.st_uid != expected_uid:
        raise PreconditionError(
            f"{display} is owned by uid {file_stat.st_uid}, "
            f"expected current effective uid {expected_uid}"
        )
    file_mode = stat.S_IMODE(file_stat.st_mode)
    if file_mode & 0o177 != 0:
        raise PreconditionError(
            f"{display} has mode {file_mode:04o}, which is more permissive than "
            f"{ADMIN_OPENRC_MODE:04o} (execute, group and other bits must be 0)"
        )


def write_admin_openrc(
    root: Path,
    admin_password: str,
    *,
    dry_run: bool = False,
    euid: Callable[[], int] = os.geteuid,
) -> FileChange:
    """Write ``/etc/openstack-forge/admin-openrc`` with mode ``0600`` and no ``.orig`` backup."""
    expected = render_admin_openrc(admin_password)
    path, display = resolve_target(root, ADMIN_OPENRC_TARGET)
    display_dir = str(PurePosixPath(display).parent)
    _reject_openrc_symlinks(root, path, display)
    expected_uid = euid()
    parent = path.parent

    if parent.exists():
        _check_openrc_directory(parent, display_dir, expected_uid)

    existing: str | None = None
    if path.exists():
        _check_openrc_file(path, display, expected_uid)
        existing = read_text_file(path, display)

    changed = existing != expected
    diff = (
        unified_diff(existing or "", expected, display).replace(admin_password, MASK)
        if changed
        else ""
    )
    if not dry_run and changed:
        if not parent.exists():
            parent.parent.mkdir(parents=True, exist_ok=True)
            parent.mkdir(mode=ADMIN_OPENRC_DIR_MODE, exist_ok=False)
            os.chmod(parent, ADMIN_OPENRC_DIR_MODE)
            _check_openrc_directory(parent, display_dir, expected_uid)
        atomic_write_text(path, expected, mode=ADMIN_OPENRC_MODE)
        _check_openrc_file(path, display, expected_uid)
    return FileChange(
        path=display,
        changed=changed,
        created=existing is None,
        backup=None,
        diff=diff,
    )


edit_admin_openrc = write_admin_openrc


def verify_admin_openrc(
    root: Path,
    admin_password: str | None,
    *,
    euid: Callable[[], int] = os.geteuid,
) -> list[str]:
    """Verify ``/etc/openstack-forge/admin-openrc`` exists, is ``0600``, and has expected lines."""
    path, display = resolve_target(root, ADMIN_OPENRC_TARGET)
    display_dir = str(PurePosixPath(display).parent)
    _reject_openrc_symlinks(root, path, display)
    expected_uid = euid()
    parent = path.parent

    if not parent.exists():
        return [f"{display} does not exist (directory {display_dir} is missing)"]
    try:
        _check_openrc_directory(parent, display_dir, expected_uid)
    except PreconditionError as exc:
        return [str(exc)]

    if not path.exists():
        return [f"{display} does not exist"]
    try:
        _check_openrc_file(path, display, expected_uid)
    except PreconditionError as exc:
        return [str(exc)]

    text = read_text_file(path, display)
    if text is None:  # pragma: no cover - defensive after path.exists()
        return [f"{display} does not exist"]
    if admin_password is None:
        return []
    return verify_admin_openrc_text(text, admin_password)


def verify_keystone_key_repositories(root: Path) -> list[str]:
    """Check that ``/etc/keystone/fernet-keys`` and ``/etc/keystone/credential-keys`` exist
    under ``root`` and each contains a non-empty regular key file ``0``."""
    problems: list[str] = []
    for label, target in KEY_REPOSITORIES:
        path, display = resolve_target(root, target)
        if path.is_symlink():
            raise UnsafePathError(f"Refusing symlinked {label}: {display}")
        if not path.is_dir():
            problems.append(f"{label} {display} does not exist")
            continue
        key_zero, key_zero_display = resolve_target(root, f"{target}/0")
        if key_zero.is_symlink():
            raise UnsafePathError(f"Refusing symlinked key file: {key_zero_display}")
        if not key_zero.is_file() or key_zero.stat().st_size == 0:
            problems.append(f"{label} {display} is not initialized (missing key '0')")
    return problems


def _load_keystone_secrets(root: Path) -> tuple[str, str]:
    """Return ``(KEYSTONE_DBPASS, ADMIN_PASS)`` from :class:`SecretStore` or raise
    :class:`PreconditionError`."""
    store = SecretStore(root)
    try:
        db_password = store.get(KEYSTONE_DB_SECRET_NAME)
        admin_password = store.get(KEYSTONE_ADMIN_SECRET_NAME)
    except (SecretError, UnsafePathError) as exc:
        raise PreconditionError(
            f"{KEYSTONE_STEP_ID} requires {KEYSTONE_DB_SECRET_NAME} and "
            f"{KEYSTONE_ADMIN_SECRET_NAME} in {DEFAULT_SECRETS_TARGET}: {exc}. "
            "Run 'openstack-forge secrets init' first (the step never generates secrets "
            "itself)."
        ) from exc
    return db_password, admin_password


def _plan_bootstrap(admin_password: str) -> PlannedCommand:
    return PlannedCommand(KEYSTONE_MANAGE_BOOTSTRAP, {"password": admin_password})


@dataclass(frozen=True)
class KeystoneStep:
    """Executable ``keystone`` step (controller only)."""

    root: Path
    config: Mapping[str, Any]
    local_node: Mapping[str, Any]
    executor: CommandExecutor
    id: str = KEYSTONE_STEP_ID
    sleep: Callable[[float], None] = time.sleep
    readiness_max_attempts: int = DEFAULT_READINESS_MAX_ATTEMPTS
    readiness_initial_backoff: float = DEFAULT_READINESS_INITIAL_BACKOFF
    readiness_max_backoff: float = DEFAULT_READINESS_MAX_BACKOFF
    declares_commands: ClassVar[bool] = True

    def __post_init__(self) -> None:
        object.__setattr__(self, "root", Path(self.root))
        if self.local_node["role"] != "controller":
            raise ExecutionError(
                f"{self.id} only runs on the controller, not on "
                f"{self.local_node['hostname']!r} ({self.local_node['role']})"
            )

    def dry_run_preview(self) -> StepPreview:
        """Preview file diffs, mutating/read-only commands, and operational notes."""
        notes: list[str] = []
        try:
            db_password, admin_password = _load_keystone_secrets(self.root)
        except PreconditionError as exc:
            db_password = _DRY_RUN_PLACEHOLDER_SECRET
            admin_password = _DRY_RUN_PLACEHOLDER_SECRET
            notes.append(f"Precondition not yet met: {exc}")

        file_changes: list[FileChange] = []
        try:
            file_changes.append(edit_keystone_conf(self.root, db_password, dry_run=True))
        except PreconditionError:
            rendered = apply_line_edit(
                "",
                KEYSTONE_CONF_TARGET,
                _ini_set_many_editor(keystone_conf_settings(MASK)),
            )
            notes.append(
                f"{KEYSTONE_CONF_TARGET} does not exist yet; it is created when "
                f"{KEYSTONE_PACKAGE} is installed. After that, its [database] and [token] "
                f"sections are updated with:\n{rendered}"
            )

        try:
            file_changes.append(edit_apache2_conf(self.root, dry_run=True))
        except PreconditionError:
            block = apply_line_edit(
                "",
                APACHE2_CONF_TARGET,
                _managed_block_editor(APACHE2_BLOCK_NAME, apache2_block_content()),
            )
            notes.append(
                f"{APACHE2_CONF_TARGET} does not exist yet; it is created when "
                f"{KEYSTONE_PACKAGE} (and its dependency {APACHE2_PACKAGE}) is installed. "
                "After that, any active ServerName lines outside the block are commented out "
                f"and this block is appended:\n{block}"
            )

        try:
            file_changes.append(write_admin_openrc(self.root, admin_password, dry_run=True))
        except PreconditionError as exc:
            notes.append(f"Precondition not yet met for {ADMIN_OPENRC_TARGET}: {exc}")

        sql_cmd = plan_service_database_sql((KEYSTONE_DB_NAME,), KEYSTONE_DB_USER, db_password)
        bootstrap_cmd = _plan_bootstrap(admin_password)
        create_service_project_cmd = plan_create_service_project(admin_password)
        query_project_list_cmd = plan_project_list(admin_password)

        notes += [
            "Both configuration files are edited after 'apt-get install --yes keystone' and "
            "before 'keystone-manage db_sync' and 'systemctl restart apache2'.",
            SQL_STDIN_NOTE,
            DB_SYNC_NOTE,
            BOOTSTRAP_ENV_NOTE,
            PACKAGE_NOTE,
            USERS_AND_OPENRC_NOTE,
            "apt-get install downloads from the configured apt sources (network).",
        ]
        return StepPreview(
            file_changes=tuple(file_changes),
            commands=(
                sql_cmd,
                INSTALL_KEYSTONE,
                RUN_DB_SYNC,
                RUN_FERNET_SETUP,
                RUN_CREDENTIAL_SETUP,
                bootstrap_cmd,
                RESTART_APACHE2,
                create_service_project_cmd,
            ),
            read_only_commands=(
                QUERY_KEYSTONE_PACKAGE,
                QUERY_APACHE2_ACTIVE,
                QUERY_MARIADB_STATE,
                query_project_list_cmd,
            ),
            notes=tuple(notes),
        )

    def check(self) -> bool:
        """True if secrets, files, keys, package, Apache, MariaDB, and 'service' are ready."""
        try:
            db_password, admin_password = _load_keystone_secrets(self.root)
        except PreconditionError:
            return False
        try:
            if edit_keystone_conf(self.root, db_password, dry_run=True).changed:
                return False
            if edit_apache2_conf(self.root, dry_run=True).changed:
                return False
            if write_admin_openrc(self.root, admin_password, dry_run=True).changed:
                return False
            if verify_admin_openrc(self.root, admin_password):
                return False
            if verify_keystone_key_repositories(self.root):
                return False
        except (PreconditionError, UnsafePathError, FileFormatError):
            return False
        if not package_installed(self.executor, KEYSTONE_PACKAGE):
            return False
        if unit_active_problems(self.executor, APACHE2_UNIT):
            return False
        if not service_database_ready(self.executor, (KEYSTONE_DB_NAME,), KEYSTONE_DB_USER):
            return False
        return service_project_ready(self.executor, admin_password)

    def apply(self) -> None:
        """Provision MariaDB, install keystone, edit configs, sync/setup/bootstrap, restart,
        ensure the 'service' project, and write /etc/openstack-forge/admin-openrc."""
        db_password, admin_password = _load_keystone_secrets(self.root)
        provision_service_database(
            self.executor, (KEYSTONE_DB_NAME,), KEYSTONE_DB_USER, db_password
        )
        self.executor.run(INSTALL_KEYSTONE.command_id, INSTALL_KEYSTONE.params)
        edit_keystone_conf(self.root, db_password, dry_run=False)
        edit_apache2_conf(self.root, dry_run=False)
        self.executor.run(RUN_DB_SYNC.command_id, RUN_DB_SYNC.params)
        self.executor.run(RUN_FERNET_SETUP.command_id, RUN_FERNET_SETUP.params)
        self.executor.run(RUN_CREDENTIAL_SETUP.command_id, RUN_CREDENTIAL_SETUP.params)
        self.executor.run(KEYSTONE_MANAGE_BOOTSTRAP, {"password": admin_password})
        self.executor.run(RESTART_APACHE2.command_id, RESTART_APACHE2.params)
        ensure_service_project(
            self.executor,
            admin_password,
            max_attempts=self.readiness_max_attempts,
            initial_backoff=self.readiness_initial_backoff,
            max_backoff=self.readiness_max_backoff,
            sleep=self.sleep,
        )
        write_admin_openrc(self.root, admin_password, dry_run=False)

    def verify(self) -> VerifyResult:
        """Verify secrets, files, Fernet/credential keys, package, Apache, MariaDB, and
        the Keystone 'service' project."""
        problems: list[str] = []
        db_password: str | None = None
        admin_password: str | None = None
        try:
            db_password, admin_password = _load_keystone_secrets(self.root)
        except PreconditionError as exc:
            problems.append(str(exc))

        keystone_path, keystone_display = resolve_target(self.root, KEYSTONE_CONF_TARGET)
        try:
            keystone_text = _read_existing_conf(keystone_path, keystone_display)
        except (UnsafePathError, FileFormatError) as exc:
            return VerifyResult(False, f"{exc}. {NOT_CHECKED}")
        if keystone_text is None:
            problems.append(f"{keystone_display} does not exist")
        elif db_password is not None:
            problems += verify_keystone_conf_text(keystone_text, db_password)

        apache_path, apache_display = resolve_target(self.root, APACHE2_CONF_TARGET)
        try:
            apache_text = _read_existing_conf(apache_path, apache_display)
        except (UnsafePathError, FileFormatError) as exc:
            return VerifyResult(False, f"{exc}. {NOT_CHECKED}")
        if apache_text is None:
            problems.append(f"{apache_display} does not exist")
        else:
            problems += verify_apache2_conf_text(apache_text)

        try:
            problems += verify_admin_openrc(self.root, admin_password)
        except (UnsafePathError, FileFormatError) as exc:
            return VerifyResult(False, f"{exc}. {NOT_CHECKED}")

        try:
            problems += verify_keystone_key_repositories(self.root)
        except UnsafePathError as exc:
            return VerifyResult(False, f"{exc}. {NOT_CHECKED}")

        if not package_installed(self.executor, KEYSTONE_PACKAGE):
            problems.append(f"{KEYSTONE_PACKAGE} is not installed")
        problems += unit_active_problems(self.executor, APACHE2_UNIT)
        problems += service_database_problems(self.executor, (KEYSTONE_DB_NAME,), KEYSTONE_DB_USER)
        if admin_password is not None:
            problems += service_project_problems(self.executor, admin_password)

        if problems:
            return VerifyResult(False, f"{'; '.join(problems)}. {NOT_CHECKED}")
        return VerifyResult(
            True,
            f"{KEYSTONE_PACKAGE} is installed; {keystone_display} sets [database] connection "
            f"and [token] provider = fernet; {apache_display} sets '{APACHE2_SERVER_NAME_LINE}'; "
            f"{ADMIN_OPENRC_TARGET} is present (0600); "
            f"{FERNET_KEYS_TARGET} and {CREDENTIAL_KEYS_TARGET} are initialized; MariaDB "
            f"database {KEYSTONE_DB_NAME!r} and grants for {KEYSTONE_DB_USER!r} exist; "
            f"{APACHE2_UNIT} is active; Keystone project {SERVICE_PROJECT_NAME!r} exists. "
            f"{NOT_CHECKED}",
        )


def make_keystone_step(
    root: Path,
    config: Mapping[str, Any],
    local_node: Mapping[str, Any],
    *,
    executor: CommandExecutor,
    sleep: Callable[[float], None] = time.sleep,
) -> KeystoneStep:
    return KeystoneStep(
        root=Path(root),
        config=config,
        local_node=local_node,
        executor=executor,
        sleep=sleep,
    )
