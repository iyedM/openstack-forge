"""Execution building blocks and implemented steps (`hosts-file`).

This package is imported lazily by ``openstack-forge apply`` so that read-only CLI
commands (``config validate``, ``doctor``, ``plan``) and ``openstack_forge.orchestration``
never import it. It runs no external command, uses no network, and writes only below an
explicitly given root directory.
"""

from openstack_forge.execution.engine import (
    Engine,
    ExecutableStep,
    FailureReport,
    FunctionStep,
    Outcome,
    Phase,
    RunReport,
    StepOutcome,
    VerifyResult,
    format_report,
)
from openstack_forge.execution.errors import (
    EngineError,
    ExecutionError,
    FileFormatError,
    RootEscapeError,
    StateError,
    UnsafePathError,
)
from openstack_forge.execution.files import (
    DISABLED_LINE_PREFIX,
    FileChange,
    comment_out_lines,
    ini_set,
    managed_block,
    uncomment_lines,
)
from openstack_forge.execution.fsutil import BACKUP_SUFFIX, resolve_target
from openstack_forge.execution.hosts import (
    CONTROLLER_ALIAS,
    DEFAULT_STATE_TARGET,
    HOSTS_BLOCK_NAME,
    HOSTS_STEP_ID,
    HOSTS_TARGET,
    IMPLEMENTED_STEPS,
    LOOPBACK_127_0_1_1,
    HostsFileStep,
    apply_hosts_file,
    build_127_0_1_1_pattern,
    build_hosts_block,
    make_hosts_file_step,
    parse_hosts,
    resolve_apply_node,
    resolve_in_hosts,
    verify_hosts_file,
    verify_hosts_text,
)
from openstack_forge.execution.redaction import MASK, redact_data, redact_text
from openstack_forge.execution.state import (
    STATE_FILE_MODE,
    StateStore,
    StepRecord,
    StepStatus,
)

__all__ = [
    "BACKUP_SUFFIX",
    "CONTROLLER_ALIAS",
    "DEFAULT_STATE_TARGET",
    "DISABLED_LINE_PREFIX",
    "HOSTS_BLOCK_NAME",
    "HOSTS_STEP_ID",
    "HOSTS_TARGET",
    "IMPLEMENTED_STEPS",
    "LOOPBACK_127_0_1_1",
    "MASK",
    "STATE_FILE_MODE",
    "Engine",
    "EngineError",
    "ExecutableStep",
    "ExecutionError",
    "FailureReport",
    "FileChange",
    "FileFormatError",
    "FunctionStep",
    "HostsFileStep",
    "Outcome",
    "Phase",
    "RootEscapeError",
    "RunReport",
    "StateError",
    "StateStore",
    "StepOutcome",
    "StepRecord",
    "StepStatus",
    "UnsafePathError",
    "VerifyResult",
    "apply_hosts_file",
    "build_127_0_1_1_pattern",
    "build_hosts_block",
    "comment_out_lines",
    "format_report",
    "ini_set",
    "make_hosts_file_step",
    "managed_block",
    "parse_hosts",
    "redact_data",
    "redact_text",
    "resolve_apply_node",
    "resolve_in_hosts",
    "resolve_target",
    "uncomment_lines",
    "verify_hosts_file",
    "verify_hosts_text",
]
