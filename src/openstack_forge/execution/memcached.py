"""The ``memcached`` step: Memcached on the controller.

Follows the official page
(https://docs.openstack.org/install-guide/environment-memcached-ubuntu.html), which for
Ubuntu 18.04 and newer says:

1. ``apt install memcached python3-memcache``;
2. edit ``/etc/memcached.conf`` and "configure the service to use the management IP
   address of the controller node": ``-l 10.0.0.11``, with the note "Change the existing
   line that had -l 127.0.0.1";
3. ``service memcached restart``.

What this step does, and its deviations from the page:

- installs the two packages with two ``apt-get install --yes`` commands (the page uses one
  ``apt install``);
- comments out every active ``-l`` / ``--listen`` line outside its managed block and adds
  ``-l <management_ip>`` in the block, in one atomic edit. memcached(1) says addresses
  may be given "by using -l multiple times", so leaving the old line active would keep
  memcached listening on it too; and with no ``-l`` at all it listens on INADDR_ANY;
- restarts with ``systemctl restart memcached`` instead of ``service memcached restart``.

Package names are the page's; ``apt-cache policy`` on an Ubuntu 24.04 development machine
shows both in ``noble/main`` (``memcached 1.6.24-1ubuntu0.3``). Read-only inspection of that
package on the development machine confirms:

- ``/usr/lib/systemd/system/memcached.service`` (``dpkg -S``: ``memcached``) runs
  ``ExecStart=/usr/share/memcached/scripts/systemd-memcached-wrapper /etc/memcached.conf``,
  and the wrapper passes every non-comment line of ``/etc/memcached.conf`` (except ``-d``,
  ``-h`` and ``logfile``) as arguments to ``/usr/bin/memcached``;
- ``/etc/memcached.conf`` is not a dpkg conffile: ``/var/lib/dpkg/info/memcached.postinst``
  creates it on ``configure`` by copying ``/usr/share/memcached/memcached.conf.default`` if
  ``/etc/memcached.conf`` does not exist yet;
- ``/usr/share/memcached/memcached.conf.default`` contains **two** default listen lines
  (lines 35–36), ``-l 127.0.0.1`` and ``-l ::1`` (whereas the OpenStack guide mentions only
  ``-l 127.0.0.1``; on the development machine ``/etc/memcached.conf`` had line 35 already
  edited to the machine's local IP — sanitized to the RFC 5737 documentation address
  ``-l 192.0.2.170`` in the repository fixtures — and line 36 still at ``-l ::1``). Disabling
  every active ``-l`` / ``--listen`` line outside the managed block comments out both
  ``-l 127.0.0.1`` and ``-l ::1`` (and any prior edit).

Security: Memcached has no authentication in this configuration and listens on the
management IP; the management network must be trusted (the page recommends firewalling,
authentication and encryption for production). The step changes no firewall.

The step is UNVERIFIED and has never run on a real host.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from ipaddress import IPv4Address
from pathlib import Path
from typing import Any, ClassVar

from openstack_forge.execution.commands import (
    APT_GET_INSTALL,
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
from openstack_forge.execution.preview import PlannedCommand, StepPreview
from openstack_forge.execution.service_checks import (
    package_installed,
    query_active,
    query_package,
    unit_active_problems,
)

MEMCACHED_STEP_ID = "memcached"
MEMCACHED_CONF_TARGET = "/etc/memcached.conf"
MEMCACHED_BLOCK_NAME = "openstack-forge memcached"
MEMCACHED_PACKAGE = "memcached"
MEMCACHE_PYTHON_PACKAGE = "python3-memcache"
MEMCACHED_UNIT = "memcached"  # memcached ships memcached.service (dpkg -S)
MEMCACHED_PORT = 11211  # memcached(1) and /usr/share/memcached/memcached.conf.default: "-p 11211"

# Active listen options: "-l ADDR", "-lADDR", "--listen=ADDR" or "--listen ADDR"; not
# "--listen-backlog".
LISTEN_LINE_RE = re.compile(r"^\s*(?:-l|--listen(?=[=\s]|$))")

INSTALL_MEMCACHED = PlannedCommand(APT_GET_INSTALL, {"package": MEMCACHED_PACKAGE})
INSTALL_MEMCACHE_PYTHON = PlannedCommand(APT_GET_INSTALL, {"package": MEMCACHE_PYTHON_PACKAGE})
RESTART_MEMCACHED = PlannedCommand(SYSTEMCTL_RESTART, {"unit": MEMCACHED_UNIT})
QUERY_MEMCACHED = query_package(MEMCACHED_PACKAGE)
QUERY_MEMCACHE_PYTHON = query_package(MEMCACHE_PYTHON_PACKAGE)
QUERY_MEMCACHED_ACTIVE = query_active(MEMCACHED_UNIT)

PACKAGE_NOTE = (
    "In Ubuntu 24.04 memcached 1.6.24-1ubuntu0.3, memcached.service passes /etc/memcached.conf "
    "to /usr/share/memcached/scripts/systemd-memcached-wrapper, and memcached.postinst copies "
    "/usr/share/memcached/memcached.conf.default (whose default listen lines are '-l 127.0.0.1' "
    "and '-l ::1') to /etc/memcached.conf."
)
NOT_CHECKED = (
    "No TCP connection to {address}:{port} is attempted, and what memcached actually "
    "listens on at runtime is not checked (unconfirmed on a live service)."
)


def memcached_block(local_node: Mapping[str, Any]) -> str:
    """The managed block content: the page's ``-l`` line with the management IP."""
    return f"-l {IPv4Address(local_node['management_ip'])}\n"


