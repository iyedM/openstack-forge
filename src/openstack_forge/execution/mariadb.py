"""The ``mariadb`` step: the SQL database on the controller.

Follows the official page
(https://docs.openstack.org/install-guide/environment-sql-database-ubuntu.html), for
Ubuntu 20.04 and later:

1. install ``mariadb-server`` and ``python3-pymysql``;
2. create ``/etc/mysql/mariadb.conf.d/99-openstack.cnf`` with a ``[mysqld]`` section
   setting ``bind-address`` to the controller's management IP address, plus
   ``default-storage-engine = innodb``, ``innodb_file_per_table = on``,
   ``max_connections = 4096``, ``collation-server = utf8_general_ci`` and
   ``character-set-server = utf8``;
3. restart the database service (the guide: ``service mysql restart``; here
   ``systemctl restart mariadb``, see :data:`MARIADB_UNIT`).

The guide then runs ``mysql_secure_installation``. This step does NOT: the script is
interactive and commands run with stdin from ``/dev/null``, and the same page notes that
on Ubuntu MariaDB uses the ``unix_socket`` authentication plugin, so the local ``root``
user needs no password. The page also notes that since Ubuntu 18.04 ``mariadb-server`` is
not in the default repository and the Universe repository must be enabled; this step
enables no repository.

This step does not update the package lists: on the controller it runs after
``time-sync-controller`` and ``openstack-packages``, which already did. Run alone on a host
whose package lists are empty, ``apt-get install`` may fail until they are updated.

The step is UNVERIFIED and has never run on a real host. ``verify()`` does not check SQL
connectivity or that MariaDB listens on ``<management_ip>:3306``.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from ipaddress import IPv4Address
from pathlib import Path
from typing import Any, ClassVar

from openstack_forge.execution.apt_output import parse_dpkg_status
from openstack_forge.execution.commands import (
    APT_GET_INSTALL,
    DPKG_QUERY_STATUS,
    SYSTEMCTL_IS_ACTIVE,
    SYSTEMCTL_RESTART,
    CommandExecutor,
)
from openstack_forge.execution.engine import VerifyResult
from openstack_forge.execution.errors import (
    ExecutionError,
    FileFormatError,
    PreconditionError,
    UnsafePathError,
)
from openstack_forge.execution.files import (
    FileChange,
    _ini_set_many_editor,
    apply_line_edit,
    ini_set_many,
)
from openstack_forge.execution.fsutil import read_text_file, resolve_target
from openstack_forge.execution.preview import PlannedCommand, StepPreview

MARIADB_STEP_ID = "mariadb"
MARIADB_CONF_DIR_TARGET = "/etc/mysql/mariadb.conf.d"
MARIADB_CONF_TARGET = f"{MARIADB_CONF_DIR_TARGET}/99-openstack.cnf"
MARIADB_SECTION = "mysqld"
MARIADB_SERVER_PACKAGE = "mariadb-server"
PYMYSQL_PACKAGE = "python3-pymysql"
# mariadb-server on Ubuntu 24.04 ships /usr/lib/systemd/system/mariadb.service and
# mysql.service as a symlink to it (both checked with `dpkg -S`); the canonical name is
# used so the step does not depend on the alias the guide's "service mysql restart" uses.
MARIADB_UNIT = "mariadb"

INSTALL_MARIADB_SERVER = PlannedCommand(APT_GET_INSTALL, {"package": MARIADB_SERVER_PACKAGE})
INSTALL_PYMYSQL = PlannedCommand(APT_GET_INSTALL, {"package": PYMYSQL_PACKAGE})
RESTART_MARIADB = PlannedCommand(SYSTEMCTL_RESTART, {"unit": MARIADB_UNIT})
QUERY_MARIADB_SERVER = PlannedCommand(DPKG_QUERY_STATUS, {"package": MARIADB_SERVER_PACKAGE})
QUERY_PYMYSQL = PlannedCommand(DPKG_QUERY_STATUS, {"package": PYMYSQL_PACKAGE})
QUERY_MARIADB_ACTIVE = PlannedCommand(SYSTEMCTL_IS_ACTIVE, {"unit": MARIADB_UNIT})

NOT_CHECKED = (
    "SQL connectivity and TCP listening on {address}:3306 are not checked, and "
    "mysql_secure_installation is not run."
)


def mariadb_settings(local_node: Mapping[str, Any]) -> tuple[tuple[str, str, str], ...]:
    """The guide's ``[mysqld]`` settings, in its order, for ``local_node``."""
    address = str(IPv4Address(local_node["management_ip"]))
    return tuple(
        (MARIADB_SECTION, key, value)
        for key, value in (
            ("bind-address", address),
            ("default-storage-engine", "innodb"),
            ("innodb_file_per_table", "on"),
            ("max_connections", "4096"),
            ("collation-server", "utf8_general_ci"),
            ("character-set-server", "utf8"),
        )
    )


