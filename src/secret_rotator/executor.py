"""The executor of design §4.5: runs one plan step by step under the lock and keeps where it is in
the plan's staging leaf (§3.4), so a plan resumes where it stopped, in this process or the next. It
talks to its front end, the nightly log, the terminal or the UI, through a Renderer: events out,
the operator's answers in.

Where a plan stands lives in OpenBao: its staging leaf records the plan's keys, the step it is at
and what it derived from the cluster, written before that step runs, so every step before it
finished, and whether every run of that step failed reporting it did not land; it exists exactly
while the plan is in flight; the leaf's status in the run state says whether it failed. A leaf has
one plan in flight. Step ids repeat across plans, and a leaf's plans differ by kind or by keys:
only the plan of the kind and keys recorded resumes it. The staging leaf also holds the values the
plan's steps produced, what their undos need, and, while a rollback runs, how far it got."""

import datetime
from collections.abc import Callable
from enum import StrEnum
from typing import Protocol

from secret_rotator.lock import Lock, utcnow
from secret_rotator.model import (
    Action,
    Actor,
    Event,
    Finished,
    Progress,
    Skipped,
    Started,
    Step,
    StepFailed,
    failure,
)
from secret_rotator.openbao import OpenBao, OpenBaoError
from secret_rotator.plan import Plan
from secret_rotator.staging import NOT_LANDED, STEP, Staging, flights
from secret_rotator.state import LeafState, State

ROLLBACK = "rollback"  # staging: how many of the rollback's items are done


class Stand(StrEnum):
    """Where a plan stands, read from its staging leaf and the leaf's state."""

    FRESH = "fresh"  # not started
    IN_FLIGHT = "in-flight"  # left at a step by an exit or a crash
    FAILED = "failed"  # stopped at the step that failed: Retry, Abort, Details
    ROLLING_BACK = "rolling-back"  # a rollback stopped part-way: Retry continues it


class Outcome(StrEnum):
    DONE = "done"
    FAILED = "failed"
    EXITED = "exited"  # the operator left the plan in flight
    CANCELLED = "cancelled"  # aborted while nothing had mutated
    ROLLED_BACK = "rolled-back"
    ROLLBACK_FAILED = "rollback-failed"
    DRY_RUN = "dry-run"


class Abandon(StrEnum):
    """An operator's answer that leaves an operator step undone."""

    ABORT = "abort"
    EXIT = "exit"


class Renderer(Protocol):
    def event(self, event: Event) -> None: ...

    def ask(self, step: Step, request: object) -> dict[str, str] | Abandon:
        """The operator's answer to an operator step's request."""


class PlanMismatch(Exception):
    pass


class AbortRefused(Exception):
    pass


class _Abandoned(BaseException):
    """Unwinds an operator step past any handler in its code."""

    def __init__(self, choice: Abandon):
        self.choice = choice


class _RunContext:
    def __init__(self, executor: "Executor", step: Step, action: Action):
        self.executor = executor
        self.step = step
        self.action = action
        self.bao = executor.bao
        self.state = executor.state
        self.now = executor.clock()

    def progress(self, detail: str) -> None:
        if (stop := self.executor._stop_in(self.action)) is not None:
            raise _Abandoned(stop)
        self.executor.renderer.event(Progress(self.step, self.action, detail))

    def stage(self, name: str, value: str) -> None:
        self.executor.staging.put(name, value)

    def staged(self, name: str) -> str | None:
        return self.executor.staging.get(name)

    def ask(self, request: object) -> dict[str, str]:
        answer = self.executor.renderer.ask(self.step, request)
        if isinstance(answer, Abandon):
            raise _Abandoned(answer)
        return answer


