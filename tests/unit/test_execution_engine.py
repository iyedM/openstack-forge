"""Tests for the step engine (abstract steps and temporary roots only)."""

from __future__ import annotations

from pathlib import Path

import pytest

from openstack_forge.execution import (
    MASK,
    Engine,
    EngineError,
    FunctionStep,
    Outcome,
    Phase,
    StateStore,
    StepStatus,
    VerifyResult,
    format_report,
    ini_set,
    managed_block,
)


class FakeStep:
    """A step whose host state is a flag; records every call made by the engine."""

    def __init__(self, step_id: str, calls: list[str], *, done: bool = False) -> None:
        self.id = step_id
        self.calls = calls
        self.done = done
        self.apply_error: Exception | None = None
        self.check_error: Exception | None = None
        self.verify_ok = True
        self.verify_details = ""

    def check(self) -> bool:
        self.calls.append(f"check:{self.id}")
        if self.check_error:
            raise self.check_error
        return self.done

    def apply(self) -> None:
        self.calls.append(f"apply:{self.id}")
        if self.apply_error:
            raise self.apply_error
        self.done = True

    def verify(self) -> VerifyResult:
        self.calls.append(f"verify:{self.id}")
        return VerifyResult(self.verify_ok and self.done, self.verify_details)


@pytest.fixture
def calls() -> list[str]:
    return []


@pytest.fixture
def steps(calls: list[str]) -> list[FakeStep]:
    return [FakeStep(name, calls) for name in ("first", "second", "third")]


@pytest.fixture
def engine(tmp_path: Path) -> Engine:
    return Engine(StateStore(tmp_path / "state.json"))


def _statuses(engine: Engine) -> dict[str, StepStatus]:
    return {step_id: record.status for step_id, record in engine.store.load().items()}


def _outcomes(report) -> list[tuple[str, Outcome]]:
    return [(o.step_id, o.outcome) for o in report.outcomes]


# --- run ------------------------------------------------------------------------------


def test_run_applies_and_verifies_in_order(engine: Engine, steps, calls) -> None:
    report = engine.run(steps)
    assert report.ok
    assert calls == [
        "check:first", "apply:first", "verify:first",
        "check:second", "apply:second", "verify:second",
        "check:third", "apply:third", "verify:third",
    ]  # fmt: skip
    assert _outcomes(report) == [(s.id, Outcome.APPLIED) for s in steps]
    assert _statuses(engine) == dict.fromkeys(("first", "second", "third"), StepStatus.DONE)


def test_steps_already_satisfied_are_skipped(engine: Engine, steps, calls) -> None:
    steps[1].done = True
    report = engine.run(steps)
    assert "apply:second" not in calls and "verify:second" not in calls
    assert _outcomes(report)[1] == ("second", Outcome.SKIPPED)
    assert _statuses(engine)["second"] is StepStatus.DONE


def test_stop_on_apply_failure(engine: Engine, steps, calls) -> None:
    steps[1].apply_error = RuntimeError("mysqld refused: password=hunter2")
    report = engine.run(steps)
    assert not report.ok
    assert calls == [
        "check:first", "apply:first", "verify:first", "check:second", "apply:second",
    ]  # fmt: skip
    failure = report.failure
    assert failure.step_id == "second"
    assert failure.phase is Phase.APPLY
    assert failure.message == "apply() raised RuntimeError"
    assert failure.details == f"mysqld refused: password={MASK}"
    assert "resume()" in failure.next_action
    assert report.not_started == ("third",)
    assert _outcomes(report) == [("first", Outcome.APPLIED), ("second", Outcome.FAILED)]
    assert _statuses(engine) == {"first": StepStatus.DONE, "second": StepStatus.FAILED}
    assert "hunter2" not in engine.store.path.read_text()


def test_step_is_only_done_if_verify_passes(engine: Engine, steps, calls) -> None:
    steps[0].verify_ok = False
    steps[0].verify_details = "chronyc reports no source; token=abc"
    report = engine.run(steps)
    failure = report.failure
    assert (failure.step_id, failure.phase) == ("first", Phase.VERIFY)
    assert failure.message == "verification did not pass"
    assert failure.details == f"chronyc reports no source; token={MASK}"
    assert "verification failed" in failure.next_action
    assert _statuses(engine) == {"first": StepStatus.FAILED}
    assert "check:second" not in calls


def test_check_errors_stop_the_run(engine: Engine, steps) -> None:
    steps[0].check_error = OSError("cannot read /etc/hosts")
    report = engine.run(steps)
    assert (report.failure.phase, report.failure.message) == (
        Phase.CHECK,
        "check() raised OSError",
    )
    assert report.not_started == ("second", "third")


def test_verify_errors_are_reported(engine: Engine, steps) -> None:
    def broken() -> VerifyResult:
        raise ValueError("unexpected output")

    steps[0].verify = broken
    report = engine.run(steps)
    assert (report.failure.phase, report.failure.message) == (
        Phase.VERIFY,
        "verify() raised ValueError",
    )


