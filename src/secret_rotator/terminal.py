"""The terminal front end of design §4.4: `plan <path>` prints a leaf's plans and executes nothing;
`run <path>` runs one with the executor, its operator steps as prompts. A value the operator types
or is shown is never echoed or printed: only Reveal puts a shown one on the screen, until Enter."""

import datetime
import time
from collections.abc import Callable, Mapping

from secret_rotator.audit import Audit, Leaf, audit, copies_in, live_store
from secret_rotator.cluster import Cluster
from secret_rotator.console import Console
from secret_rotator.executor import (
    Abandon,
    AbortRefused,
    Executor,
    Outcome,
    PlanMismatch,
    Stand,
)
from secret_rotator.kube import KubeError
from secret_rotator.lock import Holder, Lock, LockError, LockHeld
from secret_rotator.model import Action, Actor, Event, Finished, Progress, Skipped, Started, Step
from secret_rotator.openbao import OpenBao, OpenBaoError
from secret_rotator.opsteps import ConfirmRequest, CredentialRequest, Field, ShowRequest
from secret_rotator.plan import Kind, LeafPlan, Plan, PlanError, make, of_leaf
from secret_rotator.state import LeafState, State
from secret_rotator.telegram import failed

Choice = tuple[str, str]  # the letter that answers it, and the word it is in
ABORT: Choice = ("a", "abort")
EXIT: Choice = ("x", "exit")


def due_text(due_at: datetime.date | None, today: datetime.date) -> str:
    if due_at is None:
        return "never due: rotated by hand only"
    if due_at == datetime.date.min:
        return "due: never rotated"
    return f"due since {due_at}" if due_at <= today else f"due {due_at}"


def heading(p: LeafPlan, today: datetime.date) -> str:
    text = f"{p.kind} plan of {', '.join(p.keys)} · {due_text(p.due_at, today)}"
    return text + (f" · {p.plan.ask}" if p.plan and p.plan.ask else "")


def plan_lines(plan: Plan) -> list[str]:
    """The plan's steps, numbered, each with who does it and its target."""
    return [
        f"{n:>3}  {'you' if step.actor is Actor.OPERATOR else 'tool':<4}  {step.type:<28}  "
        f"{step.title}{'  (silent)' if step.silent else ''}"
        for n, step in enumerate(plan.steps, 1)
    ]


def print_leaf(
    out: Callable[[str], None],
    leaf: str,
    store: Mapping[str, Leaf],
    result: Audit,
    kinds: Mapping[str, Kind],
    today: datetime.date,
    cluster: Cluster | None = None,
) -> int:
    """`plan <path>`: every plan of the leaf with its steps, and why each other key has none. 1 when
    the leaf does not exist or a plan of it cannot be built."""
    if leaf not in store:
        out(f"error: no leaf {leaf}")
        return 1
    plans, unplanned = of_leaf(leaf, store, result, kinds, cluster)
    out(leaf)
    if leaf not in result.kinds:
        out("  its keys cannot be read: its current version is deleted or destroyed")
    if flight := store[leaf].flight:
        out(f"  in flight: its {flight.kind} plan of {', '.join(flight.keys)}, at {flight.step}")
    for p in plans:
        out(f"  {heading(p, today)}")
        if p.plan is None:
            out(f"      cannot be built: {p.error}")
            continue
        out(f"      {p.plan.description}")
        for line in plan_lines(p.plan):
            out(f"    {line}")
    for key, why in unplanned.items():
        out(f"  {key}: {why}")
    return 1 if any(p.plan is None for p in plans) else 0


def took(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.1f}s"
    return f"{int(seconds // 60)}m {int(seconds % 60):02d}s"


def label(step: Step, action: Action) -> str:
    return {Action.RUN: "", Action.UNDO: "undo: ", Action.RERUN: "again: "}[action] + step.title


