"""The UI's test double: the mock's simulated backend over the step and kind contracts. Its tool
steps report progress and take as long as the test holds them, then finish or fail on its cue;
its kind builds a vendor key's plan, you · tool · you, around them. Test code drives every cue."""

import datetime
import queue
import re
import threading

from plans import plan_of

from secret_rotator.executor import Stand
from secret_rotator.listing import Rotation
from secret_rotator.model import Step, StepFailed
from secret_rotator.opsteps import Shape

PAT = Shape("starts with vpat_", lambda value: re.fullmatch(r"vpat_\w+", value) is not None)
REVOKED = "the old token is revoked at the vendor"


class Cue:
    """A test's hold on a step: it reports progress until let go, then finishes or fails."""

    def __init__(self):
        self.verdicts = queue.Queue()
        self.holding = threading.Event()  # set while a step holds on it

    def go(self):
        self.verdicts.put(None)

    def fail(self, error):
        self.verdicts.put(error)


class Held(Step):
    """A mutating tool step that reports `1/2 Ready` until its cue lets it finish, `Ready`, or
    fail with the cue's error; without a cue it finishes at once."""

    type = "sim.held"
    mutates = True

    def __init__(self, id, title, *, cue=None, estimate=0, silent=False):
        super().__init__(id, title, estimate=estimate)
        self.cue = cue
        self.silent = silent

    def run(self, ctx):
        if self.cue is None:
            return "Ready"
        self.cue.holding.set()
        try:
            while True:
                try:
                    verdict = self.cue.verdicts.get(timeout=0.02)
                except queue.Empty:
                    ctx.progress("1/2 Ready")
                    continue
                if verdict is None:
                    return "Ready"
                raise StepFailed(verdict)
        finally:
            self.cue.holding.clear()

    def undo(self, ctx):
        return "undone"


class Vendor:
    """A vendor key's plan, the mock's GitHub token's: you mint a token and enter it with its
    expiry; the tool writes it and runs the held steps; you revoke the old token, which cannot be
    undone, and the stamp rides on that."""

    name = "random"
    per_key = False

    def __init__(self, *held):
        self.held = held

    def args_problems(self, args):
        return []

    def ask(self, leaf):
        return "paste a new vendor token"

    def description(self, leaf):
        return "You mint a new vendor token and paste it; the tool writes it and rolls it out."

    def plan(self, leaf, ctx):
        return [
            *ctx.steps.credential(
                "Mint a new vendor token",
                "Vendor → Settings → Tokens → New token\nScopes: read, write",
                PAT,
                expires=True,
            ),
            *ctx.steps.write(),
            *self.held,
            *ctx.steps.confirm(
                "revoke",
                "Revoke the old token",
                "Vendor → Settings → Tokens → Revoke",
                irreversible=REVOKED,
            ),
        ]


def rotation(plan, due_at=datetime.date(2026, 9, 1)):
    """The plan as listed, not in flight."""
    return Rotation(plan, Stand.FRESH, 0, due_at, None, "vendor token", "vendor")


def vendor(*held, leaf="eso/prd/app/prd/token", store=None):
    """The vendor plan of a random leaf of the store, the compliant one by default."""
    return rotation(plan_of(kind=Vendor(*held), leaf=leaf, store=store))
