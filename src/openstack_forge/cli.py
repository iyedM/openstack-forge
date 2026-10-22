"""CLI entry point for OpenStack Forge."""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from openstack_forge import __version__
from openstack_forge.config import ConfigError, ConfigValidationError, find_release, load_config
from openstack_forge.diagnostics import (
    CommandRunner,
    DoctorError,
    format_report_json,
    format_report_table,
    run_doctor,
)
from openstack_forge.orchestration import (
    OrchestrationError,
    build_plan,
    format_plan_json,
    format_plan_text,
    load_catalog,
)

Handler = Callable[[argparse.Namespace], int]

STARTER_CLUSTER_YAML = """\
# OpenStack Forge - Starter cluster definition
# Check it with: openstack-forge config validate cluster.yaml
# All addresses below use RFC 5737 documentation ranges (192.0.2.0/24,
# 198.51.100.0/24, 203.0.113.0/24); replace them with your cluster's addresses.

cluster:
  name: "forge-demo"
  ubuntu_version: "24.04"       # the only supported Ubuntu release
  openstack_release: "2025.1"   # Epoxy, installed from the cloud-archive:epoxy repository
  topology: "private-cloud"

networks:
  management:
    subnet: "192.0.2.0/24"
    gateway: "192.0.2.1"
    dns:
      - "192.0.2.10"
    interface: "ens3"
  storage:
    subnet: "198.51.100.0/24"
    gateway: "198.51.100.1"
    interface: "ens4"
  provider:
    subnet: "203.0.113.0/24"
    gateway: "203.0.113.1"
    interface: "ens5"
    floating_ip_range:
      start: "203.0.113.100"
      end: "203.0.113.199"
    dhcp_range:
      start: "203.0.113.200"
      end: "203.0.113.250"

nodes:
  - role: controller
    hostname: "controller"
    fqdn: "controller.example.internal"
    management_ip: "192.0.2.11"
    vcpus: 8
    ram_gb: 32
    interfaces:
      management: "ens3"
      storage: "ens4"
      provider: "ens5"
    tags: ["control-plane", "identity", "network"]

  - role: compute
    hostname: "compute1"
    fqdn: "compute1.example.internal"
    management_ip: "192.0.2.21"
    vcpus: 16
    ram_gb: 64
    interfaces:
      management: "ens3"
      storage: "ens4"
      provider: "ens5"
    tags: ["compute"]

  - role: block
    hostname: "block1"
    fqdn: "block1.example.internal"
    management_ip: "192.0.2.31"
    vcpus: 4
    ram_gb: 16
    disk: "/dev/sdb"
    interfaces:
      management: "ens3"
      storage: "ens4"
    tags: ["cinder"]
"""


def _init(args: argparse.Namespace) -> int:
    """Write a starter cluster.yaml configuration file and validate it."""
    output_path = Path(args.output)
    abs_output = Path(os.path.abspath(output_path))

    for candidate in (output_path, abs_output, *abs_output.parents):
        if candidate.is_symlink():
            print(f"Error: Refusing to write to symlink: {output_path}", file=sys.stderr)
            return 1

    if output_path.exists():
        if output_path.is_dir():
            print(f"Error: Destination is a directory: {output_path}", file=sys.stderr)
            return 1
        if not args.force:
            print(
                f"Error: File already exists: {output_path} (use --force to overwrite).",
                file=sys.stderr,
            )
            return 1

    try:
        abs_output.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            prefix=f".{abs_output.name}.",
            suffix=".tmp",
            dir=abs_output.parent,
        )
        tmp_path = Path(tmp_name)
        try:
            os.chmod(fd, 0o644)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(STARTER_CLUSTER_YAML)
                handle.flush()
                os.fsync(handle.fileno())
            if abs_output.is_symlink():
                tmp_path.unlink(missing_ok=True)
                print(f"Error: Refusing to write to symlink: {output_path}", file=sys.stderr)
                return 1
            os.replace(tmp_path, abs_output)
        except BaseException:
            tmp_path.unlink(missing_ok=True)
            raise
    except OSError as exc:
        print(f"Error: Cannot write {output_path}: {exc}", file=sys.stderr)
        return 1

    if _load_validated_config(str(abs_output)) is None:
        return 1

    print(f"Initialized starter cluster configuration: {output_path}")
    return 0


