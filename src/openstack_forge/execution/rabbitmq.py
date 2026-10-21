"""Install and configure RabbitMQ on the controller node.

Source of truth (read directly for this module):
- ``https://docs.openstack.org/install-guide/environment-messaging-ubuntu.html``
  (source: ``doc/install-guide/source/environment-messaging-ubuntu.rst`` in
  ``openstack/openstack-manuals``).

What the page gives for Ubuntu:
1. Install the package::

       # apt install rabbitmq-server

2. Add the ``openstack`` user (replacing ``RABBIT_PASS`` with a suitable password)::

       # rabbitmqctl add_user openstack RABBIT_PASS

3. Permit configuration, write, and read access for the ``openstack`` user::

       # rabbitmqctl set_permissions openstack ".*" ".*" ".*"

What was established on the Ubuntu 24.04 development machine (read-only inspection):
- ``apt-cache policy rabbitmq-server`` reports ``rabbitmq-server`` ``3.12.1-1ubuntu1.2``
  in ``noble-updates/main`` and ``noble-security/main`` (``3.12.1-1ubuntu1`` in
  ``noble/main``). No Ubuntu Cloud Archive repository is required to obtain it.
- ``dpkg -S rabbitmqctl`` maps to ``rabbitmq-server: /usr/sbin/rabbitmqctl``.
  ``/usr/sbin/rabbitmqctl`` is a symlink to
  ``../lib/rabbitmq/bin/rabbitmq-script-wrapper``. When run as ``root`` (``id -u = 0``),
  ``rabbitmq-script-wrapper`` executes::

      su rabbitmq -s /bin/sh -c "/usr/lib/rabbitmq/bin/rabbitmqctl ${CMDLINE}"

- ``/usr/lib/systemd/system/rabbitmq-server.service`` runs
  ``ExecStart=/usr/lib/rabbitmq/bin/rabbitmq-server`` as ``User=rabbitmq`` /
  ``Group=rabbitmq`` under ``[Install] WantedBy=multi-user.target``.
- ``/var/lib/dpkg/info/rabbitmq-server.postinst`` creates ``/var/lib/rabbitmq/.erlang.cookie``
  (mode ``0400``, owned by ``rabbitmq:rabbitmq``), installs ``/etc/rabbitmq/rabbitmq-env.conf``
  from ``/usr/share/rabbitmq/rabbitmq-env.conf`` (all ``#KEY=value`` lines commented out,
  default ``#NODENAME=rabbit``), and enables and starts ``rabbitmq-server.service`` on
  package ``configure`` via ``deb-systemd-helper`` / ``invoke-rc.d rabbitmq-server start``.
- ``rabbitmqctl(8)`` documents:
  - ``add_user username password``
  - ``change_password username newpassword``
  - ``set_permissions [-p vhost] user conf write read`` (``-p vhost`` defaults to ``"/"``)
  - ``list_users [--no-table-headers]`` ("Each result row will contain the user name
    followed by a list of the tags set for that user.")
  - ``list_user_permissions [--no-table-headers] username`` ("Each result row will contain
    the virtual host name, the user's configure permission, write permission, and read
    permission.")
- Assumption (unconfirmed on a live broker): because ``rabbitmqctl`` was not executed on
  the development machine, the exact tabular output format of ``list_users`` and
  ``list_user_permissions`` (optional ``Listing ...`` banner, optional ``user\\ttags`` or
  ``vhost\\tconfigure\\twrite\\tread`` header line, and whitespace/tab-separated columns)
  is taken from ``rabbitmqctl(8)`` and the examples in ``rabbitmqctl(8)`` rather than a live
  broker run.

Password delivery and ``/proc/<pid>/cmdline`` (``ps``) limitation:
- ``rabbitmqctl(8)`` on Ubuntu 24.04 (``3.12.1-1ubuntu1.2``) documents only the positional
  ``argv`` forms ``add_user username password`` and ``change_password username newpassword``;
  neither ``--stdin`` nor an environment variable is documented in ``rabbitmqctl(8)``, and
  ``/usr/lib/rabbitmq/bin/rabbitmq-script-wrapper`` interpolates ``${CMDLINE}`` into
  ``su rabbitmq -s /bin/sh -c "/usr/lib/rabbitmq/bin/rabbitmqctl ${CMDLINE}"``.
- OpenStack Forge declares ``password`` with parameter type ``"secret"`` (hence in
  ``sensitive_params``), registers ``RABBIT_PASS`` with
  :func:`openstack_forge.execution.redaction.register_secrets`, and masks it with
  ``********`` in ``build_shown_argv``, :class:`PlannedCommand`, dry-run output,
  :attr:`CommandResult.argv`, the audit log, and all error messages.
- However, because the real password must be passed in ``argv``, any local user on the
  machine can read ``RABBIT_PASS`` from the process table (``/proc/<pid>/cmdline`` / ``ps``)
  for the duration of ``rabbitmqctl add_user`` or ``rabbitmqctl change_password``. This
  OS-level limitation is stated explicitly in dry-run notes and docs.

Deviations from the guide page:
1. Non-interactive ``apt-get install --yes rabbitmq-server`` is used instead of interactive
   ``apt install rabbitmq-server``, per ``apt(8)`` ("SCRIPT USAGE").
2. Precondition on :class:`SecretStore`: ``RABBIT_PASS`` must already exist in
   ``/etc/openstack-forge/secrets.json`` (created by ``openstack-forge secrets init``).
   The step never generates or overwrites secrets itself; if the store or ``RABBIT_PASS``
   is missing or invalid, :meth:`RabbitMQStep.apply` raises an actionable
   :class:`PreconditionError` telling the operator to run ``openstack-forge secrets init``.
3. Idempotence when ``openstack`` already exists: ``rabbitmqctl add_user`` fails non-zero
   if the user already exists, and ``rabbitmqctl list_users`` shows user names and tags only
   (never password hashes). Therefore:
   - :meth:`RabbitMQStep.check` runs read-only checks (``RABBIT_PASS`` in ``SecretStore``,
     ``dpkg-query --status rabbitmq-server``, ``systemctl is-active rabbitmq-server``,
     ``rabbitmqctl list_users``, and ``rabbitmqctl list_user_permissions openstack``) so a
     second run on an already-configured host changes nothing.
   - In :meth:`RabbitMQStep.apply`, after ``apt-get install --yes rabbitmq-server``, the
     step runs ``rabbitmqctl list_users``: if ``openstack`` is absent it runs
     ``rabbitmqctl add_user openstack {password}``, whereas if ``openstack`` already exists
     it converges the password explicitly with
     ``rabbitmqctl change_password openstack {password}``, then runs
     ``rabbitmqctl set_permissions openstack ".*" ".*" ".*"``.
   - Trade-off: converging with ``change_password`` ensures that a half-applied step (for
     example ``add_user`` succeeded before ``set_permissions`` failed, or ``RABBIT_PASS``
     was rotated in ``secrets.json``) leaves the broker's ``openstack`` password matching
     ``secrets.json``, rather than silently leaving a stale password or failing hard on a
     rerun.
4. Active-service check: the guide page has no ``systemctl`` command because the Debian
   package ``postinst`` starts ``rabbitmq-server.service`` during ``apt-get install``.
   The step checks ``systemctl is-active rabbitmq-server`` in ``check()`` and ``verify()``
   without restarting the service.

Hostname resolution and security notes:
- RabbitMQ names its local Erlang node ``rabbit@<short-hostname>`` by default and connects
  to it over Erlang distribution using ``/var/lib/rabbitmq/.erlang.cookie``. If the short
  hostname does not resolve (for example ``controller`` is missing from ``/etc/hosts``),
  both ``rabbitmq-server.service`` and ``rabbitmqctl`` fail. Apply the ``hosts-file`` step
  before ``rabbitmq``; this step never modifies ``/etc/hosts``.
- As on the OpenStack installation guide page, RabbitMQ listens for AMQP on TCP port 5672
  without TLS; the management network must be trusted.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar

from openstack_forge.execution.commands import (
    APT_GET_INSTALL,
    RABBITMQCTL_ADD_USER,
    RABBITMQCTL_CHANGE_PASSWORD,
    RABBITMQCTL_LIST_USER_PERMISSIONS,
    RABBITMQCTL_LIST_USERS,
    RABBITMQCTL_SET_PERMISSIONS,
    CommandExecutor,
)
from openstack_forge.execution.engine import VerifyResult
from openstack_forge.execution.errors import (
    ExecutionError,
    PreconditionError,
    SecretError,
    UnexpectedExitError,
    UnsafePathError,
)
from openstack_forge.execution.preview import PlannedCommand, StepPreview
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

RABBITMQ_STEP_ID = "rabbitmq"
RABBITMQ_PACKAGE = "rabbitmq-server"
RABBITMQ_UNIT = "rabbitmq-server"
RABBITMQ_USER = "openstack"
RABBITMQ_SECRET_NAME = "RABBIT_PASS"
RABBITMQ_VHOST = "/"
RABBITMQ_PERMISSIONS: tuple[str, str, str] = (".*", ".*", ".*")
RABBITMQ_PORT = 5672

# Placeholder secret used ONLY to build a masked PlannedCommand preview when secrets.json
# has not been initialized yet; build_shown_argv replaces it with ********.
_DRY_RUN_PLACEHOLDER_SECRET = "A" * DEFAULT_SECRET_LENGTH

INSTALL_RABBITMQ = PlannedCommand(APT_GET_INSTALL, {"package": RABBITMQ_PACKAGE})
SET_RABBITMQ_PERMISSIONS = PlannedCommand(RABBITMQCTL_SET_PERMISSIONS)

QUERY_RABBITMQ_PACKAGE = query_package(RABBITMQ_PACKAGE)
QUERY_RABBITMQ_ACTIVE = query_active(RABBITMQ_UNIT)
QUERY_RABBITMQ_USERS = PlannedCommand(RABBITMQCTL_LIST_USERS)
QUERY_RABBITMQ_PERMISSIONS = PlannedCommand(RABBITMQCTL_LIST_USER_PERMISSIONS)

PACKAGE_NOTE = (
    "Ubuntu 24.04 package facts (established read-only on the dev machine): "
    "rabbitmq-server 3.12.1-1ubuntu1.2 is in noble-updates/main and noble-security/main; "
    "/usr/sbin/rabbitmqctl is a symlink to ../lib/rabbitmq/bin/rabbitmq-script-wrapper; "
    "/usr/lib/systemd/system/rabbitmq-server.service runs /usr/lib/rabbitmq/bin/rabbitmq-server "
    "as User=rabbitmq, and rabbitmq-server.postinst enables and starts rabbitmq-server.service "
    "on package configure."
)
PS_EXPOSURE_NOTE = (
    "Password delivery limitation: rabbitmqctl(8) only accepts the password in argv "
    "('add_user openstack <password>' / 'change_password openstack <password>'). OpenStack "
    "Forge masks RABBIT_PASS as '********' in dry-run output, PlannedCommand, CommandResult.argv, "
    "the audit log, and error messages, but local users on the host can read /proc/<pid>/cmdline "
    "(ps) for the duration of the rabbitmqctl command."
)
HOSTNAME_NOTE = (
    "Hostname resolution: RabbitMQ names its local Erlang node 'rabbit@<short-hostname>' by "
    "default; if the short hostname does not resolve in /etc/hosts, rabbitmq-server.service "
    "and rabbitmqctl fail. Apply the 'hosts-file' step first (this step does not edit "
    "/etc/hosts)."
)
CONVERGENCE_NOTE = (
    "Idempotence: apply() runs 'rabbitmqctl list_users' after installing rabbitmq-server; if "
    "user 'openstack' does not exist it runs 'rabbitmqctl add_user openstack ********', and if "
    "'openstack' already exists it converges the password with "
    "'rabbitmqctl change_password openstack ********' before running 'set_permissions'."
)
OUTPUT_FORMAT_NOTE = (
    "Output format assumption (unconfirmed on a live broker): rabbitmqctl was not executed on "
    "the dev machine; parsing of 'list_users' and 'list_user_permissions' follows the row "
    "descriptions and examples in rabbitmqctl(8)."
)
SECURITY_NOTE = (
    f"Security: as on the OpenStack installation guide page, RabbitMQ listens for AMQP on "
    f"TCP port {RABBITMQ_PORT} without TLS; the management network must be trusted. No "
    "firewall rule is changed."
)
NOT_CHECKED = (
    f"Not checked: no AMQP login test with {RABBITMQ_SECRET_NAME} and no TCP check on port "
    f"{RABBITMQ_PORT} are performed; the exact tabular output format of rabbitmqctl "
    "list_users / list_user_permissions is an assumption from rabbitmqctl(8) not confirmed on "
    "a live broker; if rabbitmqctl fails on the real host, verify that the short hostname "
    "resolves via the 'hosts-file' step."
)


def _plan_add_user(password: str) -> PlannedCommand:
    return PlannedCommand(RABBITMQCTL_ADD_USER, {"password": password})


def _load_rabbit_pass(root: Path) -> str:
    """Return validated ``RABBIT_PASS`` from :class:`SecretStore` or raise PreconditionError."""
    store = SecretStore(root)
    try:
        return store.get(RABBITMQ_SECRET_NAME)
    except (SecretError, UnsafePathError) as exc:
        raise PreconditionError(
            f"{RABBITMQ_STEP_ID} requires {RABBITMQ_SECRET_NAME} in {DEFAULT_SECRETS_TARGET}: "
            f"{exc}. Run 'openstack-forge secrets init' first (the step never generates secrets "
            "itself)."
        ) from exc


def parse_rabbitmq_users(stdout: str) -> set[str]:
    """Extract user names from ``rabbitmqctl list_users`` output.

     Per ``rabbitmqctl(8)``, each result row contains the user name followed by a list of
    tags (for example ``openstack\\t[]``), optionally preceded by a ``Listing users ...``
    banner and/or a ``user\\ttags`` table header.
    """
    users: set[str] = set()
    for raw_line in stdout.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("Listing "):
            continue
        fields = line.split()
        if not fields:  # pragma: no cover - defensive
            continue
        if fields[0] == "user" and (len(fields) == 1 or fields[1] == "tags"):
            continue
        users.add(fields[0])
    return users


def parse_rabbitmq_user_permissions(stdout: str) -> dict[str, tuple[str, str, str]]:
    """Extract ``{vhost: (conf, write, read)}`` from ``rabbitmqctl list_user_permissions``.

    Per ``rabbitmqctl(8)``, each result row contains ``vhost``, ``configure``, ``write``,
    and ``read`` permissions (for example ``/\\t.*\\t.*\\t.*``), optionally preceded by a
    ``Listing permissions ...`` banner and/or a ``vhost\\tconfigure\\twrite\\tread`` header.
    """
    permissions: dict[str, tuple[str, str, str]] = {}
    for raw_line in stdout.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("Listing "):
            continue
        fields = line.split()
        if len(fields) != 4:
            continue
        if tuple(fields) == ("vhost", "configure", "write", "read"):
            continue
        vhost, conf, write, read = fields
        permissions[vhost] = (conf, write, read)
    return permissions


def verify_rabbitmq_permissions_text(stdout: str) -> list[str]:
    """Return human-readable problems if ``stdout`` lacks ``/ -> .* .* .*`` permissions."""
    perms = parse_rabbitmq_user_permissions(stdout)
    actual = perms.get(RABBITMQ_VHOST)
    if actual is None:
        return [f"user {RABBITMQ_USER!r} has no permissions in vhost {RABBITMQ_VHOST!r}"]
    if actual != RABBITMQ_PERMISSIONS:
        shown_actual = " ".join(actual)
        shown_expected = " ".join(RABBITMQ_PERMISSIONS)
        return [
            f"user {RABBITMQ_USER!r} permissions in vhost {RABBITMQ_VHOST!r} are "
            f"'{shown_actual}', expected '{shown_expected}'"
        ]
    return []


@dataclass(frozen=True)
class RabbitMQStep:
    """Executable ``rabbitmq`` step (controller only; secret from ``SecretStore``)."""

    root: Path
    config: Mapping[str, Any]
    local_node: Mapping[str, Any]
    executor: CommandExecutor
    id: str = RABBITMQ_STEP_ID
    declares_commands: ClassVar[bool] = True

    def __post_init__(self) -> None:
        object.__setattr__(self, "root", Path(self.root))
        if self.local_node["role"] != "controller":
            raise ExecutionError(
                f"{self.id} only runs on the controller, not on "
                f"{self.local_node['hostname']!r} ({self.local_node['role']})"
            )

    def dry_run_preview(self) -> StepPreview:
        """Preview the commands and operational/security notes without exposing RABBIT_PASS."""
        notes: list[str] = []
        try:
            password = _load_rabbit_pass(self.root)
        except PreconditionError as exc:
            password = _DRY_RUN_PLACEHOLDER_SECRET
            notes.append(f"Precondition not yet met: {exc}")
        add_user_cmd = _plan_add_user(password)
        notes += [
            CONVERGENCE_NOTE,
            PS_EXPOSURE_NOTE,
            HOSTNAME_NOTE,
            PACKAGE_NOTE,
            OUTPUT_FORMAT_NOTE,
            SECURITY_NOTE,
            "apt-get install downloads from the configured apt sources (network).",
        ]
        return StepPreview(
            file_changes=(),
            commands=(INSTALL_RABBITMQ, add_user_cmd, SET_RABBITMQ_PERMISSIONS),
            read_only_commands=(
                QUERY_RABBITMQ_PACKAGE,
                QUERY_RABBITMQ_ACTIVE,
                QUERY_RABBITMQ_USERS,
                QUERY_RABBITMQ_PERMISSIONS,
            ),
            notes=tuple(notes),
        )

    def check(self) -> bool:
        """True if RABBIT_PASS exists, rabbitmq-server is installed/active, and user/perms match."""
        try:
            _load_rabbit_pass(self.root)
        except PreconditionError:
            return False
        if not package_installed(self.executor, RABBITMQ_PACKAGE):
            return False
        if unit_active_problems(self.executor, RABBITMQ_UNIT):
            return False
        try:
            users_res = self.executor.run(RABBITMQCTL_LIST_USERS)
        except UnexpectedExitError:
            return False
        if RABBITMQ_USER not in parse_rabbitmq_users(users_res.stdout):
            return False
        try:
            perms_res = self.executor.run(RABBITMQCTL_LIST_USER_PERMISSIONS)
        except UnexpectedExitError:
            return False
        return not verify_rabbitmq_permissions_text(perms_res.stdout)

    def apply(self) -> None:
        """Check RABBIT_PASS, install rabbitmq-server, create/converge user, set permissions."""
        password = _load_rabbit_pass(self.root)
        self.executor.run(INSTALL_RABBITMQ.command_id, INSTALL_RABBITMQ.params)
        users_res = self.executor.run(RABBITMQCTL_LIST_USERS)
        if RABBITMQ_USER in parse_rabbitmq_users(users_res.stdout):
            self.executor.run(RABBITMQCTL_CHANGE_PASSWORD, {"password": password})
        else:
            self.executor.run(RABBITMQCTL_ADD_USER, {"password": password})
        self.executor.run(SET_RABBITMQ_PERMISSIONS.command_id, SET_RABBITMQ_PERMISSIONS.params)

    def verify(self) -> VerifyResult:
        """Verify package installed, unit active, user 'openstack' exists, and permissions match."""
        problems: list[str] = []
        try:
            _load_rabbit_pass(self.root)
        except PreconditionError as exc:
            problems.append(str(exc))
        pkg_installed = package_installed(self.executor, RABBITMQ_PACKAGE)
        if not pkg_installed:
            problems.append(f"{RABBITMQ_PACKAGE} is not installed")
        active_problems = unit_active_problems(self.executor, RABBITMQ_UNIT)
        problems += active_problems

        user_exists = False
        if pkg_installed and not active_problems:
            try:
                users_res = self.executor.run(RABBITMQCTL_LIST_USERS)
            except UnexpectedExitError as exc:
                problems.append(f"rabbitmqctl list_users failed: {exc}")
            else:
                if RABBITMQ_USER in parse_rabbitmq_users(users_res.stdout):
                    user_exists = True
                else:
                    problems.append(f"RabbitMQ user {RABBITMQ_USER!r} does not exist")

            if user_exists:
                try:
                    perms_res = self.executor.run(RABBITMQCTL_LIST_USER_PERMISSIONS)
                except UnexpectedExitError as exc:
                    problems.append(f"rabbitmqctl list_user_permissions failed: {exc}")
                else:
                    problems += verify_rabbitmq_permissions_text(perms_res.stdout)

        if problems:
            return VerifyResult(False, f"{'; '.join(problems)}. {NOT_CHECKED}")
        return VerifyResult(
            True,
            f"{RABBITMQ_PACKAGE} is installed; {RABBITMQ_UNIT} is active; RabbitMQ user "
            f"{RABBITMQ_USER!r} exists with permissions '.* .* .*' in vhost {RABBITMQ_VHOST!r}. "
            f"{NOT_CHECKED}",
        )


def make_rabbitmq_step(
    root: Path,
    config: Mapping[str, Any],
    local_node: Mapping[str, Any],
    *,
    executor: CommandExecutor,
) -> RabbitMQStep:
    return RabbitMQStep(root=Path(root), config=config, local_node=local_node, executor=executor)
