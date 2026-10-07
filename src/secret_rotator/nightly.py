"""The nightly run, `secret-rotator run` without a path: design §8's run loop. The lock first: held,
the run says so in Telegram and ends there. Then compliance, the due set oldest first, admission —
a plan with an operator step is marked manual-due and never started (design §4.4) — the health of
each plan's rollout targets, and the plans under the executor, a failed one rolled back by the run
itself (ruling D2). Last the standing card and the digest. One plan's failure never ends the run:
it exits non-zero only when the run itself broke."""

import datetime
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass

from secret_rotator import card as cards
from secret_rotator.audit import Audit, Leaf, audit, due_keys, live_store, report
from secret_rotator.cluster import Cluster
from secret_rotator.executor import Executor, Outcome
from secret_rotator.k8ssteps import K8sRollout
from secret_rotator.lock import Holder, Lock, LockHeld, utcnow
from secret_rotator.model import Event, Finished, Started, Step
from secret_rotator.openbao import OpenBao
from secret_rotator.plan import Kind, Plan, PlanError, make, split
from secret_rotator.schedule import KeySchedule
from secret_rotator.staging import flights
from secret_rotator.state import LeafState, State
from secret_rotator.switches import Switches
from secret_rotator.telegram import TOKEN as BOT_TOKEN
from secret_rotator.telegram import Telegram, TelegramError, failed
from secret_rotator.terminal import due_text, label, plan_lines, took
from secret_rotator.youtrack import TOKEN as CARD_TOKEN
from secret_rotator.youtrack import Card, YouTrack, YouTrackError

HOLD_AFTER = 3  # failed nights in a row, after which a leaf waits for the card naming it to close
WARN_AT = (28, 21, 14)  # days before a manual rotation falls due that it gets a Telegram line
WARN_DAILY = 13  # from this many days before it, a line every night until it is rotated
HORIZON = datetime.timedelta(days=max(WARN_AT))
FAILED = ("failed", "failed-activation")
MANUAL_DUE = "manual-due"


@dataclass(frozen=True)
class Due:
    """The keys of one plan, one kind's keys of one leaf, each with the day it falls due."""

    leaf: str
    kind: str
    keys: tuple[str, ...]
    dates: tuple[datetime.date, ...]

    @property
    def due_at(self) -> datetime.date:
        return min(self.dates)

    def __str__(self) -> str:
        return f"{self.kind} plan of {self.leaf} ({', '.join(self.keys)})"


def plans_of(schedules: Iterable[KeySchedule], kinds: Mapping[str, Kind]) -> list[Due]:
    """The keys grouped as a leaf's plans group them, one per key for a per-key kind; oldest
    first."""
    groups: dict[tuple[str, str], dict[str, datetime.date]] = {}
    for s in schedules:
        groups.setdefault((s.leaf, s.kind), {})[s.key] = s.due_at
    plans = [
        Due(leaf, kind, keys, tuple(due[key] for key in keys))
        for (leaf, kind), due in groups.items()
        for keys in split(kinds[kind], due)
    ]
    return sorted(plans, key=lambda d: (d.due_at, d.leaf, d.kind, d.keys))


def warns(due_at: datetime.date, today: datetime.date) -> bool:
    """Whether a manual rotation gets its Telegram line tonight: 28, 21 and 14 days before it falls
    due, then every night from 13 days before it until it is rotated."""
    days = (due_at - today).days
    return days <= WARN_DAILY or days in WARN_AT


def when(due_at: datetime.date, today: datetime.date) -> str:
    days = (due_at - today).days
    if days <= 0:
        return "is due"
    return f"is due in {days} day{'' if days == 1 else 's'}, on {due_at}"


def manual_line(leaf: str, keys: Iterable[tuple[str, datetime.date]], today: datetime.date) -> str:
    """The one line of a leaf (design R30): `Manual rotation of <keys> at `<leaf>` is due`, its
    keys grouped by when they fall due."""
    groups: dict[str, list[str]] = {}
    for key, due_at in sorted(set(keys), key=lambda k: (k[1], k[0])):
        groups.setdefault(when(due_at, today), []).append(key)
    (first, first_keys), *rest = groups.items()
    text = f"Manual rotation of {', '.join(first_keys)} at `{leaf}` {first}"
    return text + "".join(f"; of {', '.join(k)} {w.removeprefix('is ')}" for w, k in rest)