def menu(console: Console, choices: list[Choice]) -> str:
    """The letter of one of the choices, asked again until the answer is one."""
    words = [word.replace(letter, f"[{letter}]", 1) for letter, word in choices]
    question = (", ".join(words[:-1]) + f" or {words[-1]}" if len(words) > 1 else words[0]) + "? "
    while (answer := console.ask(question).lower()) not in {letter for letter, _ in choices}:
        pass
    return answer


def yes(console: Console, question: str) -> bool:
    return console.ask(f"{question} [y/N] ").lower() in ("y", "yes")


def guard(console: Console, executor: Executor) -> bool:
    """Abort's one question (design §4.5); its count is the steps the rollback runs."""
    n = len(executor.rollback())
    return yes(console, f"Abort and roll back {n} step{'' if n == 1 else 's'}?" if n else "Abort?")


def endings(console: Console, executor: Executor) -> list[Choice]:
    """Abort, while the plan can be aborted, and exit; says why Abort is not possible."""
    blocker = executor.abort_blocker()
    if blocker:
        console.line(f"Abort is not possible: {blocker}")
    return [EXIT] if blocker else [ABORT, EXIT]


def check(field: Field, value: str) -> str:
    """The line under an entry: its size and how it meets its shape; never the value."""
    if not value:
        return "empty"
    lines = value.count("\n") + 1
    text = f"{len(value)} characters" + (f", {lines} lines" if lines > 1 else "")
    if field.shape is None:
        return text
    return text + (
        f"  ✓ {field.shape.words}"
        if field.shape.test(value)
        else f"  ⚠ expected: {field.shape.words}"
    )


