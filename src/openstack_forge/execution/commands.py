"""Allowlisted, shell-free command execution for future mutating steps.

A command can only run if a :class:`CommandSpec` for it is registered in a
:class:`CommandRegistry`; callers pass a command id and typed parameters, never a command
string. Parameters are validated before anything runs, argv is never passed to a shell,
the environment is replaced by a minimal one, output is redacted then truncated, and a
mutating command refuses to run unless the root is ``/`` and the process is root (sudo is
never used). Every executed command is recorded in an :class:`AuditLog`.

:data:`DEFAULT_REGISTRY` holds exactly the reviewed :data:`SYSTEM_COMMANDS`.
"""

from __future__ import annotations

import ipaddress
import os
import re
import shlex
import signal
import subprocess
import time
import uuid
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Protocol

from openstack_forge.execution.audit import AuditLog
from openstack_forge.execution.engine import VerifyResult
from openstack_forge.execution.errors import (
    CommandError,
    CommandNotFoundError,
    CommandSpecError,
    CommandTimeoutError,
    MutationRefusedError,
    ParameterError,
    UnexpectedExitError,
    UnknownCommandError,
)
from openstack_forge.execution.redaction import redact_text

# The complete environment of every command; nothing is inherited from the caller.
SANITIZED_ENV: Mapping[str, str] = {
    "PATH": "/usr/sbin:/usr/bin:/sbin:/bin",
    "LC_ALL": "C",
    "LANG": "C",
    "DEBIAN_FRONTEND": "noninteractive",
}
DEFAULT_OUTPUT_LIMIT = 64 * 1024  # characters kept per stream
TRUNCATION_MARKER = "[... output truncated: {count} earlier characters removed ...]\n"

# Executables that would reintroduce a shell, an arbitrary program, or privilege changes.
FORBIDDEN_EXECUTABLES: frozenset[str] = frozenset(
    {
        "sh", "bash", "dash", "zsh", "ksh", "mksh", "csh", "tcsh", "fish", "busybox",
        "env", "xargs", "nohup", "setsid", "nice", "timeout", "script",
        "sudo", "su", "doas", "pkexec", "runuser", "chroot",
    }
)  # fmt: skip

# Rejected in every parameter value, whatever its type, before the type's own regex runs.
_FORBIDDEN_CHARACTERS = frozenset(";|&$`\"'\\<>(){}[]*?!#~%^,")
_PLACEHOLDER_RE = re.compile(r"^\{([a-z][a-z0-9_]*)\}$")
_COMMAND_ID_RE = re.compile(r"^[a-z0-9]+(?:[-.][a-z0-9]+)*$")


# --- parameter types ------------------------------------------------------------------


@dataclass(frozen=True)
class ParamType:
    """A named parameter type: a strict full-match regex plus an optional extra check."""

    name: str
    pattern: re.Pattern[str]
    description: str
    extra_check: Callable[[str], bool] | None = None

    def is_valid(self, value: str) -> bool:
        if not self.pattern.fullmatch(value):
            return False
        return self.extra_check is None or self.extra_check(value)


def _is_ipv4(value: str) -> bool:
    try:
        ipaddress.IPv4Address(value)
    except ValueError:
        return False
    return True


def _is_clean_absolute_path(value: str) -> bool:
    # Inspect the raw segments: PurePosixPath would silently collapse "." components.
    return all(part not in ("", ".", "..") for part in value.split("/")[1:])


PARAM_TYPES: Mapping[str, ParamType] = {
    param.name: param
    for param in (
        ParamType(
            "systemd_unit",
            re.compile(r"[A-Za-z0-9][A-Za-z0-9_.@:-]{0,254}"),
            'a systemd unit name such as "chrony" or "chrony.service"',
        ),
        ParamType(
            "package_name",
            re.compile(r"[a-z0-9][a-z0-9+.-]{1,127}"),
            'a Debian package name such as "python3-openstackclient"',
        ),
        ParamType(
            "cloud_archive_pocket",
            re.compile(r"cloud-archive:[a-z]+(?:-(?:updates|proposed))?"),
            'an Ubuntu Cloud Archive pocket such as "cloud-archive:epoxy"',
        ),
        ParamType(
            "hostname",
            re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"),
            "a single DNS label",
        ),
        ParamType(
            "ipv4_address",
            re.compile(r"(?:[0-9]{1,3}\.){3}[0-9]{1,3}"),
            "a dotted-quad IPv4 address",
            extra_check=_is_ipv4,
        ),
        ParamType(
            "absolute_path",
            re.compile(r"(?:/[A-Za-z0-9._@+-]+)+"),
            'an absolute path without "." or ".." segments, such as "/etc/hosts"',
            extra_check=_is_clean_absolute_path,
        ),
    )
}


