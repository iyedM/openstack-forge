"""The ``hosts-file`` deployment step and the implemented-steps registry.

This module uses only confined file operations from :mod:`openstack_forge.execution`:
it never spawns a subprocess, never calls ``getent`` or ``sudo``, and never opens a
network connection.
"""

from __future__ import annotations

import re
import socket
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from ipaddress import IPv4Address
from pathlib import Path
from typing import Any, ClassVar

from openstack_forge.execution.engine import VerifyResult
from openstack_forge.execution.errors import ExecutionError
from openstack_forge.execution.files import (
    DISABLED_LINE_PREFIX,
    FileChange,
    _comment_out_editor,
    _managed_block_editor,
    apply_line_edit,
    comment_out_lines,
    managed_block,
)
from openstack_forge.execution.fsutil import (
    read_text_file,
    resolve_target,
    unified_diff,
)
from openstack_forge.execution.preview import StepPreview

HOSTS_STEP_ID = "hosts-file"
HOSTS_TARGET = "/etc/hosts"
HOSTNAME_TARGET = "/etc/hostname"
HOSTS_BLOCK_NAME = "openstack-forge hosts"
CONTROLLER_ALIAS = "controller"
LOOPBACK_127_0_1_1 = "127.0.1.1"
DEFAULT_STATE_TARGET = "/var/lib/openstack-forge/state.json"


NodesOrConfig = Sequence[Mapping[str, Any]] | Mapping[str, Any]
NodeOrHostname = Mapping[str, Any] | str


def _extract_nodes(nodes_or_config: NodesOrConfig) -> tuple[Mapping[str, Any], ...]:
    if isinstance(nodes_or_config, Mapping):
        return tuple(nodes_or_config["nodes"])
    return tuple(nodes_or_config)


def _extract_local_node(
    nodes: Sequence[Mapping[str, Any]],
    local_node: NodeOrHostname,
) -> Mapping[str, Any]:
    if isinstance(local_node, str):
        target = local_node.strip().lower()
        for candidate in nodes:
            if candidate["hostname"].lower() == target:
                return candidate
        declared = ", ".join(candidate["hostname"] for candidate in nodes)
        raise ExecutionError(
            f"Node {local_node!r} was not found in configuration (declared nodes: {declared})."
        )
    return local_node


_HOSTNAME_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")
_FQDN_RE = re.compile(
    r"^(?=.{1,253}$)(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+"
    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$"
)


def _validate_node_host_fields(node: Mapping[str, Any]) -> tuple[str, str, str]:
    mgmt_raw = node.get("management_ip")
    hostname = node.get("hostname")
    fqdn = node.get("fqdn")
    try:
        mgmt_ip = str(IPv4Address(mgmt_raw))
    except Exception as exc:
        raise ExecutionError(f"Invalid management_ip {mgmt_raw!r}") from exc
    if not isinstance(hostname, str) or not _HOSTNAME_RE.fullmatch(hostname):
        raise ExecutionError(f"Invalid hostname {hostname!r}")
    if not isinstance(fqdn, str) or not _FQDN_RE.fullmatch(fqdn):
        raise ExecutionError(f"Invalid fqdn {fqdn!r}")
    return mgmt_ip, fqdn, hostname


def build_hosts_block(nodes_or_config: NodesOrConfig) -> str:
    """Build the managed ``/etc/hosts`` block for every node in ``nodes_or_config``.

    Each line has ``<management_ip> <fqdn> <hostname>``, and the controller node also
    receives the ``controller`` alias used by the official OpenStack installation guide
    (which assumes a single controller, already enforced by configuration validation).
    """
    nodes = _extract_nodes(nodes_or_config)
    lines: list[str] = []
    for node in nodes:
        mgmt_ip, fqdn, hostname = _validate_node_host_fields(node)
        raw_names = [fqdn, hostname]
        if node["role"] == "controller":
            raw_names.append(CONTROLLER_ALIAS)
        names = _deduplicate_case_insensitive(raw_names)
        lines.append(f"{mgmt_ip} {' '.join(names)}")
    return "\n".join(lines) + ("\n" if lines else "")


def build_127_0_1_1_pattern(local_node: Mapping[str, Any]) -> re.Pattern[str]:
    """Regex matching active ``127.0.1.1`` lines that map to ``local_node``'s name or FQDN."""
    _, fqdn, hostname = _validate_node_host_fields(local_node)
    names = "|".join(re.escape(name) for name in _deduplicate_case_insensitive((fqdn, hostname)))
    return re.compile(
        rf"^\s*127\.0\.1\.1(?:\s+[^#\s]+)*\s+(?:{names})(?=\s|$|#)",
        re.IGNORECASE,
    )