def _load_validated_config(path: str) -> dict[str, Any] | None:
    """Load and validate a configuration file, printing errors to stderr on failure."""
    try:
        return load_config(path)
    except ConfigValidationError as exc:
        print(f"Configuration is invalid: {path}", file=sys.stderr)
        for issue in exc.issues:
            print(f"  - {issue}", file=sys.stderr)
        print(f"Found {len(exc.issues)} error(s).", file=sys.stderr)
        return None
    except ConfigError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return None


def _config_validate(args: argparse.Namespace) -> int:
    """Validate a cluster configuration file; exit 0 if valid, 1 otherwise."""
    path = args.path
    config = _load_validated_config(path)
    if config is None:
        return 1

    cluster = config["cluster"]
    release = find_release(cluster["ubuntu_version"], cluster["openstack_release"])
    print(f"Configuration is valid: {path}")
    print(
        f"  cluster '{cluster['name']}': "
        f"{len(config['nodes'])} node(s), {len(config['networks'])} network(s)"
    )
    if release is not None:
        print(
            f"  OpenStack {release.openstack_release} ({release.codename.capitalize()}) "
            f"on Ubuntu {release.ubuntu_version}, from {release.cloud_archive}"
        )
    return 0


def _doctor(args: argparse.Namespace, runner: CommandRunner | None = None) -> int:
    """Run read-only local host checks for a node in the cluster configuration."""
    config = _load_validated_config(args.config)
    if config is None:
        return 1

    try:
        report = run_doctor(config, node_hostname=args.node, runner=runner)
    except DoctorError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    if args.json_output:
        print(format_report_json(report))
    else:
        print(format_report_table(report))
    return report.exit_code


def _plan(args: argparse.Namespace) -> int:
    """Print the ordered, read-only deployment plan; runs no command on any host."""
    config = _load_validated_config(args.config)
    if config is None:
        return 1

    try:
        plan = build_plan(config, load_catalog(), node=args.node)
    except OrchestrationError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    print(format_plan_json(plan) if args.json_output else format_plan_text(plan))
    return 0


