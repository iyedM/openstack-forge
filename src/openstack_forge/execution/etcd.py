"""The ``etcd`` step: a single-node etcd on the controller.

Follows the official page
(https://docs.openstack.org/install-guide/environment-etcd-ubuntu.html), which says:

1. ``apt install etcd``, with the notes that since Ubuntu 18.04 the package needs the
   Universe repository and that "for Ubuntu 24.04" the package is ``etcd-server``;
2. edit ``/etc/default/etcd`` and set (``10.0.0.11`` being the controller's management
   IP address, ``controller`` its name)::

       ETCD_NAME="controller"
       ETCD_DATA_DIR="/var/lib/etcd"
       ETCD_INITIAL_CLUSTER_STATE="new"
       ETCD_INITIAL_CLUSTER_TOKEN="etcd-cluster-01"
       ETCD_INITIAL_CLUSTER="controller=http://10.0.0.11:2380"
       ETCD_INITIAL_ADVERTISE_PEER_URLS="http://10.0.0.11:2380"
       ETCD_ADVERTISE_CLIENT_URLS="http://10.0.0.11:2379"
       ETCD_LISTEN_PEER_URLS="http://0.0.0.0:2380"
       ETCD_LISTEN_CLIENT_URLS="http://10.0.0.11:2379"

3. ``systemctl enable etcd`` and ``systemctl restart etcd``.

What this step does: it installs ``etcd-server`` (``apt-cache policy`` on an Ubuntu 24.04
development machine shows no candidate for ``etcd`` and ``etcd-server`` 3.4.30 in
``noble/universe``; the step enables no repository). It writes the nine keys in a managed
block, commenting out active assignments of the same nine keys elsewhere in the file, in one
atomic edit; other lines are kept. The IP address is the controller's ``management_ip`` and
the name is the ``controller`` alias that the ``hosts-file`` step writes to ``/etc/hosts``;
the data directory, token, state and ports are the page's literal values. Then it runs the
page's ``systemctl enable etcd`` and ``systemctl restart etcd``. It is a single-node cluster,
as in the page (the configuration allows exactly one controller).

``etcd-server`` (``3.4.30-1ubuntu0.24.04.3`` on the Ubuntu 24.04 development machine) ships
``/etc/default/etcd`` and ``/usr/lib/systemd/system/etcd.service`` (``dpkg -S``). Read-only
inspection of those files on the development machine confirms:

- ``/usr/lib/systemd/system/etcd.service`` sets ``Environment=DAEMON_ARGS=``,
  ``Environment=ETCD_NAME=%H`` and ``Environment=ETCD_DATA_DIR=/var/lib/etcd/default``, then
  loads ``EnvironmentFile=-/etc/default/%p`` (``/etc/default/etcd``, overriding the unit's
  ``Environment=`` lines) before ``ExecStart=/usr/bin/etcd $DAEMON_ARGS``;
- ``/etc/default/etcd`` is a dpkg conffile (MD5 ``b7921986da4a49739814359cc296d2fb`` in
  ``dpkg-query -W -f='${Conffiles}\n' etcd-server``) whose pristine package content is the
  five-line ``##`` comment header with no ``ETCD_*`` assignments at all (on the development
  machine ``/etc/default/etcd`` had already been hand-edited to append the nine OpenStack
  guide ``ETCD_*`` assignments after that five-line header, with its local IP sanitized to
  the RFC 5737 documentation address ``192.0.2.170`` in the repository test fixture).
- What remains unconfirmed without starting ``etcd`` (``etcd(1)`` lists only command-line
  flags, and ``/usr/share/doc/etcd-server/op-guide/configuration.md.gz`` is gzipped) is that
  ``/usr/bin/etcd`` honors the remaining seven ``ETCD_*`` environment variables at runtime
  (see :data:`UNCONFIRMED`).

Before ``apt-get install``, ``apply()`` runs ``apt-cache policy etcd-server`` and fails with
a :class:`PreconditionError` if there is no installation candidate (which happens when the
Universe repository is disabled), without enabling any repository.

Security: etcd is unauthenticated and unencrypted (plain ``http``) in this configuration;
clients connect on the management IP and, as on the page, peers on all interfaces
(``0.0.0.0:2380``). The management network must be trusted. The step changes no firewall.

The step is UNVERIFIED and has never run on a real host.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from ipaddress import IPv4Address
from pathlib import Path
from typing import Any, ClassVar

from openstack_forge.execution.apt_output import parse_apt_policy
from openstack_forge.execution.commands import (
    APT_CACHE_POLICY,
    APT_GET_INSTALL,
    SYSTEMCTL_ENABLE,
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
    _managed_block_editor,
    apply_line_edit,
    disable_lines_and_set_block,
)
from openstack_forge.execution.fsutil import read_text_file, resolve_target
from openstack_forge.execution.hosts import CONTROLLER_ALIAS
from openstack_forge.execution.preview import PlannedCommand, StepPreview
from openstack_forge.execution.service_checks import (
    package_installed,
    query_active,
    query_enabled,
    query_package,
    unit_active_problems,
    unit_enabled_problems,
)

ETCD_STEP_ID = "etcd"
ETCD_DEFAULTS_TARGET = "/etc/default/etcd"
ETCD_BLOCK_NAME = "openstack-forge etcd"
ETCD_PACKAGE = "etcd-server"  # the page's Ubuntu 24.04 name; "etcd" has no candidate
ETCD_UNIT = "etcd"  # etcd-server ships etcd.service (dpkg -S)

# The page's literal values (not configuration).
ETCD_DATA_DIR = "/var/lib/etcd"
ETCD_INITIAL_CLUSTER_STATE = "new"
ETCD_INITIAL_CLUSTER_TOKEN = "etcd-cluster-01"
ETCD_PEER_PORT = 2380
ETCD_CLIENT_PORT = 2379
ETCD_LISTEN_PEER_URLS = f"http://0.0.0.0:{ETCD_PEER_PORT}"

ETCD_KEYS = (
    "ETCD_NAME",
    "ETCD_DATA_DIR",
    "ETCD_INITIAL_CLUSTER_STATE",
    "ETCD_INITIAL_CLUSTER_TOKEN",
    "ETCD_INITIAL_CLUSTER",
    "ETCD_INITIAL_ADVERTISE_PEER_URLS",
    "ETCD_ADVERTISE_CLIENT_URLS",
    "ETCD_LISTEN_PEER_URLS",
    "ETCD_LISTEN_CLIENT_URLS",
)
# An active assignment of one of the page's keys (optionally "export KEY=...").
ASSIGNMENT_RE = re.compile(rf"^\s*(?:export\s+)?(?:{'|'.join(ETCD_KEYS)})\s*=")

INSTALL_ETCD = PlannedCommand(APT_GET_INSTALL, {"package": ETCD_PACKAGE})
ENABLE_ETCD = PlannedCommand(SYSTEMCTL_ENABLE, {"unit": ETCD_UNIT})
RESTART_ETCD = PlannedCommand(SYSTEMCTL_RESTART, {"unit": ETCD_UNIT})
QUERY_ETCD_POLICY = PlannedCommand(APT_CACHE_POLICY, {"package": ETCD_PACKAGE})
QUERY_ETCD = query_package(ETCD_PACKAGE)
QUERY_ETCD_ENABLED = query_enabled(ETCD_UNIT)
QUERY_ETCD_ACTIVE = query_active(ETCD_UNIT)

PACKAGE_NOTE = (
    "In Ubuntu 24.04 etcd-server 3.4.30-1ubuntu0.24.04.3, etcd.service sets "
    "Environment=ETCD_NAME=%H and Environment=ETCD_DATA_DIR=/var/lib/etcd/default, then "
    "loads EnvironmentFile=-/etc/default/%p (/etc/default/etcd, whose pristine conffile has "
    "only a five-line comment header and no ETCD_* assignments) before ExecStart=/usr/bin/etcd "
    "$DAEMON_ARGS."
)
UNCONFIRMED = (
    "Unconfirmed without starting etcd (etcd(1) lists only command-line flags): that "
    "/usr/bin/etcd honors the seven ETCD_* environment variables beyond ETCD_NAME and "
    "ETCD_DATA_DIR at runtime."
)
NOT_CHECKED = (
    "No TCP connection to http://{address}:{port} is attempted and cluster health is not "
    "checked. {unconfirmed}"
)


def etcd_settings(local_node: Mapping[str, Any]) -> tuple[tuple[str, str], ...]:
    """The page's nine keys, in its order, for the controller ``local_node``."""
    address = IPv4Address(local_node["management_ip"])
    peer_url = f"http://{address}:{ETCD_PEER_PORT}"
    client_url = f"http://{address}:{ETCD_CLIENT_PORT}"
    return (
        ("ETCD_NAME", CONTROLLER_ALIAS),
        ("ETCD_DATA_DIR", ETCD_DATA_DIR),
        ("ETCD_INITIAL_CLUSTER_STATE", ETCD_INITIAL_CLUSTER_STATE),
        ("ETCD_INITIAL_CLUSTER_TOKEN", ETCD_INITIAL_CLUSTER_TOKEN),
        ("ETCD_INITIAL_CLUSTER", f"{CONTROLLER_ALIAS}={peer_url}"),
        ("ETCD_INITIAL_ADVERTISE_PEER_URLS", peer_url),
        ("ETCD_ADVERTISE_CLIENT_URLS", client_url),
        ("ETCD_LISTEN_PEER_URLS", ETCD_LISTEN_PEER_URLS),
        ("ETCD_LISTEN_CLIENT_URLS", client_url),
    )


