"""Test doubles for the plan model: a store on the fake OpenBao, a kind built like `random`, steps
whose behaviour a test sets, and a renderer that records events and answers asks."""

import datetime

from fake_openbao import TOKEN, FakeOpenBao
from fixtures import COMPLIANT, compliant_store, data_of

from secret_rotator.audit import audit
from secret_rotator.lock import Lock
from secret_rotator.model import Actor, Finished, Started, Step, StepFailed
from secret_rotator.openbao import OpenBao
from secret_rotator.plan import build, target

NOW = datetime.datetime(2026, 10, 5, 4, 30, tzinfo=datetime.UTC)
LEAF = "eso/prd/app/prd/token"  # random, key token, copied to iac/copy#token
COPY = "iac/copy"


def fake():
    return FakeOpenBao(
        {path: {"data": data_of(path), "meta": dict(meta)} for path, (_, meta) in COMPLIANT.items()}
    )


def client(bao):
    return OpenBao(opener=bao, token=TOKEN)


def lock(bao, who="test run"):
    return Lock(client(bao), who, clock=lambda: NOW)


class Journal(list):
    """(action, step id), in the order the test steps did them."""


class Tool(Step):
    """A mutating tool step; fails its first `fail` runs and `undo_fail` undos."""

    type = "test.tool"
    mutates = True

    def __init__(
        self, id, journal, *, fail=0, undo_fail=0, undoable=True, activator=False, mutates=True
    ):
        super().__init__(id, f"do {id}")
        self.journal = journal
        self.fail = fail
        self.undo_fail = undo_fail
        self.activator = activator
        self.mutates = mutates
        if not undoable:
            self.undo = None
            self.no_undo = f"{id} cannot be taken back"

    def run(self, ctx):
        self.journal.append(("run", self.id))
        if self.fail:
            self.fail -= 1
            raise StepFailed(f"{self.id} failed", "the technical detail")
        return f"{self.id} done"

    def undo(self, ctx):
        self.journal.append(("undo", self.id))
        if self.undo_fail:
            self.undo_fail -= 1
            raise StepFailed(f"the undo of {self.id} failed")
        return f"{self.id} undone"


class Confirm(Step):
    """An operator step: asks, and is done when answered."""

    type = "test.confirm"
    actor = Actor.OPERATOR

    def __init__(self, id, *, irreversible=""):
        super().__init__(id, f"confirm {id}")
        if irreversible:
            self.mutates = True
            self.no_undo = irreversible

    def run(self, ctx):
        ctx.ask(f"please {self.id}")
        return "confirmed"


class RandomLike:
    """random's shape: generate each key, write it and its copies; then the extra steps."""

    name = "random"

    def __init__(self, *extra):
        self.extra = extra

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
    return build(kind, target(leaf, of, list(keys), store, audit(store)))


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