def _apply(
    args: argparse.Namespace,
    *,
    backend: Any = None,
    executor_backend: Any = None,
) -> int:
    """Apply a single registered step or all planned steps for the local node."""
    # Lazy import so that importing the CLI or running read-only commands never imports
    # openstack_forge.execution.
    from openstack_forge.execution import (
        DEFAULT_AUDIT_TARGET,
        DEFAULT_REGISTRY,
        DEFAULT_STATE_TARGET,
        IMPLEMENTED_STEPS,
        AuditLog,
        CommandExecutor,
        Engine,
        ExecutionError,
        StateStore,
        UnsafePathError,
        apply_lock,
        build_step,
        format_report,
        is_host_root,
        redact_text,
        resolve_apply_node,
        resolve_target,
    )

    active_backend = backend if backend is not None else executor_backend

    config = _load_validated_config(args.config)
    if config is None:
        return 1

    try:
        catalog = load_catalog()
    except OrchestrationError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    implemented = ", ".join(sorted(IMPLEMENTED_STEPS))
    if not args.all:
        step_id = args.step
        try:
            catalog_step = catalog.step(step_id)
        except KeyError:
            print(
                f"Error: Step {step_id!r} is not implemented "
                f"(unknown step id; implemented steps: {implemented}).",
                file=sys.stderr,
            )
            return 1
        if step_id not in IMPLEMENTED_STEPS:
            print(
                f"Error: Step {step_id!r} is not implemented yet "
                f"(implemented steps: {implemented}).",
                file=sys.stderr,
            )
            return 1

    root_path = Path(args.root) if args.root is not None else Path("/")
    host_root = args.root is None or (root_path.exists() and is_host_root(root_path))
    if args.yes and host_root and os.geteuid() != 0 and active_backend is None:
        print(
            "Error: Writing to '/' with --yes requires root privileges (effective uid 0). "
            "Re-run as root, or pass --root PATH to target an alternate directory.",
            file=sys.stderr,
        )
        return 1

    try:
        audit_path, _ = resolve_target(root_path, DEFAULT_AUDIT_TARGET)
        executor = CommandExecutor(
            DEFAULT_REGISTRY,
            root=root_path,
            audit_log=AuditLog(audit_path),
            backend=active_backend,
            dry_run=not args.yes,
            euid=(lambda: 0) if active_backend is not None else os.geteuid,
            allow_non_host_root=active_backend is not None,
        )
        local_node = resolve_apply_node(config, root=root_path, node_hostname=args.node)
    except ExecutionError as exc:
        print(f"Error: {redact_text(str(exc))}", file=sys.stderr)
        return 1

    if args.all:
        try:
            node_plan = build_plan(config, catalog, node=local_node["hostname"])
        except OrchestrationError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 1

        missing_steps = [
            item.step.id for item in node_plan.items if item.step.id not in IMPLEMENTED_STEPS
        ]
        if missing_steps:
            missing_str = ", ".join(repr(step_name) for step_name in missing_steps)
            print(
                f"Error: Step(s) {missing_str} are not implemented yet "
                f"(implemented steps: {implemented}).",
                file=sys.stderr,
            )
            return 1

        try:
            steps = [
                build_step(item.step.id, root_path, config, local_node, executor=executor)
                for item in node_plan.items
            ]
            previews = [step.dry_run_preview() for step in steps]
        except ExecutionError as exc:
            print(f"Error: {redact_text(str(exc))}", file=sys.stderr)
            return 1

        if args.yes and not host_root and active_backend is None:
            command_step = next((step for step in steps if step.declares_commands), None)
            if command_step is not None:
                print(
                    f"Error: Step {command_step.id!r} runs system commands (such as package "
                    f"installation), which would act on this host and not on {root_path}. --yes "
                    "with --root other than '/' is refused for such steps; run without --yes to "
                    "see the dry run.",
                    file=sys.stderr,
                )
                return 1

        if node_plan.unverified_count:
            print(
                f"WARNING: {node_plan.unverified_count} of {len(node_plan.items)} step(s) on node "
                f"'{local_node['hostname']}' are marked UNVERIFIED in the step catalog. "
                "Review each step's documentation before applying in production."
            )

        if not args.yes:
            what = (
                "no files modified and no commands run"
                if any(preview.commands for preview in previews)
                else "no files modified"
            )
            print(
                f"Dry run for all {len(steps)} step(s) on node '{local_node['hostname']}' "
                f"(role: {local_node['role']}): {what}."
            )
            for index, (step, preview) in enumerate(zip(steps, previews, strict=True), start=1):
                print(f"\n=== [{index}/{len(steps)}] {step.id} ===")
                _print_preview(preview, executor.registry)
            return 0

        try:
            with apply_lock(root_path):
                state_path, _ = resolve_target(root_path, DEFAULT_STATE_TARGET)
                if state_path.is_symlink():
                    raise UnsafePathError(
                        f"Refusing to use state file {DEFAULT_STATE_TARGET}: it is a symlink"
                    )
                state_path.parent.mkdir(parents=True, exist_ok=True)
                if any(step.declares_commands for step in steps):
                    audit_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                report = Engine(StateStore(state_path)).run(steps)
        except ExecutionError as exc:
            print(f"Error: {redact_text(str(exc))}", file=sys.stderr)
            return 1

        applied_ids = {
            outcome.step_id for outcome in report.outcomes if outcome.outcome.value == "applied"
        }
        if report.ok:
            for step, preview in zip(steps, previews, strict=True):
                if step.id in applied_ids and preview.files_changed:
                    for change in preview.file_changes:
                        if change.changed:
                            diff_out = redact_text(change.diff)
                            print(diff_out, end="" if diff_out.endswith("\n") else "\n")
        print(format_report(report))
        return 0 if report.ok else 1

    try:
        if local_node["role"] not in catalog_step.roles:
            print(
                f"Error: Step {step_id!r} does not apply to role {local_node['role']!r} "
                f"on node {local_node['hostname']!r}.",
                file=sys.stderr,
            )
            return 1
        step = build_step(step_id, root_path, config, local_node, executor=executor)
        preview = step.dry_run_preview()
    except ExecutionError as exc:
        print(f"Error: {redact_text(str(exc))}", file=sys.stderr)
        return 1

    if args.yes and step.declares_commands and not host_root and active_backend is None:
        print(
            f"Error: Step {step_id!r} runs system commands (such as package installation), "
            f"which would act on this host and not on {root_path}. --yes with "
            "--root other than '/' is refused for such steps; run without --yes to see the "
            "dry run.",
            file=sys.stderr,
        )
        return 1

    if not catalog_step.verified:
        print(
            f"WARNING: Step '{catalog_step.id}' is marked UNVERIFIED in the step catalog. "
            f"Review {catalog_step.doc_url} before applying in production."
        )

    if not args.yes:
        what = "no files modified and no commands run" if preview.commands else "no files modified"
        print(
            f"Dry run for step '{step_id}' on node '{local_node['hostname']}' "
            f"(role: {local_node['role']}): {what}."
        )
        _print_preview(preview, executor.registry)
        return 0

    try:
        with apply_lock(root_path):
            state_path, _ = resolve_target(root_path, DEFAULT_STATE_TARGET)
            if state_path.is_symlink():
                raise UnsafePathError(
                    f"Refusing to use state file {DEFAULT_STATE_TARGET}: it is a symlink"
                )
            state_path.parent.mkdir(parents=True, exist_ok=True)
            if step.declares_commands:
                audit_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            report = Engine(StateStore(state_path)).run([step])
    except ExecutionError as exc:
        print(f"Error: {redact_text(str(exc))}", file=sys.stderr)
        return 1

    if preview.files_changed and report.ok:
        for change in preview.file_changes:
            if change.changed:
                diff_out = redact_text(change.diff)
                print(diff_out, end="" if diff_out.endswith("\n") else "\n")
    print(format_report(report))
    return 0 if report.ok else 1