def sentence(text: str) -> str:
    return text if text.endswith((".", "!", "?")) else f"{text}."


def problem(e: Exception) -> str:
    """An error as one sentence: the rotator's own errors say what they are."""
    text = str(e)
    return text if type(e).__module__.startswith("secret_rotator") else f"{type(e).__name__}: {e}"


class LogRenderer:
    """The executor's events as lines of the job's log, a failure with its technical detail. The
    nightly run starts no plan with an operator step, so nothing asks."""

    def __init__(self, out: Callable[[str], None], clock: Callable[[], float]):
        self.out = out
        self.clock = clock
        self.began: dict[tuple[str, str], float] = {}
        self.failure: Finished | None = None  # the last failed line

    def event(self, event: Event) -> None:
        step = event.step
        if isinstance(event, Started):
            self.began[step.id, event.action] = self.clock()
            if not step.silent:
                self.out(f"    ◐ {label(step, event.action)}")
        elif isinstance(event, Finished):
            began = self.began.pop((step.id, event.action), None)
            spent = "" if began is None else f"  {took(self.clock() - began)}"
            if not event.ok:
                self.failure = event
                self.out(f"    ✗ {label(step, event.action)}{spent}")
                self.out(f"        {event.error}")
                for line in event.technical.rstrip().splitlines():
                    self.out(f"        {line}")
            elif not step.silent:
                detail = f" · {event.detail}" if event.detail else ""
                self.out(f"    ✓ {label(step, event.action)}{detail}{spent}")

    def ask(self, step: Step, request: object) -> dict[str, str]:
        raise RuntimeError(f"{step!r}: the nightly run starts no plan with an operator step")


