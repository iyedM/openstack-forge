"""Allowlisted, shell-free command execution for future mutating steps.

A command can only run if a :class:`CommandSpec` for it is registered in a
:class:`CommandRegistry`; callers pass a command id and typed parameters, never a command
string. Parameters are validated before anything runs, argv is never passed to a shell,
the environment is replaced by a minimal one, output is redacted then truncated, and a
mutating command refuses to run unless the root is ``/`` and the process is root (sudo is
never used). Every executed command is recorded in an :class:`AuditLog`.

:data:`DEFAULT_REGISTRY` holds exactly the reviewed :data:`SYSTEM_COMMANDS`.
"""

from __future__ import annotations

import ipaddress
import os
import re
import shlex
import signal
import subprocess
import time
import uuid
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Protocol

from openstack_forge.execution.audit import AuditLog
from openstack_forge.execution.engine import VerifyResult
from openstack_forge.execution.errors import (
    CommandError,
    CommandNotFoundError,
    CommandSpecError,
    CommandTimeoutError,
    MutationRefusedError,
    ParameterError,
    UnexpectedExitError,
    UnknownCommandError,
)
from openstack_forge.execution.redaction import MASK, redact_text

# The complete environment of every command; nothing is inherited from the caller.
SANITIZED_ENV: Mapping[str, str] = {
    "PATH": "/usr/sbin:/usr/bin:/sbin:/bin",
    "LC_ALL": "C",
    "LANG": "C",
    "DEBIAN_FRONTEND": "noninteractive",
}
DEFAULT_OUTPUT_LIMIT = 64 * 1024  # characters kept per stream
TRUNCATION_MARKER = "[... output truncated: {count} earlier characters removed ...]\n"

# Executables that would reintroduce a shell, an arbitrary program, or privilege changes.
FORBIDDEN_EXECUTABLES: frozenset[str] = frozenset(
    {
        "sh", "bash", "dash", "zsh", "ksh", "mksh", "csh", "tcsh", "fish", "busybox",
        "env", "xargs", "nohup", "setsid", "nice", "timeout", "script",
        "sudo", "su", "doas", "pkexec", "runuser", "chroot",
    }
)  # fmt: skip

# Rejected in every parameter value, whatever its type, before the type's own regex runs.
_FORBIDDEN_CHARACTERS = frozenset(";|&$`\"'\\<>(){}[]*?!#~%^,")
_PLACEHOLDER_RE = re.compile(r"^\{([a-z][a-z0-9_]*)\}$")
_COMMAND_ID_RE = re.compile(r"^[a-z0-9]+(?:[-.][a-z0-9]+)*$")
_ENV_VAR_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")


# --- parameter types ------------------------------------------------------------------


@dataclass(frozen=True)
class ParamType:
    """A named parameter type: a strict full-match regex plus an optional extra check."""

    name: str
    pattern: re.Pattern[str]
    description: str
    extra_check: Callable[[str], bool] | None = None

    def is_valid(self, value: str) -> bool:
        if not self.pattern.fullmatch(value):
            return False
        return self.extra_check is None or self.extra_check(value)


def _is_ipv4(value: str) -> bool:
    try:
        ipaddress.IPv4Address(value)
    except ValueError:
        return False
    return True


def _is_clean_absolute_path(value: str) -> bool:
    # Inspect the raw segments: PurePosixPath would silently collapse "." components.
    return all(part not in ("", ".", "..") for part in value.split("/")[1:])


MAX_SQL_STDIN_LENGTH = 4096
_ENDPOINT_URL_RE = re.compile(
    r"https?://(?P<host>[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?)"
    r":(?P<port>[1-9][0-9]{0,4})(?P<path>(?:/[A-Za-z0-9._-]+)*/?)"
)


def _is_valid_sql_stdin(value: str) -> bool:
    if not (1 <= len(value) <= MAX_SQL_STDIN_LENGTH):
        return False
    return all(char == "\n" or (" " <= char <= "~") for char in value)


def _is_valid_endpoint_url(value: str) -> bool:
    match = _ENDPOINT_URL_RE.fullmatch(value)
    if match is None:
        return False
    port = int(match["port"])
    if not (1 <= port <= 65535):
        return False
    host = match["host"]
    if ".." in host or any(
        not label or label.startswith("-") or label.endswith("-") for label in host.split(".")
    ):
        return False
    path = match["path"]
    return all(part not in (".", "..") for part in path.split("/") if part)


PARAM_TYPES: Mapping[str, ParamType] = {
    param.name: param
    for param in (
        ParamType(
            "systemd_unit",
            re.compile(r"[A-Za-z0-9][A-Za-z0-9_.@:-]{0,254}"),
            'a systemd unit name such as "chrony" or "chrony.service"',
        ),
        ParamType(
            "package_name",
            re.compile(r"[a-z0-9][a-z0-9+.-]{1,127}"),
            'a Debian package name such as "python3-openstackclient"',
        ),
        ParamType(
            "cloud_archive_pocket",
            re.compile(r"cloud-archive:[a-z]+(?:-(?:updates|proposed))?"),
            'an Ubuntu Cloud Archive pocket such as "cloud-archive:epoxy"',
        ),
        ParamType(
            "hostname",
            re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"),
            "a single DNS label",
        ),
        ParamType(
            "ipv4_address",
            re.compile(r"(?:[0-9]{1,3}\.){3}[0-9]{1,3}"),
            "a dotted-quad IPv4 address",
            extra_check=_is_ipv4,
        ),
        ParamType(
            "absolute_path",
            re.compile(r"(?:/[A-Za-z0-9._@+-]+)+"),
            'an absolute path without "." or ".." segments, such as "/etc/hosts"',
            extra_check=_is_clean_absolute_path,
        ),
        ParamType(
            "secret",
            re.compile(r"[A-Za-z0-9]{32,256}"),
            "an alphanumeric secret of at least 32 characters",
        ),
        ParamType(
            "sql_stdin",
            re.compile(r"[\x20-\x7e\n]{1,4096}"),
            "printable ASCII SQL statements separated by LF (at most 4096 characters)",
            extra_check=_is_valid_sql_stdin,
        ),
        ParamType(
            "openstack_name",
            re.compile(r"[a-z][a-z0-9_-]{0,62}"),
            'an OpenStack resource name or type such as "service", "glance", or "image"',
        ),
        ParamType(
            "endpoint_interface",
            re.compile(r"public|internal|admin"),
            'an OpenStack endpoint interface ("public", "internal", or "admin")',
        ),
        ParamType(
            "endpoint_url",
            _ENDPOINT_URL_RE,
            'an HTTP(S) endpoint URL such as "http://controller:9292" or "http://controller:8774/v2.1"',
            extra_check=_is_valid_endpoint_url,
        ),
    )
}


def validate_parameter(type_name: str, name: str, value: object, *, sensitive: bool = False) -> str:
    """Return ``value`` if it is a valid ``type_name`` string, else raise ParameterError."""
    param_type = PARAM_TYPES[type_name]
    if not isinstance(value, str):
        raise ParameterError(f"Parameter {name!r} must be a string, got {type(value).__name__}")
    is_secret = sensitive or type_name in ("secret", "sql_stdin")
    shown = repr(MASK) if is_secret else redact_text(repr(value))
    if not value:
        raise ParameterError(f"Parameter {name!r} must not be empty")
    if type_name == "sql_stdin":
        if not param_type.is_valid(value):
            raise ParameterError(
                f"Parameter {name!r} is not {param_type.description} ({type_name}): {shown}"
            )
        return value
    if value.startswith("-"):
        raise ParameterError(f"Parameter {name!r} must not start with '-': {shown}")
    if any(char.isspace() or not char.isprintable() for char in value):
        raise ParameterError(f"Parameter {name!r} contains whitespace or control characters")
    bad = sorted(set(value) & _FORBIDDEN_CHARACTERS)
    if bad:
        raise ParameterError(
            f"Parameter {name!r} contains forbidden character(s) {''.join(bad)!r}: {shown}"
        )
    if not param_type.is_valid(value):
        raise ParameterError(
            f"Parameter {name!r} is not {param_type.description} ({type_name}): {shown}"
        )
    return value