def validate_parameter(type_name: str, name: str, value: object) -> str:
    """Return ``value`` if it is a valid ``type_name`` string, else raise ParameterError."""
    param_type = PARAM_TYPES[type_name]
    if not isinstance(value, str):
        raise ParameterError(f"Parameter {name!r} must be a string, got {type(value).__name__}")
    shown = redact_text(repr(value))
    if not value:
        raise ParameterError(f"Parameter {name!r} must not be empty")
    if value.startswith("-"):
        raise ParameterError(f"Parameter {name!r} must not start with '-': {shown}")
    if any(char.isspace() or not char.isprintable() for char in value):
        raise ParameterError(f"Parameter {name!r} contains whitespace or control characters")
    bad = sorted(set(value) & _FORBIDDEN_CHARACTERS)
    if bad:
        raise ParameterError(
            f"Parameter {name!r} contains forbidden character(s) {''.join(bad)!r}: {shown}"
        )
    if not param_type.is_valid(value):
        raise ParameterError(
            f"Parameter {name!r} is not {param_type.description} ({type_name}): {shown}"
        )
    return value


# --- specifications and registry -------------------------------------------------------


@dataclass(frozen=True)
class CommandSpec:
    """An allowlisted command: a fixed argv template with typed whole-argument parameters.

    Each ``argv`` element is either a literal or exactly ``{name}``; placeholders are never
    interpolated into part of an argument. ``argv[0]`` must be a literal absolute path.
    """

    id: str
    argv: tuple[str, ...]
    params: Mapping[str, str] = field(default_factory=dict)
    timeout: float = 60.0
    allowed_exit_codes: frozenset[int] = frozenset({0})
    mutates: bool = True
    description: str = ""
    # Appended to the engine's suggested next action when this command fails.
    failure_hint: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "argv", tuple(self.argv))
        object.__setattr__(self, "params", dict(self.params))
        object.__setattr__(self, "allowed_exit_codes", frozenset(self.allowed_exit_codes))
        if not _COMMAND_ID_RE.fullmatch(self.id):
            raise CommandSpecError(f"Invalid command id {self.id!r}")
        if not self.argv:
            raise CommandSpecError(f"Command {self.id!r} has an empty argv")
        executable = self.argv[0]
        if _PLACEHOLDER_RE.match(executable) or not executable.startswith("/"):
            raise CommandSpecError(
                f"Command {self.id!r}: argv[0] must be a literal absolute path, got {executable!r}"
            )
        if PurePosixPath(executable).name in FORBIDDEN_EXECUTABLES:
            raise CommandSpecError(
                f"Command {self.id!r}: {executable!r} is not allowed (no shell, env or sudo)"
            )
        if any(element == "" or "\0" in element for element in self.argv):
            raise CommandSpecError(f"Command {self.id!r} has an empty or NUL argv element")
        if any(PurePosixPath(e).name in {"sudo", "su", "doas", "pkexec"} for e in self.argv):
            raise CommandSpecError(f"Command {self.id!r} must not invoke sudo or su")
        used = [m[1] for e in self.argv if (m := _PLACEHOLDER_RE.match(e))]
        for element in self.argv:
            if not _PLACEHOLDER_RE.match(element) and ("{" in element or "}" in element):
                raise CommandSpecError(
                    f"Command {self.id!r}: placeholders must be whole arguments, got {element!r}"
                )
        unknown_types = sorted(t for t in self.params.values() if t not in PARAM_TYPES)
        if unknown_types:
            raise CommandSpecError(
                f"Command {self.id!r}: unknown parameter type(s) {unknown_types}"
            )
        if sorted(set(used)) != sorted(self.params) or len(used) != len(set(used)):
            raise CommandSpecError(
                f"Command {self.id!r}: every declared parameter must appear exactly once in argv"
            )
        if not self.timeout > 0:
            raise CommandSpecError(f"Command {self.id!r}: timeout must be positive")
        if not self.allowed_exit_codes or not all(
            isinstance(code, int) and 0 <= code <= 255 for code in self.allowed_exit_codes
        ):
            raise CommandSpecError(f"Command {self.id!r}: invalid allowed exit codes")

    def build_argv(self, values: Mapping[str, object]) -> tuple[str, ...]:
        """Validate ``values`` against the declared parameters and fill the template."""
        missing = sorted(set(self.params) - set(values))
        unexpected = sorted(set(values) - set(self.params))
        if missing or unexpected:
            raise ParameterError(
                f"Command {self.id!r}: missing parameter(s) {missing}, unexpected {unexpected}"
            )
        checked = {
            name: validate_parameter(self.params[name], name, values[name]) for name in self.params
        }
        return tuple(
            checked[m[1]] if (m := _PLACEHOLDER_RE.match(element)) else element
            for element in self.argv
        )