def apply_hosts_file(
    root: Path,
    nodes_or_config: NodesOrConfig,
    local_node: NodeOrHostname,
    *,
    dry_run: bool = False,
) -> FileChange:
    """Comment out ``127.0.1.1`` for ``local_node`` and write the cluster hosts block."""
    nodes = _extract_nodes(nodes_or_config)
    node = _extract_local_node(nodes, local_node)
    path, display = resolve_target(root, HOSTS_TARGET)
    before = read_text_file(path, display)
    pattern = build_127_0_1_1_pattern(node)
    block_content = build_hosts_block(nodes)

    if dry_run:
        comment_edit = _comment_out_editor(pattern, prefix=DISABLED_LINE_PREFIX)
        block_edit = _managed_block_editor(HOSTS_BLOCK_NAME, block_content)
        after_comment = apply_line_edit(before or "", display, comment_edit) if before else ""
        after_both = apply_line_edit(after_comment, display, block_edit)
        if after_both == before:
            return FileChange(path=display, changed=False, diff="")
        return FileChange(
            path=display,
            changed=True,
            diff=unified_diff(before or "", after_both, display),
            created=before is None,
        )

    comment_change = comment_out_lines(root, HOSTS_TARGET, pattern, dry_run=False)
    block_change = managed_block(root, HOSTS_TARGET, HOSTS_BLOCK_NAME, block_content, dry_run=False)
    if not comment_change.changed and not block_change.changed:
        return FileChange(path=display, changed=False, diff="")

    after = read_text_file(path, display) or ""
    return FileChange(
        path=display,
        changed=True,
        diff=unified_diff(before or "", after, display),
        backup=comment_change.backup or block_change.backup,
        created=before is None,
    )


def parse_hosts(text: str) -> list[tuple[str, tuple[str, ...]]]:
    """Parse ``/etc/hosts`` text into ``(address, (name, ...))`` entries."""
    entries: list[tuple[str, tuple[str, ...]]] = []
    for raw_line in text.splitlines():
        active = raw_line.split("#", 1)[0].strip()
        if not active:
            continue
        tokens = active.split()
        if len(tokens) >= 2:
            entries.append((tokens[0], tuple(tokens[1:])))
    return entries


def resolve_in_hosts(
    entries: Sequence[tuple[str, Sequence[str]]],
    name: str,
) -> str | None:
    """Resolve ``name`` against parsed ``/etc/hosts`` entries (first matching line wins)."""
    target = name.lower()
    for address, aliases in entries:
        if any(alias.lower() == target for alias in aliases):
            return address
    return None


def verify_hosts_text(
    text: str,
    nodes_or_config: NodesOrConfig,
    local_node: NodeOrHostname,
) -> VerifyResult:
    """Verify ``/etc/hosts`` content purely in memory (first matching line wins)."""
    nodes = _extract_nodes(nodes_or_config)
    node_obj = _extract_local_node(nodes, local_node)
    entries = parse_hosts(text)
    problems: list[str] = []

    for own_name in _deduplicate_case_insensitive((node_obj["hostname"], node_obj["fqdn"])):
        resolved_own = resolve_in_hosts(entries, own_name)
        if resolved_own == LOOPBACK_127_0_1_1:
            problems.append(f"local name {own_name!r} resolves to {LOOPBACK_127_0_1_1}")

    for node in nodes:
        expected_ip = node["management_ip"]
        expected_names = [node["hostname"], node["fqdn"]]
        if node["role"] == "controller":
            expected_names.append(CONTROLLER_ALIAS)
        for name in _deduplicate_case_insensitive(expected_names):
            resolved = resolve_in_hosts(entries, name)
            if resolved is None:
                problems.append(f"{name!r} does not resolve in /etc/hosts (expected {expected_ip})")
            elif resolved != expected_ip:
                problems.append(
                    f"{name!r} resolves to {resolved} in /etc/hosts (expected {expected_ip})"
                )

    unique_problems = list(dict.fromkeys(problems))
    if unique_problems:
        return VerifyResult(ok=False, details="; ".join(unique_problems))
    return VerifyResult(
        ok=True,
        details=f"All {len(nodes)} cluster node(s) resolve to their management IPs in /etc/hosts.",
    )


