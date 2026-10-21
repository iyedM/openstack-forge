"""Registry of implemented catalog steps.

Any catalog step absent from :data:`IMPLEMENTED_STEPS` is refused by
``openstack-forge apply`` with exit code 1.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from openstack_forge.execution.chrony import (
    TIME_SYNC_CONTROLLER_STEP_ID,
    TIME_SYNC_OTHER_STEP_ID,
    make_time_sync_controller_step,
    make_time_sync_other_step,
)
from openstack_forge.execution.commands import CommandExecutor
from openstack_forge.execution.hosts import HOSTS_STEP_ID, make_hosts_file_step
from openstack_forge.execution.mariadb import MARIADB_STEP_ID, make_mariadb_step
from openstack_forge.execution.openstack_packages import (
    OPENSTACK_PACKAGES_STEP_ID,
    make_openstack_packages_step,
)

# factory(root, config, local_node, *, executor) -> step. Steps expose ``id``, ``check()``,
# ``apply()``, ``verify()``, ``dry_run_preview()`` and ``declares_commands``.
StepFactory = Callable[..., Any]

IMPLEMENTED_STEPS: Mapping[str, StepFactory] = {
    HOSTS_STEP_ID: make_hosts_file_step,
    TIME_SYNC_CONTROLLER_STEP_ID: make_time_sync_controller_step,
    TIME_SYNC_OTHER_STEP_ID: make_time_sync_other_step,
    OPENSTACK_PACKAGES_STEP_ID: make_openstack_packages_step,
    MARIADB_STEP_ID: make_mariadb_step,
}


def build_step(
    step_id: str,
    root: Path,
    config: Mapping[str, Any],
    local_node: Mapping[str, Any],
    *,
    executor: CommandExecutor,
) -> Any:
    """Construct the implemented step ``step_id``; KeyError if it is not implemented."""
    return IMPLEMENTED_STEPS[step_id](root, config, local_node, executor=executor)
