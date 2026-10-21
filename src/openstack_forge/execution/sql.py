"""Reusable MariaDB database and user-grant provisioning brick (via ``stdin``).

Every OpenStack controller service in the 2025.1 (Epoxy) Ubuntu installation guide
(Keystone, Glance, Placement, Nova, Neutron, Cinder) begins with a "Prerequisites" database
section that creates one or more MariaDB databases and grants ``ALL PRIVILEGES`` to
``'<user>'@'localhost'`` and ``'<user>'@'%'`` identified by ``<SERVICE>_DBPASS``.

Why SQL is delivered over ``stdin`` (:data:`MARIADB_EXECUTE_SQL`), never ``argv``:
- Passing ``-p<password>`` or ``--execute="... IDENTIFIED BY '<password>' ..."`` in ``argv``
  exposes the database password to every local user in ``/proc/<pid>/cmdline`` (``ps``).
- :data:`MARIADB_EXECUTE_SQL` invokes ``/usr/bin/mariadb --batch --skip-column-names`` with
  ``stdin_param="sql"`` (parameter type ``"sql_stdin"``). The SQL statements are piped to
  ``/usr/bin/mariadb`` over ``stdin=subprocess.PIPE`` and never appear in ``argv``.
- ``"sql_stdin"`` is restricted by :class:`CommandSpec` to ``stdin_param`` only (never
  permitted in ``argv`` or ``env_params``), accepts only printable ASCII plus LF (``\\n``)
  up to :data:`MAX_SQL_STDIN_LENGTH` (4096) characters, and is always treated as sensitive
  (masked as ``********`` if validation fails).

Why :data:`MARIADB_QUERY_SQL` uses a fixed literal ``--execute`` query in ``argv``:
- :func:`validate_parameter` forbids whitespace and SQL metacharacters in ``argv``
  parameters, and a ``mutates=False`` command that accepted arbitrary SQL over ``stdin``
  could be misused to run mutating statements without root/host-root checks.
- Instead, :data:`MARIADB_QUERY_SQL` embeds a fixed literal read-only ``SELECT`` over
  ``information_schema.SCHEMATA`` and ``mysql.db`` in ``argv`` with zero parameters
  (``params={}``, ``mutates=False``), reusable by ``check()`` and ``verify()`` across all
  OpenStack controller service steps.

Ubuntu 24.04 package and authentication facts (established read-only on the dev machine):
- ``dpkg -S /usr/bin/mariadb /usr/bin/mysql`` maps both paths to ``mariadb-client-core``
  (``1:10.11.14-0ubuntu0.24.04.1``, installed as a dependency of ``mariadb-server``);
  ``/usr/bin/mariadb`` is the ELF binary and ``/usr/bin/mysql -> mariadb`` is a symlink.
- On Ubuntu, MariaDB's local ``root`` account uses the ``unix_socket`` authentication
  plugin by default, so ``/usr/bin/mariadb`` run as effective UID 0 connects over the local
  Unix socket without a root password.

Idempotence deviation from the guide pages:
- The guide pages write ``CREATE DATABASE <db>;`` interactively, which fails with
  ``ERROR 1007 (HY000): Can't create database '<db>'; database exists`` if a previous run
  created the database before a later command failed.
- :func:`build_service_database_sql` emits ``CREATE DATABASE IF NOT EXISTS `<db>`;``
  followed by ``GRANT ALL PRIVILEGES ON `<db>`.* TO '<user>'@'localhost' IDENTIFIED BY
  '<password>';``, ``GRANT ALL PRIVILEGES ON `<db>`.* TO '<user>'@'%' IDENTIFIED BY
  '<password>';``, and ``FLUSH PRIVILEGES;`` so reruns converge both the database and the
  user's password idempotently.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

from openstack_forge.execution.commands import (
    MARIADB_EXECUTE_SQL,
    MARIADB_QUERY_SQL,
    CommandExecutor,
    CommandResult,
)
from openstack_forge.execution.errors import ParameterError, UnexpectedExitError
from openstack_forge.execution.preview import PlannedCommand
from openstack_forge.execution.secrets import validate_secret_value

SQL_IDENTIFIER_RE = re.compile(r"^[a-z][a-z0-9_]*$")
GRANT_HOSTS: tuple[str, str] = ("localhost", "%")

QUERY_MARIADB_STATE = PlannedCommand(MARIADB_QUERY_SQL)


@dataclass(frozen=True)
class MariaDBState:
    """Databases and ``(user, host, db)`` grants parsed from :data:`MARIADB_QUERY_SQL`."""

    databases: frozenset[str]
    grants: frozenset[tuple[str, str, str]]


def validate_sql_identifier(kind: str, value: object) -> str:
    """Validate a MariaDB database or user identifier against ``^[a-z][a-z0-9_]*$``."""
    if not isinstance(value, str):
        raise ParameterError(f"SQL {kind} must be a string, got {type(value).__name__}")
    if not SQL_IDENTIFIER_RE.fullmatch(value):
        raise ParameterError(
            f"Invalid SQL {kind} {value!r} (must match {SQL_IDENTIFIER_RE.pattern})"
        )
    return value


def _validate_databases(databases: Sequence[str]) -> tuple[str, ...]:
    if isinstance(databases, (str, bytes)) or not isinstance(databases, Sequence):
        raise ParameterError("databases must be a non-empty sequence of database name strings")
    if not databases:
        raise ParameterError("databases must not be empty")
    checked: list[str] = []
    seen: set[str] = set()
    for db in databases:
        valid_db = validate_sql_identifier("database name", db)
        if valid_db in seen:
            raise ParameterError(f"Duplicate SQL database name {valid_db!r}")
        seen.add(valid_db)
        checked.append(valid_db)
    return tuple(checked)


def build_service_database_sql(databases: Sequence[str], user: str, password: str) -> str:
    """Build idempotent MariaDB provisioning SQL for ``databases`` and ``user``."""
    checked_dbs = _validate_databases(databases)
    checked_user = validate_sql_identifier("user name", user)
    checked_password = validate_secret_value("password", password)

    lines: list[str] = []
    for db in checked_dbs:
        lines.append(f"CREATE DATABASE IF NOT EXISTS `{db}`;")
        for host in GRANT_HOSTS:
            lines.append(
                f"GRANT ALL PRIVILEGES ON `{db}`.* TO '{checked_user}'@'{host}' "
                f"IDENTIFIED BY '{checked_password}';"
            )
    lines.append("FLUSH PRIVILEGES;")
    return "\n".join(lines) + "\n"


def plan_service_database_sql(databases: Sequence[str], user: str, password: str) -> PlannedCommand:
    """Return the :class:`PlannedCommand` for :data:`MARIADB_EXECUTE_SQL`."""
    return PlannedCommand(
        MARIADB_EXECUTE_SQL,
        {"sql": build_service_database_sql(databases, user, password)},
    )


def parse_mariadb_state(stdout: str) -> MariaDBState:
    """Parse tab-separated ``DB`` and ``GRANT`` rows from :data:`MARIADB_QUERY_SQL`."""
    databases: set[str] = set()
    grants: set[tuple[str, str, str]] = set()
    for raw_line in stdout.splitlines():
        if not raw_line.strip():
            continue
        fields = [field.strip() for field in raw_line.split("\t")]
        if len(fields) == 1:
            fields = raw_line.split()
        kind = fields[0] if fields else ""
        if kind == "DB" and len(fields) >= 2 and fields[1]:
            databases.add(fields[1])
        elif kind == "GRANT" and len(fields) >= 4 and fields[1] and fields[2] and fields[3]:
            grants.add((fields[1], fields[2], fields[3]))
    return MariaDBState(
        databases=frozenset(databases),
        grants=frozenset(grants),
    )


def verify_service_database_state(stdout: str, databases: Sequence[str], user: str) -> list[str]:
    """Return human-readable problems if any database or ``localhost``/``%`` grant is missing."""
    checked_dbs = _validate_databases(databases)
    checked_user = validate_sql_identifier("user name", user)
    state = parse_mariadb_state(stdout)
    problems: list[str] = []
    for db in checked_dbs:
        if db not in state.databases:
            problems.append(f"MariaDB database {db!r} does not exist")
        for host in GRANT_HOSTS:
            if (checked_user, host, db) not in state.grants:
                problems.append(
                    f"MariaDB grant for '{checked_user}'@'{host}' on database {db!r} is missing"
                )
    return problems


def service_database_ready(executor: CommandExecutor, databases: Sequence[str], user: str) -> bool:
    """True if :data:`MARIADB_QUERY_SQL` succeeds and all ``databases`` and grants exist."""
    checked_dbs = _validate_databases(databases)
    checked_user = validate_sql_identifier("user name", user)
    try:
        result = executor.run(MARIADB_QUERY_SQL)
    except UnexpectedExitError:
        return False
    return not verify_service_database_state(result.stdout, checked_dbs, checked_user)


def service_database_problems(
    executor: CommandExecutor, databases: Sequence[str], user: str
) -> list[str]:
    """Run :data:`MARIADB_QUERY_SQL` and return any missing database or grant problems."""
    checked_dbs = _validate_databases(databases)
    checked_user = validate_sql_identifier("user name", user)
    try:
        result = executor.run(MARIADB_QUERY_SQL)
    except UnexpectedExitError as exc:
        return [f"mariadb query failed: {exc}"]
    return verify_service_database_state(result.stdout, checked_dbs, checked_user)


def provision_service_database(
    executor: CommandExecutor, databases: Sequence[str], user: str, password: str
) -> CommandResult:
    """Provision ``databases`` and ``user`` grants on MariaDB via ``stdin``."""
    sql = build_service_database_sql(databases, user, password)
    return executor.run(MARIADB_EXECUTE_SQL, {"sql": sql})