class TerminalRenderer:
    """The executor's events as lines, live while a step runs; its operator steps as prompts.
    Silent steps show only when they fail, the rollback under its own heading."""

    def __init__(self, console: Console, clock: Callable[[], float] = time.monotonic):
        self.console = console
        self.clock = clock
        self.executor: Executor | None = None  # set by its driver: Abort's count and blocker
        self.began: dict[tuple[str, Action], float] = {}
        self.rolling_back = False
        self.failure: Finished | None = None  # the last failed line, for Details

    def event(self, event: Event) -> None:
        c, step = self.console, event.step
        if isinstance(event, Started):
            self.began[step.id, event.action] = self.clock()
            if event.action is not Action.RUN and not self.rolling_back:
                self.rolling_back = True
                c.line("Rolling back")
            if step.actor is Actor.OPERATOR:
                c.line()
                c.line(f"── {label(step, event.action)}")
            elif not step.silent:
                c.live(f"◐ {label(step, event.action)}")
        elif isinstance(event, Progress):
            if not step.silent:
                c.live(f"◐ {label(step, event.action)} · {event.detail}")
        elif isinstance(event, Finished):
            began = self.began.pop((step.id, event.action), None)
            spent = "" if began is None else f"  {took(self.clock() - began)}"
            if not event.ok:
                self.failure = event
                c.line(f"✗ {label(step, event.action)}{spent}")
                c.line(f"    {event.error}")
            elif step.actor is Actor.OPERATOR:
                c.line(f"✓ {label(step, event.action)}")
            elif not step.silent:
                detail = f" · {event.detail}" if event.detail else ""
                c.line(f"✓ {label(step, event.action)}{detail}{spent}")
        elif isinstance(event, Skipped):
            c.line(f"- {step.title}: {event.reason}")

    def ask(self, step: Step, request: object) -> dict[str, str] | Abandon:
        try:
            if isinstance(request, CredentialRequest):
                return self._credential(request)
            if isinstance(request, ShowRequest):
                return self._show(request)
            if isinstance(request, ConfirmRequest):
                return self._confirm(request)
        except (EOFError, KeyboardInterrupt):
            self.console.line()
            return Abandon.EXIT
        raise TypeError(f"{step!r}: the terminal has no prompt for {type(request).__name__}")

    def _end(self, letter: str) -> Abandon | None:
        """Exit, or Abort once its guard is answered yes; None when it is not."""
        if letter == EXIT[0]:
            return Abandon.EXIT
        return Abandon.ABORT if guard(self.console, self.executor) else None

    def _endings(self) -> list[Choice]:
        """A rollback is an Abort under way: its prompts offer exit only."""
        return [EXIT] if self.rolling_back else endings(self.console, self.executor)

    def _instruct(self, text: str) -> None:
        for line in text.splitlines():
            self.console.line(line)

    def _enter(self, fields: tuple[Field, ...]) -> dict[str, str]:
        values = {}
        for field in fields:
            values[field.key] = self.console.hidden(f"{field.key} (hidden): ")
            self.console.line(f"  {check(field, values[field.key])}")
        return values

    def _credential(self, request: CredentialRequest) -> dict[str, str] | Abandon:
        c = self.console
        self._instruct(request.instruction)
        ending = self._endings()
        values = self._enter(request.fields)
        while True:
            letter = menu(c, [("c", "continue"), ("e", "enter again"), *ending])
            if letter == "e":
                values = self._enter(request.fields)
            elif letter == "c":
                if empty := [f.key for f in request.fields if not values[f.key]]:
                    c.line(f"Every field needs a value: {', '.join(empty)} is empty.")
                    continue
                odd = [f for f in request.fields if f.shape and not f.shape.test(values[f.key])]
                if all(self._anyway(f) for f in odd):
                    return values
            elif (end := self._end(letter)) is not None:
                return end

    def _anyway(self, field: Field) -> bool:
        self.console.line(f"The {field.key} does not look as expected: it {field.shape.words}.")
        return yes(self.console, "Continue anyway?")

    def _show(self, request: ShowRequest) -> dict[str, str] | Abandon:
        self._instruct(request.instruction)
        ending = self._endings()
        while True:
            letter = menu(self.console, [("r", "reveal"), ("d", "done"), *ending])
            if letter == "r":
                self.console.reveal(request.value)
            elif letter == "d":
                return {}
            elif (end := self._end(letter)) is not None:
                return end

    def _confirm(self, request: ConfirmRequest) -> dict[str, str] | Abandon:
        self._instruct(request.instruction)
        ending = self._endings()
        while True:
            letter = menu(self.console, [("d", "done"), *ending])
            if letter == "d":
                return {}
            if (end := self._end(letter)) is not None:
                return end


def choose(
    console: Console,
    leaf: str,
    plans: list[LeafPlan],
    unplanned: Mapping[str, str],
    today: datetime.date,
) -> Plan | None:
    """The plan the operator starts, after its steps are shown; None when they start none."""
    ready = [p for p in plans if p.plan is not None]
    console.line(leaf)
    for p in plans:
        number = f"{ready.index(p) + 1:>3}" if p.plan else "  -"
        console.line(f"{number}  {heading(p, today)}")
        if p.plan is None:
            console.line(f"       cannot be built: {p.error}")
    for key, why in unplanned.items():
        console.line(f"     {key}: {why}")
    if not ready:
        console.line("Nothing to run.")
        return None
    chosen = ready[0]
    if len(ready) > 1:
        answer = console.ask(f"Which plan? [1-{len(ready)}, ⏎ for none] ")
        if not answer.isdigit() or not 1 <= int(answer) <= len(ready):
            return None
        chosen = ready[int(answer) - 1]
    console.line(chosen.plan.description)
    for line in plan_lines(chosen.plan):
        console.line(line)
    return chosen.plan if yes(console, "Start it?") else None


