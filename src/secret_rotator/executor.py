"""The executor of design §4.5: runs one plan step by step under the lock and keeps where it is in
the leaf's run state (§3.4), so a plan resumes where it stopped, in this process or the next. It
talks to its front end, the nightly log, the terminal or the UI, through a Renderer: events out,
the operator's answers in.

Where a plan stands lives in OpenBao: rotator_step names the plan a leaf has in flight and the
step it is at, as <kind>/<keys>/<step id>, written before that step runs, so every step before it
finished; rotator_status says whether it failed. A leaf has one plan in flight. Step ids repeat
across plans, and a leaf's plans differ by kind or by keys: only the plan of the kind and keys
recorded resumes it. The plan's staging leaf holds the values its steps produced, what their undos
need, and, while a rollback runs, how far it got."""

import datetime
import traceback
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from secret_rotator.contract import LAST_ERROR, LAST_RUN, MAX_VALUE_BYTES, STATUS, STEP
from secret_rotator.jenkins import JenkinsError
from secret_rotator.kube import KubeError
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
)
from secret_rotator.openbao import OpenBao, OpenBaoError
from secret_rotator.plan import Plan
from secret_rotator.staging import Staging

ROLLBACK = "rollback"  # staging: how many of the rollback's items are done


class Stand(StrEnum):
    """Where a plan stands, read from the leaf's state."""

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


@dataclass(frozen=True)
class InFlight:
    """The plan a leaf has in flight, by kind and keys, and the step it is at."""

    kind: str
    keys: tuple[str, ...]
    step: str

    def __str__(self) -> str:
        return f"{self.kind}/{','.join(self.keys)}/{self.step}"


def in_flight(meta: Mapping[str, str]) -> InFlight | None:
    """The leaf's plan in flight, read from its rotator_step; None when it has none. PlanMismatch
    for a rotator_step that is not <kind>/<keys>/<step id>."""
    mark = meta.get(STEP)
    if mark is None:
        return None
    parts = mark.split("/", 2)
    if len(parts) != 3:
        raise PlanMismatch(f"its rotator_step {mark!r} is not <kind>/<keys>/<step id>")
    kind, keys, step = parts
    return InFlight(kind, tuple(keys.split(",")), step)


class AbortRefused(Exception):
    pass


class _Abandoned(BaseException):
    """Unwinds an operator step past any handler in its code."""

    def __init__(self, choice: Abandon):
        self.choice = choice


def _clip(text: str) -> str:
    return text.encode()[:MAX_VALUE_BYTES].decode(errors="ignore")


def _failure(e: Exception) -> tuple[str, str]:
    """A failure's one sentence and its technical detail. An OpenBaoError, a KubeError or a
    JenkinsError names its request, and a transport error as such."""
    technical = "".join(traceback.format_exception(e))
    if isinstance(e, StepFailed):
        return e.error, e.technical or technical
    if isinstance(e, OpenBaoError | KubeError | JenkinsError):
        return str(e), technical
    return f"{type(e).__name__}: {e}", technical


class _RunContext:
    def __init__(self, executor: "Executor", step: Step, action: Action):
        self.executor = executor
        self.step = step
        self.action = action
        self.bao = executor.bao
        self.now = executor.clock()

    def progress(self, detail: str) -> None:
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
    read or written."""

    def __init__(
        self,
        bao: OpenBao,
        plan: Plan,
        renderer: Renderer,
        lock: Lock,
        *,
        dry_run: bool,
        clock: Callable[[], datetime.datetime] = utcnow,
    ):
        self.bao = bao
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
        self.recorded: str | None = None  # the leaf's rotator_step

    def load(self) -> Stand:
        meta = self.bao.metadata(self.leaf)
        if meta is None:
            raise PlanMismatch(f"no leaf {self.leaf}")
        self.recorded = meta.get(STEP)
        self.staging.load()
        flight = in_flight(meta)
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
                f"rebuilt from its annotations does not have: they changed mid-rotation"
            )
        self.at = at
        failed = meta.get(STATUS, "").startswith("failed")
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
        with self.lock.held(self.plan.name):
            if self.load() is Stand.ROLLING_BACK:
                return self._roll_back()
            if self.stand is Stand.FRESH:
                # Left by a plan whose staging leaf was not destroyed after its stamp: its values
                # must not be taken for this plan's.
                self.staging.destroy()
            return self._advance()

    def abort(self) -> Outcome:
        if self.dry_run:
            return Outcome.DRY_RUN
        with self.lock.held(self.plan.name):
            stand = self.load()
            if stand is Stand.ROLLING_BACK:
                raise AbortRefused("the rollback is under way: Retry continues it")
            if stand is Stand.FRESH:
                return Outcome.CANCELLED
            return self._abort()

    def _touched(self) -> list[Step]:
        """The steps that ran: every step before the one the plan is at, and that one too when
        it is a tool step, which may have landed before it stopped. None before a plan starts
        or after its stamp."""
        if self.recorded is None:
            return []
        steps = self.plan.steps
        at = steps[self.at]
        return [*steps[: self.at], *([at] if at.actor is Actor.TOOL else [])]

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

    def _mark(self, step: Step) -> str:
        return str(InFlight(self.kind, self.plan.target.keys, step.id))

    def _record(self, step: Step) -> None:
        mark = self._mark(step)
        self.bao.patch_metadata(self.leaf, {STEP: mark})
        self.recorded = mark

    def _clear(self) -> None:
        if self.recorded is not None:
            self.bao.patch_metadata(self.leaf, {STEP: None})
            self.recorded = None

    def _fail(self, step: Step, action: Action, e: Exception) -> Outcome:
        error, technical = _failure(e)
        state = {
            STATUS: "failed-activation" if step.activator else "failed",
            STEP: self._mark(self.plan.steps[self.at]),
            LAST_ERROR: _clip(error if action is Action.RUN else f"rollback: {error}"),
            LAST_RUN: self.clock().isoformat(timespec="seconds"),
        }
        try:
            self.bao.patch_metadata(self.leaf, state)
            self.recorded = state[STEP]
        except OpenBaoError as e2:
            error += f" The failure is not recorded on the leaf: {e2}"
        self.renderer.event(Finished(step, action, False, error=error, technical=technical))
        return Outcome.FAILED if action is Action.RUN else Outcome.ROLLBACK_FAILED

    def _advance(self) -> Outcome:
        for at in range(self.at, len(self.plan.steps)):
            self.at = at
            step = self.plan.steps[at]
            self.renderer.event(Started(step))
            try:
                self._record(step)
                detail = step.run(_RunContext(self, step, Action.RUN))
            except _Abandoned as a:
                return Outcome.EXITED if a.choice is Abandon.EXIT else self._abort()
            except Exception as e:
                return self._fail(step, Action.RUN, e)
            self.renderer.event(Finished(step, Action.RUN, True, detail or ""))
        self.recorded = None  # kv.stamp, the last step, cleared it
        self.staging.destroy()
        return Outcome.DONE

    def _abort(self) -> Outcome:
        if reason := self.abort_blocker():
            raise AbortRefused(reason)
        if not any(step.mutates for step in self._touched()):
            self._clear()
            self.staging.destroy()
            return Outcome.CANCELLED
        self.staging.put(ROLLBACK, "0")
        return self._roll_back()

    def _roll_back(self) -> Outcome:
        items = self.rollback()
        for done in range(int(self.staging.get(ROLLBACK)), len(items)):
            step, action = items[done]
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
        # rotator_step goes first: a staging leaf left behind is destroyed by the next start.
        self._clear()
        self.staging.destroy()
        return Outcome.ROLLED_BACK
