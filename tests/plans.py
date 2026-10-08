"""Test doubles for the plan model: a store on the fake OpenBao with its run state and plans in
flight, a kind built like `random`, steps whose behaviour a test sets, a renderer that records
events and answers asks, and a thread that runs a plan as a front end with an event loop does."""

import dataclasses
import datetime
import json
import threading
import time

from fake_openbao import TOKEN, WRITTEN_AT, FakeOpenBao
from fixtures import COMPLIANT, compliant_store, data_of

from secret_rotator.audit import audit
from secret_rotator.cluster import Derived
from secret_rotator.contract import STATE_LEAF
from secret_rotator.lock import Lock
from secret_rotator.model import Actor, Finished, Started, Step, StepFailed
from secret_rotator.openbao import OpenBao
from secret_rotator.plan import build, target
from secret_rotator.staging import (
    DERIVED,
    KEYS,
    ROLLBACK,
    STEP,
    InFlight,
    flights,
    staging_leaf,
)
from secret_rotator.state import LeafState, State

NOW = datetime.datetime(2026, 10, 5, 4, 30, tzinfo=datetime.UTC)
LEAF = "eso/prd/app/prd/token"  # random, key token, copied to iac/copy#token
COPY = "iac/copy"


def fake():
    return FakeOpenBao(
        {path: {"data": data_of(path), "meta": dict(meta)} for path, (_, meta) in COMPLIANT.items()}
    )


def fake_of(store, data=None):
    """The fake OpenBao holding the store's leaves with their annotations; data: a leaf's data
    where it is not SECRET-<path>-<key> per key."""
    data = data or {}
    return FakeOpenBao(
        {
            path: {
                "data": data.get(path) or {k: f"SECRET-{path}-{k}" for k in sorted(leaf.keys)},
                "meta": dict(leaf.meta),
            }
            for path, leaf in store.items()
        }
    )


def client(bao):
    return OpenBao(opener=bao, token=TOKEN)


def lock(bao, who="test run"):
    return Lock(client(bao), who, clock=lambda: NOW)


def run_state(bao):
    """The run state as a process that read every leaf of the fake's store writes it."""
    return State(client(bao), set(bao.leaves))


def state_of(bao, leaf):
    """The leaf's run state in the fake."""
    data = bao.leaves.get(STATE_LEAF, {}).get("data") or {}
    return LeafState.load(data[leaf]) if leaf in data else LeafState()


def put_state(bao, leaf, **fields):
    """Sets fields of the leaf's run state in the fake, as a run left them; stamps are added to
    the leaf's."""
    current = state_of(bao, leaf)
    if "stamps" in fields:
        fields["stamps"] = current.stamps | fields["stamps"]
    entry = bao.leaves.setdefault(STATE_LEAF, {"data": {}, "meta": {}})
    entry["data"] = entry["data"] | {leaf: dataclasses.replace(current, **fields).dump()}


def flight_of(bao, leaf):
    """The leaf's plan in flight in the fake, read from its staging leaf; None when it has none."""
    return flights(client(bao)).get(leaf)


def put_flight(bao, kind, leaf, keys, step, **staged):
    """A plan in flight in the fake, as a run left it at the step, having derived nothing."""
    data = {KEYS: json.dumps(list(keys)), STEP: step, DERIVED: Derived().dump()} | staged
    bao.leaves[staging_leaf(kind, leaf)] = {"data": data, "meta": {}}
    return InFlight(kind, tuple(keys), step, WRITTEN_AT, rolling_back=ROLLBACK in staged)


class Journal(list):
    """(action, step id), in the order the test steps did them."""


class Tool(Step):
    """A mutating tool step; fails its first `fail` runs, reporting `landed`, and `undo_fail`
    undos."""

    type = "test.tool"
    mutates = True

    def __init__(
        self,
        id,
        journal,
        *,
        fail=0,
        undo_fail=0,
        undoable=True,
        activator=False,
        mutates=True,
        landed=True,
    ):
        super().__init__(id, f"do {id}")
        self.journal = journal
        self.fail = fail
        self.undo_fail = undo_fail
        self.landed = landed
        self.activator = activator
        self.mutates = mutates
        if not undoable:
            self.undo = None
            self.no_undo = f"{id} cannot be taken back"

    def run(self, ctx):
        self.journal.append(("run", self.id))
        if self.fail:
            self.fail -= 1
            raise StepFailed(f"{self.id} failed", "the technical detail", landed=self.landed)
        return f"{self.id} done"

    def undo(self, ctx):
        self.journal.append(("undo", self.id))
        if self.undo_fail:
            self.undo_fail -= 1
            raise StepFailed(f"the undo of {self.id} failed")
        return f"{self.id} undone"