# --- specifications and registry -------------------------------------------------------


@dataclass(frozen=True)
class CommandSpec:
    """An allowlisted command: a fixed argv template with typed whole-argument parameters.

    Each ``argv`` element is either a literal or exactly ``{name}``; placeholders are never
    interpolated into part of an argument. ``argv[0]`` must be a literal absolute path.

    Sensitive parameters and delivery modes (``argv``, ``stdin_param``, ``env_params``):
    - Any parameter listed in ``sensitive_params`` (or declared with type ``"secret"``) is
      treated as a secret: its real value is passed to the process, while
      :meth:`build_shown_argv`, :meth:`build_shown_env`, :class:`PlannedCommand`, dry-run
      output, :attr:`CommandResult.argv`, the audit log, and :class:`ParameterError` /
      :class:`CommandError` messages replace it with :data:`MASK` (``********``).
    - ``stdin_param`` is an explicit per-spec opt-in to pass one sensitive parameter via
      standard input (``stdin=subprocess.PIPE``) instead of ``argv``. All other specs keep
      ``stdin=subprocess.DEVNULL``.
    - ``env_params`` is an explicit per-spec opt-in mapping ``{param_name: ENV_VAR_NAME}``
      to pass sensitive parameters through environment variables added to
      :data:`SANITIZED_ENV` instead of ``argv``. Each variable name must match
      ``^[A-Z][A-Z0-9_]*$`` and must not collide with :data:`SANITIZED_ENV` keys
      (``PATH``, ``LC_ALL``, ``LANG``, ``DEBIAN_FRONTEND``). On Linux, ``/proc/<pid>/environ``
      is readable only by the same user and ``root``, unlike ``/proc/<pid>/cmdline`` (``ps``)
      which is world-readable by default. Used by ``keystone-manage.bootstrap``
      (``OS_BOOTSTRAP_PASSWORD``) and the ``openstack.*`` specs (``OS_PASSWORD``).
    - Process-table limitation: when an external tool only accepts a password in ``argv``
      (and cannot read it from ``stdin``, an environment variable, or a ``0600`` file), any
      local user on the machine can read ``argv`` from ``/proc/<pid>/cmdline`` (``ps``) for
      the duration of the command, and OpenStack Forge cannot prevent this OS-level exposure.
      Prefer ``stdin_param``, ``env_params``, or a ``0600`` file whenever the tool supports it.
    """

    id: str
    argv: tuple[str, ...]
    params: Mapping[str, str] = field(default_factory=dict)
    timeout: float = 60.0
    allowed_exit_codes: frozenset[int] = frozenset({0})
    mutates: bool = True
    description: str = ""
    # Appended to the engine's suggested next action when this command fails.
    failure_hint: str = ""
    sensitive_params: frozenset[str] = frozenset()
    stdin_param: str | None = None
    env_params: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "argv", tuple(self.argv))
        object.__setattr__(self, "params", dict(self.params))
        object.__setattr__(self, "allowed_exit_codes", frozenset(self.allowed_exit_codes))
        object.__setattr__(self, "env_params", dict(self.env_params))
        if not _COMMAND_ID_RE.fullmatch(self.id):
            raise CommandSpecError(f"Invalid command id {self.id!r}")
        if not self.argv:
            raise CommandSpecError(f"Command {self.id!r} has an empty argv")
        executable = self.argv[0]
        if _PLACEHOLDER_RE.match(executable) or not executable.startswith("/"):
            raise CommandSpecError(
                f"Command {self.id!r}: argv[0] must be a literal absolute path, got {executable!r}"
            )
        if PurePosixPath(executable).name in FORBIDDEN_EXECUTABLES:
            raise CommandSpecError(
                f"Command {self.id!r}: {executable!r} is not allowed (no shell, env or sudo)"
            )
        if any(element == "" or "\0" in element for element in self.argv):
            raise CommandSpecError(f"Command {self.id!r} has an empty or NUL argv element")
        if any(PurePosixPath(e).name in {"sudo", "su", "doas", "pkexec"} for e in self.argv):
            raise CommandSpecError(f"Command {self.id!r} must not invoke sudo or su")
        used = [m[1] for e in self.argv if (m := _PLACEHOLDER_RE.match(e))]
        for element in self.argv:
            if not _PLACEHOLDER_RE.match(element) and ("{" in element or "}" in element):
                raise CommandSpecError(
                    f"Command {self.id!r}: placeholders must be whole arguments, got {element!r}"
                )
        unknown_types = sorted(t for t in self.params.values() if t not in PARAM_TYPES)
        if unknown_types:
            raise CommandSpecError(
                f"Command {self.id!r}: unknown parameter type(s) {unknown_types}"
            )
        explicit_sensitive = frozenset(self.sensitive_params)
        unknown_sensitive = sorted(explicit_sensitive - set(self.params))
        if unknown_sensitive:
            raise CommandSpecError(
                f"Command {self.id!r}: sensitive_params {unknown_sensitive} are not in params"
            )
        inferred_sensitive = explicit_sensitive | {
            name for name, type_name in self.params.items() if type_name in ("secret", "sql_stdin")
        }
        object.__setattr__(self, "sensitive_params", frozenset(inferred_sensitive))
        if self.stdin_param is not None:
            if not isinstance(self.stdin_param, str) or self.stdin_param not in self.params:
                raise CommandSpecError(
                    f"Command {self.id!r}: stdin_param {self.stdin_param!r} is not in params"
                )
            if self.stdin_param not in self.sensitive_params:
                raise CommandSpecError(
                    f"Command {self.id!r}: stdin_param {self.stdin_param!r} must be sensitive"
                )
            if self.stdin_param in used:
                raise CommandSpecError(
                    f"Command {self.id!r}: stdin_param {self.stdin_param!r} must not appear in argv"
                )
        for param_name, type_name in self.params.items():
            if type_name == "sql_stdin" and self.stdin_param != param_name:
                raise CommandSpecError(
                    f"Command {self.id!r}: parameter {param_name!r} of type 'sql_stdin' "
                    "is only allowed as stdin_param"
                )
        seen_env_vars: set[str] = set()
        for param_name, env_var in self.env_params.items():
            if not isinstance(param_name, str) or param_name not in self.params:
                raise CommandSpecError(
                    f"Command {self.id!r}: env_params parameter {param_name!r} is not in params"
                )
            if param_name not in self.sensitive_params:
                raise CommandSpecError(
                    f"Command {self.id!r}: env_params parameter {param_name!r} must be sensitive"
                )
            if param_name == self.stdin_param:
                raise CommandSpecError(
                    f"Command {self.id!r}: parameter {param_name!r} cannot be in both "
                    "stdin_param and env_params"
                )
            if param_name in used:
                raise CommandSpecError(
                    f"Command {self.id!r}: env_params parameter {param_name!r} must not "
                    "appear in argv"
                )
            if not isinstance(env_var, str) or not _ENV_VAR_RE.fullmatch(env_var):
                raise CommandSpecError(
                    f"Command {self.id!r}: invalid env_params variable name {env_var!r} "
                    f"(must match {_ENV_VAR_RE.pattern})"
                )
            if env_var in SANITIZED_ENV:
                raise CommandSpecError(
                    f"Command {self.id!r}: env_params variable {env_var!r} collides with "
                    "SANITIZED_ENV"
                )
            if env_var in seen_env_vars:
                raise CommandSpecError(
                    f"Command {self.id!r}: duplicate env_params variable {env_var!r}"
                )
            seen_env_vars.add(env_var)
        non_argv_params = ({self.stdin_param} if self.stdin_param is not None else set()) | set(
            self.env_params
        )
        expected_argv_params = set(self.params) - non_argv_params
        if sorted(set(used)) != sorted(expected_argv_params) or len(used) != len(set(used)):
            raise CommandSpecError(
                f"Command {self.id!r}: every declared parameter must appear exactly once in argv"
            )
        if not self.timeout > 0:
            raise CommandSpecError(f"Command {self.id!r}: timeout must be positive")
        if not self.allowed_exit_codes or not all(
            isinstance(code, int) and 0 <= code <= 255 for code in self.allowed_exit_codes
        ):
            raise CommandSpecError(f"Command {self.id!r}: invalid allowed exit codes")

    def _validate_values(self, values: Mapping[str, object]) -> dict[str, str]:
        missing = sorted(set(self.params) - set(values))
        unexpected = sorted(set(values) - set(self.params))
        if missing or unexpected:
            raise ParameterError(
                f"Command {self.id!r}: missing parameter(s) {missing}, unexpected {unexpected}"
            )
        return {
            name: validate_parameter(
                self.params[name],
                name,
                values[name],
                sensitive=name in self.sensitive_params,
            )
            for name in self.params
        }

    def build_argv(self, values: Mapping[str, object]) -> tuple[str, ...]:
        """Validate ``values`` against the declared parameters and fill the template."""
        checked = self._validate_values(values)
        return tuple(
            checked[m[1]] if (m := _PLACEHOLDER_RE.match(element)) else element
            for element in self.argv
        )

    def build_shown_argv(self, values: Mapping[str, object]) -> tuple[str, ...]:
        """Validate ``values`` and return ``argv`` with sensitive parameters masked."""
        checked = self._validate_values(values)
        shown: list[str] = []
        for element in self.argv:
            if m := _PLACEHOLDER_RE.match(element):
                name = m[1]
                shown.append(MASK if name in self.sensitive_params else redact_text(checked[name]))
            else:
                shown.append(redact_text(element))
        return tuple(shown)

    def build_env(self, values: Mapping[str, object]) -> dict[str, str]:
        """Validate ``values`` and return :data:`SANITIZED_ENV` plus any ``env_params``."""
        checked = self._validate_values(values)
        env = dict(SANITIZED_ENV)
        for param_name, env_var in self.env_params.items():
            env[env_var] = checked[param_name]
        return env

    def build_shown_env(self, values: Mapping[str, object]) -> dict[str, str]:
        """Validate ``values`` and return :data:`SANITIZED_ENV` with ``env_params`` masked."""
        self._validate_values(values)
        env = dict(SANITIZED_ENV)
        for env_var in self.env_params.values():
            env[env_var] = MASK
        return env

    def extract_stdin(self, values: Mapping[str, object]) -> str | None:
        """Validate ``values`` and return the ``stdin_param`` value, or ``None``."""
        checked = self._validate_values(values)
        if self.stdin_param is None:
            return None
        return checked[self.stdin_param]