def verify_hosts_file(
    root: Path,
    nodes_or_config: NodesOrConfig,
    local_node: NodeOrHostname,
) -> VerifyResult:
    """Read ``<root>/etc/hosts`` and verify name resolution for all cluster nodes."""
    path, display = resolve_target(root, HOSTS_TARGET)
    text = read_text_file(path, display)
    if text is None:
        return VerifyResult(ok=False, details="/etc/hosts does not exist")
    return verify_hosts_text(text, nodes_or_config, local_node)


@dataclass(frozen=True)
class HostsFileStep:
    """Executable ``hosts-file`` step over a validated configuration and target root."""

    root: Path
    nodes: NodesOrConfig
    local_node: NodeOrHostname
    id: str = HOSTS_STEP_ID
    declares_commands: ClassVar[bool] = False

    def __post_init__(self) -> None:
        extracted_nodes = _extract_nodes(self.nodes)
        extracted_local = _extract_local_node(extracted_nodes, self.local_node)
        object.__setattr__(self, "root", Path(self.root))
        object.__setattr__(self, "nodes", extracted_nodes)
        object.__setattr__(self, "local_node", extracted_local)

    def preview(self) -> FileChange:
        """Compute the dry-run :class:`FileChange` without writing anything."""
        return apply_hosts_file(self.root, self.nodes, self.local_node, dry_run=True)

    def dry_run_preview(self) -> StepPreview:
        """The ``/etc/hosts`` diff; this step runs no command."""
        return StepPreview(file_changes=(self.preview(),))

    def check(self) -> bool:
        """Return True when ``/etc/hosts`` needs no edit and passes verification."""
        if self.preview().changed:
            return False
        return self.verify().ok

    def apply(self) -> None:
        """Apply the ``/etc/hosts`` changes under ``self.root``."""
        apply_hosts_file(self.root, self.nodes, self.local_node, dry_run=False)

    def verify(self) -> VerifyResult:
        """Verify ``/etc/hosts`` resolution without calling external commands."""
        return verify_hosts_file(self.root, self.nodes, self.local_node)


def make_hosts_file_step(
    root: Path,
    config: NodesOrConfig,
    local_node: NodeOrHostname,
    *,
    executor: object = None,
) -> HostsFileStep:
    """Construct a :class:`HostsFileStep` for ``local_node`` under ``root``.

    ``executor`` is accepted for a uniform factory signature and ignored: this step runs
    no command.
    """
    return HostsFileStep(
        root=Path(root),
        nodes=config,
        local_node=local_node,
    )


def resolve_apply_node(
    config: Mapping[str, Any],
    *,
    root: Path,
    node_hostname: str | None = None,
) -> Mapping[str, Any]:
    """Identify the target node for ``apply`` without running any subprocess.

    Uses ``--node HOSTNAME`` when given, otherwise reads ``<root>/etc/hostname`` if it
    exists under ``root`` (falling back to ``socket.gethostname()``) and matches it
    against ``nodes[].hostname``.
    """
    nodes: Sequence[Mapping[str, Any]] = config["nodes"]
    if node_hostname is not None:
        target = node_hostname.strip()
        for candidate in nodes:
            if candidate["hostname"].lower() == target.lower():
                return candidate
        declared = ", ".join(candidate["hostname"] for candidate in nodes)
        raise ExecutionError(
            f"Node {target!r} was not found in configuration (declared nodes: {declared})."
        )

    observed = _read_root_hostname(root) or socket.gethostname().strip()
    if observed:
        lowered = observed.lower()
        short = lowered.split(".", 1)[0]
        matches = [
            candidate for candidate in nodes if candidate["hostname"].lower() in {lowered, short}
        ]
        if len(matches) == 1:
            return matches[0]

    declared = ", ".join(candidate["hostname"] for candidate in nodes)
    raise ExecutionError(
        f"Could not identify local node from hostname {observed!r} "
        f"(declared nodes: {declared}). Pass --node HOSTNAME to select a node explicitly."
    )


def _read_root_hostname(root: Path) -> str | None:
    try:
        path, display = resolve_target(root, HOSTNAME_TARGET)
        if not path.parent.is_dir():
            return None
        text = read_text_file(path, display)
    except ExecutionError:
        return None
    if text is None:
        return None
    for line in text.splitlines():
        stripped = line.split("#", 1)[0].strip()
        if stripped:
            return stripped
    return None


def _deduplicate_case_insensitive(names: Sequence[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for name in names:
        key = name.lower()
        if key not in seen:
            seen.add(key)
            result.append(name)
    return result