def etcd_block(local_node: Mapping[str, Any]) -> str:
    """The managed block content: ``KEY="value"`` lines, as on the page."""
    return "".join(f'{key}="{value}"\n' for key, value in etcd_settings(local_node))


def edit_etcd_defaults(root: Path, local_node: Mapping[str, Any], *, dry_run: bool) -> FileChange:
    """Disable other assignments of the keys and set the block, in one edit."""
    path, display = resolve_target(root, ETCD_DEFAULTS_TARGET)
    if read_text_file(path, display) is None:
        raise PreconditionError(
            f"{display} does not exist; it is installed by the {ETCD_PACKAGE} package"
        )
    return disable_lines_and_set_block(
        root,
        ETCD_DEFAULTS_TARGET,
        ASSIGNMENT_RE,
        ETCD_BLOCK_NAME,
        etcd_block(local_node),
        dry_run=dry_run,
    )


def verify_etcd_text(text: str, local_node: Mapping[str, Any]) -> list[str]:
    """Pure check: the block has exactly the nine lines, no other active assignment."""
    lines = text.split("\n")
    begin, end = f"# BEGIN {ETCD_BLOCK_NAME}", f"# END {ETCD_BLOCK_NAME}"
    begins = [i for i, line in enumerate(lines) if line.strip() == begin]
    ends = [i for i, line in enumerate(lines) if line.strip() == end]
    if not begins and not ends:
        return [f"the managed block '{ETCD_BLOCK_NAME}' is missing"]
    if len(begins) != 1 or len(ends) != 1 or ends[0] < begins[0]:
        return [f"the managed block '{ETCD_BLOCK_NAME}' has broken markers"]
    body = {
        line.strip().partition("=")[0]: line.strip().partition("=")[2]
        for line in lines[begins[0] + 1 : ends[0]]
        if line.strip()
    }
    problems: list[str] = []
    for key, value in etcd_settings(local_node):
        expected = f'"{value}"'
        if key not in body:
            problems.append(f"{key} is missing from the managed block (expected {expected})")
        elif body[key] != expected:
            problems.append(f"{key} is {body[key]} in the managed block, expected {expected}")
    outside = [
        line.strip()
        for index, line in enumerate(lines)
        if not begins[0] <= index <= ends[0] and ASSIGNMENT_RE.match(line)
    ]
    if outside:
        problems.append(f"active assignment(s) outside the block: {', '.join(outside)}")
    return problems