class Executor:
    """Runs one plan. run() starts it, resumes it and retries its failed step; abort() rolls it
    back. Each takes the lock for as long as it runs. In a dry run nothing runs and nothing is
    read or written. A front end that runs it off its own thread stops it with stop()."""

    def __init__(
        self,
        bao: OpenBao,
        plan: Plan,
        renderer: Renderer,
        lock: Lock,
        *,
        state: State,
        dry_run: bool,
        clock: Callable[[], datetime.datetime] = utcnow,
    ):
        self.bao = bao
        self.state = state
        self.plan = plan
        self.leaf = plan.target.leaf
        self.renderer = renderer
        self.lock = lock
        self.dry_run = dry_run
        self.clock = clock
        self.staging = Staging(bao, plan.target.kind, self.leaf)
        self.stand = Stand.FRESH
        self.at = 0  # the index of the step the plan is at
        self.kind = plan.target.kind
        self.failure: Finished | None = None  # the last failed line: Details, the Telegram message
        self._stop: Abandon | None = None

    def stop(self, choice: Abandon) -> None:
        """Stops the run under way from another thread, as the operator's answer at an operator
        step does: the tool step running stops at its next progress detail, else the plan before
        its next step. ABORT then rolls the plan back, the step it stopped counting as run, so its
        undo runs; it is refused, as Abort is, while abort_blocker() names a reason. EXIT leaves
        the plan in flight at that step, which a resume runs again, and releases the lock. A
        rollback stops on EXIT only, being an abort already."""
        self._stop = choice

    def _stop_in(self, action: Action) -> Abandon | None:
        """The stop asked for, where it applies to a line of this action."""
        return self._stop if self._stop is Abandon.EXIT or action is Action.RUN else None

    def load(self) -> Stand:
        flight = flights(self.bao).get(self.leaf)
        self.staging.load()
        if flight is None:
            self.at, self.stand = 0, Stand.FRESH
            return self.stand
        if (flight.kind, flight.keys) != (self.kind, self.plan.target.keys):
            raise PlanMismatch(
                f"{self.leaf} is in flight in its {flight.kind} plan of {', '.join(flight.keys)}, "
                f"at {flight.step}"
            )
        at = self.plan.index(flight.step)
        if at is None:
            raise PlanMismatch(
                f"{self.leaf} is in flight at step {flight.step}, which the {self.plan.name} "
                f"rebuilt from its entries does not have: they changed mid-rotation"
            )
        self.at = at
        failed = (self.state.of(self.leaf).status or "").startswith("failed")
        if self.staging.get(ROLLBACK) is not None:
            self.stand = Stand.ROLLING_BACK
        elif failed and self.plan.steps[at].actor is Actor.TOOL:
            self.stand = Stand.FAILED
        else:
            self.stand = Stand.IN_FLIGHT
        return self.stand

    def run(self) -> Outcome:
        if self.dry_run:
            for step in self.plan.steps:
                self.renderer.event(Skipped(step, "dry run"))
            return Outcome.DRY_RUN
        self._stop = None
        with self.lock.held(self.plan.name):
            if self.load() is Stand.ROLLING_BACK:
                return self._roll_back()
            return self._advance()

    def abort(self) -> Outcome:
        if self.dry_run:
            return Outcome.DRY_RUN
        self._stop = None
        with self.lock.held(self.plan.name):
            stand = self.load()
            if stand is Stand.ROLLING_BACK:
                raise AbortRefused("the rollback is under way: Retry continues it")
            if stand is Stand.FRESH:
                return Outcome.CANCELLED
            return self._abort()

    def _touched(self) -> list[Step]:
        """The steps that ran: every step before the one the plan is at, and that one too when
        it is a tool step, which may have landed before it stopped, unless it failed reporting
        that it did not land. None while the plan is not in flight: before it starts or after its
        stamp."""
        if not self.staging.exists:
            return []
        steps = self.plan.steps
        at = steps[self.at]
        ran = at.actor is Actor.TOOL and self.staging.get(NOT_LANDED) != at.id
        return [*steps[: self.at], *([at] if ran else [])]

    def rollback(self) -> list[tuple[Step, Action]]:
        """What Abort runs: the undos in reverse order, then the activators that ran, re-run in
        their original order (design §4.5)."""
        mutating = [step for step in self._touched() if step.mutates]
        undos = [(s, Action.UNDO) for s in reversed(mutating) if not s.activator and s.undo]
        return undos + [(s, Action.RERUN) for s in mutating if s.activator]

    def abort_blocker(self) -> str | None:
        """Why Abort is refused: a step that ran, mutated and has no undo. None when it is not."""
        for step in self._touched():
            if step.mutates and not step.activator and step.undo is None:
                return step.no_undo or f"{step.title}: it cannot be undone"
        return None

    def _fail(self, step: Step, action: Action, e: Exception, *, unlanded: bool = False) -> Outcome:
        """unlanded: no earlier run of the step landed, so this one may mark it not landed."""
        error, technical = failure(e)
        if unlanded and isinstance(e, StepFailed) and not e.landed:
            try:
                self.staging.put(NOT_LANDED, step.id)
            except OpenBaoError as e2:
                error += f" That it did not land is not recorded, so it counts as landed: {e2}"

        def failed(state: LeafState) -> None:
            state.status = "failed-activation" if step.activator else "failed"
            state.last_error = error if action is Action.RUN else f"rollback: {error}"
            state.last_run = self.clock().isoformat(timespec="seconds")

        try:
            self.state.update(self.leaf, failed)
        except OpenBaoError as e2:
            error += f" The failure is not recorded in the run state: {e2}"
        self.failure = Finished(step, action, False, error=error, technical=technical)
        self.renderer.event(self.failure)
        return Outcome.FAILED if action is Action.RUN else Outcome.ROLLBACK_FAILED

    def _advance(self) -> Outcome:
        for at in range(self.at, len(self.plan.steps)):
            if (stop := self._stop_in(Action.RUN)) is not None:
                return self._abandon(stop)
            self.at = at
            step = self.plan.steps[at]
            self.renderer.event(Started(step))
            # A step judges only its own run, so one the plan was already at counts as landed
            # unless every run of it so far failed reporting it did not land.
            unlanded = self.staging.get(STEP) != step.id or self.staging.get(NOT_LANDED) == step.id
            try:
                self.staging.record(self.plan.target.keys, step.id, self.plan.derived)
                detail = step.run(_RunContext(self, step, Action.RUN))
            except _Abandoned as a:
                return self._abandon(a.choice)
            except Exception as e:
                return self._fail(step, Action.RUN, e, unlanded=unlanded)
            self.renderer.event(Finished(step, Action.RUN, True, detail or ""))
        self.staging.destroy()
        return Outcome.DONE

    def _abandon(self, choice: Abandon) -> Outcome:
        return Outcome.EXITED if choice is Abandon.EXIT else self._abort()

    def _abort(self) -> Outcome:
        if reason := self.abort_blocker():
            raise AbortRefused(reason)
        if not any(step.mutates for step in self._touched()):
            self.staging.destroy()
            return Outcome.CANCELLED
        self.staging.put(ROLLBACK, "0")
        return self._roll_back()

    def _roll_back(self) -> Outcome:
        items = self.rollback()
        for done in range(int(self.staging.get(ROLLBACK)), len(items)):
            step, action = items[done]
            if self._stop_in(action) is not None:
                return Outcome.EXITED
            self.renderer.event(Started(step, action))
            try:
                run = step.undo if action is Action.UNDO else step.run
                detail = run(_RunContext(self, step, action))
                self.staging.put(ROLLBACK, str(done + 1))
            except _Abandoned:
                return Outcome.EXITED
            except Exception as e:
                return self._fail(step, action, e)
            self.renderer.event(Finished(step, action, True, detail or ""))
        self.staging.destroy()
        return Outcome.ROLLED_BACK