class CommandRegistry:
    """The allowlist: only commands registered here can be executed."""

    def __init__(self, specs: Iterable[CommandSpec] = ()) -> None:
        self._specs: dict[str, CommandSpec] = {}
        for spec in specs:
            self.register(spec)

    def register(self, spec: CommandSpec) -> None:
        if spec.id in self._specs:
            raise CommandSpecError(f"Command {spec.id!r} is already registered")
        self._specs[spec.id] = spec

    def get(self, command_id: str) -> CommandSpec:
        try:
            return self._specs[command_id]
        except KeyError:
            known = ", ".join(sorted(self._specs)) or "none"
            raise UnknownCommandError(
                f"Command {command_id!r} is not in the allowlist (registered: {known})"
            ) from None

    def ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._specs))


# --- reviewed system commands ---------------------------------------------------------
#
# The only real commands OpenStack Forge may run. Each argv[0] is the literal path that
# the named Ubuntu 24.04 package installs (checked with `dpkg -S`), so PATH is never
# searched. Options are limited to ones justified by the tools' manual pages.
#
# The Python code never opens a network connection itself, but apt-get update,
# apt-get install and add-apt-repository download package lists and packages from the
# configured apt sources: running them uses the network.

APT_GET_INSTALL = "apt-get.install"
APT_GET_UPDATE = "apt-get.update"
ADD_APT_REPOSITORY_CLOUD_ARCHIVE = "add-apt-repository.cloud-archive"
APT_CACHE_POLICY = "apt-cache.policy"
SYSTEMCTL_RESTART = "systemctl.restart"
SYSTEMCTL_IS_ACTIVE = "systemctl.is-active"
DPKG_QUERY_STATUS = "dpkg-query.status"

APT_FAILURE_HINT = (
    "apt-get may have stopped part-way, leaving packages half-installed or "
    "half-configured: inspect the package state with 'dpkg --audit' before retrying. If "
    "apt-get reported that it could not get the dpkg lock, another package manager (for "
    "example unattended-upgrades) is running: wait for it to finish; do not delete lock "
    "files. OpenStack Forge does not attempt any automatic recovery."
)

APT_UPDATE_FAILURE_HINT = (
    "apt-get update downloads the package lists over the network: check that this node "
    "can reach its configured apt sources (DNS, proxy, firewall, mirror) and read the "
    "'E:'/'W:' lines above. If apt-get reported that it could not get a lock, another "
    "package manager (for example unattended-upgrades) is running: wait for it to finish; "
    "do not delete lock files. OpenStack Forge does not attempt any automatic recovery."
)

ADD_APT_REPOSITORY_FAILURE_HINT = (
    "add-apt-repository writes an apt source entry and then updates the package lists over "
    "the network: check whether the Ubuntu Cloud Archive entry was added (for example with "
    "'add-apt-repository --list') and whether this node can reach the Ubuntu Cloud Archive "
    "and its other apt sources. If a lock could not be obtained, another package manager "
    "is running: wait for it to finish; do not delete lock files. OpenStack Forge does not "
    "attempt any automatic recovery."
)