class Driver:
    """Runs one plan to an end: Retry, Abort and Details after a failure, the rollback's Retry,
    and the offer to break a dead holder's lock. notify: where each failure, of the plan or of
    its rollback, is told in Telegram too (design R66)."""

    def __init__(
        self,
        console: Console,
        executor: Executor,
        renderer: TerminalRenderer,
        notify: Callable[[str], None] | None = None,
    ):
        self.console = console
        self.executor = executor
        self.renderer = renderer
        self.leaf = executor.leaf
        self.notify = notify

    def go(self, stand: Stand, state: LeafState) -> int:
        e, c = self.executor, self.console
        at = e.plan.steps[e.at]
        if stand is Stand.FRESH:
            if all(step.silent for step in e.plan.steps):
                c.line("Working…")
            outcome = self.attempt(e.run)
        elif stand is Stand.IN_FLIGHT:
            c.line(f"It stopped at step {e.at + 1} of {len(e.plan.steps)}: {at.title}.")
            outcome = self.stopped()
        else:
            what = "Its rollback stopped at" if stand is Stand.ROLLING_BACK else "It failed at"
            c.line(f"{what} step {e.at + 1} of {len(e.plan.steps)}: {at.title}.")
            c.line(f"    {state.last_error or ''}")
            outcome = Outcome.FAILED if stand is Stand.FAILED else Outcome.ROLLBACK_FAILED
        return self.settle(outcome)

    def settle(self, outcome: Outcome | None) -> int:
        c, keys = self.console, ", ".join(self.executor.plan.target.keys)
        while outcome in (Outcome.FAILED, Outcome.ROLLBACK_FAILED):
            outcome = self.failed(rollback=outcome is Outcome.ROLLBACK_FAILED)
        if outcome is Outcome.DONE:
            c.line(f"Done: {keys} of {self.leaf} rotated.")
        elif outcome is Outcome.CANCELLED:
            c.line("Cancelled: nothing was changed.")
        elif outcome is Outcome.ROLLED_BACK:
            c.line(f"Rolled back: the rotation of {keys} is undone.")
        elif outcome is Outcome.EXITED:
            c.line(f"Left in flight: `secret-rotator run {self.leaf}` takes it up there.")
        return 1 if outcome is None else 0

    def stopped(self) -> Outcome | None:
        """A plan left in flight: resume, abort or exit."""
        ending = endings(self.console, self.executor)
        while True:
            letter = menu(self.console, [("r", "resume"), *ending])
            if letter == "r":
                return self.attempt(self.executor.run)
            if letter == EXIT[0]:
                return Outcome.EXITED
            if guard(self.console, self.executor):
                return self.attempt(self.executor.abort)

    def failed(self, *, rollback: bool) -> Outcome | None:
        """Retry, Details, and Abort where it is possible: never on a rollback, which is one."""
        e, c = self.executor, self.console
        blocker = None if rollback else e.abort_blocker()
        if blocker:
            c.line(f"Abort is not possible: {blocker}")
        aborts = [] if rollback or blocker else [ABORT]
        while True:
            letter = menu(c, [("r", "retry"), *aborts, ("d", "details"), EXIT])
            if letter == "r":
                return self.attempt(e.run)
            if letter == "d":
                self.details()
            elif letter == EXIT[0]:
                what = "Its rollback stays stopped part-way" if rollback else "It stays stopped"
                c.line(
                    f"{what} at: {e.plan.steps[e.at].title}. "
                    f"`secret-rotator run {self.leaf}` takes it up there."
                )
                return None
            elif guard(c, e):
                return self.attempt(e.abort)

    def details(self) -> None:
        failure = self.renderer.failure
        if failure is not None:
            self.console.line(failure.technical.rstrip())
            return
        state = self.executor.state.of(self.leaf)
        self.console.line(f"{state.last_run or '?'}: {state.last_error or 'no error recorded'}")

    def attempt(self, action: Callable[[], Outcome]) -> Outcome | None:
        """The executor's run or abort, offering to break a dead holder's lock; None when it did
        not get to an outcome."""
        e, c = self.executor, self.console
        while True:
            try:
                outcome = action()
            except LockHeld as held:
                if not self.break_lock(held.holder):
                    return None
                continue
            except KeyboardInterrupt:
                c.line()
                c.line(
                    f"Interrupted at: {e.plan.steps[e.at].title}. It is left in flight there: "
                    f"`secret-rotator run {self.leaf}` takes it up, running that step again."
                )
                return None
            except (AbortRefused, PlanMismatch, OpenBaoError, LockError) as err:
                c.line(f"error: {err}")
                return None
            if self.notify is not None and outcome in (Outcome.FAILED, Outcome.ROLLBACK_FAILED):
                failure, plan = self.renderer.failure, e.plan
                self.notify(
                    f"In `secret-rotator run {self.leaf}`: "
                    + failed(
                        plan.name,
                        plan.target.keys,
                        label(failure.step, failure.action),
                        failure.error,
                        rollback=outcome is Outcome.ROLLBACK_FAILED,
                    )
                )
            return outcome

    def break_lock(self, holder: Holder) -> bool:
        c = self.console
        c.line(f"Another plan runs: {holder}.")
        if not yes(c, f"Is {holder.who} gone? Break its lock?"):
            return False
        try:
            self.executor.lock.break_held(holder)
        except LockHeld as held:
            c.line(f"Not broken: it was taken since. {held.holder}.")
            return False
        except OpenBaoError as err:
            c.line(f"error: {err}")
            return False
        c.line("The lock is broken.")
        return True