class Night:
    def __init__(
        self,
        bao: OpenBao,
        cluster: Cluster,
        kinds: Mapping[str, Kind],
        switches: Switches,
        *,
        youtrack: YouTrack,
        telegram: Telegram | None,
        out: Callable[[str], None],
        holder: str,
        now: Callable[[], datetime.datetime] = utcnow,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.bao = bao
        self.cluster = cluster
        self.kinds = kinds
        self.switches = switches
        self.dry_run = switches.dry_run
        self.youtrack = youtrack
        self.telegram = telegram
        self.out = out
        self.now = now
        self.today = now().date()
        self.clock = clock
        self.lock = Lock(bao, holder, clock=now)
        self.broken = False  # the run itself broke: it exits non-zero
        self.store: dict[str, Leaf] = {}
        self.state = State(bao, self.store)  # a write keeps the state of the leaves of the store
        self.result = Audit([], {})
        self.card: Card | None = None
        self.card_known = False  # whether the open card was found, or found to be none
        self.locked: Holder | None = None
        self.executed = 0  # plans started, under the cap
        self.deferred = 0
        self.rotated: list[str] = []
        self.failures: list[str] = []
        self.problems: list[str] = []  # card items of plans that cannot be built or broke off
        self.skipped: list[str] = []  # card items of plans whose targets are not healthy
        self.manual: dict[str, list[tuple[str, datetime.date]]] = {}  # due, per leaf
        self.upcoming: dict[str, list[tuple[str, datetime.date]]] = {}  # warned, not due yet
        self.failed_tonight: set[str] = set()

    def go(self) -> int:
        enabled_kinds = self.switches.kinds_enabled
        self.out(
            f"secret-rotator run, {self.today}{cards.DRY_RUN if self.dry_run else ''}: kinds "
            f"{', '.join(sorted(enabled_kinds))}, at most "
            f"{self.switches.max_rotations_per_run} rotation(s)"
        )
        if (holder := self.lock.holder()) is not None:
            self.locked_out(holder, "it ran nothing tonight")
            return 1 if self.broken else 0
        self.store.update(live_store(self.bao, runs=True))
        self.result = audit(self.store, self.cluster.referenced())
        report(self.result, self.store, self.out)
        self.find_card()
        horizon = due_keys(self.store, self.result, self.today + HORIZON)
        enabled = [s for s in horizon if s.kind in self.kinds and s.kind in enabled_kinds]
        others = sum(1 for s in horizon if s.due(self.today) and s.kind not in enabled_kinds)
        due = plans_of([s for s in enabled if s.due(self.today)], self.kinds)
        self.out(f"due: {len(due)} plan(s); {others} due key(s) of kinds not enabled")
        for plan in due:
            self.take(plan)
        for plan in plans_of([s for s in enabled if not s.due(self.today)], self.kinds):
            self.forewarn(plan)
        if self.deferred:
            self.out(f"{self.deferred} plan(s) past the cap: the next nights take them")
        self.post_card()
        self.digest()
        return 1 if self.broken else 0

    def send(self, text: str) -> None:
        if self.dry_run:
            text = f"Dry run. {text}"
        if self.telegram is None:
            self.out("telegram: no telegram_chat_id is committed, so this is not sent:")
            for line in text.splitlines():
                self.out(f"    {line}")
            return
        try:
            self.telegram.send(text)
        except TelegramError as e:
            self.out(f"error: a Telegram message was not sent: {e}")
            self.broken = True

    def locked_out(self, holder: Holder, what: str) -> None:
        """One message the first time the lock is found held (ruling 2026-10-05): the run breaks
        nothing and waits for nothing."""
        if self.locked is not None:
            return
        self.locked = holder
        self.out(f"the lock is held: {holder}")
        self.send(
            f"The nightly run found kv/rotator/lock held by {holder.who} since {holder.since}, "
            f"for the {holder.plan}, and {what}. If that holder is gone, `secret-rotator run "
            f"<path>` offers to break its lock."
        )

    def find_card(self) -> None:
        try:
            self.card = self.youtrack.open_card(self.switches.card_tag)
            self.card_known = True
        except YouTrackError as e:
            self.out(f"error: the open card cannot be looked up: {e}")
            self.broken = True

    def take(self, due: Due) -> None:
        self.out(f"── {due} · {due_text(due.due_at, self.today)}")
        try:
            self.admit(due)
        except LockHeld as e:
            self.locked_out(e.holder, "started no plan after it")
        except Exception as e:  # one plan's failure never ends the run (ruling D2)
            why = problem(e)
            self.out(f"    error: {why}")
            self.problems.append(f"`{due.leaf}`: its {due.kind} plan of {keys(due)}: {why}")
            self.failures.append(f"{due}: {why}")
            self.send(sentence(f"The {due} broke off: {why}"))

    def admit(self, due: Due) -> None:
        """Starts the plan, or says why not; a failed plan is rolled back."""
        if (flight := self.store[due.leaf].flight) is not None:
            self.out(f"    not started: the leaf has its {flight.kind} plan in flight, on the card")
            return
        try:
            plan = make(
                self.kinds,
                due.leaf,
                due.kind,
                list(due.keys),
                self.result,
                self.cluster,
            )
        except PlanError as e:
            why = str(e).removeprefix(f"{due.leaf}: ")
            self.out(f"    cannot be built: {why}")
            self.problems.append(f"`{due.leaf}`: its {due.kind} plan of {keys(due)}: {why}")
            return
        if plan.needs_operator:
            self.manual_due(due)
            return
        if self.held(due.leaf):
            self.out(f"    not retried: it failed {HOLD_AFTER} nights in a row; the card is open")
            return
        if self.locked is not None:
            self.out("    not started: the lock is held")
            return
        # A dry run starts no plan: it plans every due one (A9).
        if not self.dry_run and self.executed >= self.switches.max_rotations_per_run:
            self.deferred += 1
            self.out(f"    not started: past the cap of {self.switches.max_rotations_per_run}")
            return
        if why := self.unhealthy(plan):
            self.out(f"    skipped: {why}")
            self.skipped.append(f"`{due.leaf}`: its {due.kind} plan of {keys(due)}: {why}")
            return
        if self.dry_run:
            self.out(f"    {plan.description}")
            for line in plan_lines(plan):
                self.out(f"    {line}")
            self.rotated.append(str(due))
            return
        self.executed += 1
        self.execute(plan, due)

    def manual_due(self, due: Due) -> None:
        self.out("    not started: it has an operator step, so the leaf is manual-due")
        self.manual.setdefault(due.leaf, []).extend(zip(due.keys, due.dates, strict=True))
        # A failure of another plan of the leaf keeps its status: the card names it until then.
        if self.dry_run or self.store[due.leaf].state.status in (MANUAL_DUE, *FAILED):
            return

        def mark(state: LeafState) -> None:
            if state.status not in (MANUAL_DUE, *FAILED):
                state.status = MANUAL_DUE

        self.store[due.leaf].state = self.state.update(due.leaf, mark)

    def held(self, leaf: str) -> bool:
        """Whether the leaf waits for the card naming it to close (ruling 2026-10-05). Once that
        card is closed, the leaf is due again with its count from zero."""
        state = self.store[leaf].state
        if state.held_by is None or state.failed_nights < HOLD_AFTER:
            return False
        if not self.card_known or (self.card is not None and self.card.readable == state.held_by):
            return True
        if not self.dry_run:
            self.store[leaf].state = self.state.update(leaf, release)
        return False

    def unhealthy(self, plan: Plan) -> str | None:
        """Why a rollout target of the plan is not Ready with its Argo Application Healthy; None
        when every one is."""
        whys = [
            f"{step.workload}: {why}"
            for step in plan.steps
            if isinstance(step, K8sRollout) and (why := self.cluster.health(step.workload))
        ]
        return "; ".join(whys) or None

    def execute(self, plan: Plan, due: Due) -> None:
        leaf = plan.target.leaf
        renderer = LogRenderer(self.out, self.clock)
        executor = Executor(
            self.bao, plan, renderer, self.lock, state=self.state, dry_run=False, clock=self.now
        )
        if executor.run() is Outcome.DONE:
            self.out("    rotated")
            self.rotated.append(str(due))
            self.refresh(leaf)
            return
        failure = renderer.failure
        message = failed(plan.name, plan.target.keys, failure.step.title, failure.error)
        if blocker := executor.abort_blocker():
            self.send(
                f"{sentence(message)} It is not rolled back: {sentence(blocker)} It stays "
                f"stopped: `secret-rotator run {leaf}` takes it up."
            )
            self.failures.append(f"{due}: stopped for `secret-rotator run {leaf}`")
        else:
            self.send(f"{sentence(message)} The run rolls it back; the leaf is due again.")
            if executor.abort() is Outcome.ROLLBACK_FAILED:
                undo = renderer.failure
                text = failed(
                    plan.name,
                    plan.target.keys,
                    label(undo.step, undo.action),
                    undo.error,
                    rollback=True,
                )
                self.send(
                    f"{sentence(text)} It stays stopped part-way: `secret-rotator run {leaf}` "
                    f"continues it."
                )
                self.failures.append(f"{due}: its rollback stopped part-way")
            else:
                self.failures.append(f"{due}: rolled back")
        self.count_failure(leaf)
        self.refresh(leaf)

    def count_failure(self, leaf: str) -> None:
        if leaf in self.failed_tonight:
            return
        self.failed_tonight.add(leaf)

        def count(state: LeafState) -> None:
            state.failed_nights += 1

        self.state.update(leaf, count)

    def refresh(self, leaf: str) -> None:
        """The leaf as the plan left it, for the leaf's later plans and the card."""
        held = self.store[leaf]
        held.meta = self.bao.metadata(leaf) or {}
        held.state = self.state.of(leaf)
        held.flight = flights(self.bao).get(leaf)

    def forewarn(self, due: Due) -> None:
        """The advance warning of a manual rotation not due yet (ruling 2026-10-05)."""
        warned = [(k, d) for k, d in zip(due.keys, due.dates, strict=True) if warns(d, self.today)]
        if not warned or self.store[due.leaf].flight is not None:
            return
        try:
            plan = make(
                self.kinds,
                due.leaf,
                due.kind,
                list(due.keys),
                self.result,
                self.cluster,
            )
        except PlanError:
            return  # it is on the card once it falls due
        except Exception as e:
            why = problem(e)
            self.out(f"── {due}: its warning cannot be worked out: {why}")
            self.problems.append(f"`{due.leaf}`: its {due.kind} plan of {keys(due)}: {why}")
            return
        if plan.needs_operator:
            self.upcoming.setdefault(due.leaf, []).extend(warned)

    def state_items(self) -> list[str]:
        """What the leaves' state keeps open: plans stopped or in flight, failed rotations."""
        items = []
        for path, leaf in sorted(self.store.items()):
            status, error = leaf.state.status, leaf.state.last_error or ""
            if (flight := leaf.flight) is not None:
                what = f"{status} at {flight.step}" if status in FAILED else "in flight"
                items.append(
                    f"`{path}`: its {flight.kind} plan of {', '.join(flight.keys)} is {what}, "
                    f"stopped: `secret-rotator run {path}` takes it up"
                    + (f". {error}" if status in FAILED else "")
                )
            elif status in FAILED:
                held = leaf.state.failed_nights >= HOLD_AFTER
                then = "not retried while this card is open" if held else "due again"
                items.append(f"`{path}`: {status}, rolled back, {then}: {error}")
        return items

    def post_card(self) -> None:
        sections = [
            cards.Section(
                "Findings", tuple(f"`{f.leaf}`: {f.key}: {f.message}" for f in self.result.findings)
            ),
            cards.Section("Rotations", (*self.state_items(), *self.problems, *self.skipped)),
            cards.Section(
                "Manual rotations due",
                tuple(manual_line(leaf, k, self.today) for leaf, k in sorted(self.manual.items())),
            ),
        ]
        if not self.card_known:
            self.out("card: not posted, since the open card could not be looked up")
            return
        try:
            posted, what = cards.post(
                self.youtrack,
                self.switches.card_tag,
                self.card,
                sections,
                today=self.today,
                dry_run=self.dry_run,
            )
        except YouTrackError as e:
            self.out(f"error: the standing card is not posted: {e}")
            self.broken = True
            return
        self.out(f"card: {what}")
        if posted is not None and not self.dry_run:
            self.hold(posted)

    def hold(self, card: Card) -> None:
        """Every leaf failed HOLD_AFTER nights in a row now waits for this card to close."""

        def wait(state: LeafState) -> None:
            state.held_by = card.readable

        for path, leaf in sorted(self.store.items()):
            state = leaf.state
            if (
                state.status in FAILED
                and state.failed_nights >= HOLD_AFTER
                and state.held_by != card.readable
            ):
                leaf.state = self.state.update(path, wait)

    def digest(self) -> None:
        """The night's one message, when something happened (design §3.6)."""
        lines = []
        if self.rotated:
            done = "Would rotate" if self.dry_run else "Rotated"
            lines += [f"{done} {len(self.rotated)}:", *(f"• {r}" for r in self.rotated)]
        if self.failures:
            lines += [f"Failed {len(self.failures)}:", *(f"• {f}" for f in self.failures)]
        if self.deferred:
            cap = self.switches.max_rotations_per_run
            lines.append(
                f"{self.deferred} more due past the cap of {cap}: the next nights take them."
            )
        warned: dict[str, list[tuple[str, datetime.date]]] = {}
        for leaf, k in [*self.manual.items(), *self.upcoming.items()]:
            warned.setdefault(leaf, []).extend(k)
        lines += [manual_line(leaf, k, self.today) for leaf, k in sorted(warned.items())]
        if not lines:
            self.out("telegram: a quiet night, nothing to send")
            return
        self.send("\n".join([f"Secret rotation, {self.today}", *lines]))


def release(state: LeafState) -> None:
    """A held leaf's card is closed: it is due again with its count from zero."""
    state.failed_nights = 0
    state.held_by = None


def keys(due: Due) -> str:
    return ", ".join(due.keys)


def run(
    bao: OpenBao,
    cluster: Cluster,
    kinds: Mapping[str, Kind],
    switches: Switches,
    *,
    youtrack: Callable[[str], YouTrack] = YouTrack,
    telegram: Callable[[str, int], Telegram] = Telegram,
    out: Callable[[str], None],
    holder: str,
    now: Callable[[], datetime.datetime] = utcnow,
    clock: Callable[[], float] = time.monotonic,
) -> int:
    """The nightly run: Jeeves's token for the card and the bot's for Telegram, from the store."""
    chat = switches.telegram_chat_id
    bot = None if chat is None else telegram(bao.value(*BOT_TOKEN), chat)
    return Night(
        bao,
        cluster,
        kinds,
        switches,
        youtrack=youtrack(bao.value(*CARD_TOKEN)),
        telegram=bot,
        out=out,
        holder=holder,
        now=now,
        clock=clock,
    ).go()
