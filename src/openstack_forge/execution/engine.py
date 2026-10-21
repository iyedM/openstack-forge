"""Run abstract steps in order: check, apply, verify, with resumable recorded state.

The engine knows nothing about OpenStack. A step is anything with an ``id`` and
``check()``, ``apply()`` and ``verify()`` methods; no catalog step is wired to it yet.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Protocol

from openstack_forge.execution.errors import EngineError
from openstack_forge.execution.redaction import redact_text
from openstack_forge.execution.state import StateStore, StepStatus, summarise_error

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class VerifyResult:
    ok: bool
    details: str = ""


class ExecutableStep(Protocol):
    """A step the engine can run. ``check()`` is True when the step is already done."""

    @property
    def id(self) -> str: ...

    def check(self) -> bool: ...

    def apply(self) -> None: ...

    def verify(self) -> VerifyResult: ...


@dataclass(frozen=True)
class FunctionStep:
    """An :class:`ExecutableStep` built from three callables."""

    id: str
    check_fn: Callable[[], bool]
    apply_fn: Callable[[], None]
    verify_fn: Callable[[], VerifyResult]

    def check(self) -> bool:
        return self.check_fn()

    def apply(self) -> None:
        self.apply_fn()

    def verify(self) -> VerifyResult:
        return self.verify_fn()


class Phase(str, Enum):
    CHECK = "check"
    APPLY = "apply"
    VERIFY = "verify"


class Outcome(str, Enum):
    SKIPPED = "skipped"  # check() was already true
    APPLIED = "applied"  # apply() ran and verify() passed
    VERIFIED = "verified"  # resume(): verify() passed without re-applying
    FAILED = "failed"


_NEXT_ACTIONS: dict[Phase, str] = {
    Phase.CHECK: (
        "Find out why the check of '{step}' raised an error and fix it, then run resume(); "
        "the step will be verified before anything else runs."
    ),
    Phase.APPLY: (
        "Read the error, then inspect and fix the host by hand so that '{step}' is complete; "
        "resume() verifies '{step}' first and does not apply it again."
    ),
    Phase.VERIFY: (
        "'{step}' was applied but its verification failed. Compare the host with the step's "
        "documentation and fix it by hand, then run resume() to verify it again."
    ),
}


@dataclass(frozen=True)
class FailureReport:
    """Why the engine stopped, with redacted details and what to do next."""

    step_id: str
    phase: Phase
    message: str
    details: str
    next_action: str


@dataclass(frozen=True)
class StepOutcome:
    step_id: str
    outcome: Outcome


@dataclass(frozen=True)
class RunReport:
    outcomes: tuple[StepOutcome, ...] = ()
    failure: FailureReport | None = None
    not_started: tuple[str, ...] = field(default=())

    @property
    def ok(self) -> bool:
        return self.failure is None


class Engine:
    """Run steps in order and record their status in a :class:`StateStore`."""

    def __init__(self, store: StateStore) -> None:
        self.store = store

    def run(self, steps: Sequence[ExecutableStep]) -> RunReport:
        """Run every step from the start; steps whose check() is true are skipped."""
        _require_unique_ids(steps)
        return self._run_from(steps, 0, ())

    def resume(self, steps: Sequence[ExecutableStep]) -> RunReport:
        """Verify the step that failed (or was interrupted) first, then continue after it.

        The failed step is never applied again here; if nothing failed, this is run().
        """
        _require_unique_ids(steps)
        records = self.store.load()
        unknown = [
            step_id
            for step_id, record in records.items()
            if record.status in (StepStatus.FAILED, StepStatus.RUNNING)
            and step_id not in {step.id for step in steps}
        ]
        if unknown:
            raise EngineError(
                f"Recorded failed step(s) {', '.join(unknown)} are not in the given steps"
            )
        index = next(
            (
                position
                for position, step in enumerate(steps)
                if step.id in records
                and records[step.id].status in (StepStatus.FAILED, StepStatus.RUNNING)
            ),
            None,
        )
        if index is None:
            return self.run(steps)

        step = steps[index]
        failure = self._verify(step)
        if failure is not None:
            return self._stop(steps, index, (), failure)
        self.store.mark_done(step.id)
        logger.info("Step %s verified on resume", step.id)
        return self._run_from(steps, index + 1, (StepOutcome(step.id, Outcome.VERIFIED),))

    def _run_from(
        self,
        steps: Sequence[ExecutableStep],
        start: int,
        outcomes: tuple[StepOutcome, ...],
    ) -> RunReport:
        for index in range(start, len(steps)):
            step = steps[index]
            try:
                satisfied = step.check()
            except Exception as exc:  # noqa: BLE001 - any step error is reported
                return self._stop(steps, index, outcomes, self._error(step, Phase.CHECK, exc))
            if satisfied:
                record = self.store.get(step.id)
                if record is None or record.status is not StepStatus.DONE:
                    self.store.mark_done(step.id)
                outcomes += (StepOutcome(step.id, Outcome.SKIPPED),)
                continue

            self.store.mark_running(step.id)
            logger.info("Applying step %s", step.id)
            try:
                step.apply()
            except Exception as exc:  # noqa: BLE001 - any step error is reported
                return self._stop(steps, index, outcomes, self._error(step, Phase.APPLY, exc))
            failure = self._verify(step)
            if failure is not None:
                return self._stop(steps, index, outcomes, failure)
            self.store.mark_done(step.id)
            outcomes += (StepOutcome(step.id, Outcome.APPLIED),)
        return RunReport(outcomes=outcomes)

    def _verify(self, step: ExecutableStep) -> FailureReport | None:
        try:
            result = step.verify()
        except Exception as exc:  # noqa: BLE001 - any step error is reported
            return self._error(step, Phase.VERIFY, exc)
        if result.ok:
            return None
        return _failure(step.id, Phase.VERIFY, "verification did not pass", result.details)

    def _error(self, step: ExecutableStep, phase: Phase, exc: Exception) -> FailureReport:
        failure = _failure(step.id, phase, f"{phase.value}() raised {type(exc).__name__}", str(exc))
        # Errors may carry a command-specific hint (e.g. apt-get and the dpkg state).
        hint = getattr(exc, "hint", None)
        if isinstance(hint, str) and hint:
            failure = replace(failure, next_action=f"{failure.next_action} {hint}")
        return failure

    def _stop(
        self,
        steps: Sequence[ExecutableStep],
        index: int,
        outcomes: tuple[StepOutcome, ...],
        failure: FailureReport,
    ) -> RunReport:
        self.store.mark_failed(failure.step_id, f"{failure.message}: {failure.details}")
        logger.error(
            "Step %s failed during %s: %s", failure.step_id, failure.phase.value, failure.details
        )
        return RunReport(
            outcomes=(*outcomes, StepOutcome(failure.step_id, Outcome.FAILED)),
            failure=failure,
            not_started=tuple(step.id for step in steps[index + 1 :]),
        )


def format_report(report: RunReport) -> str:
    """Human-readable summary of a run; all details are already redacted."""
    lines = [f"{outcome.step_id}: {outcome.outcome.value}" for outcome in report.outcomes]
    if report.failure is None:
        lines.append("All steps are done.")
        return "\n".join(lines)
    failure = report.failure
    lines += [
        "",
        f"Stopped at step '{failure.step_id}' during {failure.phase.value}: {failure.message}.",
        f"Details: {failure.details or '(none)'}",
        f"Next action: {failure.next_action}",
    ]
    if report.not_started:
        lines.append(f"Not started: {', '.join(report.not_started)}")
    return "\n".join(lines)


def _failure(step_id: str, phase: Phase, message: str, details: str) -> FailureReport:
    return FailureReport(
        step_id=step_id,
        phase=phase,
        message=message,
        details=summarise_error(redact_text(details)),
        next_action=_NEXT_ACTIONS[phase].format(step=step_id),
    )


def _require_unique_ids(steps: Sequence[ExecutableStep]) -> None:
    seen: set[str] = set()
    for step in steps:
        if step.id in seen:
            raise EngineError(f"Duplicate step id {step.id!r}")
        seen.add(step.id)