def test_duplicate_step_ids_are_rejected(engine: Engine, calls) -> None:
    with pytest.raises(EngineError, match="Duplicate step id 'a'"):
        engine.run([FakeStep("a", calls), FakeStep("a", calls)])


def test_format_report(engine: Engine, steps) -> None:
    steps[1].apply_error = RuntimeError("secret: s3")
    text = format_report(engine.run(steps))
    assert "first: applied\nsecond: failed" in text
    assert "Stopped at step 'second' during apply: apply() raised RuntimeError." in text
    assert f"Details: secret: {MASK}" in text
    assert "Next action:" in text
    assert "Not started: third" in text
    assert format_report(engine.run(steps[:1])).endswith("All steps are done.")


# --- resume ------------------------------------------------------------------------------


def test_resume_verifies_the_failed_step_first_then_continues(engine: Engine, steps, calls) -> None:
    steps[1].apply_error = RuntimeError("partial")
    engine.run(steps)
    steps[1].done = True  # the operator fixed the host by hand
    calls.clear()

    report = engine.resume(steps)

    assert report.ok
    assert calls == ["verify:second", "check:third", "apply:third", "verify:third"]
    assert _outcomes(report) == [("second", Outcome.VERIFIED), ("third", Outcome.APPLIED)]
    assert _statuses(engine) == dict.fromkeys(("first", "second", "third"), StepStatus.DONE)


def test_resume_does_not_reapply_a_step_that_still_fails(engine: Engine, steps, calls) -> None:
    steps[1].apply_error = RuntimeError("partial")
    engine.run(steps)
    calls.clear()

    report = engine.resume(steps)

    assert calls == ["verify:second"]
    assert (report.failure.step_id, report.failure.phase) == ("second", Phase.VERIFY)
    assert report.not_started == ("third",)
    assert _statuses(engine)["second"] is StepStatus.FAILED


def test_resume_after_an_interrupted_step(engine: Engine, steps, calls) -> None:
    engine.store.mark_done("first")
    engine.store.mark_running("second")  # the process died during apply()
    steps[0].done = steps[1].done = True

    report = engine.resume(steps)

    assert calls == ["verify:second", "check:third", "apply:third", "verify:third"]
    assert report.ok


def test_resume_without_a_failure_is_a_normal_run(engine: Engine, steps, calls) -> None:
    report = engine.resume(steps)
    assert report.ok
    assert calls[0] == "check:first"


def test_resume_with_unknown_failed_step(engine: Engine, steps) -> None:
    engine.store.mark_failed("removed-step", "old failure")
    with pytest.raises(EngineError, match="removed-step"):
        engine.resume(steps)


def test_run_retries_a_failed_step_from_its_check(engine: Engine, steps, calls) -> None:
    steps[1].apply_error = RuntimeError("transient")
    engine.run(steps)
    steps[1].apply_error = None
    calls.clear()
    report = engine.run(steps)
    assert report.ok
    assert calls[:4] == ["check:first", "check:second", "apply:second", "verify:second"]


# --- re-running is a no-op (real file primitives in a temporary root) ----------------------


def _file_steps(root: Path) -> list[FunctionStep]:
    hosts_block = "192.0.2.11 ctl-01\n192.0.2.21 cmp-01\n"

    def hosts_change(dry_run: bool):
        return managed_block(root, "/etc/hosts", "openstack-forge hosts", hosts_block,
                             dry_run=dry_run)  # fmt: skip

    def chrony_change(dry_run: bool):
        return ini_set(root, "/etc/app.ini", "main", "server", "ctl-01", dry_run=dry_run)

    def file_step(step_id: str, change) -> FunctionStep:
        return FunctionStep(
            id=step_id,
            check_fn=lambda: not change(True).changed,
            apply_fn=lambda: change(False),
            verify_fn=lambda: VerifyResult(not change(True).changed, "file differs"),
        )

    return [file_step("hosts-file", hosts_change), file_step("app-config", chrony_change)]


def _tree(root: Path) -> dict[str, tuple[bytes, int]]:
    return {
        str(path.relative_to(root)): (path.read_bytes(), path.stat().st_mtime_ns)
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def test_rerun_after_success_changes_nothing(tmp_path: Path) -> None:
    root = tmp_path / "root"
    (root / "etc").mkdir(parents=True)
    (root / "etc/hosts").write_text("127.0.0.1 localhost\n")
    engine = Engine(StateStore(root / "state.json"))

    first = engine.run(_file_steps(root))
    snapshot = _tree(root)
    second = engine.run(_file_steps(root))

    assert [o.outcome for o in first.outcomes] == [Outcome.APPLIED, Outcome.APPLIED]
    assert [o.outcome for o in second.outcomes] == [Outcome.SKIPPED, Outcome.SKIPPED]
    assert _tree(root) == snapshot  # same bytes and mtimes, including the state file
    assert set(snapshot) == {"etc/hosts", "etc/hosts.orig", "etc/app.ini", "state.json"}