class CommandRegistry:
    """The allowlist: only commands registered here can be executed."""

    def __init__(self, specs: Iterable[CommandSpec] = ()) -> None:
        self._specs: dict[str, CommandSpec] = {}
        for spec in specs:
            self.register(spec)

    def register(self, spec: CommandSpec) -> None:
        if spec.id in self._specs:
            raise CommandSpecError(f"Command {spec.id!r} is already registered")
        self._specs[spec.id] = spec

    def get(self, command_id: str) -> CommandSpec:
        try:
            return self._specs[command_id]
        except KeyError:
            known = ", ".join(sorted(self._specs)) or "none"
            raise UnknownCommandError(
                f"Command {command_id!r} is not in the allowlist (registered: {known})"
            ) from None

    def ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._specs))


# --- reviewed system commands ---------------------------------------------------------
#
# The only real commands OpenStack Forge may run. Each argv[0] is the literal path that
# the named Ubuntu 24.04 package installs (checked with `dpkg -S`), so PATH is never
# searched. Options are limited to ones justified by the tools' manual pages.
#
# The Python code never opens a network connection itself, but apt-get update,
# apt-get install and add-apt-repository download package lists and packages from the
# configured apt sources: running them uses the network.

APT_GET_INSTALL = "apt-get.install"
APT_GET_UPDATE = "apt-get.update"
ADD_APT_REPOSITORY_CLOUD_ARCHIVE = "add-apt-repository.cloud-archive"
APT_CACHE_POLICY = "apt-cache.policy"
SYSTEMCTL_RESTART = "systemctl.restart"
SYSTEMCTL_IS_ACTIVE = "systemctl.is-active"
SYSTEMCTL_ENABLE = "systemctl.enable"
SYSTEMCTL_IS_ENABLED = "systemctl.is-enabled"
DPKG_QUERY_STATUS = "dpkg-query.status"
RABBITMQCTL_ADD_USER = "rabbitmqctl.add-user"
RABBITMQCTL_CHANGE_PASSWORD = "rabbitmqctl.change-password"
RABBITMQCTL_SET_PERMISSIONS = "rabbitmqctl.set-permissions"
RABBITMQCTL_LIST_USERS = "rabbitmqctl.list-users"
RABBITMQCTL_LIST_USER_PERMISSIONS = "rabbitmqctl.list-user-permissions"
MARIADB_EXECUTE_SQL = "mariadb.execute-sql"
MARIADB_QUERY_SQL = "mariadb.query-sql"
KEYSTONE_MANAGE_DB_SYNC = "keystone-manage.db-sync"
KEYSTONE_MANAGE_FERNET_SETUP = "keystone-manage.fernet-setup"
KEYSTONE_MANAGE_CREDENTIAL_SETUP = "keystone-manage.credential-setup"
KEYSTONE_MANAGE_BOOTSTRAP = "keystone-manage.bootstrap"
OPENSTACK_PROJECT_LIST = "openstack.project-list"
OPENSTACK_PROJECT_CREATE_SERVICE = "openstack.project-create-service"
OPENSTACK_USER_LIST = "openstack.user-list"
OPENSTACK_USER_CREATE = "openstack.user-create"
OPENSTACK_USER_SET_PASSWORD = "openstack.user-set-password"
OPENSTACK_ROLE_ASSIGNMENT_LIST = "openstack.role-assignment-list"
OPENSTACK_ROLE_ADD_USER_PROJECT = "openstack.role-add-user-project"
OPENSTACK_SERVICE_LIST = "openstack.service-list"
OPENSTACK_SERVICE_CREATE = "openstack.service-create"
OPENSTACK_ENDPOINT_LIST = "openstack.endpoint-list"
OPENSTACK_ENDPOINT_CREATE = "openstack.endpoint-create"
GLANCE_MANAGE_DB_SYNC = "glance-manage.db-sync"
PLACEMENT_MANAGE_DB_SYNC = "placement-manage.db-sync"

# Fixed literal admin authentication flags for /usr/bin/openstack (package
# "python3-openstackclient", verified with `dpkg -S /usr/bin/openstack`).
# The admin password is supplied via env_params={"admin_password": "OS_PASSWORD"} so no
# shell sourcing of admin-openrc is needed and ADMIN_PASS never appears in argv.
OPENSTACK_ADMIN_AUTH_ARGS: tuple[str, ...] = (
    "/usr/bin/openstack",
    "--os-auth-url",
    "http://controller:5000/v3",
    "--os-project-domain-name",
    "Default",
    "--os-user-domain-name",
    "Default",
    "--os-project-name",
    "admin",
    "--os-username",
    "admin",
    "--os-identity-api-version",
    "3",
    "--os-interface",
    "internal",
)

APT_FAILURE_HINT = (
    "apt-get may have stopped part-way, leaving packages half-installed or "
    "half-configured: inspect the package state with 'dpkg --audit' before retrying. If "
    "apt-get reported that it could not get the dpkg lock, another package manager (for "
    "example unattended-upgrades) is running: wait for it to finish; do not delete lock "
    "files. OpenStack Forge does not attempt any automatic recovery."
)

APT_UPDATE_FAILURE_HINT = (
    "apt-get update downloads the package lists over the network: check that this node "
    "can reach its configured apt sources (DNS, proxy, firewall, mirror) and read the "
    "'E:'/'W:' lines above. If apt-get reported that it could not get a lock, another "
    "package manager (for example unattended-upgrades) is running: wait for it to finish; "
    "do not delete lock files. OpenStack Forge does not attempt any automatic recovery."
)

