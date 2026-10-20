"""CLI entry point for OpenStack Forge."""

from __future__ import annotations

import argparse

from openstack_forge import __version__


def _not_implemented_message(command: str) -> str:
    return (
        f"The '{command}' workflow is not implemented yet. "
        "OpenStack Forge is currently an early-stage project scaffold."
    )


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
    init_parser.set_defaults(func=lambda args: print(_not_implemented_message("init")))

    config_parser = subparsers.add_parser("config", help="Configuration commands.")
    config_subparsers = config_parser.add_subparsers(dest="config_command")
    config_validate = config_subparsers.add_parser(
        "validate",
        help="Validate a cluster configuration file.",
    )
    config_validate.add_argument("path", nargs="?", default="cluster.yaml")
    config_validate.set_defaults(
        func=lambda args: print(_not_implemented_message("config validate"))
    )

    doctor_parser = subparsers.add_parser("doctor", help="Run environment diagnostics.")
    doctor_parser.set_defaults(func=lambda args: print(_not_implemented_message("doctor")))

    plan_parser = subparsers.add_parser("plan", help="Create a deployment plan.")
    plan_parser.set_defaults(func=lambda args: print(_not_implemented_message("plan")))

    return parser


def main() -> int:
    """Run the CLI."""
    parser = build_parser()
    args = parser.parse_args()

    if not hasattr(args, "func"):
        parser.print_help()
        return 0

    args.func(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
