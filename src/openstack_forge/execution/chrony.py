"""The ``time-sync-controller`` step: chrony on the controller.

Follows the official controller page
(https://docs.openstack.org/install-guide/environment-ntp-controller.html), for Ubuntu:

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
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar

from openstack_forge.execution.commands import (
    APT_GET_INSTALL,
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
from openstack_forge.execution.preview import PlannedCommand, StepPreview

TIME_SYNC_CONTROLLER_STEP_ID = "time-sync-controller"
CHRONY_CONF_TARGET = "/etc/chrony/chrony.conf"
CHRONY_BLOCK_NAME = "openstack-forge time-sync"
CHRONY_PACKAGE = "chrony"
CHRONY_UNIT = "chrony"  # the guide restarts it as "service chrony restart"

# Active "pool ..." and "server ..." source directives.
SOURCE_LINE_RE = re.compile(r"^\s*(?:pool|server)\s+\S")

INSTALL_CHRONY = PlannedCommand(APT_GET_INSTALL, {"package": CHRONY_PACKAGE})
RESTART_CHRONY = PlannedCommand(SYSTEMCTL_RESTART, {"unit": CHRONY_UNIT})
QUERY_CHRONY_PACKAGE = PlannedCommand(DPKG_QUERY_STATUS, {"package": CHRONY_PACKAGE})
QUERY_CHRONY_ACTIVE = PlannedCommand(SYSTEMCTL_IS_ACTIVE, {"unit": CHRONY_UNIT})

TIME_SYNC_NOT_CHECKED = (
    "Actual time synchronization is not checked: the guide's NTP verification page has "
    "not been reviewed yet."
)


def build_chrony_block(config: Mapping[str, Any]) -> str:
    """The managed block content: one ``server`` line per NTP server, then ``allow``."""
    networks = config["networks"]
    if "management" not in networks:
        raise ExecutionError(
            "time-sync-controller needs networks.management: the guide's 'allow' line "
            "uses the management subnet"
        )
    servers = config["cluster"].get("ntp_servers", ())
    lines = [f"server {server} iburst" for server in servers]
    lines.append(f"allow {networks['management']['subnet']}")
    return "\n".join(lines) + "\n"


def edit_chrony_conf(root: Path, config: Mapping[str, Any], *, dry_run: bool) -> FileChange:
    """Edit ``/etc/chrony/chrony.conf`` under ``root``; the file must already exist.

    With ``ntp_servers``, active ``pool``/``server`` lines outside the managed block are
    commented out; without them, lines this step commented out earlier are restored.
    Everything else in the file is left as it is.
    """
    path, display = resolve_target(root, CHRONY_CONF_TARGET)
    before = read_text_file(path, display)
    if before is None:
        raise UnsafePathError(
            f"{display} does not exist; it is installed by the {CHRONY_PACKAGE} package"
        )
    block = build_chrony_block(config)
    servers = bool(config["cluster"].get("ntp_servers"))

    if dry_run:
        sources = (
            _comment_out_editor(SOURCE_LINE_RE, exclude_block=CHRONY_BLOCK_NAME)
            if servers
            else _uncomment_editor(SOURCE_LINE_RE)
        )
        after = apply_line_edit(before, display, sources)
        after = apply_line_edit(after, display, _managed_block_editor(CHRONY_BLOCK_NAME, block))
        if after == before:
            return FileChange(path=display, changed=False, diff="")
        return FileChange(path=display, changed=True, diff=unified_diff(before, after, display))

    if servers:
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
    block = build_chrony_block(config)
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
    allow = block.strip().split("\n")[-1]
    if allow not in body:
        problems.append(f"'{allow}' is missing from the managed block")
    if body != block.strip().split("\n"):
        problems.append("the managed block differs from the configuration")
    if config["cluster"].get("ntp_servers"):
        outside = [
            line.strip()
            for index, line in enumerate(lines)
            if not begins[0] <= index <= ends[0] and SOURCE_LINE_RE.match(line)
        ]
        if outside:
            problems.append(f"active source line(s) outside the block: {', '.join(outside)}")
    return problems


def parse_dpkg_status(text: str) -> bool:
    """True if a ``dpkg-query --status`` entry says ``Status: install ok installed``."""
    for line in text.splitlines():
        if line.startswith("Status:"):
            words = line.split(":", 1)[1].split()
            return len(words) == 3 and words[1] == "ok" and words[2] == "installed"
    return False


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
        return StepPreview(
            file_changes=file_changes,
            commands=(INSTALL_CHRONY, RESTART_CHRONY),
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
        return self._package_installed()

    def apply(self) -> None:
        """Install chrony, edit chrony.conf, then restart chrony."""
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
        result = self.executor.run(QUERY_CHRONY_ACTIVE.command_id, QUERY_CHRONY_ACTIVE.params)
        state = result.stdout.strip() or f"exit status {result.returncode}"
        if not (result.returncode == 0 and state == "active"):
            problems.append(f"the {CHRONY_UNIT} service is not active (state: {state})")
        if problems:
            return VerifyResult(False, "; ".join(problems))
        return VerifyResult(
            True,
            f"{display} has the managed block; {CHRONY_UNIT} is active. {TIME_SYNC_NOT_CHECKED}",
        )

    def _package_installed(self) -> bool:
        result = self.executor.run(QUERY_CHRONY_PACKAGE.command_id, QUERY_CHRONY_PACKAGE.params)
        return result.returncode == 0 and parse_dpkg_status(result.stdout)

    def _block(self) -> str:
        return build_chrony_block(self.config)


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