def edit_memcached_conf(root: Path, local_node: Mapping[str, Any], *, dry_run: bool) -> FileChange:
    """Disable other listen lines and set the block, in one edit; the file must exist."""
    path, display = resolve_target(root, MEMCACHED_CONF_TARGET)
    if read_text_file(path, display) is None:
        raise PreconditionError(
            f"{display} does not exist; it is created when {MEMCACHED_PACKAGE} is installed"
        )
    return disable_lines_and_set_block(
        root,
        MEMCACHED_CONF_TARGET,
        LISTEN_LINE_RE,
        MEMCACHED_BLOCK_NAME,
        memcached_block(local_node),
        dry_run=dry_run,
    )


def verify_memcached_text(text: str, local_node: Mapping[str, Any]) -> list[str]:
    """Pure check: the block is present with the expected line, no other listen line."""
    lines = text.split("\n")
    begin, end = f"# BEGIN {MEMCACHED_BLOCK_NAME}", f"# END {MEMCACHED_BLOCK_NAME}"
    begins = [i for i, line in enumerate(lines) if line.strip() == begin]
    ends = [i for i, line in enumerate(lines) if line.strip() == end]
    if not begins and not ends:
        return [f"the managed block '{MEMCACHED_BLOCK_NAME}' is missing"]
    if len(begins) != 1 or len(ends) != 1 or ends[0] < begins[0]:
        return [f"the managed block '{MEMCACHED_BLOCK_NAME}' has broken markers"]
    problems: list[str] = []
    expected = memcached_block(local_node).strip()
    body = [line.strip() for line in lines[begins[0] + 1 : ends[0]]]
    if body != [expected]:
        problems.append(f"the managed block does not contain exactly '{expected}'")
    outside = [
        line.strip()
        for index, line in enumerate(lines)
        if not begins[0] <= index <= ends[0] and LISTEN_LINE_RE.match(line)
    ]
    if outside:
        problems.append(f"active listen line(s) outside the block: {', '.join(outside)}")
    return problems