def run_leaf(
    bao: OpenBao,
    leaf: str,
    kinds: Mapping[str, Kind],
    console: Console,
    *,
    holder: str,
    today: datetime.date,
    cluster: Cluster | None = None,
    clock: Callable[[], float] = time.monotonic,
    notify: Callable[[str], None] | None = None,
) -> int:
    """`run <path>`: the leaf's plan in flight, else the plan the operator picks, run to an end. 1
    when it did not end with the plan done, rolled back, cancelled or left by the operator.
    notify: where each failure is told in Telegram too."""
    store = live_store(bao, runs=True)
    if leaf not in store:
        console.line(f"error: no leaf {leaf}")
        return 1
    flight = store[leaf].flight
    referenced = None if cluster is None else cluster.referenced()
    if flight is not None and referenced is not None:
        # A plan in flight is not blocked by the orphan finding of its leaf, or of a leaf holding a
        # copy of its keys (design §3.3).
        referenced |= {leaf} | {
            path
            for path, other in store.items()
            if any((leaf, key) in copies_in(other.meta) for key in flight.keys)
        }
    result = audit(store, referenced)
    plans, unplanned = of_leaf(leaf, store, result, kinds, cluster)
    try:
        if flight is None:
            plan = choose(console, leaf, plans, unplanned, today)
            if plan is None:
                return 0 if any(p.plan for p in plans) else 1
        else:
            try:
                plan = make(
                    kinds,
                    leaf,
                    flight.kind,
                    list(flight.keys),
                    store,
                    result,
                    cluster,
                    derived=flight.derived,
                )
            except PlanError as e:
                console.line(f"error: its plan in flight cannot be built again: {e}")
                return 1
            console.line(f"{leaf}: its {plan.target.kind} plan of {', '.join(flight.keys)}.")
            if any((p.kind, p.keys) != (flight.kind, flight.keys) for p in plans):
                console.line("Its other plans wait until this one is done or rolled back.")
        renderer = TerminalRenderer(console, clock)
        state = State(bao, store)
        executor = Executor(bao, plan, renderer, Lock(bao, holder), state=state, dry_run=False)
        renderer.executor = executor
        stand = Stand.FRESH if flight is None else executor.load()
        return Driver(console, executor, renderer, notify).go(stand, store[leaf].state)
    except (PlanMismatch, OpenBaoError, KubeError) as e:
        console.line(f"error: {e}")
        return 1
    except (EOFError, KeyboardInterrupt):
        console.line()
        return 1