def _verify(
    args: argparse.Namespace,
    *,
    backend: Any = None,
    executor_backend: Any = None,
) -> int:
    """Verify one implemented step or all planned steps for the local node (read-only)."""
    from openstack_forge.execution import (
        DEFAULT_REGISTRY,
        DEFAULT_STATE_TARGET,
        IMPLEMENTED_STEPS,
        CommandExecutor,
        ExecutionError,
        StateStore,
        build_step,
        redact_text,
        resolve_apply_node,
        resolve_target,
    )

    active_backend = backend if backend is not None else executor_backend

    config = _load_validated_config(args.config)
    if config is None:
        return 1

    try:
        catalog = load_catalog()
    except OrchestrationError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    implemented = ", ".join(sorted(IMPLEMENTED_STEPS))
    if args.step is not None:
        step_id = args.step
        try:
            catalog_step = catalog.step(step_id)
        except KeyError:
            print(
                f"Error: Step {step_id!r} is not implemented "
                f"(unknown step id; implemented steps: {implemented}).",
                file=sys.stderr,
            )
            return 1
        if step_id not in IMPLEMENTED_STEPS:
            print(
                f"Error: Step {step_id!r} is not implemented yet "
                f"(implemented steps: {implemented}).",
                file=sys.stderr,
            )
            return 1

    root_path = Path(args.root) if args.root is not None else Path("/")
    try:
        executor = CommandExecutor(
            DEFAULT_REGISTRY,
            root=root_path,
            backend=active_backend,
            dry_run=False,
            read_only=True,
        )
        local_node = resolve_apply_node(config, root=root_path, node_hostname=args.node)
        state_path, _ = resolve_target(root_path, DEFAULT_STATE_TARGET)
        recorded_states = StateStore(state_path).load()
    except ExecutionError as exc:
        print(f"Error: {redact_text(str(exc))}", file=sys.stderr)
        return 1

    if args.step is not None:
        if local_node["role"] not in catalog_step.roles:
            print(
                f"Error: Step {args.step!r} does not apply to role {local_node['role']!r} "
                f"on node {local_node['hostname']!r}.",
                file=sys.stderr,
            )
            return 1
        target_step_ids = [args.step]
    else:
        try:
            node_plan = build_plan(config, catalog, node=local_node["hostname"])
        except OrchestrationError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 1
        missing_steps = [
            item.step.id for item in node_plan.items if item.step.id not in IMPLEMENTED_STEPS
        ]
        if missing_steps:
            missing_str = ", ".join(repr(step_name) for step_name in missing_steps)
            print(
                f"Error: Step(s) {missing_str} are not implemented yet "
                f"(implemented steps: {implemented}).",
                file=sys.stderr,
            )
            return 1
        target_step_ids = [item.step.id for item in node_plan.items]

    step_entries: list[dict[str, Any]] = []
    for step_id in target_step_ids:
        recorded_status = (
            recorded_states[step_id].status.value if step_id in recorded_states else "not_recorded"
        )
        try:
            step = build_step(step_id, root_path, config, local_node, executor=executor)
            result = step.verify()
            step_ok = result.ok
            if step_ok:
                problems: list[str] = []
            else:
                detail_text = redact_text(result.details.strip())
                problems = [detail_text] if detail_text else ["verification failed"]
        except ExecutionError as exc:
            step_ok = False
            problems = [redact_text(str(exc))]
        step_entries.append(
            {
                "id": step_id,
                "recorded_status": recorded_status,
                "ok": step_ok,
                "problems": problems,
            }
        )

    passed_count = sum(1 for entry in step_entries if entry["ok"])
    failed_count = len(step_entries) - passed_count
    all_ok = failed_count == 0

    if args.json_output:
        payload = {
            "node": local_node["hostname"],
            "role": local_node["role"],
            "ok": all_ok,
            "passed": passed_count,
            "failed": failed_count,
            "steps": step_entries,
        }
        print(json.dumps(payload, indent=2))
    else:
        for entry in step_entries:
            status_label = "PASS" if entry["ok"] else "FAIL"
            print(f"{status_label:<4}  {entry['id']} (recorded: {entry['recorded_status']})")
            for problem in entry["problems"]:
                print(f"      - {problem}")
        print(
            f"Verified {len(step_entries)} step(s) on node '{local_node['hostname']}' "
            f"(role: {local_node['role']}): {passed_count} passed, {failed_count} failed."
        )

    return 0 if all_ok else 1


