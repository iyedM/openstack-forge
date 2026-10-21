"""Shared fake backend for the controller service step tests (runs nothing).

All host-derived fixtures in the controller service tests use RFC 5737 documentation
addresses (such as ``192.0.2.11`` and ``192.0.2.170``, with the development machine's
local IP sanitized to ``192.0.2.170`` for the repository).
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path

from openstack_forge.execution import ProcessOutput

DEV_MACHINE_SANITIZED_IP = "192.0.2.170"

_CREATE_DB_RE = re.compile(
    r"CREATE\s+DATABASE(?:\s+IF\s+NOT\s+EXISTS)?\s+`?([a-z][a-z0-9_]*)`?\s*;",
    re.IGNORECASE,
)
_GRANT_RE = re.compile(
    r"GRANT\s+ALL\s+PRIVILEGES\s+ON\s+`?([a-z][a-z0-9_]*)`?\.\*\s+"
    r"TO\s+'([a-z][a-z0-9_]*)'@'([^']+)'\s+IDENTIFIED\s+BY\s+'([^']*)'\s*;",
    re.IGNORECASE,
)

CreateEntry = tuple[str, str] | Sequence[tuple[str, str]]


class ServiceHost:
    """Simulates dpkg-query, apt-cache policy, apt-get install, systemctl, rabbitmqctl,
    mariadb, and keystone-manage.

    ``creates`` maps a package to the (root-relative path, content) pair (or sequence of
    pairs) its installation writes under the temporary root, imitating the package;
    ``starts`` names packages whose postinst starts a systemd unit of the same name (such
    as ``rabbitmq-server``); ``watch`` is a root-relative file whose content is recorded at
    every ``systemctl restart`` and ``systemctl enable`` (``watch_files`` records multiple
    files in ``multi_snapshots``). Packages in ``no_candidate`` report ``Candidate: (none)``
    in ``apt-cache policy``.
    """

    def __init__(
        self,
        root: Path,
        *,
        installed: set[str] | frozenset[str] = frozenset(),
        active: set[str] | frozenset[str] = frozenset(),
        enabled: set[str] | frozenset[str] = frozenset(),
        no_candidate: set[str] | frozenset[str] = frozenset(),
        starts: set[str] | frozenset[str] = frozenset(),
        creates: Mapping[str, CreateEntry] | None = None,
        watch: str | None = None,
        watch_files: Sequence[str] = (),
        rabbit_users: Mapping[str, str] | None = None,
        rabbit_permissions: Mapping[str, Mapping[str, tuple[str, str, str]]] | None = None,
        mariadb_databases: set[str] | frozenset[str] = frozenset(),
        mariadb_grants: set[tuple[str, str, str]] | frozenset[tuple[str, str, str]] = frozenset(),
    ) -> None:
        self.root = root
        self.installed = set(installed)
        self.active = set(active)
        self.enabled = set(enabled)
        self.no_candidate = set(no_candidate)
        self.starts = set(starts)
        self.creates = dict(creates or {})
        self.watch = watch
        self.watch_files = tuple(watch_files)
        self.rabbit_users: dict[str, str] = (
            {"guest": "[administrator]"} if rabbit_users is None else dict(rabbit_users)
        )
        self.rabbit_passwords: dict[str, str] = {}
        self.rabbit_permissions: dict[str, dict[str, tuple[str, str, str]]] = (
            {"guest": {"/": (".*", ".*", ".*")}}
            if rabbit_permissions is None
            else {user: dict(vhosts) for user, vhosts in rabbit_permissions.items()}
        )
        self.mariadb_databases: set[str] = set(mariadb_databases)
        self.mariadb_grants: set[tuple[str, str, str]] = set(mariadb_grants)
        self.mariadb_passwords: dict[tuple[str, str], str] = {}
        self.keystone_db_synced: bool = False
        self.keystone_bootstrapped: dict[str, str] | None = None
        self.calls: list[tuple[str, ...]] = []
        self.stdin_calls: list[tuple[tuple[str, ...], str | None]] = []
        self.failures: dict[tuple[str, ...], ProcessOutput] = {}
        self.snapshots: list[tuple[str, str | None]] = []
        self.multi_snapshots: list[tuple[str, dict[str, str | None]]] = []

    def run(
        self,
        argv: tuple[str, ...],
        *,
        env: Mapping[str, str],
        timeout: float,
        stdin: str | None = None,
    ) -> ProcessOutput:
        self.calls.append(argv)
        self.stdin_calls.append((argv, stdin))
        if argv in self.failures:
            return self.failures[argv]
        if argv[:2] in self.failures:
            return self.failures[argv[:2]]
        if argv[:3] in self.failures:
            return self.failures[argv[:3]]
        if argv[:2] == ("/usr/bin/dpkg-query", "--status"):
            if argv[2] in self.installed:
                return ProcessOutput(0, f"Package: {argv[2]}\nStatus: install ok installed\n", "")
            return ProcessOutput(1, "", f"dpkg-query: package '{argv[2]}' is not installed\n")
        if argv[:2] == ("/usr/bin/apt-cache", "policy"):
            package = argv[2]
            installed = "3.4.30-1ubuntu0.24.04.3" if package in self.installed else "(none)"
            candidate = "(none)" if package in self.no_candidate else "3.4.30-1ubuntu0.24.04.3"
            return ProcessOutput(
                0,
                f"{package}:\n  Installed: {installed}\n  Candidate: {candidate}\n"
                " Version table:\n",
                "",
            )
        if argv[:3] == ("/usr/bin/apt-get", "install", "--yes"):
            package = argv[3]
            self.installed.add(package)
            if package in self.starts:
                self.active.add(package)
                self.enabled.add(package)
            if package in self.creates:
                entry = self.creates[package]
                pairs: Iterable[tuple[str, str]]
                if (
                    isinstance(entry, tuple)
                    and len(entry) == 2
                    and isinstance(entry[0], str)
                    and isinstance(entry[1], str)
                ):
                    pairs = (entry,)
                else:
                    pairs = entry  # type: ignore[assignment]
                for relative, content in pairs:
                    path = self.root / relative
                    if not path.exists():
                        path.parent.mkdir(parents=True, exist_ok=True)
                        path.write_text(content)
            return ProcessOutput(0, f"Setting up {package} ...\n", "")
        if argv[:2] == ("/usr/bin/systemctl", "restart"):
            self._snapshot("restart")
            self.active.add(argv[2])
            return ProcessOutput(0, "", "")
        if argv[:2] == ("/usr/bin/systemctl", "enable"):
            self._snapshot("enable")
            self.enabled.add(argv[2])
            return ProcessOutput(0, "", "")
        if argv[:2] == ("/usr/bin/systemctl", "is-active"):
            if argv[2] in self.active:
                return ProcessOutput(0, "active\n", "")
            return ProcessOutput(3, "inactive\n", "")
        if argv[:2] == ("/usr/bin/systemctl", "is-enabled"):
            if argv[2] in self.enabled:
                return ProcessOutput(0, "enabled\n", "")
            return ProcessOutput(1, "disabled\n", "")
        if argv == ("/usr/sbin/rabbitmqctl", "list_users"):
            rows = "".join(f"{user}\t{tags}\n" for user, tags in self.rabbit_users.items())
            return ProcessOutput(0, f"Listing users ...\nuser\ttags\n{rows}", "")
        if argv[:2] == ("/usr/sbin/rabbitmqctl", "list_user_permissions") and len(argv) == 3:
            user = argv[2]
            if user not in self.rabbit_users:
                return ProcessOutput(67, "", f'Error: no_such_user: User "{user}" does not exist\n')
            vhosts = self.rabbit_permissions.get(user, {})
            rows = "".join(
                f"{vhost}\t{conf}\t{write}\t{read}\n"
                for vhost, (conf, write, read) in vhosts.items()
            )
            return ProcessOutput(
                0,
                f'Listing permissions for user "{user}" ...\nvhost\tconfigure\twrite\tread\n{rows}',
                "",
            )
        if argv[:2] == ("/usr/sbin/rabbitmqctl", "add_user") and len(argv) == 4:
            user, password = argv[2], argv[3]
            if user in self.rabbit_users:
                return ProcessOutput(
                    70, "", f'Error: user_already_exists: User "{user}" already exists\n'
                )
            self.rabbit_users[user] = "[]"
            self.rabbit_passwords[user] = password
            return ProcessOutput(0, f'Creating user "{user}" ...\n', "")
        if argv[:2] == ("/usr/sbin/rabbitmqctl", "change_password") and len(argv) == 4:
            user, password = argv[2], argv[3]
            if user not in self.rabbit_users:
                return ProcessOutput(67, "", f'Error: no_such_user: User "{user}" does not exist\n')
            self.rabbit_passwords[user] = password
            return ProcessOutput(0, f'Changing password for user "{user}" ...\n', "")
        if argv[:2] == ("/usr/sbin/rabbitmqctl", "set_permissions") and len(argv) == 6:
            user, conf, write, read = argv[2], argv[3], argv[4], argv[5]
            if user not in self.rabbit_users:
                return ProcessOutput(67, "", f'Error: no_such_user: User "{user}" does not exist\n')
            self.rabbit_permissions.setdefault(user, {})["/"] = (conf, write, read)
            return ProcessOutput(0, f'Setting permissions for user "{user}" in vhost "/" ...\n', "")
        if argv == ("/usr/bin/mariadb", "--batch", "--skip-column-names"):
            if stdin is None:
                return ProcessOutput(1, "", "ERROR: expected SQL on stdin\n")
            for match in _CREATE_DB_RE.finditer(stdin):
                self.mariadb_databases.add(match.group(1))
            for match in _GRANT_RE.finditer(stdin):
                db, user, host, password = (
                    match.group(1),
                    match.group(2),
                    match.group(3),
                    match.group(4),
                )
                self.mariadb_grants.add((user, host, db))
                self.mariadb_passwords[(user, host)] = password
            return ProcessOutput(0, "", "")
        if argv[:4] == ("/usr/bin/mariadb", "--batch", "--skip-column-names", "--execute"):
            db_rows = "".join(f"DB\t{db}\t\t\n" for db in sorted(self.mariadb_databases))
            grant_rows = "".join(
                f"GRANT\t{user}\t{host}\t{db}\n" for user, host, db in sorted(self.mariadb_grants)
            )
            return ProcessOutput(0, f"{db_rows}{grant_rows}", "")
        if argv == ("/usr/bin/keystone-manage", "db_sync"):
            self.keystone_db_synced = True
            return ProcessOutput(0, "", "")
        if argv == (
            "/usr/bin/keystone-manage",
            "fernet_setup",
            "--keystone-user",
            "keystone",
            "--keystone-group",
            "keystone",
        ):
            self._init_fernet_repo("etc/keystone/fernet-keys")
            return ProcessOutput(0, "", "")
        if argv == (
            "/usr/bin/keystone-manage",
            "credential_setup",
            "--keystone-user",
            "keystone",
            "--keystone-group",
            "keystone",
        ):
            self._init_fernet_repo("etc/keystone/credential-keys")
            return ProcessOutput(0, "", "")
        if argv[:3] == ("/usr/bin/keystone-manage", "bootstrap", "--bootstrap-password"):
            self.keystone_bootstrapped = {
                "password": argv[3],
                "admin_url": argv[5],
                "internal_url": argv[7],
                "public_url": argv[9],
                "region_id": argv[11],
            }
            return ProcessOutput(0, "", "")
        raise AssertionError(f"unexpected argv {argv!r}")

    def _init_fernet_repo(self, relative_dir: str) -> None:
        repo = self.root / relative_dir
        repo.mkdir(parents=True, exist_ok=True)
        for index in ("0", "1"):
            key_path = repo / index
            if not key_path.exists():
                key_path.write_text(f"fake-fernet-key-{index}\n")

    def _snapshot(self, event: str) -> None:
        if self.watch is not None:
            path = self.root / self.watch
            self.snapshots.append((event, path.read_text() if path.exists() else None))
        if self.watch_files:
            captured: dict[str, str | None] = {}
            for rel in self.watch_files:
                path = self.root / rel
                captured[rel] = path.read_text() if path.exists() else None
            self.multi_snapshots.append((event, captured))