ADD_APT_REPOSITORY_FAILURE_HINT = (
    "add-apt-repository writes an apt source entry and then updates the package lists over "
    "the network: check whether the Ubuntu Cloud Archive entry was added (for example with "
    "'add-apt-repository --list') and whether this node can reach the Ubuntu Cloud Archive "
    "and its other apt sources. If a lock could not be obtained, another package manager "
    "is running: wait for it to finish; do not delete lock files. OpenStack Forge does not "
    "attempt any automatic recovery."
)

RABBITMQCTL_FAILURE_HINT = (
    "rabbitmqctl connects to the local RabbitMQ Erlang node ('rabbit@<short-hostname>') "
    "using /var/lib/rabbitmq/.erlang.cookie: check that 'systemctl is-active "
    "rabbitmq-server' is 'active' and that the short hostname resolves in /etc/hosts "
    "(apply the 'hosts-file' step first; see 'journalctl -u rabbitmq-server'). "
    "OpenStack Forge does not attempt any automatic recovery."
)

MARIADB_SQL_FAILURE_HINT = (
    "/usr/bin/mariadb connects to the local MariaDB server over its Unix socket using "
    "Ubuntu's default unix_socket root authentication: check that 'systemctl is-active "
    "mariadb' is 'active' and that the 'mariadb' step has been applied first (see "
    "'journalctl -u mariadb'). OpenStack Forge does not attempt any automatic recovery."
)

KEYSTONE_MANAGE_FAILURE_HINT = (
    "keystone-manage reads /etc/keystone/keystone.conf and connects to the MariaDB "
    "'keystone' database at 'controller': check that 'systemctl is-active mariadb' is "
    "'active', that 'controller' resolves in /etc/hosts ('hosts-file' step), and that "
    "/etc/keystone/keystone.conf has the expected [database] connection. OpenStack Forge "
    "does not attempt any automatic recovery."
)

OPENSTACK_CLI_FAILURE_HINT = (
    "/usr/bin/openstack authenticates as 'admin' against the Keystone Identity API at "
    "'http://controller:5000/v3': check that 'systemctl is-active apache2' is 'active', "
    "that 'controller' resolves in /etc/hosts ('hosts-file' step), that the 'keystone' "
    "step has been applied, and inspect /var/log/apache2/keystone.log (or "
    "'journalctl -u apache2'). OpenStack Forge does not attempt any automatic recovery."
)

GLANCE_MANAGE_FAILURE_HINT = (
    "glance-manage reads /etc/glance/glance-api.conf and connects to the MariaDB 'glance' "
    "database at 'controller': check that 'systemctl is-active mariadb' is 'active', that "
    "'controller' resolves in /etc/hosts ('hosts-file' step), and that "
    "/etc/glance/glance-api.conf has the expected [database] connection. OpenStack Forge "
    "does not attempt any automatic recovery."
)

PLACEMENT_MANAGE_FAILURE_HINT = (
    "placement-manage reads /etc/placement/placement.conf and connects to the MariaDB "
    "'placement' database at 'controller': check that 'systemctl is-active mariadb' is "
    "'active', that 'controller' resolves in /etc/hosts ('hosts-file' step), and that "
    "/etc/placement/placement.conf has the expected [placement_database] connection. "
    "OpenStack Forge does not attempt any automatic recovery."
)

