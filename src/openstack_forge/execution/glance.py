"""The ``glance`` step (controller only).

**Unverified: this step has never been run on a real host.**

Source of truth
===============
Read directly from the official OpenStack 2025.1 (Epoxy) Glance installation guide for Ubuntu:

- https://docs.openstack.org/glance/2025.1/install/install-ubuntu.html
  (raw source: ``doc/source/install/install-ubuntu.rst`` and its includes
  ``doc/source/install/edit-glance-api-conf.rst``,
  ``doc/source/install/register-quotas.rst``, and
  ``doc/source/install/configure-quotas.rst`` on ``stable/2025.1`` in ``openstack/glance``).

What the official page prescribes
---------------------------------
1. **Prerequisites — SQL database** (``mysql`` as ``root``):
   - ``CREATE DATABASE glance;``
   - ``GRANT ALL PRIVILEGES ON glance.* TO 'glance'@'localhost' IDENTIFIED BY 'GLANCE_DBPASS';``
   - ``GRANT ALL PRIVILEGES ON glance.* TO 'glance'@'%' IDENTIFIED BY 'GLANCE_DBPASS';``
2. **Prerequisites — Keystone service credentials and API endpoints** (after ``. admin-openrc``):
   - ``openstack user create --domain default --password-prompt glance`` (``GLANCE_PASS``)
   - ``openstack role add --project service --user glance admin``
   - ``openstack service create --name glance --description "OpenStack Image" image``
   - ``openstack endpoint create --region RegionOne image public http://controller:9292``
   - ``openstack endpoint create --region RegionOne image internal http://controller:9292``
   - ``openstack endpoint create --region RegionOne image admin http://controller:9292``
   - *Optional* step 5 on the page (``register-quotas.rst``): registering unified quota limits
     in Keystone (``openstack --os-cloud devstack-system-admin registered limit create ...``).
3. **Install the packages**:
   - ``apt install glance``
4. **Edit ``/etc/glance/glance-api.conf``** (note: ``install-ubuntu.rst`` names
   ``/etc/glance/glance-api.conf``, while the ``.. path`` comments inside
   ``edit-glance-api-conf.rst`` contain the upstream typo ``/etc/glance/glance.conf``; the
   real file shipped by ``glance-common`` on Ubuntu 24.04 is ``/etc/glance/glance-api.conf``):
   - In ``[database]``:
     ``connection = mysql+pymysql://glance:GLANCE_DBPASS@controller/glance``
   - In ``[keystone_authtoken]``:
     - ``www_authenticate_uri = http://controller:5000``
     - ``auth_url = http://controller:5000``
     - ``memcached_servers = controller:11211``
     - ``auth_type = password``
     - ``project_domain_name = Default``
     - ``user_domain_name = Default``
     - ``project_name = service``
     - ``username = glance``
     - ``password = GLANCE_PASS``
   - In ``[paste_deploy]``:
     ``flavor = keystone``
   - In ``[DEFAULT]``, ``[glance_store]``, and ``[fs]``:
     - ``[DEFAULT] enabled_backends = fs:file``
     - ``[glance_store] default_backend = fs``
     - ``[fs] filesystem_store_datadir = /var/lib/glance/images/``
   - *Optional* unified quota section (``configure-quotas.rst``): ``[oslo_limit]`` and
     ``[DEFAULT] use_keystone_limits = True``.
5. **Populate the Image service database**:
   - ``su -s /bin/sh -c "glance-manage db_sync" glance``
6. **Finalize installation**:
   - ``service glance-api restart``

Ubuntu 24.04 + Epoxy Cloud Archive package facts (established read-only on the dev machine)
-------------------------------------------------------------------------------------------
- ``apt-cache policy glance glance-api glance-common python3-glance``:
  ``2:30.0.0-0ubuntu1~cloud1.1`` in ``noble-updates/epoxy/main`` (Ubuntu Cloud Archive for
  OpenStack 2025.1 Epoxy) and ``2:28.1.0-0ubuntu1.2`` in ``noble-updates/main``.
- ``apt-cache show glance glance-api``:
  - ``glance`` is a metapackage with ``Depends: glance-api (= 2:30.0.0-0ubuntu1~cloud1.1)``;
  - ``glance-api`` depends on ``python3-glance (= 2:30.0.0-0ubuntu1~cloud1.1)`` and
    ``glance-common``.
- ``dpkg -S`` confirms:
  - ``python3-glance: /usr/bin/glance-manage`` (entry point for ``glance.cmd.manage:main``);
  - ``glance-common: /usr/share/man/man1/glance-manage.1.gz`` (documents ``glance-manage
    db_sync``);
  - ``glance-common: /etc/glance/glance-api.conf`` (dpkg conffile MD5
    ``3e7b519df650970b3bca9e7cb16d19d0``; ``glance-common.postinst`` creates system user/group
    ``glance`` UID/GID ``64062``, sets ``/etc/glance`` to ``0750`` ``root:glance`` and files
    in ``/etc/glance`` to ``0640`` ``root:glance``, and sets ``/var/lib/glance`` to ``0750``
    ``glance:glance``);
  - ``glance-api: /usr/lib/systemd/system/glance-api.service`` (runs as ``User=glance`` /
    ``Group=glance``, ``WantedBy=multi-user.target``, enabled and started by
    ``glance-api.postinst``).

Deviations, trade-offs, and scope boundary
------------------------------------------
1. **SQL provisioning via ``stdin`` (``openstack_forge.execution.sql``)**:
   Uses ``CREATE DATABASE IF NOT EXISTS `glance`;`` and grants for ``'glance'@'localhost'``
   and ``'glance'@'%'`` piped over ``stdin`` to ``/usr/bin/mariadb --batch
   --skip-column-names`` so ``GLANCE_DBPASS`` never appears in ``argv`` or
   ``/proc/<pid>/cmdline`` (``ps``).
2. **Keystone provisioning via ``/usr/bin/openstack``
   (``openstack_forge.execution.openstack_cli``)**:
   - Uses :func:`ensure_service_user` (queries ``user list -f json`` first; runs ``user
     create`` if missing or ``user set --password`` if present; queries ``role assignment list
     -f json`` and adds ``admin`` on ``service`` if missing) and :func:`ensure_service_catalog`
     (queries ``service list -f json`` and ``endpoint list -f json`` first and creates only
     missing ``glance`` / ``image`` service and ``RegionOne`` ``http://controller:9292``
     endpoints so duplicate UUIDs are never created on reruns).
   - ``ADMIN_PASS`` is delivered via ``env_params={"admin_password": "OS_PASSWORD"}`` (never in
     ``argv``). Because ``--password-prompt`` in ``python3-openstackclient`` requires
     ``stdin.isatty()``, ``GLANCE_PASS`` is passed via ``--password {user_password}`` in
     ``openstack.user-create`` / ``openstack.user-set-password`` (masked as ``********``
     everywhere by OpenStack Forge, though visible in ``/proc/<pid>/cmdline`` for the duration
     of that subcommand).
3. **Optional Keystone unified quota limits (``[oslo_limit]`` / ``use_keystone_limits``) omitted**:
   ``install-ubuntu.rst`` marks step 5 (``register-quotas.rst``) as "(optional)" ("If you
   decide to use per-tenant quotas in Glance..."), ``configure-quotas.rst`` marks
   ``use_keystone_limits = True`` as optional, and ``register-quotas.rst`` assumes a DevStack
   system-admin profile (``--os-cloud devstack-system-admin``) and a dynamic endpoint UUID in
   ``[oslo_limit] endpoint_id``. Standard Glance image storage and Nova image boot do not
   require Keystone unified limits, so ``[oslo_limit]`` and ``use_keystone_limits`` are kept
   out of scope.
4. **``glance-manage db_sync`` run directly as ``root`` instead of ``su``**:
   ``CommandSpec`` forbids ``su`` and ``sudo``. Because ``/etc/glance/glance-api.conf`` is
   edited **before** ``glance-manage db_sync`` to point ``[database] connection`` at MariaDB
   (``mysql+pymysql://...``), running ``/usr/bin/glance-manage db_sync`` as ``root`` populates
   MariaDB without creating a root-owned SQLite file in ``/var/lib/glance``. Caveat: if
   ``log_file`` or ``log_dir`` is enabled in ``glance-api.conf``, running ``glance-manage`` as
   ``root`` can create a root-owned log file in ``/var/log/glance``.
5. **Non-interactive apt & systemd**:
   ``/usr/bin/apt-get install --yes glance`` instead of ``apt install glance``, and
   ``/usr/bin/systemctl restart glance-api`` instead of ``service glance-api restart``.
6. **What ``verify()`` does not check**:
   No HTTP request to ``http://controller:9292`` and no test image upload
   (``glance-verify.html``) are performed, and the Alembic schema revision inside the
   ``glance`` MariaDB database is not queried directly.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from openstack_forge.execution.commands import (
    APT_GET_INSTALL,
    GLANCE_MANAGE_DB_SYNC,
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

GLANCE_STEP_ID = "glance"
GLANCE_PACKAGE = "glance"
GLANCE_API_UNIT = "glance-api"

GLANCE_DB_NAME = "glance"
GLANCE_DB_USER = "glance"
GLANCE_DB_SECRET_NAME = "GLANCE_DBPASS"
GLANCE_SERVICE_SECRET_NAME = "GLANCE_PASS"
GLANCE_ADMIN_SECRET_NAME = "ADMIN_PASS"

GLANCE_SERVICE_USER = "glance"
GLANCE_SERVICE_NAME = "glance"
GLANCE_SERVICE_TYPE = "image"
GLANCE_ENDPOINT_URL = "http://controller:9292"

GLANCE_API_CONF_TARGET = "/etc/glance/glance-api.conf"
GLANCE_IMAGES_DATADIR = "/var/lib/glance/images/"

# Placeholder secret used ONLY to build a masked preview when secrets.json has not been
# initialized yet.
_DRY_RUN_PLACEHOLDER_SECRET = "A" * DEFAULT_SECRET_LENGTH

INSTALL_GLANCE = PlannedCommand(APT_GET_INSTALL, {"package": GLANCE_PACKAGE})
RUN_GLANCE_DB_SYNC = PlannedCommand(GLANCE_MANAGE_DB_SYNC)
RESTART_GLANCE_API = PlannedCommand(SYSTEMCTL_RESTART, {"unit": GLANCE_API_UNIT})

QUERY_GLANCE_PACKAGE = query_package(GLANCE_PACKAGE)
QUERY_GLANCE_API_ACTIVE = query_active(GLANCE_API_UNIT)

SQL_STDIN_NOTE = (
    "MariaDB provisioning: 'CREATE DATABASE IF NOT EXISTS `glance`;' and grants for "
    "'glance'@'localhost' and 'glance'@'%' are sent to '/usr/bin/mariadb --batch "
    "--skip-column-names' over stdin (stdin_param='sql') so GLANCE_DBPASS never appears in "
    "argv or /proc/<pid>/cmdline (ps)."
)
KEYSTONE_PROVISIONING_NOTE = (
    "Keystone provisioning: /usr/bin/openstack receives ADMIN_PASS via OS_PASSWORD "
    "(env_params, never in argv) and idempotently ensures the 'glance' user (with 'admin' "
    "role on project 'service'), the 'glance' ('image') service, and its public/internal/admin "
    "endpoints (http://controller:9292 in RegionOne). Because python3-openstackclient's "
    "--password-prompt requires a TTY, GLANCE_PASS is passed via --password in "
    "openstack.user-create / openstack.user-set-password (masked as ******** in logs/previews, "
    "but visible in /proc/<pid>/cmdline for the duration of that subcommand)."
)
QUOTA_SCOPE_NOTE = (
    "Scope decision: the guide's optional Keystone unified quota limits "
    "(register-quotas.rst / configure-quotas.rst: [oslo_limit] and "
    "[DEFAULT] use_keystone_limits = True) are omitted."
)
DB_SYNC_ROOT_NOTE = (
    "Deviation from guide: '/usr/bin/glance-manage db_sync' is run directly as root (after "
    "/etc/glance/glance-api.conf points [database] connection at MariaDB) instead of "
    "'su -s /bin/sh -c \"glance-manage db_sync\" glance' because CommandSpec forbids su."
)
PACKAGE_NOTE = (
    "Ubuntu 24.04 package facts (established read-only on the dev machine): glance "
    "2:30.0.0-0ubuntu1~cloud1.1 in cloud-archive:epoxy depends on glance-api, python3-glance, "
    "and glance-common (/etc/glance/glance-api.conf mode 0640 root:glance)."
)
NOT_CHECKED = (
    "Not checked: no HTTP request to http://controller:9292 and no test image upload are "
    "performed, and the Alembic schema version in the glance database is not queried directly."
)


def glance_db_connection(db_password: str) -> str:
    """Return the ``[database] connection`` URL for ``/etc/glance/glance-api.conf``."""
    return f"mysql+pymysql://{GLANCE_DB_USER}:{db_password}@controller/{GLANCE_DB_NAME}"


def glance_api_conf_settings(
    db_password: str, glance_password: str
) -> tuple[tuple[str, str, str], ...]:
    """Return the ``(section, key, value)`` settings for ``/etc/glance/glance-api.conf``."""
    return (
        ("database", "connection", glance_db_connection(db_password)),
        ("keystone_authtoken", "www_authenticate_uri", "http://controller:5000"),
        ("keystone_authtoken", "auth_url", "http://controller:5000"),
        ("keystone_authtoken", "memcached_servers", "controller:11211"),
        ("keystone_authtoken", "auth_type", "password"),
        ("keystone_authtoken", "project_domain_name", "Default"),
        ("keystone_authtoken", "user_domain_name", "Default"),
        ("keystone_authtoken", "project_name", SERVICE_PROJECT_NAME),
        ("keystone_authtoken", "username", GLANCE_SERVICE_USER),
        ("keystone_authtoken", "password", glance_password),
        ("paste_deploy", "flavor", "keystone"),
        ("DEFAULT", "enabled_backends", "fs:file"),
        ("glance_store", "default_backend", "fs"),
        ("fs", "filesystem_store_datadir", GLANCE_IMAGES_DATADIR),
    )


def _read_existing_conf(path: Path, display: str) -> str | None:
    """Read an existing config file, returning ``None`` if neither it nor its parent dir exists."""
    if not path.is_symlink() and not path.exists():
        return None
    return read_text_file(path, display)


def edit_glance_api_conf(
    root: Path, db_password: str, glance_password: str, *, dry_run: bool
) -> FileChange:
    """Apply the 2025.1 Ubuntu guide settings to ``/etc/glance/glance-api.conf``.

    The file comes from ``glance-common`` (installed by ``glance``) and is never created from
    scratch.
    """
    path, display = resolve_target(root, GLANCE_API_CONF_TARGET)
    if _read_existing_conf(path, display) is None:
        raise PreconditionError(
            f"{display} does not exist; it is created when {GLANCE_PACKAGE} is installed"
        )
    return ini_set_many(
        root,
        GLANCE_API_CONF_TARGET,
        glance_api_conf_settings(db_password, glance_password),
        dry_run=dry_run,
    )


def _mask_secrets(text: str, *secrets: str) -> str:
    masked = redact_text(text)
    for secret in secrets:
        if secret:
            masked = masked.replace(secret, MASK)
    return masked


def verify_glance_api_conf_text(text: str, db_password: str, glance_password: str) -> list[str]:
    """Pure check: all expected ``/etc/glance/glance-api.conf`` keys match expected values."""
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
    for section_name, key, expected in glance_api_conf_settings(db_password, glance_password):
        found = values.get((section_name, key), [])
        is_secret_key = key == "password"
        shown_expected = (
            MASK if is_secret_key else _mask_secrets(expected, db_password, glance_password)
        )
        if not found:
            problems.append(
                f"{key} is not set in [{section_name}] of {GLANCE_API_CONF_TARGET} "
                f"(expected {shown_expected!r})"
            )
            continue
        for actual in dict.fromkeys(found):
            if actual != expected:
                shown_actual = (
                    MASK if is_secret_key else _mask_secrets(actual, db_password, glance_password)
                )
                problems.append(
                    f"{key} is {shown_actual!r} in [{section_name}] of "
                    f"{GLANCE_API_CONF_TARGET}, expected {shown_expected!r}"
                )
    return problems


def _load_glance_secrets(root: Path) -> tuple[str, str, str]:
    """Load ``(ADMIN_PASS, GLANCE_DBPASS, GLANCE_PASS)`` from :class:`SecretStore`."""
    store = SecretStore(root)
    try:
        admin_password = store.get(GLANCE_ADMIN_SECRET_NAME)
        db_password = store.get(GLANCE_DB_SECRET_NAME)
        glance_password = store.get(GLANCE_SERVICE_SECRET_NAME)
    except (SecretError, UnsafePathError) as exc:
        raise PreconditionError(
            f"{GLANCE_STEP_ID} requires {GLANCE_ADMIN_SECRET_NAME}, "
            f"{GLANCE_DB_SECRET_NAME}, and {GLANCE_SERVICE_SECRET_NAME} in "
            f"{DEFAULT_SECRETS_TARGET}: {exc}. Run 'openstack-forge secrets init' first "
            "(the step never generates secrets itself)."
        ) from exc
    return admin_password, db_password, glance_password


@dataclass(frozen=True)
class GlanceStep:
    """``check`` / ``apply`` / ``verify`` for the ``glance`` catalog step."""

    root: Path
    config: Mapping[str, Any]
    local_node: Mapping[str, Any]
    executor: CommandExecutor
    id: str = GLANCE_STEP_ID
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
            admin_password, db_password, glance_password = _load_glance_secrets(self.root)
        except PreconditionError as exc:
            admin_password = _DRY_RUN_PLACEHOLDER_SECRET
            db_password = _DRY_RUN_PLACEHOLDER_SECRET
            glance_password = _DRY_RUN_PLACEHOLDER_SECRET
            notes.append(f"Precondition (checked by apply()): {exc}")

        file_changes: list[FileChange] = []
        path, display = resolve_target(self.root, GLANCE_API_CONF_TARGET)
        if _read_existing_conf(path, display) is None:
            rendered = "\n".join(
                f"[{sec}] {k} = {_mask_secrets(v, db_password, glance_password)}"
                for sec, k, v in glance_api_conf_settings(db_password, glance_password)
            )
            notes.append(
                f"{display} does not exist yet (created when {GLANCE_PACKAGE} is installed); "
                f"apply() will install {GLANCE_PACKAGE} first and then set:\n{rendered}"
            )
        else:
            file_changes.append(
                edit_glance_api_conf(self.root, db_password, glance_password, dry_run=True)
            )

        notes.extend(
            (
                SQL_STDIN_NOTE,
                KEYSTONE_PROVISIONING_NOTE,
                QUOTA_SCOPE_NOTE,
                DB_SYNC_ROOT_NOTE,
                PACKAGE_NOTE,
                NOT_CHECKED,
            )
        )
        commands = (
            plan_service_database_sql((GLANCE_DB_NAME,), GLANCE_DB_USER, db_password),
            PlannedCommand(
                OPENSTACK_USER_CREATE,
                {
                    "admin_password": admin_password,
                    "user": GLANCE_SERVICE_USER,
                    "user_password": glance_password,
                },
            ),
            PlannedCommand(
                OPENSTACK_ROLE_ADD_USER_PROJECT,
                {
                    "admin_password": admin_password,
                    "project": SERVICE_PROJECT_NAME,
                    "user": GLANCE_SERVICE_USER,
                    "role": ADMIN_ROLE_NAME,
                },
            ),
            PlannedCommand(
                OPENSTACK_SERVICE_CREATE,
                {
                    "admin_password": admin_password,
                    "service_name": GLANCE_SERVICE_NAME,
                    "service_type": GLANCE_SERVICE_TYPE,
                },
            ),
            *(
                PlannedCommand(
                    OPENSTACK_ENDPOINT_CREATE,
                    {
                        "admin_password": admin_password,
                        "service_name": GLANCE_SERVICE_NAME,
                        "interface": iface,
                        "url": GLANCE_ENDPOINT_URL,
                    },
                )
                for iface in ENDPOINT_INTERFACES
            ),
            INSTALL_GLANCE,
            RUN_GLANCE_DB_SYNC,
            RESTART_GLANCE_API,
        )
        read_only_commands = (
            QUERY_GLANCE_PACKAGE,
            QUERY_GLANCE_API_ACTIVE,
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
        """True if secrets, glance-api.conf, package, unit, MariaDB, and Keystone are ready."""
        try:
            admin_password, db_password, glance_password = _load_glance_secrets(self.root)
        except PreconditionError:
            return False
        try:
            if edit_glance_api_conf(self.root, db_password, glance_password, dry_run=True).changed:
                return False
        except (PreconditionError, UnsafePathError, FileFormatError):
            return False
        if not package_installed(self.executor, GLANCE_PACKAGE):
            return False
        if unit_active_problems(self.executor, GLANCE_API_UNIT):
            return False
        if not service_database_ready(self.executor, (GLANCE_DB_NAME,), GLANCE_DB_USER):
            return False
        if not service_user_ready(self.executor, admin_password, GLANCE_SERVICE_USER):
            return False
        return service_catalog_ready(
            self.executor,
            admin_password,
            GLANCE_SERVICE_NAME,
            GLANCE_SERVICE_TYPE,
            GLANCE_ENDPOINT_URL,
        )

    def apply(self) -> None:
        """Provision MariaDB & Keystone, install glance, edit glance-api.conf, db_sync, restart."""
        admin_password, db_password, glance_password = _load_glance_secrets(self.root)
        provision_service_database(self.executor, (GLANCE_DB_NAME,), GLANCE_DB_USER, db_password)
        ensure_service_user(self.executor, admin_password, GLANCE_SERVICE_USER, glance_password)
        ensure_service_catalog(
            self.executor,
            admin_password,
            GLANCE_SERVICE_NAME,
            GLANCE_SERVICE_TYPE,
            GLANCE_ENDPOINT_URL,
        )
        self.executor.run(INSTALL_GLANCE.command_id, INSTALL_GLANCE.params)
        edit_glance_api_conf(self.root, db_password, glance_password, dry_run=False)
        self.executor.run(RUN_GLANCE_DB_SYNC.command_id, RUN_GLANCE_DB_SYNC.params)
        self.executor.run(RESTART_GLANCE_API.command_id, RESTART_GLANCE_API.params)

    def verify(self) -> VerifyResult:
        """Verify secrets, glance-api.conf, package, glance-api unit, MariaDB, and Keystone."""
        problems: list[str] = []
        admin_password: str | None = None
        db_password: str | None = None
        glance_password: str | None = None
        try:
            admin_password, db_password, glance_password = _load_glance_secrets(self.root)
        except PreconditionError as exc:
            problems.append(str(exc))

        conf_path, conf_display = resolve_target(self.root, GLANCE_API_CONF_TARGET)
        try:
            conf_text = _read_existing_conf(conf_path, conf_display)
        except (UnsafePathError, FileFormatError) as exc:
            return VerifyResult(False, f"{exc}. {NOT_CHECKED}")
        if conf_text is None:
            problems.append(f"{conf_display} does not exist")
        elif db_password is not None and glance_password is not None:
            problems += verify_glance_api_conf_text(conf_text, db_password, glance_password)

        if not package_installed(self.executor, GLANCE_PACKAGE):
            problems.append(f"package {GLANCE_PACKAGE!r} is not installed")
        problems += unit_active_problems(self.executor, GLANCE_API_UNIT)
        problems += service_database_problems(self.executor, (GLANCE_DB_NAME,), GLANCE_DB_USER)
        if admin_password is not None:
            problems += service_user_problems(self.executor, admin_password, GLANCE_SERVICE_USER)
            problems += service_catalog_problems(
                self.executor,
                admin_password,
                GLANCE_SERVICE_NAME,
                GLANCE_SERVICE_TYPE,
                GLANCE_ENDPOINT_URL,
            )

        if problems:
            return VerifyResult(False, f"{'; '.join(problems)}. {NOT_CHECKED}")
        return VerifyResult(True, NOT_CHECKED)


def make_glance_step(
    root: Path,
    config: Mapping[str, Any],
    local_node: Mapping[str, Any],
    *,
    executor: CommandExecutor,
) -> GlanceStep:
    """Factory registered in :data:`openstack_forge.execution.steps.IMPLEMENTED_STEPS`."""
    return GlanceStep(root=root, config=config, local_node=local_node, executor=executor)
