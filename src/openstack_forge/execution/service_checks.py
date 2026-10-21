"""Read-only package and service checks shared by the controller service steps.

Each helper runs one allowlisted read-only command through the executor and interprets
its output; none of them changes anything.
"""

from __future__ import annotations

from openstack_forge.execution.apt_output import parse_dpkg_status
from openstack_forge.execution.commands import (
    DPKG_QUERY_STATUS,
    SYSTEMCTL_IS_ACTIVE,
    SYSTEMCTL_IS_ENABLED,
    CommandExecutor,
)
from openstack_forge.execution.preview import PlannedCommand


def query_package(package: str) -> PlannedCommand:
    return PlannedCommand(DPKG_QUERY_STATUS, {"package": package})


def query_active(unit: str) -> PlannedCommand:
    return PlannedCommand(SYSTEMCTL_IS_ACTIVE, {"unit": unit})


def query_enabled(unit: str) -> PlannedCommand:
    return PlannedCommand(SYSTEMCTL_IS_ENABLED, {"unit": unit})


def package_installed(executor: CommandExecutor, package: str) -> bool:
    """True if ``dpkg-query --status`` reports ``install ok installed``."""
    query = query_package(package)
    result = executor.run(query.command_id, query.params)
    return result.returncode == 0 and parse_dpkg_status(result.stdout)


def unit_active_problems(executor: CommandExecutor, unit: str) -> list[str]:
    """Empty if ``systemctl is-active`` exits 0 and prints exactly ``active``."""
    query = query_active(unit)
    result = executor.run(query.command_id, query.params)
    state = result.stdout.strip() or f"exit status {result.returncode}"
    if result.returncode == 0 and state == "active":
        return []
    return [f"the {unit} service is not active (state: {state})"]


def unit_enabled_problems(executor: CommandExecutor, unit: str) -> list[str]:
    """Empty if ``systemctl is-enabled`` exits 0 and prints exactly ``enabled``."""
    query = query_enabled(unit)
    result = executor.run(query.command_id, query.params)
    state = result.stdout.strip() or f"exit status {result.returncode}"
    if result.returncode == 0 and state == "enabled":
        return []
    return [f"the {unit} service is not enabled (status: {state})"]
