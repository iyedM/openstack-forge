"""Shared fake backend for the controller service step tests (runs nothing).

All host-derived fixtures in the controller service tests use RFC 5737 documentation
addresses (such as ``192.0.2.11`` and ``192.0.2.170``, with the development machine's
local IP sanitized to ``192.0.2.170`` for the repository).
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

from openstack_forge.execution import OPENSTACK_ADMIN_AUTH_ARGS, ProcessOutput

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
    mariadb, keystone-manage, and /usr/bin/openstack.

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
        openstack_projects: Sequence[Mapping[str, Any]] | None = None,
        openstack_users: Sequence[Mapping[str, Any]] | None = None,
        openstack_role_assignments: Sequence[Mapping[str, Any]] | None = None,
        openstack_services: Sequence[Mapping[str, Any]] | None = None,
        openstack_endpoints: Sequence[Mapping[str, Any]] | None = None,
        ovs_bridges: Mapping[str, Iterable[str]] | None = None,
        lvm_pvs: set[str] | frozenset[str] = frozenset(),
        lvm_vgs: set[str] | frozenset[str] = frozenset(),
    ) -> None:
        self.root = root
        self.installed = set(installed)
        self.active = set(active)
        self.enabled = set(enabled)
        self.reloaded: list[str] = []
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
        self.glance_db_synced: bool = False
        self.placement_db_synced: bool = False
        self.nova_api_db_synced: bool = False
        self.nova_db_synced: bool = False
        self.nova_cells: set[str] = set()
        self.neutron_db_upgraded: bool = False
        self.cinder_db_synced: bool = False
        self.ovs_bridges: dict[str, set[str]] = (
            {} if ovs_bridges is None else {br: set(ports) for br, ports in ovs_bridges.items()}
        )
        self.lvm_pvs: set[str] = set(lvm_pvs)
        self.lvm_vgs: set[str] = set(lvm_vgs)
        self.cloud_archive_enabled: bool = False
        self.openstack_projects: list[dict[str, Any]] = [
            dict(item) for item in (openstack_projects or ())
        ]
        self.openstack_users: list[dict[str, Any]] = [
            dict(item) for item in (openstack_users or ())
        ]
        self.openstack_user_passwords: dict[str, str] = {}
        self.openstack_role_assignments: list[dict[str, Any]] = [
            dict(item) for item in (openstack_role_assignments or ())
        ]
        self.openstack_services: list[dict[str, Any]] = [
            dict(item) for item in (openstack_services or ())
        ]
        self.openstack_endpoints: list[dict[str, Any]] = [
            dict(item) for item in (openstack_endpoints or ())
        ]
        self.openstack_os_passwords: list[str] = []
        self.calls: list[tuple[str, ...]] = []
        self.stdin_calls: list[tuple[tuple[str, ...], str | None]] = []
        self.env_calls: list[tuple[tuple[str, ...], dict[str, str]]] = []
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
        self.env_calls.append((argv, dict(env)))
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
            if package == "python3-openstackclient" and package not in self.no_candidate:
                if self.cloud_archive_enabled and package in self.installed:
                    return ProcessOutput(
                        0,
                        "python3-openstackclient:\n"
                        "  Installed: 7.4.0-0ubuntu1~cloud0\n"
                        "  Candidate: 7.4.0-0ubuntu1~cloud0\n"
                        "  Version table:\n"
                        " *** 7.4.0-0ubuntu1~cloud0 500\n"
                        "        500 http://ubuntu-cloud.archive.canonical.com/ubuntu "
                        "noble-updates/epoxy/main amd64 Packages\n"
                        "        100 /var/lib/dpkg/status\n"
                        "     6.6.0-0ubuntu2 500\n"
                        "        500 http://archive.ubuntu.com/ubuntu noble/main amd64 Packages\n",
                        "",
                    )
                if self.cloud_archive_enabled:
                    return ProcessOutput(
                        0,
                        "python3-openstackclient:\n"
                        "  Installed: (none)\n"
                        "  Candidate: 7.4.0-0ubuntu1~cloud0\n"
                        "  Version table:\n"
                        "     7.4.0-0ubuntu1~cloud0 500\n"
                        "        500 http://ubuntu-cloud.archive.canonical.com/ubuntu "
                        "noble-updates/epoxy/main amd64 Packages\n"
                        "     6.6.0-0ubuntu2 500\n"
                        "        500 http://archive.ubuntu.com/ubuntu noble/main amd64 Packages\n",
                        "",
                    )
                if package in self.installed:
                    return ProcessOutput(
                        0,
                        "python3-openstackclient:\n"
                        "  Installed: 6.6.0-0ubuntu2\n"
                        "  Candidate: 6.6.0-0ubuntu2\n"
                        "  Version table:\n"
                        " *** 6.6.0-0ubuntu2 500\n"
                        "        500 http://archive.ubuntu.com/ubuntu noble/main amd64 Packages\n"
                        "        100 /var/lib/dpkg/status\n",
                        "",
                    )
                return ProcessOutput(
                    0,
                    "python3-openstackclient:\n"
                    "  Installed: (none)\n"
                    "  Candidate: 6.6.0-0ubuntu2\n"
                    "  Version table:\n"
                    "     6.6.0-0ubuntu2 500\n"
                    "        500 http://archive.ubuntu.com/ubuntu noble/main amd64 Packages\n",
                    "",
                )
            installed = "3.4.30-1ubuntu0.24.04.3" if package in self.installed else "(none)"
            candidate = "(none)" if package in self.no_candidate else "3.4.30-1ubuntu0.24.04.3"
            return ProcessOutput(
                0,
                f"{package}:\n  Installed: {installed}\n  Candidate: {candidate}\n"
                " Version table:\n",
                "",
            )
        if argv == ("/usr/bin/apt-get", "update"):
            return ProcessOutput(0, "Hit:1 http://archive.ubuntu.com/ubuntu noble InRelease\n", "")
        if argv[:3] == ("/usr/bin/add-apt-repository", "--yes", "--cloud"):
            self.cloud_archive_enabled = True
            return ProcessOutput(0, "Repository: 'deb ... noble-updates/epoxy main'\n", "")
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
        if argv[:2] == ("/usr/bin/systemctl", "reload"):
            self._snapshot("reload")
            self.reloaded.append(argv[2])
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
        if argv == (
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
        ):
            password = env.get("OS_BOOTSTRAP_PASSWORD")
            if not password:
                return ProcessOutput(
                    1,
                    "ERROR: Either --bootstrap-password argument or "
                    "OS_BOOTSTRAP_PASSWORD must be set.\n",
                    "",
                )
            self.keystone_bootstrapped = {
                "password": password,
                "admin_url": argv[3],
                "internal_url": argv[5],
                "public_url": argv[7],
                "region_id": argv[9],
            }
            if not any(p.get("Name") == "admin" for p in self.openstack_projects):
                self.openstack_projects.append({"ID": "proj-admin", "Name": "admin"})
            if not any(u.get("Name") == "admin" for u in self.openstack_users):
                self.openstack_users.append({"ID": "user-admin", "Name": "admin"})
            self.openstack_user_passwords["admin"] = password
            return ProcessOutput(0, "", "")
        if argv == ("/usr/bin/glance-manage", "db_sync"):
            self._snapshot("glance-manage db_sync")
            conf_path = self.root / "etc/glance/glance-api.conf"
            conf_text = conf_path.read_text(encoding="utf-8") if conf_path.exists() else ""
            if "mysql+pymysql://glance:" not in conf_text or "@controller/glance" not in conf_text:
                return ProcessOutput(
                    1,
                    "",
                    "CRITICAL glance: [database] connection is not configured for MariaDB in "
                    "/etc/glance/glance-api.conf\n",
                )
            self.glance_db_synced = True
            return ProcessOutput(0, "", "")
        if argv == ("/usr/bin/placement-manage", "db", "sync"):
            self._snapshot("placement-manage db sync")
            conf_path = self.root / "etc/placement/placement.conf"
            conf_text = conf_path.read_text(encoding="utf-8") if conf_path.exists() else ""
            if (
                "mysql+pymysql://placement:" not in conf_text
                or "@controller/placement" not in conf_text
            ):
                return ProcessOutput(
                    1,
                    "",
                    "CRITICAL placement: [placement_database] connection is not configured for "
                    "MariaDB in /etc/placement/placement.conf\n",
                )
            self.placement_db_synced = True
            return ProcessOutput(0, "", "")
        if argv == ("/usr/bin/nova-manage", "api_db", "sync"):
            self._snapshot("nova-manage api_db sync")
            conf_path = self.root / "etc/nova/nova.conf"
            conf_text = conf_path.read_text(encoding="utf-8") if conf_path.exists() else ""
            if "mysql+pymysql://nova:" not in conf_text or "@controller/nova_api" not in conf_text:
                return ProcessOutput(
                    1,
                    "",
                    "CRITICAL nova: [api_database] connection is not configured for MariaDB in "
                    "/etc/nova/nova.conf\n",
                )
            self.nova_api_db_synced = True
            return ProcessOutput(0, "", "")
        if argv == ("/usr/bin/nova-manage", "cell_v2", "map_cell0"):
            self._snapshot("nova-manage cell_v2 map_cell0")
            conf_path = self.root / "etc/nova/nova.conf"
            conf_text = conf_path.read_text(encoding="utf-8") if conf_path.exists() else ""
            if (
                not self.nova_api_db_synced
                or "mysql+pymysql://nova:" not in conf_text
                or "@controller/nova" not in conf_text
            ):
                return ProcessOutput(
                    1,
                    "",
                    "CRITICAL nova: api_db must be synced and [database] connection configured "
                    "before map_cell0\n",
                )
            self.nova_cells.add("cell0")
            return ProcessOutput(0, "", "")
        if argv == ("/usr/bin/nova-manage", "cell_v2", "list_cells"):
            border = (
                "+-------+--------------------------------------+"
                "----------------------------------------------------+"
                "--------------------------------------------------------------+----------+"
            )
            rows: list[str] = [
                border,
                "|  Name |                 UUID                 |"
                "                   Transport URL                    |"
                "                     Database Connection                      | Disabled |",
                border,
            ]
            if "cell0" in self.nova_cells:
                rows.append(
                    "| cell0 | 00000000-0000-0000-0000-000000000000 |"
                    "                       none:/                       |"
                    " mysql+pymysql://nova:****@controller/nova_cell0?charset=utf8 |  False   |"
                )
            if "cell1" in self.nova_cells:
                rows.append(
                    "| cell1 | f690f4fd-2bc5-4f15-8145-db561a7b9d3d |"
                    " rabbit://openstack:****@controller:5672/           |"
                    " mysql+pymysql://nova:****@controller/nova?charset=utf8       |  False   |"
                )
            for extra in sorted(self.nova_cells - {"cell0", "cell1"}):
                rows.append(
                    f"| {extra} | 11111111-2222-3333-4444-555555555555 |"
                    " rabbit://openstack:****@controller:5672/           |"
                    " mysql+pymysql://nova:****@controller/nova?charset=utf8       |  False   |"
                )
            rows.append(border)
            return ProcessOutput(0, "\n".join(rows) + "\n", "")
        if argv == ("/usr/bin/nova-manage", "cell_v2", "create_cell", "--name=cell1", "--verbose"):
            self._snapshot("nova-manage cell_v2 create_cell")
            conf_path = self.root / "etc/nova/nova.conf"
            conf_text = conf_path.read_text(encoding="utf-8") if conf_path.exists() else ""
            if "mysql+pymysql://nova:" not in conf_text or "rabbit://openstack:" not in conf_text:
                return ProcessOutput(
                    1,
                    "Must specify --transport-url and --database_connection if not set in "
                    "/etc/nova/nova.conf\n",
                    "",
                )
            if "cell1" in self.nova_cells:
                return ProcessOutput(
                    2,
                    "The specified transport_url and/or database_connection combination already "
                    "exists for another cell with uuid f690f4fd-2bc5-4f15-8145-db561a7b9d3d.\n",
                    "",
                )
            self.nova_cells.add("cell1")
            return ProcessOutput(0, "f690f4fd-2bc5-4f15-8145-db561a7b9d3d\n", "")
        if argv == ("/usr/bin/nova-manage", "db", "sync"):
            self._snapshot("nova-manage db sync")
            conf_path = self.root / "etc/nova/nova.conf"
            conf_text = conf_path.read_text(encoding="utf-8") if conf_path.exists() else ""
            if "mysql+pymysql://nova:" not in conf_text or "@controller/nova" not in conf_text:
                return ProcessOutput(
                    1,
                    "",
                    "CRITICAL nova: [database] connection is not configured for MariaDB in "
                    "/etc/nova/nova.conf\n",
                )
            self.nova_db_synced = True
            return ProcessOutput(0, "", "")
        if argv == (
            "/usr/bin/neutron-db-manage",
            "--config-file",
            "/etc/neutron/neutron.conf",
            "--config-file",
            "/etc/neutron/plugins/ml2/ml2_conf.ini",
            "upgrade",
            "head",
        ):
            self._snapshot("neutron-db-manage upgrade head")
            conf_path = self.root / "etc/neutron/neutron.conf"
            ml2_path = self.root / "etc/neutron/plugins/ml2/ml2_conf.ini"
            conf_text = conf_path.read_text(encoding="utf-8") if conf_path.exists() else ""
            ml2_text = ml2_path.read_text(encoding="utf-8") if ml2_path.exists() else ""
            if (
                "mysql+pymysql://neutron:" not in conf_text
                or "@controller/neutron" not in conf_text
                or "mechanism_drivers = openvswitch,l2population" not in ml2_text
            ):
                return ProcessOutput(
                    1,
                    "",
                    "CRITICAL neutron: /etc/neutron/neutron.conf and "
                    "/etc/neutron/plugins/ml2/ml2_conf.ini must be configured before "
                    "neutron-db-manage upgrade head\n",
                )
            self.neutron_db_upgraded = True
            return ProcessOutput(0, "Running upgrade for neutron ...\nOK\n", "")
        if argv == ("/usr/bin/cinder-manage", "db", "sync"):
            self._snapshot("cinder-manage db sync")
            conf_path = self.root / "etc/cinder/cinder.conf"
            conf_text = conf_path.read_text(encoding="utf-8") if conf_path.exists() else ""
            if "mysql+pymysql://cinder:" not in conf_text or "@controller/cinder" not in conf_text:
                return ProcessOutput(
                    1,
                    "",
                    "CRITICAL cinder: [database] connection is not configured for MariaDB in "
                    "/etc/cinder/cinder.conf\n",
                )
            self.cinder_db_synced = True
            return ProcessOutput(0, "", "")
        if argv == ("/usr/sbin/pvs", "--noheadings", "-o", "pv_name"):
            rows = "".join(f"  {pv}\n" for pv in sorted(self.lvm_pvs))
            return ProcessOutput(0, rows, "")
        if len(argv) == 2 and argv[0] == "/usr/sbin/pvcreate":
            device = argv[1]
            if device in self.lvm_pvs:
                return ProcessOutput(
                    5,
                    "",
                    f'  Can\'t initialize physical volume "{device}" of volume group '
                    '"cinder-volumes" without -ff\n',
                )
            self.lvm_pvs.add(device)
            return ProcessOutput(0, f'  Physical volume "{device}" successfully created.\n', "")
        if argv == ("/usr/sbin/vgs", "--noheadings", "-o", "vg_name"):
            rows = "".join(f"  {vg}\n" for vg in sorted(self.lvm_vgs))
            return ProcessOutput(0, rows, "")
        if len(argv) == 3 and argv[:2] == ("/usr/sbin/vgcreate", "cinder-volumes"):
            device = argv[2]
            if "cinder-volumes" in self.lvm_vgs:
                return ProcessOutput(5, "", '  Volume group "cinder-volumes" already exists\n')
            if device not in self.lvm_pvs:
                return ProcessOutput(5, "", f'  Physical volume "{device}" not initialized\n')
            self.lvm_vgs.add("cinder-volumes")
            return ProcessOutput(0, '  Volume group "cinder-volumes" successfully created\n', "")
        if argv[:1] == ("/usr/bin/ovs-vsctl",):
            return self._run_ovs_vsctl(argv[1:])
        if argv[: len(OPENSTACK_ADMIN_AUTH_ARGS)] == OPENSTACK_ADMIN_AUTH_ARGS:
            return self._run_openstack_cli(argv[len(OPENSTACK_ADMIN_AUTH_ARGS) :], env)
        raise AssertionError(f"unexpected argv {argv!r}")

    def _run_ovs_vsctl(self, sub: tuple[str, ...]) -> ProcessOutput:
        if sub[:2] == ("--may-exist", "add-br") and len(sub) == 3:
            bridge = sub[2]
            self.ovs_bridges.setdefault(bridge, set())
            return ProcessOutput(0, "", "")
        if sub[:1] == ("add-br",) and len(sub) == 2:
            bridge = sub[1]
            if bridge in self.ovs_bridges:
                return ProcessOutput(
                    1,
                    "",
                    f"ovs-vsctl: cannot create a bridge named {bridge} because a bridge "
                    f"named {bridge} already exists\n",
                )
            self.ovs_bridges[bridge] = set()
            return ProcessOutput(0, "", "")
        if sub[:2] == ("--may-exist", "add-port") and len(sub) == 4:
            bridge, interface = sub[2], sub[3]
            if bridge not in self.ovs_bridges:
                return ProcessOutput(1, "", f"ovs-vsctl: no bridge named {bridge}\n")
            self.ovs_bridges[bridge].add(interface)
            return ProcessOutput(0, "", "")
        if sub[:1] == ("add-port",) and len(sub) == 3:
            bridge, interface = sub[1], sub[2]
            if bridge not in self.ovs_bridges:
                return ProcessOutput(1, "", f"ovs-vsctl: no bridge named {bridge}\n")
            if interface in self.ovs_bridges[bridge]:
                return ProcessOutput(
                    1,
                    "",
                    f"ovs-vsctl: cannot create a port named {interface} on {bridge} because "
                    f"a port named {interface} already exists on bridge {bridge}\n",
                )
            self.ovs_bridges[bridge].add(interface)
            return ProcessOutput(0, "", "")
        if sub[:1] == ("br-exists",) and len(sub) == 2:
            bridge = sub[1]
            return ProcessOutput(0 if bridge in self.ovs_bridges else 2, "", "")
        if sub[:1] == ("list-ports",) and len(sub) == 2:
            bridge = sub[1]
            if bridge not in self.ovs_bridges:
                return ProcessOutput(1, "", f"ovs-vsctl: no bridge named {bridge}\n")
            ports = "".join(f"{p}\n" for p in sorted(self.ovs_bridges[bridge]))
            return ProcessOutput(0, ports, "")
        raise AssertionError(f"unexpected ovs-vsctl subcommand {sub!r}")

    def _run_openstack_cli(self, sub: tuple[str, ...], env: Mapping[str, str]) -> ProcessOutput:
        os_password = env.get("OS_PASSWORD")
        if not os_password:
            return ProcessOutput(
                1, "", "Missing value auth-password required for auth plugin password\n"
            )
        if (
            self.keystone_bootstrapped is not None
            and os_password != self.keystone_bootstrapped["password"]
        ):
            return ProcessOutput(
                1, "", "The request you have made requires authentication. (HTTP 401)\n"
            )
        self.openstack_os_passwords.append(os_password)

        if sub == ("project", "list", "--domain", "default", "-f", "json"):
            return ProcessOutput(0, json.dumps(self.openstack_projects), "")
        if sub == (
            "project",
            "create",
            "--domain",
            "default",
            "--description",
            "Service Project",
            "service",
            "-f",
            "json",
        ):
            if any(p.get("Name") == "service" for p in self.openstack_projects):
                return ProcessOutput(
                    1,
                    "",
                    "Conflict occurred attempting to store project - Duplicate Entry (HTTP 409)\n",
                )
            created = {"ID": f"proj-{len(self.openstack_projects) + 1}", "Name": "service"}
            self.openstack_projects.append(created)
            return ProcessOutput(0, json.dumps(created), "")
        if sub == ("user", "list", "--domain", "default", "-f", "json"):
            return ProcessOutput(0, json.dumps(self.openstack_users), "")
        if (
            len(sub) == 9
            and sub[:5] == ("user", "create", "--domain", "default", "--password")
            and sub[7:] == ("-f", "json")
        ):
            user_password, user = sub[5], sub[6]
            if any(u.get("Name") == user for u in self.openstack_users):
                return ProcessOutput(
                    1,
                    "",
                    f"Conflict occurred attempting to store user - Duplicate Entry {user} "
                    "(HTTP 409)\n",
                )
            created_user = {"ID": f"user-{user}", "Name": user}
            self.openstack_users.append(created_user)
            self.openstack_user_passwords[user] = user_password
            return ProcessOutput(0, json.dumps(created_user), "")
        if len(sub) == 7 and sub[:5] == ("user", "set", "--domain", "default", "--password"):
            user_password, user = sub[5], sub[6]
            if not any(u.get("Name") == user for u in self.openstack_users):
                return ProcessOutput(1, "", f"No user with a name or ID of '{user}' exists.\n")
            self.openstack_user_passwords[user] = user_password
            return ProcessOutput(0, "", "")
        if sub == ("role", "assignment", "list", "--names", "-f", "json"):
            return ProcessOutput(0, json.dumps(self.openstack_role_assignments), "")
        if len(sub) == 7 and sub[:3] == ("role", "add", "--project") and sub[4] == "--user":
            project, user, role = sub[3], sub[5], sub[6]
            entry = {
                "Role": role,
                "User": f"{user}@Default",
                "Group": "",
                "Project": f"{project}@Default",
                "Domain": "",
                "System": "",
                "Inherited": False,
            }
            if entry not in self.openstack_role_assignments:
                self.openstack_role_assignments.append(entry)
            return ProcessOutput(0, "", "")
        if sub == ("service", "list", "-f", "json"):
            return ProcessOutput(0, json.dumps(self.openstack_services), "")
        if (
            len(sub) == 7
            and sub[:3] == ("service", "create", "--name")
            and sub[5:] == ("-f", "json")
        ):
            service_name, service_type = sub[3], sub[4]
            # Keystone v3 creates a duplicate UUID if service create is invoked twice.
            svc = {
                "ID": f"svc-{len(self.openstack_services) + 1}",
                "Name": service_name,
                "Type": service_type,
            }
            self.openstack_services.append(svc)
            return ProcessOutput(0, json.dumps(svc), "")
        if sub == ("endpoint", "list", "-f", "json"):
            return ProcessOutput(0, json.dumps(self.openstack_endpoints), "")
        if (
            len(sub) == 9
            and sub[:4] == ("endpoint", "create", "--region", "RegionOne")
            and sub[7:] == ("-f", "json")
        ):
            service_name, interface, url = sub[4], sub[5], sub[6]
            matching = [
                svc
                for svc in self.openstack_services
                if svc.get("Name") == service_name or svc.get("Type") == service_name
            ]
            if not matching:
                return ProcessOutput(
                    1, "", f"No service with a type, name or ID of '{service_name}' exists.\n"
                )
            svc = matching[0]
            # Keystone v3 creates a duplicate UUID if endpoint create is invoked twice.
            ep = {
                "ID": f"ep-{len(self.openstack_endpoints) + 1}",
                "Region": "RegionOne",
                "Service Name": svc["Name"],
                "Service Type": svc["Type"],
                "Enabled": True,
                "Interface": interface,
                "URL": url,
            }
            self.openstack_endpoints.append(ep)
            return ProcessOutput(0, json.dumps(ep), "")
        raise AssertionError(f"unexpected openstack subcommand {sub!r}")

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


CLUSTER_PACKAGE_CREATES: Mapping[str, CreateEntry] = {
    "chrony": ("etc/chrony/chrony.conf", "pool ntp.ubuntu.com iburst maxsources 4\n"),
    "mariadb-server": ("etc/mysql/mariadb.conf.d/.placeholder", ""),
    "memcached": (
        "etc/memcached.conf",
        "-d\nlogfile /var/log/memcached.log\n-m 64\n-p 11211\n-u memcache\n-l 127.0.0.1\n",
    ),
    "etcd-server": ("etc/default/etcd", "# etcd default settings\n"),
    "keystone": (
        (
            "etc/keystone/keystone.conf",
            "[DEFAULT]\n[database]\nconnection = sqlite:////var/lib/keystone/keystone.db\n[token]\n",
        ),
        (
            "etc/apache2/apache2.conf",
            "# Global configuration\nIncludeOptional sites-enabled/*.conf\n",
        ),
    ),
    "glance": (
        "etc/glance/glance-api.conf",
        "[DEFAULT]\n[database]\nconnection = sqlite:////var/lib/glance/glance.sqlite\n"
        "[keystone_authtoken]\n[paste_deploy]\n",
    ),
    "placement-api": (
        "etc/placement/placement.conf",
        "[DEFAULT]\n[placement_database]\n"
        "connection = sqlite:////var/lib/placement/placement.sqlite\n"
        "[api]\n[keystone_authtoken]\n",
    ),
    "nova-api": (
        "etc/nova/nova.conf",
        "[DEFAULT]\nmy_ip = 127.0.0.1\n"
        "[api_database]\nconnection = sqlite:////var/lib/nova/nova_api.sqlite\n"
        "[database]\nconnection = sqlite:////var/lib/nova/nova.sqlite\n"
        "[api]\n[keystone_authtoken]\n[vnc]\n[glance]\n"
        "[oslo_concurrency]\nlock_path = /var/lib/nova/tmp\n[placement]\n",
    ),
    "nova-compute": (
        (
            "etc/nova/nova.conf",
            "[DEFAULT]\n[api]\n[keystone_authtoken]\n[vnc]\n[glance]\n"
            "[oslo_concurrency]\nlock_path = /var/lib/nova/tmp\n[placement]\n",
        ),
        (
            "etc/nova/nova-compute.conf",
            "[DEFAULT]\ncompute_driver = libvirt.LibvirtDriver\n[libvirt]\nvirt_type = kvm\n",
        ),
    ),
    "neutron-server": (
        (
            "etc/neutron/neutron.conf",
            "[DEFAULT]\ncore_plugin = ml2\n"
            "[database]\nconnection = sqlite:////var/lib/neutron/neutron.sqlite\n"
            "[keystone_authtoken]\n[nova]\n"
            "[oslo_concurrency]\nlock_path = /var/lib/neutron/tmp\n",
        ),
        (
            "etc/neutron/plugins/ml2/ml2_conf.ini",
            "[DEFAULT]\n[ml2]\n[ml2_type_flat]\n[ml2_type_vxlan]\n[securitygroup]\n",
        ),
    ),
    "neutron-openvswitch-agent": (
        (
            "etc/neutron/neutron.conf",
            "[DEFAULT]\n[keystone_authtoken]\n"
            "[oslo_concurrency]\nlock_path = /var/lib/neutron/tmp\n",
        ),
        (
            "etc/neutron/plugins/ml2/openvswitch_agent.ini",
            "[DEFAULT]\n[ovs]\n[agent]\n[securitygroup]\n",
        ),
    ),
    "neutron-dhcp-agent": ("etc/neutron/dhcp_agent.ini", "[DEFAULT]\n"),
    "neutron-metadata-agent": ("etc/neutron/metadata_agent.ini", "[DEFAULT]\n"),
    "neutron-l3-agent": ("etc/neutron/l3_agent.ini", "[DEFAULT]\n"),
    "openstack-dashboard": (
        (
            "etc/openstack-dashboard/local_settings.py",
            'OPENSTACK_HOST = "127.0.0.1"\n'
            'OPENSTACK_KEYSTONE_URL = "http://%s:5000/identity/v3" % OPENSTACK_HOST\n'
            'OPENSTACK_KEYSTONE_DEFAULT_ROLE = "member"\n'
            'TIME_ZONE = "UTC"\n'
            "ALLOWED_HOSTS = ['*']\n",
        ),
        (
            "etc/apache2/conf-available/openstack-dashboard.conf",
            "WSGIProcessGroup horizon\nWSGIApplicationGroup %{GLOBAL}\n",
        ),
    ),
    "cinder-api": (
        "etc/cinder/cinder.conf",
        "[DEFAULT]\nrootwrap_config = /etc/cinder/rootwrap.conf\n"
        "[database]\nconnection = sqlite:////var/lib/cinder/cinder.sqlite\n"
        "[keystone_authtoken]\n[oslo_concurrency]\nlock_path = /var/lib/cinder/tmp\n",
    ),
    "lvm2": (
        "etc/lvm/lvm.conf",
        'config {\n\tchecks = 1\n}\ndevices {\n\tdir = "/dev"\n\t# filter = [ "a|.*|" ]\n}\n',
    ),
    "cinder-volume": (
        (
            "etc/cinder/cinder.conf",
            "[DEFAULT]\nrootwrap_config = /etc/cinder/rootwrap.conf\n"
            "[database]\nconnection = sqlite:////var/lib/cinder/cinder.sqlite\n"
            "[keystone_authtoken]\n[oslo_concurrency]\nlock_path = /var/lib/cinder/tmp\n",
        ),
        ("etc/tgt/conf.d/cinder.conf", "# Confirm cinder_tgt.conf is loaded\n"),
    ),
}


def make_node_service_host(root: Path, hostname: str) -> ServiceHost:
    """Seed ``root`` with base OS files and return a ``ServiceHost`` for all 18 steps."""
    (root / "etc/default").mkdir(parents=True, exist_ok=True)
    hosts_path = root / "etc/hosts"
    hosts_path.parent.mkdir(parents=True, exist_ok=True)
    if not hosts_path.exists():
        hosts_path.write_text(
            f"127.0.0.1 localhost\n127.0.1.1 {hostname}\n",
            encoding="utf-8",
        )
    cpuinfo_path = root / "proc/cpuinfo"
    cpuinfo_path.parent.mkdir(parents=True, exist_ok=True)
    if not cpuinfo_path.exists():
        cpuinfo_path.write_text(
            "processor\t: 0\n"
            "flags\t\t: fpu vme de pse tsc msr pae mce cx8 apic sep mtrr pge mca cmov "
            "pat pse36 clflush mmx fxsr sse sse2 vmx\n",
            encoding="utf-8",
        )
    return ServiceHost(
        root,
        installed={"software-properties-common"},
        starts={"rabbitmq-server", "apache2"},
        creates=CLUSTER_PACKAGE_CREATES,
    )