def edit_mariadb_conf(root: Path, local_node: Mapping[str, Any], *, dry_run: bool) -> FileChange:
    """Set the guide's keys in ``99-openstack.cnf`` under ``root`` in a single edit.

    The configuration directory comes with the MariaDB packages and is never created.
    """
    directory, display = resolve_target(root, MARIADB_CONF_DIR_TARGET)
    if not directory.is_dir():
        raise PreconditionError(
            f"{display} does not exist; it is created when {MARIADB_SERVER_PACKAGE} is installed"
        )
    return ini_set_many(root, MARIADB_CONF_TARGET, mariadb_settings(local_node), dry_run=dry_run)


def mariadb_conf_content(local_node: Mapping[str, Any]) -> str:
    """Exactly what :func:`edit_mariadb_conf` writes into a file that does not exist yet."""
    editor = _ini_set_many_editor(mariadb_settings(local_node))
    return apply_line_edit("", MARIADB_CONF_TARGET, editor)


def verify_mariadb_text(text: str, local_node: Mapping[str, Any]) -> list[str]:
    """Pure check: each guide key has its expected active value in ``[mysqld]``.

    Keys are compared exactly. Any active line for one of the keys with another value is a
    conflict, wherever it is in the file's ``[mysqld]`` sections.
    """
    values: dict[str, list[str]] = {}
    section: str | None = None
    for raw in text.split("\n"):
        line = raw.strip()
        if not line or line[0] in "#;":
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1].strip()
            continue
        if section != MARIADB_SECTION:
            continue
        key, _, value = line.partition("=")
        values.setdefault(key.strip(), []).append(value.strip())

    problems: list[str] = []
    for _, key, expected in mariadb_settings(local_node):
        found = values.get(key, [])
        if not found:
            problems.append(f"{key} is not set in [{MARIADB_SECTION}] (expected {expected!r})")
            continue
        for actual in dict.fromkeys(found):
            if actual != expected:
                problems.append(
                    f"{key} is {actual!r} in [{MARIADB_SECTION}], expected {expected!r}"
                )
    return problems