@dataclass(frozen=True)
class MemcachedStep:
    """Executable ``memcached`` step (file edit under ``root``, commands via ``executor``)."""

    root: Path
    config: Mapping[str, Any]
    local_node: Mapping[str, Any]
    executor: CommandExecutor
    id: str = MEMCACHED_STEP_ID
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
                edit_memcached_conf(self.root, self.local_node, dry_run=True),
            )
        except PreconditionError:
            file_changes = ()
            block = apply_line_edit(
                "",
                MEMCACHED_CONF_TARGET,
                _managed_block_editor(MEMCACHED_BLOCK_NAME, memcached_block(self.local_node)),
            )
            notes.append(
                f"{MEMCACHED_CONF_TARGET} does not exist yet; it is created when "
                f"{MEMCACHED_PACKAGE} is installed. After that, its active -l/--listen lines "
                f"are commented out and this block is appended:\n{block}"
            )
        notes += [
            "The file is edited after both install commands and before the restart.",
            PACKAGE_NOTE,
            "Security: memcached is unauthenticated here and listens on the management IP; "
            "the management network must be trusted. No firewall rule is changed.",
            "apt-get install downloads from the configured apt sources (network).",
        ]
        return StepPreview(
            file_changes=file_changes,
            commands=(INSTALL_MEMCACHED, INSTALL_MEMCACHE_PYTHON, RESTART_MEMCACHED),
            read_only_commands=(QUERY_MEMCACHED, QUERY_MEMCACHE_PYTHON, QUERY_MEMCACHED_ACTIVE),
            notes=tuple(notes),
        )

    def check(self) -> bool:
        """True if memcached.conf needs no edit (pure) and both packages are installed."""
        try:
            if edit_memcached_conf(self.root, self.local_node, dry_run=True).changed:
                return False
        except (PreconditionError, UnsafePathError):
            return False
        return package_installed(self.executor, MEMCACHED_PACKAGE) and package_installed(
            self.executor, MEMCACHE_PYTHON_PACKAGE
        )

    def apply(self) -> None:
        """Install memcached and python3-memcache, edit the file, restart memcached."""
        self.executor.run(INSTALL_MEMCACHED.command_id, INSTALL_MEMCACHED.params)
        self.executor.run(INSTALL_MEMCACHE_PYTHON.command_id, INSTALL_MEMCACHE_PYTHON.params)
        edit_memcached_conf(self.root, self.local_node, dry_run=False)
        self.executor.run(RESTART_MEMCACHED.command_id, RESTART_MEMCACHED.params)

    def verify(self) -> VerifyResult:
        """The file listens on the management IP only, python3-memcache is installed, and
        memcached is active."""
        not_checked = NOT_CHECKED.format(
            address=self.local_node["management_ip"], port=MEMCACHED_PORT
        )
        path, display = resolve_target(self.root, MEMCACHED_CONF_TARGET)
        try:
            text = read_text_file(path, display)
        except (UnsafePathError, FileFormatError) as exc:
            return VerifyResult(False, f"{exc}. {not_checked}")
        problems = (
            [f"{display} does not exist"]
            if text is None
            else verify_memcached_text(text, self.local_node)
        )
        if not package_installed(self.executor, MEMCACHE_PYTHON_PACKAGE):
            problems.append(f"{MEMCACHE_PYTHON_PACKAGE} is not installed")
        problems += unit_active_problems(self.executor, MEMCACHED_UNIT)
        if problems:
            return VerifyResult(False, f"{'; '.join(problems)}. {not_checked}")
        return VerifyResult(
            True,
            f"{display} sets '{memcached_block(self.local_node).strip()}' and no other listen "
            f"line; {MEMCACHE_PYTHON_PACKAGE} is installed; {MEMCACHED_UNIT} is active. "
            f"{not_checked}",
        )


def make_memcached_step(
    root: Path,
    config: Mapping[str, Any],
    local_node: Mapping[str, Any],
    *,
    executor: CommandExecutor,
) -> MemcachedStep:
    return MemcachedStep(root=Path(root), config=config, local_node=local_node, executor=executor)