SYSTEM_COMMANDS: tuple[CommandSpec, ...] = (
    CommandSpec(
        APT_GET_INSTALL,
        # /usr/bin/apt-get (package "apt"). apt(8), "SCRIPT USAGE", says scripts should use
        # apt-get rather than apt. --yes: apt-get(8) "assume yes to all prompts and run
        # non-interactively", still aborting on held, unauthenticated or essential packages.
        # DEBIAN_FRONTEND=noninteractive comes from the sanitized environment.
        ("/usr/bin/apt-get", "install", "--yes", "{package}"),
        {"package": "package_name"},
        timeout=900,
        mutates=True,
        description="Install one Debian package",
        failure_hint=APT_FAILURE_HINT,
    ),
    CommandSpec(
        APT_GET_UPDATE,
        # /usr/bin/apt-get (package "apt"). apt-get(8): "update is used to resynchronize
        # the package index files from their sources", fetched from the locations in the
        # apt sources: it USES THE NETWORK. It takes no option here: it asks no question,
        # and DIAGNOSTICS says it "returns zero on normal operation, decimal 100 on error".
        # It changes /var/lib/apt/lists, hence mutates=True (and the root/uid checks).
        ("/usr/bin/apt-get", "update"),
        timeout=600,
        mutates=True,
        description="Download the package lists from the configured apt sources (network)",
        failure_hint=APT_UPDATE_FAILURE_HINT,
    ),
    CommandSpec(
        ADD_APT_REPOSITORY_CLOUD_ARCHIVE,
        # /usr/bin/add-apt-repository (package "software-properties-common", NOT "apt").
        # add-apt-repository(1):
        # - "-y, --yes  Assume yes to all queries": the non-interactive option (stdin is
        #   /dev/null, so a prompt would otherwise fail).
        # - "-C, --cloud  Add an Ubuntu Cloud Archive. Must be in the format
        #   cloud-archive:CANAME ... If not specified, the pocket defaults to -updates."
        #   The guide's positional form (add-apt-repository cloud-archive:epoxy) is the
        #   LINE argument, which the man page calls "deprecated" and "not recommended as
        #   the autodetection of which line format is intended can be ambiguous".
        # - Does it run an update? Yes: "-n, --no-update  After adding the repository, do
        #   not update the package cache." means it updates by default. --no-update is not
        #   used, so it refreshes the package lists itself (over the network) and no
        #   separate apt-get update is needed after it.
        ("/usr/bin/add-apt-repository", "--yes", "--cloud", "{pocket}"),
        {"pocket": "cloud_archive_pocket"},
        timeout=600,
        mutates=True,
        description="Enable one Ubuntu Cloud Archive pocket, then update the package lists",
        failure_hint=ADD_APT_REPOSITORY_FAILURE_HINT,
    ),
    CommandSpec(
        APT_CACHE_POLICY,
        # /usr/bin/apt-cache (package "apt"). apt-cache(8): it "does not manipulate the
        # state of the system"; "policy [pkg...] ... prints out detailed information about
        # the priority selection of the named package" (its versions and the apt sources
        # providing them), from the local package lists: apt-cache "works independently of
        # the availability of the configured sources (e.g. offline)".
        # DIAGNOSTICS: "returns zero on normal operation, decimal 100 on error". An unknown
        # package still exits 0, with nothing on stdout and an "N:" notice on stderr.
        ("/usr/bin/apt-cache", "policy", "{package}"),
        {"package": "package_name"},
        timeout=60,
        mutates=False,
        description="Show the versions of one package and the apt sources providing them",
    ),
    CommandSpec(
        SYSTEMCTL_RESTART,
        # /usr/bin/systemctl (package "systemd"). systemctl(1): "restart: stop and then
        # start one or more units ... If the units are not running yet, they will be started."
        ("/usr/bin/systemctl", "restart", "{unit}"),
        {"unit": "systemd_unit"},
        timeout=120,
        mutates=True,
        description="Restart one systemd unit",
    ),
    CommandSpec(
        SYSTEMCTL_IS_ACTIVE,
        # systemctl(1): is-active "returns an exit code 0 if at least one is active, or
        # non-zero otherwise" and prints the unit state. The LSB codes 1-4 all mean "not
        # active"; callers must also check that the printed state is exactly "active".
        ("/usr/bin/systemctl", "is-active", "{unit}"),
        {"unit": "systemd_unit"},
        timeout=30,
        allowed_exit_codes={0, 1, 2, 3, 4},
        mutates=False,
        description="Report whether one systemd unit is active",
    ),
    CommandSpec(
        DPKG_QUERY_STATUS,
        # /usr/bin/dpkg-query (package "dpkg"). dpkg-query(1): --status "displays the entry
        # in the installed package status database"; exit status 1 means "no ... package
        # being found", 2 is a fatal error.
        ("/usr/bin/dpkg-query", "--status", "{package}"),
        {"package": "package_name"},
        timeout=30,
        allowed_exit_codes={0, 1},
        mutates=False,
        description="Show the dpkg status entry of one package",
    ),
)