@dataclass(frozen=True)
class EtcdStep:
    """Executable ``etcd`` step (file edit under ``root``, commands via ``executor``)."""

    root: Path
    config: Mapping[str, Any]
    local_node: Mapping[str, Any]
    executor: CommandExecutor
    id: str = ETCD_STEP_ID
    declares_commands: ClassVar[bool] = True

    def __post_init__(self) -> None:
        object.__setattr__(self, "root", Path(self.root))
        if self.local_node["role"] != "controller":
            raise ExecutionError(
                f"{self.id} only runs on the controller, not on "
                f"{self.local_node['hostname']!r} ({self.local_node['role']})"
            )

    def dry_run_preview(self) -> StepPreview:
        """Diff of the file edit (or the block to add) plus the commands it would run."""
        notes: list[str] = []
        try:
            file_changes: tuple[FileChange, ...] = (
                edit_etcd_defaults(self.root, self.local_node, dry_run=True),
            )
        except PreconditionError:
            file_changes = ()
            block = apply_line_edit(
                "",
                ETCD_DEFAULTS_TARGET,
                _managed_block_editor(ETCD_BLOCK_NAME, etcd_block(self.local_node)),
            )
            notes.append(
                f"{ETCD_DEFAULTS_TARGET} does not exist yet; it is installed by the "
                f"{ETCD_PACKAGE} package. After that, active assignments of these keys are "
                f"commented out and this block is appended:\n{block}"
            )
        notes += [
            "The file is edited after the install command and before enable and restart.",
            PACKAGE_NOTE,
            UNCONFIRMED,
            f"Before apt-get install, apply() runs 'apt-cache policy {ETCD_PACKAGE}' and fails "
            "if there is no installation candidate (on Ubuntu 24.04 the package is in the "
            "Universe repository; this step enables no repository).",
            "Security: etcd is unauthenticated and unencrypted here; clients use the "
            "management IP and peers listen on all interfaces (0.0.0.0:2380), as on the page. "
            "The management network must be trusted. No firewall rule is changed.",
            "apt-get install downloads from the configured apt sources (network).",
        ]
        return StepPreview(
            file_changes=file_changes,
            commands=(INSTALL_ETCD, ENABLE_ETCD, RESTART_ETCD),
            read_only_commands=(
                QUERY_ETCD_POLICY,
                QUERY_ETCD,
                QUERY_ETCD_ENABLED,
                QUERY_ETCD_ACTIVE,
            ),
            notes=tuple(notes),
        )

    def check(self) -> bool:
        """True if /etc/default/etcd needs no edit (pure), etcd-server is installed and
        the etcd unit is enabled."""
        try:
            if edit_etcd_defaults(self.root, self.local_node, dry_run=True).changed:
                return False
        except (PreconditionError, UnsafePathError):
            return False
        if not package_installed(self.executor, ETCD_PACKAGE):
            return False
        return not unit_enabled_problems(self.executor, ETCD_UNIT)

    def apply(self) -> None:
        """Require an apt candidate, install etcd-server, edit /etc/default/etcd, enable and
        restart etcd."""
        self._require_candidate()
        self.executor.run(INSTALL_ETCD.command_id, INSTALL_ETCD.params)
        edit_etcd_defaults(self.root, self.local_node, dry_run=False)
        self.executor.run(ENABLE_ETCD.command_id, ENABLE_ETCD.params)
        self.executor.run(RESTART_ETCD.command_id, RESTART_ETCD.params)

    def _require_candidate(self) -> None:
        result = self.executor.run(QUERY_ETCD_POLICY.command_id, QUERY_ETCD_POLICY.params)
        policy = parse_apt_policy(result.stdout)
        if policy.candidate is None:
            raise PreconditionError(
                f"'apt-cache policy {ETCD_PACKAGE}' reports no installation candidate "
                f"for {ETCD_PACKAGE} (Candidate: (none)). On Ubuntu 24.04 {ETCD_PACKAGE} "
                "is in the 'universe' repository, which is probably disabled (or the "
                "package lists have not been updated). Enable the 'universe' repository "
                "and update the package lists yourself, then run the step again; "
                "OpenStack Forge does not enable it."
            )

    def verify(self) -> VerifyResult:
        """The file has the page's keys, and etcd is enabled and active."""
        not_checked = NOT_CHECKED.format(
            address=self.local_node["management_ip"],
            port=ETCD_CLIENT_PORT,
            unconfirmed=UNCONFIRMED,
        )
        path, display = resolve_target(self.root, ETCD_DEFAULTS_TARGET)
        try:
            text = read_text_file(path, display)
        except (UnsafePathError, FileFormatError) as exc:
            return VerifyResult(False, f"{exc}. {not_checked}")
        problems = (
            [f"{display} does not exist"]
            if text is None
            else verify_etcd_text(text, self.local_node)
        )
        problems += unit_enabled_problems(self.executor, ETCD_UNIT)
        problems += unit_active_problems(self.executor, ETCD_UNIT)
        if problems:
            return VerifyResult(False, f"{'; '.join(problems)}. {not_checked}")
        return VerifyResult(
            True,
            f"{display} sets the page's nine ETCD_* keys; {ETCD_UNIT} is enabled and active. "
            f"{not_checked}",
        )


def make_etcd_step(
    root: Path,
    config: Mapping[str, Any],
    local_node: Mapping[str, Any],
    *,
    executor: CommandExecutor,
) -> EtcdStep:
    return EtcdStep(root=Path(root), config=config, local_node=local_node, executor=executor)
