"""The ``time-sync-controller`` step: chrony on the controller.

Follows the official controller page
(https://docs.openstack.org/install-guide/environment-ntp-controller.html), for Ubuntu:

0. (not in the page) ``apt-get update``, because a fresh image may have stale or empty
   package lists; it downloads them over the network;
1. install the ``chrony`` package;
2. in ``/etc/chrony/chrony.conf``, add ``server NTP_SERVER iburst`` for each configured
   server (the guide: "add, change, or remove the following keys as necessary"; here the
   active ``pool``/``server`` lines are commented out when servers are configured, and the
   distribution's default pool is kept when they are not), and ``allow <subnet>`` so the
   other nodes can synchronize from the controller;
3. restart the chrony service (the guide uses ``service chrony restart``; this step uses
   ``systemctl restart chrony``).

The step is UNVERIFIED and has never run on a real host. It does not check that time is
actually synchronized: the guide's NTP verification page has not been reviewed yet.

The ``time-sync-other`` step follows the official page for the other nodes
(https://docs.openstack.org/install-guide/environment-ntp-other.html) on ``compute``,
``block`` and ``object`` nodes, for Ubuntu:

0. (not in the page) ``apt-get update``, as above;
1. install the ``chrony`` package;
2. in ``/etc/chrony/chrony.conf``, "comment out or remove all but one ``server`` key" and
   change it to reference the controller node: every active ``pool``/``server`` line
   outside the managed block is commented out (the page also says to comment out the
   ``pool 2.debian.pool.ntp.org offline iburst`` line), and the managed block holds
   ``server controller iburst``; ``controller`` is the alias the ``hosts-file`` step writes
   to ``/etc/hosts``;
3. restart the chrony service (``systemctl restart chrony``, for the guide's
   ``service chrony restart``).

Only ``pool`` and ``server`` lines are commented out; other directives that can add time
sources (such as ``sourcedir``, ``confdir`` or ``include``) are left unchanged. This step
is UNVERIFIED too, has never run on a real host and does not check actual synchronization.

Both steps use only the reviewed commands already in the registry (``apt-get.update``,
``apt-get.install``, ``systemctl.restart``, ``systemctl.is-active``, ``dpkg-query.status``).
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from ipaddress import IPv4Network
from pathlib import Path
from typing import Any, ClassVar

from openstack_forge.execution.apt_output import parse_dpkg_status
from openstack_forge.execution.commands import (
    APT_GET_INSTALL,
    APT_GET_UPDATE,
    DPKG_QUERY_STATUS,
    SYSTEMCTL_IS_ACTIVE,
    SYSTEMCTL_RESTART,
    CommandExecutor,
)
from openstack_forge.execution.engine import VerifyResult
from openstack_forge.execution.errors import ExecutionError, FileFormatError, UnsafePathError
from openstack_forge.execution.files import (
    FileChange,
    _comment_out_editor,
    _managed_block_editor,
    _uncomment_editor,
    apply_line_edit,
    comment_out_lines,
    managed_block,
    uncomment_lines,
)
from openstack_forge.execution.fsutil import read_text_file, resolve_target, unified_diff
from openstack_forge.execution.hosts import CONTROLLER_ALIAS
from openstack_forge.execution.preview import PlannedCommand, StepPreview

TIME_SYNC_CONTROLLER_STEP_ID = "time-sync-controller"
TIME_SYNC_OTHER_STEP_ID = "time-sync-other"
CHRONY_CONF_TARGET = "/etc/chrony/chrony.conf"
CHRONY_BLOCK_NAME = "openstack-forge time-sync"
CHRONY_PACKAGE = "chrony"
CHRONY_UNIT = "chrony"  # the guide restarts it as "service chrony restart"

# Roles that run time-sync-other (the catalog's roles for it); the controller runs
# time-sync-controller instead.
OTHER_NODE_ROLES = ("compute", "block", "object")
# The guide's line for the other nodes; "controller" resolves through /etc/hosts.
OTHER_NODE_SERVER_LINE = f"server {CONTROLLER_ALIAS} iburst"

# Active "pool ..." and "server ..." source directives.
SOURCE_LINE_RE = re.compile(r"^\s*(?:pool|server)\s+\S")

UPDATE_PACKAGE_LISTS = PlannedCommand(APT_GET_UPDATE)
INSTALL_CHRONY = PlannedCommand(APT_GET_INSTALL, {"package": CHRONY_PACKAGE})
RESTART_CHRONY = PlannedCommand(SYSTEMCTL_RESTART, {"unit": CHRONY_UNIT})
QUERY_CHRONY_PACKAGE = PlannedCommand(DPKG_QUERY_STATUS, {"package": CHRONY_PACKAGE})
QUERY_CHRONY_ACTIVE = PlannedCommand(SYSTEMCTL_IS_ACTIVE, {"unit": CHRONY_UNIT})

TIME_SYNC_NOT_CHECKED = (
    "Actual time synchronization is not checked: the guide's NTP verification page has "
    "not been reviewed yet."
)


_NTP_SERVER_RE = re.compile(
    r"^(?=.{1,253}$)[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*$"
)


def build_chrony_block(config: Mapping[str, Any]) -> str:
    """The managed block content: one ``server`` line per NTP server, then ``allow``."""
    networks = config["networks"]
    if "management" not in networks:
        raise ExecutionError(
            "time-sync-controller needs networks.management: the guide's 'allow' line "
            "uses the management subnet"
        )
    subnet_raw = networks["management"]["subnet"]
    try:
        subnet = str(IPv4Network(subnet_raw, strict=True))
    except Exception as exc:
        raise ExecutionError(f"Invalid management subnet {subnet_raw!r}: {exc}") from exc
    servers = config["cluster"].get("ntp_servers", ())
    lines: list[str] = []
    for server in servers:
        if not isinstance(server, str) or not _NTP_SERVER_RE.fullmatch(server):
            raise ExecutionError(f"Invalid NTP server {server!r}")
        lines.append(f"server {server} iburst")
    lines.append(f"allow {subnet}")
    return "\n".join(lines) + "\n"


def build_chrony_other_block() -> str:
    """The managed block content on the other nodes: only ``server controller iburst``."""
    return OTHER_NODE_SERVER_LINE + "\n"


def edit_chrony_conf(root: Path, config: Mapping[str, Any], *, dry_run: bool) -> FileChange:
    """Edit ``/etc/chrony/chrony.conf`` under ``root``; the file must already exist.

    With ``ntp_servers``, active ``pool``/``server`` lines outside the managed block are
    commented out; without them, lines this step commented out earlier are restored.
    Everything else in the file is left as it is.
    """
    return _edit_chrony_conf(
        root,
        build_chrony_block(config),
        comment_out_sources=bool(config["cluster"].get("ntp_servers")),
        dry_run=dry_run,
    )


def edit_chrony_conf_other(root: Path, *, dry_run: bool) -> FileChange:
    """Edit ``/etc/chrony/chrony.conf`` under ``root`` for a non-controller node.

    The file must already exist. Every active ``pool``/``server`` line outside the managed
    block is commented out and the block holds ``server controller iburst``; everything
    else in the file is left as it is.
    """
    return _edit_chrony_conf(
        root, build_chrony_other_block(), comment_out_sources=True, dry_run=dry_run
    )


def _edit_chrony_conf(
    root: Path, block: str, *, comment_out_sources: bool, dry_run: bool
) -> FileChange:
    path, display = resolve_target(root, CHRONY_CONF_TARGET)
    before = read_text_file(path, display)
    if before is None:
        raise UnsafePathError(
            f"{display} does not exist; it is installed by the {CHRONY_PACKAGE} package"
        )

    if dry_run:
        sources = (
            _comment_out_editor(SOURCE_LINE_RE, exclude_block=CHRONY_BLOCK_NAME)
            if comment_out_sources
            else _uncomment_editor(SOURCE_LINE_RE)
        )
        after = apply_line_edit(before, display, sources)
        after = apply_line_edit(after, display, _managed_block_editor(CHRONY_BLOCK_NAME, block))
        if after == before:
            return FileChange(path=display, changed=False, diff="")
        return FileChange(path=display, changed=True, diff=unified_diff(before, after, display))

    if comment_out_sources:
        first = comment_out_lines(
            root, CHRONY_CONF_TARGET, SOURCE_LINE_RE, exclude_block=CHRONY_BLOCK_NAME
        )
    else:
        first = uncomment_lines(root, CHRONY_CONF_TARGET, SOURCE_LINE_RE)
    second = managed_block(root, CHRONY_CONF_TARGET, CHRONY_BLOCK_NAME, block)
    if not first.changed and not second.changed:
        return FileChange(path=display, changed=False, diff="")
    after = read_text_file(path, display) or ""
    return FileChange(
        path=display,
        changed=True,
        diff=unified_diff(before, after, display),
        backup=first.backup or second.backup,
    )


def verify_chrony_text(text: str, config: Mapping[str, Any]) -> list[str]:
    """Pure check of chrony.conf content; returns the problems found (empty if fine)."""
    block = build_chrony_block(config).strip().split("\n")
    return _verify_block_text(
        text,
        block,
        required=block[-1],  # the allow line
        differs="the managed block differs from the configuration",
        forbid_outside_sources=bool(config["cluster"].get("ntp_servers")),
    )


def verify_chrony_other_text(text: str) -> list[str]:
    """Pure check of a non-controller node's chrony.conf; returns the problems found.

    The managed block must be intact and hold exactly ``server controller iburst``, and
    no active ``pool``/``server`` line may remain outside it.
    """
    return _verify_block_text(
        text,
        [OTHER_NODE_SERVER_LINE],
        required=OTHER_NODE_SERVER_LINE,
        differs="the managed block differs from the expected content",
        forbid_outside_sources=True,
    )


def _verify_block_text(
    text: str,
    expected: list[str],
    *,
    required: str,
    differs: str,
    forbid_outside_sources: bool,
) -> list[str]:
    lines = text.split("\n")
    begin, end = f"# BEGIN {CHRONY_BLOCK_NAME}", f"# END {CHRONY_BLOCK_NAME}"
    begins = [i for i, line in enumerate(lines) if line.strip() == begin]
    ends = [i for i, line in enumerate(lines) if line.strip() == end]
    if not begins and not ends:
        return [f"the managed block '{CHRONY_BLOCK_NAME}' is missing"]
    if len(begins) != 1 or len(ends) != 1 or ends[0] < begins[0]:
        return [f"the managed block '{CHRONY_BLOCK_NAME}' has broken markers"]

    problems: list[str] = []
    body = [line.strip() for line in lines[begins[0] + 1 : ends[0]]]
    if required not in body:
        problems.append(f"'{required}' is missing from the managed block")
    if body != expected:
        problems.append(differs)
    if forbid_outside_sources:
        outside = [
            line.strip()
            for index, line in enumerate(lines)
            if not begins[0] <= index <= ends[0] and SOURCE_LINE_RE.match(line)
        ]
        if outside:
            problems.append(f"active source line(s) outside the block: {', '.join(outside)}")
    return problems


@dataclass(frozen=True)
class TimeSyncControllerStep:
    """Executable ``time-sync-controller`` step (file edits under ``root``, commands via
    ``executor``)."""

    root: Path
    config: Mapping[str, Any]
    local_node: Mapping[str, Any]
    executor: CommandExecutor
    id: str = TIME_SYNC_CONTROLLER_STEP_ID
    declares_commands: ClassVar[bool] = True

    def __post_init__(self) -> None:
        object.__setattr__(self, "root", Path(self.root))
        if self.local_node["role"] != "controller":
            raise ExecutionError(
                f"{self.id} only runs on the controller, not on "
                f"{self.local_node['hostname']!r} ({self.local_node['role']})"
            )
        build_chrony_block(self.config)  # fail early without a management network

    def dry_run_preview(self) -> StepPreview:
        """Diff of the file edit plus the commands apply()/check()/verify() would run."""
        notes: list[str] = []
        try:
            file_changes: tuple[FileChange, ...] = (
                edit_chrony_conf(self.root, self.config, dry_run=True),
            )
        except UnsafePathError:
            file_changes = ()
            block = apply_line_edit(
                "", CHRONY_CONF_TARGET, _managed_block_editor(CHRONY_BLOCK_NAME, self._block())
            )
            notes.append(
                f"{CHRONY_CONF_TARGET} does not exist yet; it is installed by the "
                f"{CHRONY_PACKAGE} package, after which this block is appended:\n{block}"
            )
        notes.append("The file is edited after the install command and before the restart.")
        notes.append(
            "apt-get update and apt-get install download from the configured apt sources "
            "(network); OpenStack Forge's Python code opens no connection itself."
        )
        return StepPreview(
            file_changes=file_changes,
            commands=(UPDATE_PACKAGE_LISTS, INSTALL_CHRONY, RESTART_CHRONY),
            read_only_commands=(QUERY_CHRONY_PACKAGE, QUERY_CHRONY_ACTIVE),
            notes=tuple(notes),
        )

    def check(self) -> bool:
        """True if chrony.conf needs no edit and the chrony package is installed."""
        try:
            if edit_chrony_conf(self.root, self.config, dry_run=True).changed:
                return False
        except UnsafePathError:
            return False
        return _chrony_installed(self.executor)

    def apply(self) -> None:
        """Update the package lists, install chrony, edit chrony.conf, restart chrony."""
        # A fresh image may have stale or empty package lists, and then the install fails.
        self.executor.run(UPDATE_PACKAGE_LISTS.command_id, UPDATE_PACKAGE_LISTS.params)
        self.executor.run(INSTALL_CHRONY.command_id, INSTALL_CHRONY.params)
        edit_chrony_conf(self.root, self.config, dry_run=False)
        self.executor.run(RESTART_CHRONY.command_id, RESTART_CHRONY.params)

    def verify(self) -> VerifyResult:
        """chrony.conf has the expected block and allow line, and chrony is active."""
        path, display = resolve_target(self.root, CHRONY_CONF_TARGET)
        try:
            text = read_text_file(path, display)
        except (UnsafePathError, FileFormatError) as exc:
            return VerifyResult(False, str(exc))
        problems = (
            [f"{display} does not exist"] if text is None else verify_chrony_text(text, self.config)
        )
        problems += _chrony_service_problems(self.executor)
        if problems:
            return VerifyResult(False, "; ".join(problems))
        return VerifyResult(
            True,
            f"{display} has the managed block; {CHRONY_UNIT} is active. {TIME_SYNC_NOT_CHECKED}",
        )

    def _block(self) -> str:
        return build_chrony_block(self.config)


@dataclass(frozen=True)
class TimeSyncOtherStep:
    """Executable ``time-sync-other`` step on a compute, block or object node (file edits
    under ``root``, commands via ``executor``)."""

    root: Path
    config: Mapping[str, Any]
    local_node: Mapping[str, Any]
    executor: CommandExecutor
    id: str = TIME_SYNC_OTHER_STEP_ID
    declares_commands: ClassVar[bool] = True

    def __post_init__(self) -> None:
        object.__setattr__(self, "root", Path(self.root))
        if self.local_node["role"] not in OTHER_NODE_ROLES:
            raise ExecutionError(
                f"{self.id} only runs on {', '.join(OTHER_NODE_ROLES)} nodes, not on "
                f"{self.local_node['hostname']!r} ({self.local_node['role']}); the controller "
                f"uses {TIME_SYNC_CONTROLLER_STEP_ID}"
            )

    def dry_run_preview(self) -> StepPreview:
        """Diff of the file edit plus the commands apply()/check()/verify() would run."""
        notes: list[str] = []
        try:
            file_changes: tuple[FileChange, ...] = (
                edit_chrony_conf_other(self.root, dry_run=True),
            )
        except UnsafePathError:
            file_changes = ()
            block = apply_line_edit(
                "",
                CHRONY_CONF_TARGET,
                _managed_block_editor(CHRONY_BLOCK_NAME, build_chrony_other_block()),
            )
            notes.append(
                f"{CHRONY_CONF_TARGET} does not exist yet; it is installed by the "
                f"{CHRONY_PACKAGE} package, after which its active pool/server lines are "
                f"commented out and this block is appended:\n{block}"
            )
        notes.append(
            f"'{CONTROLLER_ALIAS}' is the /etc/hosts alias written by the hosts-file step. "
            "Only active pool and server lines are commented out; other directives (such as "
            "sourcedir, confdir or include) are left unchanged."
        )
        notes.append("The file is edited after the install command and before the restart.")
        notes.append(
            "apt-get update and apt-get install download from the configured apt sources "
            "(network); OpenStack Forge's Python code opens no connection itself."
        )
        return StepPreview(
            file_changes=file_changes,
            commands=(UPDATE_PACKAGE_LISTS, INSTALL_CHRONY, RESTART_CHRONY),
            read_only_commands=(QUERY_CHRONY_PACKAGE, QUERY_CHRONY_ACTIVE),
            notes=tuple(notes),
        )

    def check(self) -> bool:
        """True if chrony.conf needs no edit (pure) and the chrony package is installed."""
        try:
            if edit_chrony_conf_other(self.root, dry_run=True).changed:
                return False
        except UnsafePathError:
            return False
        return _chrony_installed(self.executor)

    def apply(self) -> None:
        """Update the package lists, install chrony, edit chrony.conf, restart chrony."""
        # A fresh image may have stale or empty package lists, and then the install fails.
        self.executor.run(UPDATE_PACKAGE_LISTS.command_id, UPDATE_PACKAGE_LISTS.params)
        self.executor.run(INSTALL_CHRONY.command_id, INSTALL_CHRONY.params)
        edit_chrony_conf_other(self.root, dry_run=False)
        self.executor.run(RESTART_CHRONY.command_id, RESTART_CHRONY.params)

    def verify(self) -> VerifyResult:
        """chrony.conf points at the controller only, and chrony is active."""
        path, display = resolve_target(self.root, CHRONY_CONF_TARGET)
        try:
            text = read_text_file(path, display)
        except (UnsafePathError, FileFormatError) as exc:
            return VerifyResult(False, f"{exc}. {TIME_SYNC_NOT_CHECKED}")
        problems = [f"{display} does not exist"] if text is None else verify_chrony_other_text(text)
        problems += _chrony_service_problems(self.executor)
        if problems:
            return VerifyResult(False, f"{'; '.join(problems)}. {TIME_SYNC_NOT_CHECKED}")
        return VerifyResult(
            True,
            f"{display} has the managed block with '{OTHER_NODE_SERVER_LINE}' and no other "
            f"active pool/server line; {CHRONY_UNIT} is active. {TIME_SYNC_NOT_CHECKED}",
        )


def _chrony_installed(executor: CommandExecutor) -> bool:
    result = executor.run(QUERY_CHRONY_PACKAGE.command_id, QUERY_CHRONY_PACKAGE.params)
    return result.returncode == 0 and parse_dpkg_status(result.stdout)


def _chrony_service_problems(executor: CommandExecutor) -> list[str]:
    result = executor.run(QUERY_CHRONY_ACTIVE.command_id, QUERY_CHRONY_ACTIVE.params)
    state = result.stdout.strip() or f"exit status {result.returncode}"
    if result.returncode == 0 and state == "active":
        return []
    return [f"the {CHRONY_UNIT} service is not active (state: {state})"]


def make_time_sync_controller_step(
    root: Path,
    config: Mapping[str, Any],
    local_node: Mapping[str, Any],
    *,
    executor: CommandExecutor,
) -> TimeSyncControllerStep:
    return TimeSyncControllerStep(
        root=Path(root), config=config, local_node=local_node, executor=executor
    )


def make_time_sync_other_step(
    root: Path,
    config: Mapping[str, Any],
    local_node: Mapping[str, Any],
    *,
    executor: CommandExecutor,
) -> TimeSyncOtherStep:
    return TimeSyncOtherStep(
        root=Path(root), config=config, local_node=local_node, executor=executor
    )