DEFAULT_REGISTRY = CommandRegistry(SYSTEM_COMMANDS)

DEFAULT_TERM_GRACE_PERIOD = 5.0  # seconds between SIGTERM and SIGKILL on timeout


# --- process backend ------------------------------------------------------------------


@dataclass(frozen=True)
class ProcessOutput:
    returncode: int
    stdout: str
    stderr: str


class ProcessBackend(Protocol):
    """Runs a validated argv; injectable so tests never spawn real system commands."""

    def run(
        self, argv: tuple[str, ...], *, env: Mapping[str, str], timeout: float
    ) -> ProcessOutput: ...


class SubprocessBackend:
    """Run argv with ``subprocess`` (never a shell) in its own session, stdin closed.

    On timeout the whole process group gets SIGTERM; if it is still running after
    ``term_grace_period`` seconds it gets SIGKILL.
    """

    def __init__(self, term_grace_period: float = DEFAULT_TERM_GRACE_PERIOD) -> None:
        if term_grace_period < 0:
            raise ValueError("term_grace_period must not be negative")
        self.term_grace_period = term_grace_period

    def run(
        self, argv: tuple[str, ...], *, env: Mapping[str, str], timeout: float
    ) -> ProcessOutput:
        try:
            process = subprocess.Popen(
                list(argv),
                shell=False,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=dict(env),
                cwd="/",
                close_fds=True,
                start_new_session=True,
            )
        except FileNotFoundError as exc:
            raise CommandNotFoundError(f"Executable not found: {argv[0]}") from exc
        except PermissionError as exc:
            raise CommandNotFoundError(f"Executable is not executable: {argv[0]}") from exc
        try:
            stdout, stderr = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            # The command runs in its own session: signal it and anything it started.
            _signal_group(process.pid, signal.SIGTERM)
            try:
                process.communicate(timeout=self.term_grace_period)
            except subprocess.TimeoutExpired:
                _signal_group(process.pid, signal.SIGKILL)
                process.communicate()
                raise CommandTimeoutError(
                    f"{argv[0]} timed out after {timeout:g}s and was killed (SIGKILL) "
                    f"after ignoring SIGTERM for {self.term_grace_period:g}s",
                    killed=True,
                ) from exc
            raise CommandTimeoutError(
                f"{argv[0]} timed out after {timeout:g}s and was terminated (SIGTERM)"
            ) from exc
        return ProcessOutput(
            returncode=process.returncode,
            stdout=stdout.decode("utf-8", errors="replace"),
            stderr=stderr.decode("utf-8", errors="replace"),
        )


def _signal_group(pid: int, signum: int) -> None:
    try:
        os.killpg(pid, signum)
    except ProcessLookupError:
        pass


# --- executor --------------------------------------------------------------------------


@dataclass(frozen=True)
class CommandResult:
    """A finished (or dry-run) command; argv and output are redacted, output truncated."""

    command_id: str
    argv: tuple[str, ...]
    returncode: int | None
    stdout: str = ""
    stderr: str = ""
    duration: float = 0.0
    dry_run: bool = False
    stdout_truncated: bool = False
    stderr_truncated: bool = False

    @property
    def ok(self) -> bool:
        return self.returncode == 0


def is_host_root(root: Path) -> bool:
    """True only if ``root`` resolves to the real filesystem root ``/``."""
    return Path(root).resolve() == Path("/")


