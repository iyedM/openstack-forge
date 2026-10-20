"""CLI entry point for OpenStack Forge."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable, Sequence

from openstack_forge import __version__
from openstack_forge.config import ConfigError, ConfigValidationError, find_release, load_config

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


def _config_validate(args: argparse.Namespace) -> int:
    """Validate a cluster configuration file; exit 0 if valid, 1 otherwise."""
    path = args.path
    try:
        config = load_config(path)
    except ConfigValidationError as exc:
        print(f"Configuration is invalid: {path}", file=sys.stderr)
        for issue in exc.issues:
            print(f"  - {issue}", file=sys.stderr)
        print(f"Found {len(exc.issues)} error(s).", file=sys.stderr)
        return 1
    except ConfigError as exc:
        print(f"Error: {exc}", file=sys.stderr)
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


def build_parser() -> argparse.ArgumentParser:
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
    doctor_parser.set_defaults(func=_not_implemented("doctor"))

    plan_parser = subparsers.add_parser("plan", help="Create a deployment plan.")
    plan_parser.set_defaults(func=_not_implemented("plan"))

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the CLI and return its exit status."""
    parser = build_parser()
    args = parser.parse_args(argv)

    if not hasattr(args, "func"):
        parser.print_help()
        return 0

    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
