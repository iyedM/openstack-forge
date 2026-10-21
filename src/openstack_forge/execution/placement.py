"""The ``placement`` step (controller only).

**Unverified: this step has never been run on a real host.**

Source of truth
===============
Read directly from the official OpenStack 2025.1 (Epoxy) Placement installation guide for Ubuntu:

- https://docs.openstack.org/placement/2025.1/install/install-ubuntu.html
  (raw source: ``doc/source/install/install-ubuntu.rst`` and its include
  ``doc/source/install/shared/endpoints.rst`` on ``stable/2025.1`` in ``openstack/placement``).

What the official page prescribes
---------------------------------
1. **Prerequisites — SQL database** (``mysql`` as ``root``):
   - ``CREATE DATABASE placement;``
   - ``GRANT ALL PRIVILEGES ON placement.* TO 'placement'@'localhost'``
     ``IDENTIFIED BY 'PLACEMENT_DBPASS';``
   - ``GRANT ALL PRIVILEGES ON placement.* TO 'placement'@'%' IDENTIFIED BY 'PLACEMENT_DBPASS';``
2. **Prerequisites — Keystone service credentials and API endpoints** (after ``. admin-openrc``):
   - ``openstack user create --domain default --password-prompt placement`` (``PLACEMENT_PASS``)
   - ``openstack role add --project service --user placement admin``
   - ``openstack service create --name placement --description "Placement API" placement``
   - ``openstack endpoint create --region RegionOne placement public http://controller:8778``
   - ``openstack endpoint create --region RegionOne placement internal http://controller:8778``
   - ``openstack endpoint create --region RegionOne placement admin http://controller:8778``
3. **Install the packages**:
   - ``apt install placement-api``
4. **Edit ``/etc/placement/placement.conf``**:
   - In ``[placement_database]``:
     ``connection = mysql+pymysql://placement:PLACEMENT_DBPASS@controller/placement``
   - In ``[api]``:
     ``auth_strategy = keystone``
   - In ``[keystone_authtoken]``:
     - ``auth_url = http://controller:5000/v3``
     - ``memcached_servers = controller:11211``
     - ``auth_type = password``
     - ``project_domain_name = Default``
     - ``user_domain_name = Default``
     - ``project_name = service``
     - ``username = placement``
     - ``password = PLACEMENT_PASS``
5. **Populate the ``placement`` database**:
   - ``su -s /bin/sh -c "placement-manage db sync" placement``
6. **Finalize installation**:
   - ``service apache2 restart``

Ubuntu 24.04 + Epoxy Cloud Archive package facts (established read-only on the dev machine)
-------------------------------------------------------------------------------------------
- ``apt-cache policy placement-api placement-common python3-placement``:
  ``1:13.0.0-0ubuntu1~cloud0`` in ``noble-updates/epoxy/main`` (Ubuntu Cloud Archive for
  OpenStack 2025.1 Epoxy) and ``1:11.0.0-0ubuntu1`` in ``noble/main``.
- ``apt-cache show placement-api``:
  ``Depends: apache2 | httpd, libapache2-mod-wsgi-py3,``
  ``python3-placement (= 1:13.0.0-0ubuntu1~cloud0)``;
  ``python3-placement`` depends on ``placement-common``.
- ``dpkg -S`` confirms:
  - ``python3-placement: /usr/bin/placement-manage`` (entry point for
    ``placement.cmd.manage:main``);
  - ``/usr/lib/python3/dist-packages/placement/cmd/manage.py``: ``add_db_command_parsers()``
    registers ``subparsers.add_parser('db')`` and nested ``db_parser.add_parser('sync')`` —
    requiring two separate argv tokens ``("db", "sync")`` (unlike ``glance-manage db_sync`` or
    ``keystone-manage db_sync``);
  - ``placement-common: /etc/placement/placement.conf`` (dpkg conffile MD5
    ``1459fc6e8daadaf4f0a618036b622508``; ``placement-common.postinst`` creates system
    user/group ``placement``, sets ``/etc/placement`` to ``0750`` ``root:placement`` and files
    in ``/etc/placement`` to ``0640`` ``root:placement``, and sets ``/var/lib/placement`` to
    ``0750`` ``placement:placement``);
  - ``placement-api: /etc/apache2/sites-available/placement-api.conf`` (dpkg conffile MD5
    ``e4ca061f159949adb94276946e4d015c``, ``Listen 8778``, ``<VirtualHost *:8778>``,
    ``WSGIScriptAlias / /usr/bin/placement-api`` running as ``user=placement group=placement``,
    enabled by ``placement-api.postinst`` via ``apache2_invoke ensite placement-api.conf``).

Deviations, trade-offs, and scope boundary
------------------------------------------
1. **``PLACEMENT_DBPASS`` in ``SecretStore`` (``EXTRA_SECRET_SPECS``)**:
   The summary table on ``environment-security.html`` omits ``PLACEMENT_DBPASS`` (listing only
   ``PLACEMENT_PASS``), whereas ``placement/2025.1/install/install-ubuntu.html`` requires both
   ``PLACEMENT_DBPASS`` and ``PLACEMENT_PASS``. Both are managed in :class:`SecretStore`.
2. **SQL provisioning via ``stdin`` (``openstack_forge.execution.sql``)**:
   Uses ``CREATE DATABASE IF NOT EXISTS `placement`;`` and grants for
   ``'placement'@'localhost'`` and ``'placement'@'%'`` piped over ``stdin`` to
   ``/usr/bin/mariadb --batch --skip-column-names`` so ``PLACEMENT_DBPASS`` never appears in
   ``argv`` or ``/proc/<pid>/cmdline`` (``ps``).
3. **Keystone provisioning via ``/usr/bin/openstack``
   (``openstack_forge.execution.openstack_cli``)**:
   - Uses :func:`ensure_service_user` and :func:`ensure_service_catalog` so reruns converge
     ``placement``'s password and never create duplicate ``placement`` service or
     ``http://controller:8778`` endpoint UUIDs in ``RegionOne``.
   - ``ADMIN_PASS`` is delivered via ``env_params={"admin_password": "OS_PASSWORD"}`` (never in
     ``argv``). Because ``--password-prompt`` in ``python3-openstackclient`` requires
     ``stdin.isatty()``, ``PLACEMENT_PASS`` is passed via ``--password {user_password}`` in
     ``openstack.user-create`` / ``openstack.user-set-password`` (masked as ``********``
     everywhere by OpenStack Forge, though visible in ``/proc/<pid>/cmdline`` for the duration
     of that subcommand).
4. **``placement-manage db sync`` run directly as ``root`` instead of ``su``**:
   ``CommandSpec`` forbids ``su`` and ``sudo``. Because ``/etc/placement/placement.conf`` is
   edited **before** ``placement-manage db sync`` to point ``[placement_database] connection``
   at MariaDB (``mysql+pymysql://...``), running ``/usr/bin/placement-manage db sync`` as
   ``root`` populates MariaDB without creating a root-owned SQLite file in
   ``/var/lib/placement``. Caveat: if ``log_file`` or ``log_dir`` is enabled in
   ``placement.conf``, running ``placement-manage`` as ``root`` can create a root-owned log
   file in ``/var/log/placement``.
5. **Non-interactive apt & systemd**:
   ``/usr/bin/apt-get install --yes placement-api`` instead of ``apt install placement-api``,
   and ``/usr/bin/systemctl restart apache2`` instead of ``service apache2 restart``.
6. **What ``verify()`` does not check**:
   No HTTP request to ``http://controller:8778`` and no ``placement-status upgrade check``
   (``verify.html``) are performed, and the Alembic schema revision inside the ``placement``
   MariaDB database is not queried directly.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from openstack_forge.execution.commands import (
    APT_GET_INSTALL,
    OPENSTACK_ENDPOINT_CREATE,
    OPENSTACK_ENDPOINT_LIST,
    OPENSTACK_ROLE_ADD_USER_PROJECT,
    OPENSTACK_ROLE_ASSIGNMENT_LIST,
    OPENSTACK_SERVICE_CREATE,
    OPENSTACK_SERVICE_LIST,
    OPENSTACK_USER_CREATE,
    OPENSTACK_USER_LIST,
    PLACEMENT_MANAGE_DB_SYNC,
    SYSTEMCTL_RESTART,
    CommandExecutor,
)
from openstack_forge.execution.engine import VerifyResult
from openstack_forge.execution.errors import (
    ExecutionError,
    FileFormatError,
    PreconditionError,
    SecretError,
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

PLACEMENT_STEP_ID = "placement"
PLACEMENT_PACKAGE = "placement-api"
APACHE2_UNIT = "apache2"

PLACEMENT_DB_NAME = "placement"
PLACEMENT_DB_USER = "placement"
PLACEMENT_DB_SECRET_NAME = "PLACEMENT_DBPASS"
PLACEMENT_SERVICE_SECRET_NAME = "PLACEMENT_PASS"
PLACEMENT_ADMIN_SECRET_NAME = "ADMIN_PASS"

PLACEMENT_SERVICE_USER = "placement"
PLACEMENT_SERVICE_NAME = "placement"
PLACEMENT_SERVICE_TYPE = "placement"
PLACEMENT_ENDPOINT_URL = "http://controller:8778"

PLACEMENT_CONF_TARGET = "/etc/placement/placement.conf"

# Placeholder secret used ONLY to build a masked preview when secrets.json has not been
# initialized yet.
_DRY_RUN_PLACEHOLDER_SECRET = "A" * DEFAULT_SECRET_LENGTH

INSTALL_PLACEMENT_API = PlannedCommand(APT_GET_INSTALL, {"package": PLACEMENT_PACKAGE})
RUN_PLACEMENT_DB_SYNC = PlannedCommand(PLACEMENT_MANAGE_DB_SYNC)
RESTART_APACHE2 = PlannedCommand(SYSTEMCTL_RESTART, {"unit": APACHE2_UNIT})

QUERY_PLACEMENT_PACKAGE = query_package(PLACEMENT_PACKAGE)
QUERY_APACHE2_ACTIVE = query_active(APACHE2_UNIT)

SQL_STDIN_NOTE = (
    "MariaDB provisioning: 'CREATE DATABASE IF NOT EXISTS `placement`;' and grants for "
    "'placement'@'localhost' and 'placement'@'%' are sent to '/usr/bin/mariadb --batch "
    "--skip-column-names' over stdin (stdin_param='sql') so PLACEMENT_DBPASS never appears in "
    "argv or /proc/<pid>/cmdline (ps)."
)
KEYSTONE_PROVISIONING_NOTE = (
    "Keystone provisioning: /usr/bin/openstack receives ADMIN_PASS via OS_PASSWORD "
    "(env_params, never in argv) and idempotently ensures the 'placement' user (with 'admin' "
    "role on project 'service'), the 'placement' ('placement') service, and its "
    "public/internal/admin endpoints (http://controller:8778 in RegionOne). Because "
    "python3-openstackclient's --password-prompt requires a TTY, PLACEMENT_PASS is passed via "
    "--password in openstack.user-create / openstack.user-set-password (masked as ******** in "
    "logs/previews, but visible in /proc/<pid>/cmdline for the duration of that subcommand)."
)
DB_SYNC_ROOT_NOTE = (
    "Deviation from guide: '/usr/bin/placement-manage db sync' is run directly as root (after "
    "/etc/placement/placement.conf points [placement_database] connection at MariaDB) instead "
    "of 'su -s /bin/sh -c \"placement-manage db sync\" placement' because CommandSpec forbids "
    "su."
)
PACKAGE_NOTE = (
    "Ubuntu 24.04 package facts (established read-only on the dev machine): placement-api "
    "1:13.0.0-0ubuntu1~cloud0 in cloud-archive:epoxy depends on apache2, "
    "libapache2-mod-wsgi-py3, python3-placement, and placement-common "
    "(/etc/placement/placement.conf mode 0640 root:placement); placement-api.postinst enables "
    "/etc/apache2/sites-available/placement-api.conf on port 8778."
)
NOT_CHECKED = (
    "Not checked: no HTTP request to http://controller:8778 and no 'placement-status upgrade "
    "check' are performed, and the Alembic schema version in the placement database is not "
    "queried directly."
)


def placement_db_connection(db_password: str) -> str:
    """Return the ``[placement_database] connection`` URL for ``/etc/placement/placement.conf``."""
    return f"mysql+pymysql://{PLACEMENT_DB_USER}:{db_password}@controller/{PLACEMENT_DB_NAME}"


def placement_conf_settings(
    db_password: str, placement_password: str
) -> tuple[tuple[str, str, str], ...]:
    """Return the ``(section, key, value)`` settings for ``/etc/placement/placement.conf``."""
    return (
        ("placement_database", "connection", placement_db_connection(db_password)),
        ("api", "auth_strategy", "keystone"),
        ("keystone_authtoken", "auth_url", "http://controller:5000/v3"),
        ("keystone_authtoken", "memcached_servers", "controller:11211"),
        ("keystone_authtoken", "auth_type", "password"),
        ("keystone_authtoken", "project_domain_name", "Default"),
        ("keystone_authtoken", "user_domain_name", "Default"),
        ("keystone_authtoken", "project_name", SERVICE_PROJECT_NAME),
        ("keystone_authtoken", "username", PLACEMENT_SERVICE_USER),
        ("keystone_authtoken", "password", placement_password),
    )


def _read_existing_conf(path: Path, display: str) -> str | None:
    """Read an existing config file, returning ``None`` if neither it nor its parent dir exists."""
    if not path.is_symlink() and not path.exists():
        return None
    return read_text_file(path, display)


def edit_placement_conf(
    root: Path, db_password: str, placement_password: str, *, dry_run: bool
) -> FileChange:
    """Apply the 2025.1 Ubuntu guide settings to ``/etc/placement/placement.conf``.

    The file comes from ``placement-common`` (installed by ``placement-api``) and is never
    created from scratch.
    """
    path, display = resolve_target(root, PLACEMENT_CONF_TARGET)
    if _read_existing_conf(path, display) is None:
        raise PreconditionError(
            f"{display} does not exist; it is created when {PLACEMENT_PACKAGE} is installed"
        )
    return ini_set_many(
        root,
        PLACEMENT_CONF_TARGET,
        placement_conf_settings(db_password, placement_password),
        dry_run=dry_run,
    )


def _mask_secrets(text: str, *secrets: str) -> str:
    masked = redact_text(text)
    for secret in secrets:
        if secret:
            masked = masked.replace(secret, MASK)
    return masked


def verify_placement_conf_text(text: str, db_password: str, placement_password: str) -> list[str]:
    """Pure check: all expected ``/etc/placement/placement.conf`` keys match expected values."""
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
    for section_name, key, expected in placement_conf_settings(db_password, placement_password):
        found = values.get((section_name, key), [])
        is_secret_key = key == "password"
        shown_expected = (
            MASK if is_secret_key else _mask_secrets(expected, db_password, placement_password)
        )
        if not found:
            problems.append(
                f"{key} is not set in [{section_name}] of {PLACEMENT_CONF_TARGET} "
                f"(expected {shown_expected!r})"
            )
            continue
        for actual in dict.fromkeys(found):
            if actual != expected:
                shown_actual = (
                    MASK
                    if is_secret_key
                    else _mask_secrets(actual, db_password, placement_password)
                )
                problems.append(
                    f"{key} is {shown_actual!r} in [{section_name}] of "
                    f"{PLACEMENT_CONF_TARGET}, expected {shown_expected!r}"
                )
    return problems


def _load_placement_secrets(root: Path) -> tuple[str, str, str]:
    """Load ``(ADMIN_PASS, PLACEMENT_DBPASS, PLACEMENT_PASS)`` from :class:`SecretStore`."""
    store = SecretStore(root)
    try:
        admin_password = store.get(PLACEMENT_ADMIN_SECRET_NAME)
        db_password = store.get(PLACEMENT_DB_SECRET_NAME)
        placement_password = store.get(PLACEMENT_SERVICE_SECRET_NAME)
    except (SecretError, UnsafePathError) as exc:
        raise PreconditionError(
            f"{PLACEMENT_STEP_ID} requires {PLACEMENT_ADMIN_SECRET_NAME}, "
            f"{PLACEMENT_DB_SECRET_NAME}, and {PLACEMENT_SERVICE_SECRET_NAME} in "
            f"{DEFAULT_SECRETS_TARGET}: {exc}. Run 'openstack-forge secrets init' first "
            "(the step never generates secrets itself)."
        ) from exc
    return admin_password, db_password, placement_password


@dataclass(frozen=True)
class PlacementStep:
    """``check`` / ``apply`` / ``verify`` for the ``placement`` catalog step."""

    root: Path
    config: Mapping[str, Any]
    local_node: Mapping[str, Any]
    executor: CommandExecutor
    id: str = PLACEMENT_STEP_ID
    declares_commands: bool = True

    def __post_init__(self) -> None:
        role = self.local_node.get("role")
        if role != "controller":
            raise ExecutionError(
                f"Step {self.id!r} applies only to the 'controller' role, got {role!r}"
            )

    def dry_run_preview(self) -> StepPreview:
        """File diff + commands ``apply()`` would run, with all secrets masked."""
        notes: list[str] = []
        try:
            admin_password, db_password, placement_password = _load_placement_secrets(self.root)
        except PreconditionError as exc:
            admin_password = _DRY_RUN_PLACEHOLDER_SECRET
            db_password = _DRY_RUN_PLACEHOLDER_SECRET
            placement_password = _DRY_RUN_PLACEHOLDER_SECRET
            notes.append(f"Precondition (checked by apply()): {exc}")

        file_changes: list[FileChange] = []
        path, display = resolve_target(self.root, PLACEMENT_CONF_TARGET)
        if _read_existing_conf(path, display) is None:
            rendered = "\n".join(
                f"[{sec}] {k} = {_mask_secrets(v, db_password, placement_password)}"
                for sec, k, v in placement_conf_settings(db_password, placement_password)
            )
            notes.append(
                f"{display} does not exist yet (created when {PLACEMENT_PACKAGE} is installed); "
                f"apply() will install {PLACEMENT_PACKAGE} first and then set:\n{rendered}"
            )
        else:
            file_changes.append(
                edit_placement_conf(self.root, db_password, placement_password, dry_run=True)
            )

        notes.extend(
            (
                SQL_STDIN_NOTE,
                KEYSTONE_PROVISIONING_NOTE,
                DB_SYNC_ROOT_NOTE,
                PACKAGE_NOTE,
                NOT_CHECKED,
            )
        )
        commands = (
            plan_service_database_sql((PLACEMENT_DB_NAME,), PLACEMENT_DB_USER, db_password),
            PlannedCommand(
                OPENSTACK_USER_CREATE,
                {
                    "admin_password": admin_password,
                    "user": PLACEMENT_SERVICE_USER,
                    "user_password": placement_password,
                },
            ),
            PlannedCommand(
                OPENSTACK_ROLE_ADD_USER_PROJECT,
                {
                    "admin_password": admin_password,
                    "project": SERVICE_PROJECT_NAME,
                    "user": PLACEMENT_SERVICE_USER,
                    "role": ADMIN_ROLE_NAME,
                },
            ),
            PlannedCommand(
                OPENSTACK_SERVICE_CREATE,
                {
                    "admin_password": admin_password,
                    "service_name": PLACEMENT_SERVICE_NAME,
                    "service_type": PLACEMENT_SERVICE_TYPE,
                },
            ),
            *(
                PlannedCommand(
                    OPENSTACK_ENDPOINT_CREATE,
                    {
                        "admin_password": admin_password,
                        "service_name": PLACEMENT_SERVICE_NAME,
                        "interface": iface,
                        "url": PLACEMENT_ENDPOINT_URL,
                    },
                )
                for iface in ENDPOINT_INTERFACES
            ),
            INSTALL_PLACEMENT_API,
            RUN_PLACEMENT_DB_SYNC,
            RESTART_APACHE2,
        )
        read_only_commands = (
            QUERY_PLACEMENT_PACKAGE,
            QUERY_APACHE2_ACTIVE,
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
        """True if secrets, placement.conf, package, apache2, MariaDB, and Keystone are ready."""
        try:
            admin_password, db_password, placement_password = _load_placement_secrets(self.root)
        except PreconditionError:
            return False
        try:
            if edit_placement_conf(
                self.root, db_password, placement_password, dry_run=True
            ).changed:
                return False
        except (PreconditionError, UnsafePathError, FileFormatError):
            return False
        if not package_installed(self.executor, PLACEMENT_PACKAGE):
            return False
        if unit_active_problems(self.executor, APACHE2_UNIT):
            return False
        if not service_database_ready(self.executor, (PLACEMENT_DB_NAME,), PLACEMENT_DB_USER):
            return False
        if not service_user_ready(self.executor, admin_password, PLACEMENT_SERVICE_USER):
            return False
        return service_catalog_ready(
            self.executor,
            admin_password,
            PLACEMENT_SERVICE_NAME,
            PLACEMENT_SERVICE_TYPE,
            PLACEMENT_ENDPOINT_URL,
        )

    def apply(self) -> None:
        """Provision MariaDB & Keystone, install placement-api, edit placement.conf, db sync,
        and restart apache2."""
        admin_password, db_password, placement_password = _load_placement_secrets(self.root)
        provision_service_database(
            self.executor, (PLACEMENT_DB_NAME,), PLACEMENT_DB_USER, db_password
        )
        ensure_service_user(
            self.executor, admin_password, PLACEMENT_SERVICE_USER, placement_password
        )
        ensure_service_catalog(
            self.executor,
            admin_password,
            PLACEMENT_SERVICE_NAME,
            PLACEMENT_SERVICE_TYPE,
            PLACEMENT_ENDPOINT_URL,
        )
        self.executor.run(INSTALL_PLACEMENT_API.command_id, INSTALL_PLACEMENT_API.params)
        edit_placement_conf(self.root, db_password, placement_password, dry_run=False)
        self.executor.run(RUN_PLACEMENT_DB_SYNC.command_id, RUN_PLACEMENT_DB_SYNC.params)
        self.executor.run(RESTART_APACHE2.command_id, RESTART_APACHE2.params)

    def verify(self) -> VerifyResult:
        """Verify secrets, placement.conf, package, apache2 unit, MariaDB, and Keystone."""
        problems: list[str] = []
        admin_password: str | None = None
        db_password: str | None = None
        placement_password: str | None = None
        try:
            admin_password, db_password, placement_password = _load_placement_secrets(self.root)
        except PreconditionError as exc:
            problems.append(str(exc))

        conf_path, conf_display = resolve_target(self.root, PLACEMENT_CONF_TARGET)
        try:
            conf_text = _read_existing_conf(conf_path, conf_display)
        except (UnsafePathError, FileFormatError) as exc:
            return VerifyResult(False, f"{exc}. {NOT_CHECKED}")
        if conf_text is None:
            problems.append(f"{conf_display} does not exist")
        elif db_password is not None and placement_password is not None:
            problems += verify_placement_conf_text(conf_text, db_password, placement_password)

        if not package_installed(self.executor, PLACEMENT_PACKAGE):
            problems.append(f"package {PLACEMENT_PACKAGE!r} is not installed")
        problems += unit_active_problems(self.executor, APACHE2_UNIT)
        problems += service_database_problems(
            self.executor, (PLACEMENT_DB_NAME,), PLACEMENT_DB_USER
        )
        if admin_password is not None:
            problems += service_user_problems(self.executor, admin_password, PLACEMENT_SERVICE_USER)
            problems += service_catalog_problems(
                self.executor,
                admin_password,
                PLACEMENT_SERVICE_NAME,
                PLACEMENT_SERVICE_TYPE,
                PLACEMENT_ENDPOINT_URL,
            )

        if problems:
            return VerifyResult(False, f"{'; '.join(problems)}. {NOT_CHECKED}")
        return VerifyResult(True, NOT_CHECKED)


def make_placement_step(
    root: Path,
    config: Mapping[str, Any],
    local_node: Mapping[str, Any],
    *,
    executor: CommandExecutor,
) -> PlacementStep:
    """Factory registered in :data:`openstack_forge.execution.steps.IMPLEMENTED_STEPS`."""
    return PlacementStep(root=root, config=config, local_node=local_node, executor=executor)