class CommandExecutor:
    """Execute allowlisted commands for steps; the only way this package runs a process."""

    def __init__(
        self,
        registry: CommandRegistry,
        *,
        root: Path,
        audit_log: AuditLog,
        backend: ProcessBackend | None = None,
        dry_run: bool = False,
        output_limit: int = DEFAULT_OUTPUT_LIMIT,
        echo: Callable[[str], None] = print,
        euid: Callable[[], int] = os.geteuid,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if output_limit < 1:
            raise ValueError("output_limit must be positive")
        self.registry = registry
        self.root = Path(root)
        self.audit_log = audit_log
        self.backend: ProcessBackend = SubprocessBackend() if backend is None else backend
        self.dry_run = dry_run
        self.output_limit = output_limit
        self._echo = echo
        self._euid = euid
        self._clock = clock

    def run(self, command_id: str, params: Mapping[str, object] | None = None) -> CommandResult:
        """Validate and run ``command_id``; raise a typed CommandError on any failure."""
        spec = self.registry.get(command_id)
        argv = spec.build_argv(params or {})
        shown_argv = tuple(redact_text(element) for element in argv)

        if self.dry_run:
            self._echo(f"DRY RUN [{spec.id}]: {shlex.join(shown_argv)}")
            return CommandResult(spec.id, shown_argv, returncode=None, dry_run=True)
        if spec.mutates:
            self._refuse_unsafe_mutation(spec)

        invocation = uuid.uuid4().hex
        base = {"invocation": invocation, "command_id": spec.id, "argv": list(shown_argv)}
        self.audit_log.record({**base, "event": "start", "mutates": spec.mutates})
        started = self._clock()
        try:
            output = self.backend.run(argv, env=dict(SANITIZED_ENV), timeout=spec.timeout)
        except CommandError as exc:
            outcome = "timeout" if isinstance(exc, CommandTimeoutError) else "not_found"
            self._finish(base, outcome, None, started)
            _with_hint(exc, spec)
            raise
        except Exception as exc:
            self._finish(base, "error", None, started)
            error = CommandError(f"Command {spec.id!r} could not be run: {exc}")
            raise _with_hint(error, spec) from exc

        duration = self._finish(
            base,
            "ok" if output.returncode in spec.allowed_exit_codes else "unexpected_exit",
            output.returncode,
            started,
        )
        stdout, stdout_cut = self._clean(output.stdout)
        stderr, stderr_cut = self._clean(output.stderr)
        if output.returncode not in spec.allowed_exit_codes:
            raise _with_hint(UnexpectedExitError(spec.id, output.returncode, stderr), spec)
        return CommandResult(
            spec.id,
            shown_argv,
            output.returncode,
            stdout,
            stderr,
            duration,
            stdout_truncated=stdout_cut,
            stderr_truncated=stderr_cut,
        )

    def _refuse_unsafe_mutation(self, spec: CommandSpec) -> None:
        if not is_host_root(self.root):
            raise MutationRefusedError(
                f"Refusing mutating command {spec.id!r}: root is {self.root}, not '/'; "
                "mutating commands only run against the real host"
            )
        if self._euid() != 0:
            raise MutationRefusedError(
                f"Refusing mutating command {spec.id!r}: it requires effective uid 0 "
                "(run the tool as root; sudo is never used)"
            )

    def _finish(
        self, base: Mapping[str, object], outcome: str, returncode: int | None, started: float
    ) -> float:
        duration = round(self._clock() - started, 6)
        self.audit_log.record(
            {
                **base,
                "event": "finish",
                "outcome": outcome,
                "exit_code": returncode,
                "duration_s": duration,
            }  # fmt: skip
        )
        return duration

    def _clean(self, text: str) -> tuple[str, bool]:
        """Redact first (so a cut cannot expose a secret), then keep the last characters."""
        redacted = redact_text(text)
        if len(redacted) <= self.output_limit:
            return redacted, False
        removed = len(redacted) - self.output_limit
        return TRUNCATION_MARKER.format(count=removed) + redacted[-self.output_limit :], True


def _with_hint(error: CommandError, spec: CommandSpec) -> CommandError:
    if spec.failure_hint:
        error.hint = spec.failure_hint
    return error


# --- helpers for steps ----------------------------------------------------------------


def command_succeeds(
    executor: CommandExecutor, command_id: str, params: Mapping[str, object] | None = None
) -> bool:
    """For ``check()``: True if the command exits 0 (other allowed codes mean False)."""
    return executor.run(command_id, params).returncode == 0


def verify_command(
    executor: CommandExecutor, command_id: str, params: Mapping[str, object] | None = None
) -> VerifyResult:
    """For ``verify()``: ok if the command exits 0; details are the redacted output."""
    try:
        result = executor.run(command_id, params)
    except UnexpectedExitError as exc:
        return VerifyResult(False, str(exc))
    details = (result.stdout or result.stderr).strip()
    return VerifyResult(result.returncode == 0, details)
