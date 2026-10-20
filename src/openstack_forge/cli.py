"""CLI entry point for OpenStack Forge."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable, Sequence
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


def _not_implemented_message(command: str) -> str:
    return (
        f"The '{command}' workflow is not implemented yet. "
        "OpenStack Forge is currently an early-stage project scaffold."
    )


def _not_implemented(command: str) -> Handler:
    def handler(args: argparse.Namespace) -> int:
        print(_not_implemented_message(command))
        return 0

    return handler


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


def build_parser(runner: CommandRunner | None = None) -> argparse.ArgumentParser:
    """Build the CLI parser."""
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

    init_parser = subparsers.add_parser("init", help="Initialize a deployment workspace.")
    init_parser.set_defaults(func=_not_implemented("init"))

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

    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    runner: CommandRunner | None = None,
) -> int:
    """Run the CLI and return its exit status."""
    parser = build_parser(runner=runner)
    args = parser.parse_args(argv)

    if not hasattr(args, "func"):
        parser.print_help()
        return 0

    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