def _print_preview(preview: Any, registry: Any) -> None:
    """Print a step's dry-run preview: file diffs, notes and the exact command argv."""
    changed = [change for change in preview.file_changes if change.changed]
    for change in changed:
        print(change.diff, end="" if change.diff.endswith("\n") else "\n")
    if not changed and not preview.notes:
        print("No changes needed.")
    for note in preview.notes:
        print(note if note.endswith("\n") else f"{note}\n", end="")
    if preview.commands:
        print("Commands apply() would run, in order, if check() finds the step not done:")
        for command in preview.commands:
            print(f"  {command.describe(registry)}")
    if preview.read_only_commands:
        print("Read-only commands check(), apply() and verify() may run (not run in a dry run):")
        for command in preview.read_only_commands:
            print(f"  {command.describe(registry)}")


def _secrets_init(args: argparse.Namespace) -> int:
    """Create missing secrets in the 0600 secrets file and print names only (never values)."""
    from openstack_forge.execution import (
        DEFAULT_SECRETS_TARGET,
        ExecutionError,
        SecretStore,
        is_host_root,
        redact_text,
    )

    root_path = Path(args.root) if args.root is not None else Path("/")
    host_root = args.root is None or (root_path.exists() and is_host_root(root_path))
    if host_root and os.geteuid() != 0:
        print(
            "Error: Writing secrets to '/' requires root privileges (effective uid 0). "
            "Re-run as root, or pass --root PATH to target an alternate directory.",
            file=sys.stderr,
        )
        return 1

    target = args.file if args.file is not None else DEFAULT_SECRETS_TARGET
    try:
        result = SecretStore(root_path, target).ensure()
    except ExecutionError as exc:
        print(f"Error: {redact_text(str(exc))}", file=sys.stderr)
        return 1

    print(f"Secrets store: {result.path}")
    if result.created:
        print(f"Created ({len(result.created)}): {', '.join(result.created)}")
    else:
        print("Created (0): none")
    if result.kept:
        print(f"Kept existing ({len(result.kept)}): {', '.join(result.kept)}")
    return 0