SYSTEM_COMMANDS: tuple[CommandSpec, ...] = (
    CommandSpec(
        APT_GET_INSTALL,
        # /usr/bin/apt-get (package "apt"). apt(8), "SCRIPT USAGE", says scripts should use
        # apt-get rather than apt. --yes: apt-get(8) "assume yes to all prompts and run
        # non-interactively", still aborting on held, unauthenticated or essential packages.
        # DEBIAN_FRONTEND=noninteractive comes from the sanitized environment.
        ("/usr/bin/apt-get", "install", "--yes", "{package}"),
        {"package": "package_name"},
        timeout=900,
        mutates=True,
        description="Install one Debian package",
        failure_hint=APT_FAILURE_HINT,
    ),
    CommandSpec(
        APT_GET_UPDATE,
        # /usr/bin/apt-get (package "apt"). apt-get(8): "update is used to resynchronize
        # the package index files from their sources", fetched from the locations in the
        # apt sources: it USES THE NETWORK. It takes no option here: it asks no question,
        # and DIAGNOSTICS says it "returns zero on normal operation, decimal 100 on error".
        # It changes /var/lib/apt/lists, hence mutates=True (and the root/uid checks).
        ("/usr/bin/apt-get", "update"),
        timeout=600,
        mutates=True,
        description="Download the package lists from the configured apt sources (network)",
        failure_hint=APT_UPDATE_FAILURE_HINT,
    ),
    CommandSpec(
        ADD_APT_REPOSITORY_CLOUD_ARCHIVE,
        # /usr/bin/add-apt-repository (package "software-properties-common", NOT "apt").
        # add-apt-repository(1):
        # - "-y, --yes  Assume yes to all queries": the non-interactive option (stdin is
        #   /dev/null, so a prompt would otherwise fail).
        # - "-C, --cloud  Add an Ubuntu Cloud Archive. Must be in the format
        #   cloud-archive:CANAME ... If not specified, the pocket defaults to -updates."
        #   The guide's positional form (add-apt-repository cloud-archive:epoxy) is the
        #   LINE argument, which the man page calls "deprecated" and "not recommended as
        #   the autodetection of which line format is intended can be ambiguous".
        # - Does it run an update? Yes: "-n, --no-update  After adding the repository, do
        #   not update the package cache." means it updates by default. --no-update is not
        #   used, so it refreshes the package lists itself (over the network) and no
        #   separate apt-get update is needed after it.
        ("/usr/bin/add-apt-repository", "--yes", "--cloud", "{pocket}"),
        {"pocket": "cloud_archive_pocket"},
        timeout=600,
        mutates=True,
        description="Enable one Ubuntu Cloud Archive pocket, then update the package lists",
        failure_hint=ADD_APT_REPOSITORY_FAILURE_HINT,
    ),
    CommandSpec(
        APT_CACHE_POLICY,
        # /usr/bin/apt-cache (package "apt"). apt-cache(8): it "does not manipulate the
        # state of the system"; "policy [pkg...] ... prints out detailed information about
        # the priority selection of the named package" (its versions and the apt sources
        # providing them), from the local package lists: apt-cache "works independently of
        # the availability of the configured sources (e.g. offline)".
        # DIAGNOSTICS: "returns zero on normal operation, decimal 100 on error". An unknown
        # package still exits 0, with nothing on stdout and an "N:" notice on stderr.
        ("/usr/bin/apt-cache", "policy", "{package}"),
        {"package": "package_name"},
        timeout=60,
        mutates=False,
        description="Show the versions of one package and the apt sources providing them",
    ),
    CommandSpec(
        SYSTEMCTL_RESTART,
        # /usr/bin/systemctl (package "systemd"). systemctl(1): "restart: stop and then
        # start one or more units ... If the units are not running yet, they will be started."
        ("/usr/bin/systemctl", "restart", "{unit}"),
        {"unit": "systemd_unit"},
        timeout=120,
        mutates=True,
        description="Restart one systemd unit",
    ),
    CommandSpec(
        SYSTEMCTL_IS_ACTIVE,
        # systemctl(1): is-active "returns an exit code 0 if at least one is active, or
        # non-zero otherwise" and prints the unit state. The LSB codes 1-4 all mean "not
        # active"; callers must also check that the printed state is exactly "active".
        ("/usr/bin/systemctl", "is-active", "{unit}"),
        {"unit": "systemd_unit"},
        timeout=30,
        allowed_exit_codes={0, 1, 2, 3, 4},
        mutates=False,
        description="Report whether one systemd unit is active",
    ),
    CommandSpec(
        SYSTEMCTL_ENABLE,
        # systemctl(1): enable "will create a set of symlinks, as encoded in the [Install]
        # sections of the indicated unit files", then reloads the manager configuration;
        # it does "not have the effect of also starting" the unit. Required by the etcd page
        # ("systemctl enable etcd").
        ("/usr/bin/systemctl", "enable", "{unit}"),
        {"unit": "systemd_unit"},
        timeout=60,
        mutates=True,
        description="Enable one systemd unit at boot",
    ),
    CommandSpec(
        SYSTEMCTL_IS_ENABLED,
        # systemctl(1): is-enabled "returns an exit code of 0 if at least one is enabled,
        # non-zero otherwise" and prints the enable status. Its table gives 0 for several
        # statuses (static, alias, indirect, ...), "> 0" for others and 4 for not-found, so
        # callers must require the printed status to be exactly "enabled".
        ("/usr/bin/systemctl", "is-enabled", "{unit}"),
        {"unit": "systemd_unit"},
        timeout=30,
        allowed_exit_codes={0, 1, 2, 3, 4},
        mutates=False,
        description="Report whether one systemd unit is enabled",
    ),
    CommandSpec(
        DPKG_QUERY_STATUS,
        # /usr/bin/dpkg-query (package "dpkg"). dpkg-query(1): --status "displays the entry
        # in the installed package status database"; exit status 1 means "no ... package
        # being found", 2 is a fatal error.
        ("/usr/bin/dpkg-query", "--status", "{package}"),
        {"package": "package_name"},
        timeout=30,
        allowed_exit_codes={0, 1},
        mutates=False,
        description="Show the dpkg status entry of one package",
    ),
    CommandSpec(
        RABBITMQCTL_ADD_USER,
        # /usr/sbin/rabbitmqctl (package "rabbitmq-server", verified with `dpkg -S`).
        # Guide (https://docs.openstack.org/install-guide/environment-messaging-ubuntu.html):
        # "rabbitmqctl add_user openstack RABBIT_PASS".
        # rabbitmqctl(8), "User Management": "add_user username password". Neither --stdin
        # nor an environment variable is documented in rabbitmqctl(8) on Ubuntu 24.04
        # (rabbitmq-server 3.12.1-1ubuntu1.2), and /usr/sbin/rabbitmqctl is a symlink to
        # /usr/lib/rabbitmq/bin/rabbitmq-script-wrapper which re-invokes
        # `su rabbitmq -s /bin/sh -c "/usr/lib/rabbitmq/bin/rabbitmqctl ${CMDLINE}"`.
        # Therefore {password} must be passed in argv and is masked everywhere by OpenStack
        # Forge, while remaining visible in /proc/<pid>/cmdline (ps) for the duration of the
        # command.
        ("/usr/sbin/rabbitmqctl", "add_user", "openstack", "{password}"),
        {"password": "secret"},
        timeout=60,
        mutates=True,
        description="Create the RabbitMQ 'openstack' user",
        failure_hint=RABBITMQCTL_FAILURE_HINT,
    ),
    CommandSpec(
        RABBITMQCTL_CHANGE_PASSWORD,
        # /usr/sbin/rabbitmqctl (package "rabbitmq-server").
        # rabbitmqctl(8), "User Management": "change_password username newpassword".
        # Used when `rabbitmqctl list_users` shows that the 'openstack' user already exists
        # (`add_user` fails non-zero if the user already exists, and `list_users` does not
        # expose password hashes, so converging the password to RABBIT_PASS requires
        # `change_password`). Same argv / /proc/<pid>/cmdline limitation as add_user.
        ("/usr/sbin/rabbitmqctl", "change_password", "openstack", "{password}"),
        {"password": "secret"},
        timeout=60,
        mutates=True,
        description="Converge the password of the existing RabbitMQ 'openstack' user",
        failure_hint=RABBITMQCTL_FAILURE_HINT,
    ),
    CommandSpec(
        RABBITMQCTL_SET_PERMISSIONS,
        # /usr/sbin/rabbitmqctl (package "rabbitmq-server").
        # Guide (https://docs.openstack.org/install-guide/environment-messaging-ubuntu.html):
        # 'rabbitmqctl set_permissions openstack ".*" ".*" ".*"'.
        # rabbitmqctl(8), "Access Control": "set_permissions [-p vhost] user conf write read"
        # where "-p vhost" defaults to "/". The three regexes ".*" contain '*', which
        # parameter validation forbids, so they are kept as fixed literals in argv.
        ("/usr/sbin/rabbitmqctl", "set_permissions", "openstack", ".*", ".*", ".*"),
        timeout=60,
        mutates=True,
        description="Grant the RabbitMQ 'openstack' user configure, write, and read permissions",
        failure_hint=RABBITMQCTL_FAILURE_HINT,
    ),
    CommandSpec(
        RABBITMQCTL_LIST_USERS,
        # /usr/sbin/rabbitmqctl (package "rabbitmq-server").
        # rabbitmqctl(8), "User Management": "list_users [--no-table-headers]  Lists users.
        # Each result row will contain the user name followed by a list of the tags set for
        # that user." Read-only command used by check(), apply(), and verify() to inspect
        # whether the 'openstack' user exists.
        ("/usr/sbin/rabbitmqctl", "list_users"),
        timeout=30,
        mutates=False,
        description="List RabbitMQ users and their tags",
        failure_hint=RABBITMQCTL_FAILURE_HINT,
    ),
    CommandSpec(
        RABBITMQCTL_LIST_USER_PERMISSIONS,
        # /usr/sbin/rabbitmqctl (package "rabbitmq-server").
        # rabbitmqctl(8), "Access Control": "list_user_permissions [--no-table-headers]
        # username  Lists permissions of a user across all virtual hosts. Each result row
        # will contain the virtual host name, the user's configure permission, write
        # permission, and read permission." Read-only command used by check() and verify().
        ("/usr/sbin/rabbitmqctl", "list_user_permissions", "openstack"),
        timeout=30,
        mutates=False,
        description="List virtual-host permissions of the RabbitMQ 'openstack' user",
        failure_hint=RABBITMQCTL_FAILURE_HINT,
    ),
    CommandSpec(
        MARIADB_EXECUTE_SQL,
        # /usr/bin/mariadb (package "mariadb-client-core", verified with `dpkg -S`;
        # `/usr/bin/mysql -> mariadb` is a symlink on Ubuntu 24.04).
        # mariadb(1): "--batch, -B  Print results using tab as the column separator, with
        # each row on a new line. With this option, mysql does not use the history file."
        # "--skip-column-names, -N  Do not write column names in results."
        # SQL statements containing database passwords (`IDENTIFIED BY '<password>'`) are
        # fed via `stdin_param="sql"` (`stdin=subprocess.PIPE`) so neither the SQL nor any
        # password ever appears in `argv` or `/proc/<pid>/cmdline` (`ps`).
        ("/usr/bin/mariadb", "--batch", "--skip-column-names"),
        {"sql": "sql_stdin"},
        timeout=60,
        mutates=True,
        description="Execute idempotent SQL statements on the local MariaDB server via stdin",
        failure_hint=MARIADB_SQL_FAILURE_HINT,
        stdin_param="sql",
    ),
    CommandSpec(
        MARIADB_QUERY_SQL,
        # /usr/bin/mariadb (package "mariadb-client-core").
        # mariadb(1): "--execute=statement, -e statement  Execute the statement and quit.
        # Disables --force and history file."
        # Fixed literal read-only query inspecting existing databases
        # (`information_schema.SCHEMATA`) and database-level grants (`mysql.db`) with zero
        # caller parameters (`params={}`, `mutates=False`), reusable across all controller
        # service steps (keystone, glance, placement, nova, neutron, cinder).
        (
            "/usr/bin/mariadb",
            "--batch",
            "--skip-column-names",
            "--execute",
            "SELECT 'DB', SCHEMA_NAME, '', '' FROM information_schema.SCHEMATA "
            "UNION ALL "
            "SELECT 'GRANT', User, Host, Db FROM mysql.db;",
        ),
        timeout=30,
        mutates=False,
        description="List MariaDB databases and database-level user grants (read-only)",
        failure_hint=MARIADB_SQL_FAILURE_HINT,
    ),
    CommandSpec(
        KEYSTONE_MANAGE_DB_SYNC,
        # /usr/bin/keystone-manage (package "python3-keystone", verified with `dpkg -S`).
        # Guide (https://docs.openstack.org/keystone/2025.1/install/keystone-install-ubuntu.html):
        # 'su -s /bin/sh -c "keystone-manage db_sync" keystone'.
        # CommandSpec forbids `su` and `sudo`; `/usr/bin/keystone-manage db_sync` is invoked
        # directly as root after `/etc/keystone/keystone.conf` is configured with
        # `[database] connection = mysql+pymysql://keystone:KEYSTONE_DBPASS@controller/keystone`
        # (so no root-owned SQLite file is created in `/var/lib/keystone`).
        ("/usr/bin/keystone-manage", "db_sync"),
        timeout=300,
        mutates=True,
        description="Populate or upgrade the Keystone SQL database schema",
        failure_hint=KEYSTONE_MANAGE_FAILURE_HINT,
    ),
    CommandSpec(
        KEYSTONE_MANAGE_FERNET_SETUP,
        # /usr/bin/keystone-manage (package "python3-keystone").
        # Guide (https://docs.openstack.org/keystone/2025.1/install/keystone-install-ubuntu.html):
        # "keystone-manage fernet_setup --keystone-user keystone --keystone-group keystone".
        # keystone-manage(1) and `keystone/cmd/cli.py`: initializes `/etc/keystone/fernet-keys/`
        # with keys owned by `keystone:keystone` (mode 0600); idempotent when key `0` already
        # exists.
        (
            "/usr/bin/keystone-manage",
            "fernet_setup",
            "--keystone-user",
            "keystone",
            "--keystone-group",
            "keystone",
        ),
        timeout=60,
        mutates=True,
        description="Initialize the Keystone Fernet token key repository",
        failure_hint=KEYSTONE_MANAGE_FAILURE_HINT,
    ),
    CommandSpec(
        KEYSTONE_MANAGE_CREDENTIAL_SETUP,
        # /usr/bin/keystone-manage (package "python3-keystone").
        # Guide (https://docs.openstack.org/keystone/2025.1/install/keystone-install-ubuntu.html):
        # "keystone-manage credential_setup --keystone-user keystone --keystone-group keystone".
        # keystone-manage(1) and `keystone/cmd/cli.py`: initializes
        # `/etc/keystone/credential-keys/` with keys owned by `keystone:keystone` (mode 0600);
        # idempotent when key `0` already exists.
        (
            "/usr/bin/keystone-manage",
            "credential_setup",
            "--keystone-user",
            "keystone",
            "--keystone-group",
            "keystone",
        ),
        timeout=60,
        mutates=True,
        description="Initialize the Keystone Fernet credential key repository",
        failure_hint=KEYSTONE_MANAGE_FAILURE_HINT,
    ),
    CommandSpec(
        KEYSTONE_MANAGE_BOOTSTRAP,
        # /usr/bin/keystone-manage (package "python3-keystone").
        # Guide (https://docs.openstack.org/keystone/2025.1/install/keystone-install-ubuntu.html):
        # "keystone-manage bootstrap --bootstrap-password ADMIN_PASS
        #  --bootstrap-admin-url http://controller:5000/v3/
        #  --bootstrap-internal-url http://controller:5000/v3/
        #  --bootstrap-public-url http://controller:5000/v3/
        #  --bootstrap-region-id RegionOne".
        # Verified in `/usr/lib/python3/dist-packages/keystone/cmd/cli.py` (lines 64-235):
        # `BootStrap.do_bootstrap()` reads `os.environ.get('OS_BOOTSTRAP_PASSWORD') or
        # CONF.command.bootstrap_password`. Passing `password` via
        # `env_params={"password": "OS_BOOTSTRAP_PASSWORD"}` keeps `ADMIN_PASS` in
        # `/proc/<pid>/environ` (readable only by root and the same uid) rather than in
        # `argv` / `/proc/<pid>/cmdline` (`ps`).
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
        timeout=120,
        mutates=True,
        description=(
            "Bootstrap the Keystone Identity service admin user, project, roles, and endpoints"
        ),
        failure_hint=KEYSTONE_MANAGE_FAILURE_HINT,
        env_params={"password": "OS_BOOTSTRAP_PASSWORD"},
    ),
    CommandSpec(
        OPENSTACK_PROJECT_LIST,
        # /usr/bin/openstack (package "python3-openstackclient", verified with `dpkg -S`).
        # `openstackclient/identity/v3/project.py` (`ListProject`): lists projects in domain
        # `default` as JSON (`[{"ID": "...", "Name": "..."}]`). Also verifies live Keystone
        # `/v3` HTTP API reachability and `admin` authentication (`keystone-verify-ubuntu.html`).
        (*OPENSTACK_ADMIN_AUTH_ARGS, "project", "list", "--domain", "default", "-f", "json"),
        {"admin_password": "secret"},
        timeout=60,
        mutates=False,
        description="List Keystone projects in domain 'default' as JSON (read-only)",
        failure_hint=OPENSTACK_CLI_FAILURE_HINT,
        env_params={"admin_password": "OS_PASSWORD"},
    ),
    CommandSpec(
        OPENSTACK_PROJECT_CREATE_SERVICE,
        # /usr/bin/openstack (package "python3-openstackclient").
        # Guide (https://docs.openstack.org/keystone/2025.1/install/keystone-users-ubuntu.html):
        # 'openstack project create --domain default --description "Service Project" service'.
        # Fixed literal arguments so `"Service Project"` (with a space) requires no parameter.
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
        timeout=60,
        mutates=True,
        description="Create the Keystone 'service' project in domain 'default'",
        failure_hint=OPENSTACK_CLI_FAILURE_HINT,
        env_params={"admin_password": "OS_PASSWORD"},
    ),
    CommandSpec(
        OPENSTACK_USER_LIST,
        # /usr/bin/openstack (`openstackclient/identity/v3/user.py` `ListUser`):
        # lists users in domain `default` as JSON (`[{"ID": "...", "Name": "..."}]`).
        (*OPENSTACK_ADMIN_AUTH_ARGS, "user", "list", "--domain", "default", "-f", "json"),
        {"admin_password": "secret"},
        timeout=60,
        mutates=False,
        description="List Keystone users in domain 'default' as JSON (read-only)",
        failure_hint=OPENSTACK_CLI_FAILURE_HINT,
        env_params={"admin_password": "OS_PASSWORD"},
    ),
    CommandSpec(
        OPENSTACK_USER_CREATE,
        # /usr/bin/openstack (`openstackclient/identity/v3/user.py` `CreateUser`).
        # Why `--password {user_password}` is in argv while `admin_password` is in env_params:
        # `CreateUser.take_action()` (lines 286-289) calls `osc_lib.utils.get_password(stdin)`
        # when `--password-prompt` is used, and `/usr/lib/python3/dist-packages/osc_lib/utils/`
        # `__init__.py` (lines 562-580) checks `stdin.isatty()` and raises
        # `CommandError("No terminal detected attempting to read password")` when stdin is a
        # pipe; nor does `CreateUser` read an env var for the new user's password.
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
        timeout=60,
        mutates=True,
        description="Create one Keystone user in domain 'default'",
        failure_hint=OPENSTACK_CLI_FAILURE_HINT,
        env_params={"admin_password": "OS_PASSWORD"},
    ),
    CommandSpec(
        OPENSTACK_USER_SET_PASSWORD,
        # /usr/bin/openstack (`openstackclient/identity/v3/user.py` `SetUser`, lines 507-636).
        # Used when `user list` shows that `{user}` already exists (`CreateUser` raises
        # `ConflictException` if the user already exists, and `user list` does not expose
        # password hashes, so converging the password requires `user set --password`).
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
        timeout=60,
        mutates=True,
        description="Converge the password of an existing Keystone user in domain 'default'",
        failure_hint=OPENSTACK_CLI_FAILURE_HINT,
        env_params={"admin_password": "OS_PASSWORD"},
    ),
    CommandSpec(
        OPENSTACK_ROLE_ASSIGNMENT_LIST,
        # /usr/bin/openstack (`openstackclient/identity/v3/role_assignment.py`
        # `ListRoleAssignment`, lines 63-250): lists role assignments resolved to names
        # (`--names -f json`).
        (*OPENSTACK_ADMIN_AUTH_ARGS, "role", "assignment", "list", "--names", "-f", "json"),
        {"admin_password": "secret"},
        timeout=60,
        mutates=False,
        description="List Keystone role assignments with resolved names as JSON (read-only)",
        failure_hint=OPENSTACK_CLI_FAILURE_HINT,
        env_params={"admin_password": "OS_PASSWORD"},
    ),
    CommandSpec(
        OPENSTACK_ROLE_ADD_USER_PROJECT,
        # /usr/bin/openstack (`openstackclient/identity/v3/role.py` `AddRole`, lines 197-296):
        # 'openstack role add --project <project> --user <user> <role>'.
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
        timeout=60,
        mutates=True,
        description="Assign a Keystone role to a user on a project",
        failure_hint=OPENSTACK_CLI_FAILURE_HINT,
        env_params={"admin_password": "OS_PASSWORD"},
    ),
    CommandSpec(
        OPENSTACK_SERVICE_LIST,
        # /usr/bin/openstack (`openstackclient/identity/v3/service.py` `ListService`,
        # lines 144-172): lists services (`[{"ID": "...", "Name": "...", "Type": "..."}]`).
        (*OPENSTACK_ADMIN_AUTH_ARGS, "service", "list", "-f", "json"),
        {"admin_password": "secret"},
        timeout=60,
        mutates=False,
        description="List Keystone service catalog entries as JSON (read-only)",
        failure_hint=OPENSTACK_CLI_FAILURE_HINT,
        env_params={"admin_password": "OS_PASSWORD"},
    ),
    CommandSpec(
        OPENSTACK_SERVICE_CREATE,
        # /usr/bin/openstack (`openstackclient/identity/v3/service.py` `CreateService`,
        # lines 56-104): creates a service with `--name {service_name} {service_type}`.
        # Note: in Keystone v3, `create_service` does NOT check uniqueness and creates a
        # duplicate UUID if invoked twice; callers must inspect `openstack.service-list` first.
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
        timeout=60,
        mutates=True,
        description="Create one Keystone service catalog entry",
        failure_hint=OPENSTACK_CLI_FAILURE_HINT,
        env_params={"admin_password": "OS_PASSWORD"},
    ),
    CommandSpec(
        OPENSTACK_ENDPOINT_LIST,
        # /usr/bin/openstack (`openstackclient/identity/v3/endpoint.py` `ListEndpoint`,
        # lines 176-281): lists endpoints (`ID`, `Region`, `Service Name`, `Service Type`,
        # `Enabled`, `Interface`, `URL`) as JSON.
        (*OPENSTACK_ADMIN_AUTH_ARGS, "endpoint", "list", "-f", "json"),
        {"admin_password": "secret"},
        timeout=60,
        mutates=False,
        description="List Keystone service endpoints as JSON (read-only)",
        failure_hint=OPENSTACK_CLI_FAILURE_HINT,
        env_params={"admin_password": "OS_PASSWORD"},
    ),
    CommandSpec(
        OPENSTACK_ENDPOINT_CREATE,
        # /usr/bin/openstack (`openstackclient/identity/v3/endpoint.py` `CreateEndpoint`,
        # lines 74-134): creates an endpoint in `RegionOne` for `{service_name}` and
        # `{interface}` (`public`, `internal`, or `admin`) at `{url}`.
        # Note: in Keystone v3, `endpoints.create` creates a duplicate UUID if invoked twice;
        # callers must inspect `openstack.endpoint-list` first.
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
        timeout=60,
        mutates=True,
        description="Create one Keystone service endpoint in RegionOne",
        failure_hint=OPENSTACK_CLI_FAILURE_HINT,
        env_params={"admin_password": "OS_PASSWORD"},
    ),
    CommandSpec(
        GLANCE_MANAGE_DB_SYNC,
        # /usr/bin/glance-manage (package "python3-glance", verified with `dpkg -S`).
        # Guide (https://docs.openstack.org/glance/2025.1/install/install-ubuntu.html):
        # 'su -s /bin/sh -c "glance-manage db_sync" glance'.
        # glance-manage(1) ("glance-manage db_sync") and `glance/cmd/manage.py`
        # (`add_legacy_command_parsers` registers `db_sync`).
        # CommandSpec forbids `su` and `sudo`; `/usr/bin/glance-manage db_sync` is invoked
        # directly as root after `/etc/glance/glance-api.conf` is configured with
        # `[database] connection = mysql+pymysql://glance:GLANCE_DBPASS@controller/glance`.
        ("/usr/bin/glance-manage", "db_sync"),
        timeout=300,
        mutates=True,
        description="Populate or upgrade the Glance Image service SQL database schema",
        failure_hint=GLANCE_MANAGE_FAILURE_HINT,
    ),
    CommandSpec(
        PLACEMENT_MANAGE_DB_SYNC,
        # /usr/bin/placement-manage (package "python3-placement", verified with `dpkg -S`).
        # Guide (https://docs.openstack.org/placement/2025.1/install/install-ubuntu.html):
        # 'su -s /bin/sh -c "placement-manage db sync" placement'.
        # Verified in `/usr/lib/python3/dist-packages/placement/cmd/manage.py`:
        # `add_db_command_parsers()` registers `subparsers.add_parser('db')` and nested
        # `db_parser.add_parser('sync')` (two separate tokens: `"db"`, `"sync"`).
        # CommandSpec forbids `su` and `sudo`; `/usr/bin/placement-manage db sync` is
        # invoked directly as root after `/etc/placement/placement.conf` is configured with
        # `[placement_database] connection = mysql+pymysql://placement:PLACEMENT_DBPASS@controller/placement`.
        ("/usr/bin/placement-manage", "db", "sync"),
        timeout=300,
        mutates=True,
        description="Populate or upgrade the Placement service SQL database schema",
        failure_hint=PLACEMENT_MANAGE_FAILURE_HINT,
    ),
)