@dataclass(frozen=True)
class MariaDBStep:
    """Executable ``mariadb`` step (file edit under ``root``, commands via ``executor``)."""

    root: Path
    config: Mapping[str, Any]
    local_node: Mapping[str, Any]
    executor: CommandExecutor
    id: str = MARIADB_STEP_ID
    declares_commands: ClassVar[bool] = True

    def __post_init__(self) -> None:
        object.__setattr__(self, "root", Path(self.root))
        if self.local_node["role"] != "controller":
            raise ExecutionError(
                f"{self.id} only runs on the controller, not on "
                f"{self.local_node['hostname']!r} ({self.local_node['role']})"
            )

    def dry_run_preview(self) -> StepPreview:
        """Diff of the file edit (or its full content) plus the commands it would run."""
        notes: list[str] = []
        try:
            file_changes: tuple[FileChange, ...] = (
                edit_mariadb_conf(self.root, self.local_node, dry_run=True),
            )
        except PreconditionError:
            file_changes = ()
            notes.append(
                f"{MARIADB_CONF_DIR_TARGET} does not exist yet; it is created when "
                f"{MARIADB_SERVER_PACKAGE} is installed. After that, {MARIADB_CONF_TARGET} "
                f"is written with:\n{mariadb_conf_content(self.local_node)}"
            )
        notes += [
            "The file is edited after both install commands and before the restart.",
            "mysql_secure_installation is not run: it is interactive, and the guide notes "
            "that on Ubuntu MariaDB uses the unix_socket plugin, so local root access needs "
            "no password.",
            "The package lists are not updated by this step (time-sync-controller and "
            "openstack-packages run before it on the controller); apt-get install "
            "downloads from the configured apt sources (network).",
        ]
        return StepPreview(
            file_changes=file_changes,
            commands=(INSTALL_MARIADB_SERVER, INSTALL_PYMYSQL, RESTART_MARIADB),
            read_only_commands=(QUERY_MARIADB_SERVER, QUERY_PYMYSQL, QUERY_MARIADB_ACTIVE),
            notes=tuple(notes),
        )

    def check(self) -> bool:
        """True if 99-openstack.cnf needs no edit (pure) and both packages are installed."""
        try:
            if edit_mariadb_conf(self.root, self.local_node, dry_run=True).changed:
                return False
        except (PreconditionError, UnsafePathError):
            return False
        return self._installed(QUERY_MARIADB_SERVER) and self._installed(QUERY_PYMYSQL)

    def apply(self) -> None:
        """Install mariadb-server and python3-pymysql, write the file, restart MariaDB."""
        self.executor.run(INSTALL_MARIADB_SERVER.command_id, INSTALL_MARIADB_SERVER.params)
        self.executor.run(INSTALL_PYMYSQL.command_id, INSTALL_PYMYSQL.params)
        edit_mariadb_conf(self.root, self.local_node, dry_run=False)
        self.executor.run(RESTART_MARIADB.command_id, RESTART_MARIADB.params)

    def verify(self) -> VerifyResult:
        """The file has the guide's keys, python3-pymysql is installed, MariaDB is active."""
        not_checked = NOT_CHECKED.format(address=self.local_node["management_ip"])
        path, display = resolve_target(self.root, MARIADB_CONF_TARGET)
        directory, directory_display = resolve_target(self.root, MARIADB_CONF_DIR_TARGET)
        if not directory.is_dir():
            text = None
            missing = f"{display} does not exist ({directory_display} is missing)"
        else:
            try:
                text = read_text_file(path, display)
            except (UnsafePathError, FileFormatError) as exc:
                return VerifyResult(False, f"{exc}. {not_checked}")
            missing = f"{display} does not exist"
        problems = [missing] if text is None else verify_mariadb_text(text, self.local_node)
        if not self._installed(QUERY_PYMYSQL):
            problems.append(f"{PYMYSQL_PACKAGE} is not installed")
        result = self.executor.run(QUERY_MARIADB_ACTIVE.command_id, QUERY_MARIADB_ACTIVE.params)
        state = result.stdout.strip() or f"exit status {result.returncode}"
        if not (result.returncode == 0 and state == "active"):
            problems.append(f"the {MARIADB_UNIT} service is not active (state: {state})")
        if problems:
            return VerifyResult(False, f"{'; '.join(problems)}. {not_checked}")
        return VerifyResult(
            True,
            f"{display} sets the guide's [{MARIADB_SECTION}] keys; {PYMYSQL_PACKAGE} is "
            f"installed; {MARIADB_UNIT} is active. {not_checked}",
        )

    def _installed(self, query: PlannedCommand) -> bool:
        result = self.executor.run(query.command_id, query.params)
        return result.returncode == 0 and parse_dpkg_status(result.stdout)


def make_mariadb_step(
    root: Path,
    config: Mapping[str, Any],
    local_node: Mapping[str, Any],
    *,
    executor: CommandExecutor,
) -> MariaDBStep:
    return MariaDBStep(root=Path(root), config=config, local_node=local_node, executor=executor)