def _secrets_show(args: argparse.Namespace) -> int:
    """Print a single secret value to stdout and nothing else."""
    from openstack_forge.execution import (
        DEFAULT_SECRETS_TARGET,
        ExecutionError,
        SecretStore,
        redact_text,
    )

    root_path = Path(args.root) if args.root is not None else Path("/")
    target = args.file if args.file is not None else DEFAULT_SECRETS_TARGET
    try:
        value = SecretStore(root_path, target).get(args.name)
    except ExecutionError as exc:
        print(f"Error: {redact_text(str(exc))}", file=sys.stderr)
        return 1

    print(value)
    return 0


def _secrets_list(args: argparse.Namespace) -> int:
    """List known secret names, their present/missing status, and descriptions (never values)."""
    from openstack_forge.execution import (
        DEFAULT_SECRETS_TARGET,
        ExecutionError,
        SecretStore,
        redact_text,
    )

    root_path = Path(args.root) if args.root is not None else Path("/")
    target = args.file if args.file is not None else DEFAULT_SECRETS_TARGET
    try:
        store = SecretStore(root_path, target)
        present = store.present_names()
    except ExecutionError as exc:
        print(f"Error: {redact_text(str(exc))}", file=sys.stderr)
        return 1

    print(f"Secrets store: {store.display_path}")
    for name, spec in store.specs.items():
        status = "present" if name in present else "missing"
        print(f"  {name:<18}  {status:<7}  {spec.description}")
    return 0


def _secrets_path(args: argparse.Namespace) -> int:
    """Print the resolved filesystem path of the secrets store."""
    from openstack_forge.execution import (
        DEFAULT_SECRETS_TARGET,
        ExecutionError,
        SecretStore,
        redact_text,
    )

    root_path = Path(args.root) if args.root is not None else Path("/")
    target = args.file if args.file is not None else DEFAULT_SECRETS_TARGET
    try:
        store = SecretStore(root_path, target)
    except ExecutionError as exc:
        print(f"Error: {redact_text(str(exc))}", file=sys.stderr)
        return 1

    print(store.path)
    return 0