DEFAULT_REGISTRY = CommandRegistry(SYSTEM_COMMANDS)

DEFAULT_TERM_GRACE_PERIOD = 5.0  # seconds between SIGTERM and SIGKILL on timeout


# --- process backend ------------------------------------------------------------------


@dataclass(frozen=True)
class ProcessOutput:
    returncode: int
    stdout: str
    stderr: str


class ProcessBackend(Protocol):
    """Runs a validated argv; injectable so tests never spawn real system commands."""

    def run(
        self, argv: tuple[str, ...], *, env: Mapping[str, str], timeout: float
    ) -> ProcessOutput: ...


class SubprocessBackend:
    """Run argv with ``subprocess`` (never a shell) in its own session.

    ``stdin`` is ``subprocess.DEVNULL`` by default; when a :class:`CommandSpec` opts into
    ``stdin_param``, ``stdin`` is opened as a pipe and the single sensitive parameter value
    is fed via ``communicate(input=...)``.

    On timeout the whole process group gets SIGTERM; if it is still running after
    ``term_grace_period`` seconds it gets SIGKILL.
    """

    def __init__(self, term_grace_period: float = DEFAULT_TERM_GRACE_PERIOD) -> None:
        if term_grace_period < 0:
            raise ValueError("term_grace_period must not be negative")
        self.term_grace_period = term_grace_period

    def run(
        self,
        argv: tuple[str, ...],
        *,
        env: Mapping[str, str],
        timeout: float,
        stdin: str | None = None,
    ) -> ProcessOutput:
        stdin_stream = subprocess.DEVNULL if stdin is None else subprocess.PIPE
        try:
            process = subprocess.Popen(
                list(argv),
                shell=False,
                stdin=stdin_stream,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=dict(env),
                cwd="/",
                close_fds=True,
                start_new_session=True,
            )
        except FileNotFoundError as exc:
            raise CommandNotFoundError(f"Executable not found: {argv[0]}") from exc
        except PermissionError as exc:
            raise CommandNotFoundError(f"Executable is not executable: {argv[0]}") from exc
        try:
            if stdin is None:
                stdout, stderr = process.communicate(timeout=timeout)
            else:
                stdout, stderr = process.communicate(
                    input=stdin.encode("utf-8"),
                    timeout=timeout,
                )
        except subprocess.TimeoutExpired as exc:
            # The command runs in its own session: signal it and anything it started.
            _signal_group(process.pid, signal.SIGTERM)
            try:
                process.communicate(timeout=self.term_grace_period)
            except subprocess.TimeoutExpired:
                _signal_group(process.pid, signal.SIGKILL)
                process.communicate()
                raise CommandTimeoutError(
                    f"{argv[0]} timed out after {timeout:g}s and was killed (SIGKILL) "
                    f"after ignoring SIGTERM for {self.term_grace_period:g}s",
                    killed=True,
                ) from exc
            raise CommandTimeoutError(
                f"{argv[0]} timed out after {timeout:g}s and was terminated (SIGTERM)"
            ) from exc
        return ProcessOutput(
            returncode=process.returncode,
            stdout=stdout.decode("utf-8", errors="replace"),
            stderr=stderr.decode("utf-8", errors="replace"),
        )