class Waiting(Step):
    """A mutating tool step whose run, or undo, reports a progress detail over and over until the
    executor stops it, as a wait on the cluster does; waiting is set while it does."""

    type = "test.waiting"
    mutates = True

    def __init__(self, id, journal, *, undoable=True, run_waits=True, undo_waits=False):
        super().__init__(id, f"wait for {id}")
        self.journal = journal
        self.run_waits = run_waits
        self.undo_waits = undo_waits
        self.waiting = threading.Event()
        if not undoable:
            self.undo = None
            self.no_undo = f"{id} cannot be taken back"

    def run(self, ctx):
        self.journal.append(("run", self.id))
        return self._wait(ctx) if self.run_waits else f"{self.id} done"

    def undo(self, ctx):
        self.journal.append(("undo", self.id))
        return self._wait(ctx) if self.undo_waits else f"{self.id} undone"

    def _wait(self, ctx):
        self.waiting.set()
        try:
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                ctx.progress("waiting")
                time.sleep(0.005)
            return "never stopped"
        finally:
            self.waiting.clear()


class Worker:
    """fn run on a thread of its own, as a front end whose event loop answers the renderer runs
    the executor."""

    def __init__(self, fn):
        self.result = self.error = None
        self.thread = threading.Thread(target=self._run, args=(fn,), daemon=True)
        self.thread.start()

    def _run(self, fn):
        try:
            self.result = fn()
        except Exception as e:
            self.error = e

    def join(self):
        """What fn returned, once it has; what it raised is raised."""
        self.thread.join(10)
        assert not self.thread.is_alive(), "it never ended"
        if self.error is not None:
            raise self.error
        return self.result


class Confirm(Step):
    """An operator step: asks, and is done when answered; an activator one is asked again by a
    rollback."""

    type = "test.confirm"
    actor = Actor.OPERATOR

    def __init__(self, id, *, irreversible="", activator=False):
        super().__init__(id, f"confirm {id}")
        if irreversible:
            self.mutates = True
            self.no_undo = irreversible
        if activator:
            self.mutates = self.activator = True

    def run(self, ctx):
        ctx.ask(f"please {self.id}")
        return "confirmed"


class RandomLike:
    """random's shape: generate each key, write it and its copies; then the extra steps."""

    name = "random"
    per_key = False

    def __init__(self, *extra):
        self.extra = extra

    def args_problems(self, args):
        return []

    def ask(self, leaf):
        return ""

    def description(self, leaf):
        return f"rotates {', '.join(leaf.keys)}"

    def plan(self, leaf, ctx):
        steps = [s for key in leaf.keys for s in ctx.steps.generate(key)]
        return [*steps, *ctx.steps.write(), *self.extra]


class ConfirmFirst(RandomLike):
    """generate, then a confirm before the write: nothing has mutated at the confirm."""

    def plan(self, leaf, ctx):
        return [*ctx.steps.generate("token"), Confirm("first"), *ctx.steps.write()]


def plan_of(*extra, kind=None, store=None, leaf=LEAF, of="random", keys=("token",)):
    """The plan of rotating the leaf's keys of kind `of`, built by `kind` (random's shape)."""
    store = store or compliant_store()
    kind = kind or RandomLike(*extra)
    return build(kind, target(leaf, of, list(keys), audit(store)))


class Recorder:
    def __init__(self, *answers):
        self.events = []
        self.answers = list(answers)
        self.asked = []

    def event(self, event):
        self.events.append(event)

    def ask(self, step, request):
        self.asked.append((step.id, request))
        return self.answers.pop(0)

    def lines(self):
        """(started | ok | failed, step id, action) of every started and finished line."""
        return [
            (
                "started" if isinstance(e, Started) else "ok" if e.ok else "failed",
                e.step.id,
                e.action,
            )
            for e in self.events
            if isinstance(e, Started | Finished)
        ]

    def failures(self):
        return [e for e in self.events if isinstance(e, Finished) and not e.ok]