def build_parser(
    runner: CommandRunner | None = None,
    *,
    backend: Any = None,
    executor_backend: Any = None,
) -> argparse.ArgumentParser:
    """Build the CLI parser."""
    active_backend = backend if backend is not None else executor_backend
    parser = argparse.ArgumentParser(
        prog="openstack-forge",
        description="OpenStack Forge CLI scaffold for future deployment automation.",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )

    subparsers = parser.add_subparsers(dest="command")

    init_parser = subparsers.add_parser(
        "init",
        help="Write a starter cluster.yaml configuration file.",
    )
    init_parser.add_argument(
        "--output",
        "-o",
        default="cluster.yaml",
        metavar="PATH",
        help=(
            "Path where the starter cluster configuration will be written (default: cluster.yaml)."
        ),
    )
    init_parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite --output if it already exists.",
    )
    init_parser.set_defaults(func=_init)

    config_parser = subparsers.add_parser("config", help="Configuration commands.")
    config_subparsers = config_parser.add_subparsers(dest="config_command")
    config_validate = config_subparsers.add_parser(
        "validate",
        help="Validate a cluster configuration file.",
    )
    config_validate.add_argument("path", nargs="?", default="cluster.yaml")
    config_validate.set_defaults(func=_config_validate)

    doctor_parser = subparsers.add_parser("doctor", help="Run environment diagnostics.")
    doctor_parser.add_argument(
        "--config",
        default="cluster.yaml",
        metavar="PATH",
        help="Path to the cluster configuration file (default: cluster.yaml).",
    )
    doctor_parser.add_argument(
        "--node",
        default=None,
        metavar="HOSTNAME",
        help="Hostname of the local node in the cluster configuration.",
    )
    doctor_parser.add_argument(
        "--json",
        action="store_true",
        dest="json_output",
        help="Output the diagnostic report as JSON.",
    )
    doctor_parser.set_defaults(func=lambda args: _doctor(args, runner=runner))

    plan_parser = subparsers.add_parser(
        "plan",
        help="Show the ordered deployment steps for each node (read-only).",
    )
    plan_parser.add_argument(
        "--config",
        default="cluster.yaml",
        metavar="PATH",
        help="Path to the cluster configuration file (default: cluster.yaml).",
    )
    plan_parser.add_argument(
        "--node",
        default=None,
        metavar="HOSTNAME",
        help="Only show the steps for this node (default: every node).",
    )
    plan_parser.add_argument(
        "--json",
        action="store_true",
        dest="json_output",
        help="Output the plan as JSON.",
    )
    plan_parser.set_defaults(func=_plan)

    apply_parser = subparsers.add_parser(
        "apply",
        help=(
            "Apply a single implemented deployment step or all planned steps for this node "
            "(dry-run unless --yes is given)."
        ),
    )
    apply_parser.add_argument(
        "--config",
        default="cluster.yaml",
        metavar="PATH",
        help="Path to the cluster configuration file (default: cluster.yaml).",
    )
    apply_target = apply_parser.add_mutually_exclusive_group(required=True)
    apply_target.add_argument(
        "--step",
        metavar="STEP_ID",
        help=(
            "Catalog step id to run (implemented: hosts-file, time-sync-controller, "
            "time-sync-other, openstack-packages, mariadb, rabbitmq, memcached, etcd, "
            "keystone, glance, placement, nova-controller, nova-compute, "
            "neutron-controller, neutron-compute, horizon, cinder-controller, "
            "cinder-storage)."
        ),
    )
    apply_target.add_argument(
        "--all",
        action="store_true",
        help="Run all planned catalog steps for the local node in dependency order.",
    )
    apply_parser.add_argument(
        "--node",
        default=None,
        metavar="HOSTNAME",
        help="Hostname of the local node in the cluster configuration.",
    )
    apply_parser.add_argument(
        "--root",
        default=None,
        metavar="PATH",
        help="Root directory under which target files and state are written (default: /).",
    )
    apply_parser.add_argument(
        "--yes",
        action="store_true",
        help="Write changes to disk (default: dry-run that only prints the diff).",
    )
    apply_parser.set_defaults(func=lambda args: _apply(args, backend=active_backend))

    verify_parser = subparsers.add_parser(
        "verify",
        help="Run read-only post-deployment verification for a step or all steps on this node.",
    )
    verify_parser.add_argument(
        "--config",
        default="cluster.yaml",
        metavar="PATH",
        help="Path to the cluster configuration file (default: cluster.yaml).",
    )
    verify_target = verify_parser.add_mutually_exclusive_group(required=False)
    verify_target.add_argument(
        "--step",
        default=None,
        metavar="STEP_ID",
        help="Single catalog step id to verify (default: verify all planned steps on this node).",
    )
    verify_target.add_argument(
        "--all",
        action="store_true",
        help="Verify all planned catalog steps for the local node in dependency order (default).",
    )
    verify_parser.add_argument(
        "--node",
        default=None,
        metavar="HOSTNAME",
        help="Hostname of the local node in the cluster configuration.",
    )
    verify_parser.add_argument(
        "--root",
        default=None,
        metavar="PATH",
        help="Root directory under which target files and state are read (default: /).",
    )
    verify_parser.add_argument(
        "--json",
        action="store_true",
        dest="json_output",
        help="Output the verification report as JSON.",
    )
    verify_parser.set_defaults(func=lambda args: _verify(args, backend=active_backend))

    secrets_parser = subparsers.add_parser(
        "secrets",
        help="Manage the local 0600 OpenStack secrets store.",
    )
    secrets_subparsers = secrets_parser.add_subparsers(dest="secrets_command")

    secrets_init = secrets_subparsers.add_parser(
        "init",
        help="Create missing secrets in the 0600 store and print names only.",
    )
    secrets_init.add_argument(
        "--root",
        default=None,
        metavar="PATH",
        help="Root directory under which the secrets file is stored (default: /).",
    )
    secrets_init.add_argument(
        "--file",
        default=None,
        metavar="PATH",
        help="Target path of the secrets file (default: /etc/openstack-forge/secrets.json).",
    )
    secrets_init.set_defaults(func=_secrets_init)

    secrets_show = secrets_subparsers.add_parser(
        "show",
        help="Print one secret value to stdout and nothing else.",
    )
    secrets_show.add_argument(
        "name",
        metavar="NAME",
        help="Secret name to display (for example: ADMIN_PASS or RABBIT_PASS).",
    )
    secrets_show.add_argument(
        "--root",
        default=None,
        metavar="PATH",
        help="Root directory under which the secrets file is stored (default: /).",
    )
    secrets_show.add_argument(
        "--file",
        default=None,
        metavar="PATH",
        help="Target path of the secrets file (default: /etc/openstack-forge/secrets.json).",
    )
    secrets_show.set_defaults(func=_secrets_show)

    secrets_list = secrets_subparsers.add_parser(
        "list",
        help="List secret names, present/missing status, and descriptions (never values).",
    )
    secrets_list.add_argument(
        "--root",
        default=None,
        metavar="PATH",
        help="Root directory under which the secrets file is stored (default: /).",
    )
    secrets_list.add_argument(
        "--file",
        default=None,
        metavar="PATH",
        help="Target path of the secrets file (default: /etc/openstack-forge/secrets.json).",
    )
    secrets_list.set_defaults(func=_secrets_list)

    secrets_path = secrets_subparsers.add_parser(
        "path",
        help="Print the resolved filesystem path of the secrets store.",
    )
    secrets_path.add_argument(
        "--root",
        default=None,
        metavar="PATH",
        help="Root directory under which the secrets file is stored (default: /).",
    )
    secrets_path.add_argument(
        "--file",
        default=None,
        metavar="PATH",
        help="Target path of the secrets file (default: /etc/openstack-forge/secrets.json).",
    )
    secrets_path.set_defaults(func=_secrets_path)

    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    runner: CommandRunner | None = None,
    backend: Any = None,
    executor_backend: Any = None,
) -> int:
    """Run the CLI and return its exit status."""
    parser = build_parser(
        runner=runner,
        backend=backend,
        executor_backend=executor_backend,
    )
    args = parser.parse_args(argv)

    if not hasattr(args, "func"):
        parser.print_help()
        return 0

    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