def _signal_group(pid: int, signum: int) -> None:
    try:
        os.killpg(pid, signum)
    except ProcessLookupError:
        pass


# --- executor --------------------------------------------------------------------------


@dataclass(frozen=True)
class CommandResult:
    """A finished (or dry-run) command; argv and output are redacted, output truncated."""

    command_id: str
    argv: tuple[str, ...]
    returncode: int | None
    stdout: str = ""
    stderr: str = ""
    duration: float = 0.0
    dry_run: bool = False
    stdout_truncated: bool = False
    stderr_truncated: bool = False

    @property
    def ok(self) -> bool:
        return self.returncode == 0


def is_host_root(root: Path) -> bool:
    """True only if ``root`` resolves to the real filesystem root ``/``."""
    return Path(root).resolve() == Path("/")


class CommandExecutor:
    """Execute allowlisted commands for steps; the only way this package runs a process."""

    def __init__(
        self,
        registry: CommandRegistry,
        *,
        root: Path,
        audit_log: AuditLog,
        backend: ProcessBackend | None = None,
        dry_run: bool = False,
        output_limit: int = DEFAULT_OUTPUT_LIMIT,
        echo: Callable[[str], None] = print,
        euid: Callable[[], int] = os.geteuid,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if output_limit < 1:
            raise ValueError("output_limit must be positive")
        self.registry = registry
        self.root = Path(root)
        self.audit_log = audit_log
        self.backend: ProcessBackend = SubprocessBackend() if backend is None else backend
        self.dry_run = dry_run
        self.output_limit = output_limit
        self._echo = echo
        self._euid = euid
        self._clock = clock

    def run(self, command_id: str, params: Mapping[str, object] | None = None) -> CommandResult:
        """Validate and run ``command_id``; raise a typed CommandError on any failure."""
        raw_params = params or {}
        spec = self.registry.get(command_id)
        argv = spec.build_argv(raw_params)
        shown_argv = spec.build_shown_argv(raw_params)
        env = spec.build_env(raw_params)
        stdin_data = spec.extract_stdin(raw_params)
        sensitive_values = tuple(
            sorted(
                (
                    str(raw_params[name])
                    for name in spec.sensitive_params
                    if isinstance(raw_params.get(name), str) and raw_params[name]
                ),
                key=len,
                reverse=True,
            )
        )

        if self.dry_run:
            self._echo(f"DRY RUN [{spec.id}]: {shlex.join(shown_argv)}")
            return CommandResult(spec.id, shown_argv, returncode=None, dry_run=True)
        if spec.mutates:
            self._refuse_unsafe_mutation(spec)

        invocation = uuid.uuid4().hex
        base = {"invocation": invocation, "command_id": spec.id, "argv": list(shown_argv)}
        self.audit_log.record({**base, "event": "start", "mutates": spec.mutates})
        started = self._clock()
        try:
            if stdin_data is None:
                output = self.backend.run(argv, env=env, timeout=spec.timeout)
            else:
                output = self.backend.run(  # type: ignore[call-arg]
                    argv,
                    env=env,
                    timeout=spec.timeout,
                    stdin=stdin_data,
                )
        except CommandError as exc:
            outcome = "timeout" if isinstance(exc, CommandTimeoutError) else "not_found"
            self._finish(base, outcome, None, started)
            _with_hint(exc, spec)
            raise
        except Exception as exc:
            self._finish(base, "error", None, started)
            redacted_exc = _redact_with_sensitive(str(exc), sensitive_values)
            error = CommandError(f"Command {spec.id!r} could not be run: {redacted_exc}")
            raise _with_hint(error, spec) from exc

        duration = self._finish(
            base,
            "ok" if output.returncode in spec.allowed_exit_codes else "unexpected_exit",
            output.returncode,
            started,
        )
        stdout, stdout_cut = self._clean(output.stdout, sensitive_values)
        stderr, stderr_cut = self._clean(output.stderr, sensitive_values)
        if output.returncode not in spec.allowed_exit_codes:
            raise _with_hint(UnexpectedExitError(spec.id, output.returncode, stderr), spec)
        return CommandResult(
            spec.id,
            shown_argv,
            output.returncode,
            stdout,
            stderr,
            duration,
            stdout_truncated=stdout_cut,
            stderr_truncated=stderr_cut,
        )

    def _refuse_unsafe_mutation(self, spec: CommandSpec) -> None:
        if not is_host_root(self.root):
            raise MutationRefusedError(
                f"Refusing mutating command {spec.id!r}: root is {self.root}, not '/'; "
                "mutating commands only run against the real host"
            )
        if self._euid() != 0:
            raise MutationRefusedError(
                f"Refusing mutating command {spec.id!r}: it requires effective uid 0 "
                "(run the tool as root; sudo is never used)"
            )

    def _finish(
        self, base: Mapping[str, object], outcome: str, returncode: int | None, started: float
    ) -> float:
        duration = round(self._clock() - started, 6)
        self.audit_log.record(
            {
                **base,
                "event": "finish",
                "outcome": outcome,
                "exit_code": returncode,
                "duration_s": duration,
            }  # fmt: skip
        )
        return duration

    def _clean(self, text: str, sensitive_values: tuple[str, ...] = ()) -> tuple[str, bool]:
        """Redact first (so a cut cannot expose a secret), then keep the last characters."""
        redacted = _redact_with_sensitive(text, sensitive_values)
        if len(redacted) <= self.output_limit:
            return redacted, False
        removed = len(redacted) - self.output_limit
        return TRUNCATION_MARKER.format(count=removed) + redacted[-self.output_limit :], True


def _redact_with_sensitive(text: str, sensitive_values: tuple[str, ...]) -> str:
    for secret in sensitive_values:
        if secret in text:
            text = text.replace(secret, MASK)
    return redact_text(text)


def _with_hint(error: CommandError, spec: CommandSpec) -> CommandError:
    if spec.failure_hint:
        error.hint = spec.failure_hint
    return error


# --- helpers for steps ----------------------------------------------------------------


def command_succeeds(
    executor: CommandExecutor, command_id: str, params: Mapping[str, object] | None = None
) -> bool:
    """For ``check()``: True if the command exits 0 (other allowed codes mean False)."""
    return executor.run(command_id, params).returncode == 0


def verify_command(
    executor: CommandExecutor, command_id: str, params: Mapping[str, object] | None = None
) -> VerifyResult:
    """For ``verify()``: ok if the command exits 0; details are the redacted output."""
    try:
        result = executor.run(command_id, params)
    except UnexpectedExitError as exc:
        return VerifyResult(False, str(exc))
    details = (result.stdout or result.stderr).strip()
    return VerifyResult(result.returncode == 0, details)
