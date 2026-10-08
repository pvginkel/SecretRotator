"""One box of the list: a listed rotation and the state its box shows (design §7.3), with what its
wizard has shown this session: where its plan is, the step log, the operator step's request, its
rollback."""

import datetime
import time
from dataclasses import dataclass, field
from enum import Enum

from secret_rotator.executor import Stand
from secret_rotator.listing import Rotation
from secret_rotator.model import Action, Actor, Step, label
from secret_rotator.ui.collate import Screen, collate, screen_of

# The kind whose box shows its one confirm with Done, and no wizard (R81).
EXTERNAL = "external"


class Phase(Enum):
    DUE = "due"  # not in flight, due or not
    RUNNING = "running"  # the wizard is open and the tool works
    WAITING = "waiting"  # the wizard is open at an operator step
    DONE = "done"  # stamped: the box leaves the list
    IN_FLIGHT = "in-flight"
    FAILED = "failed"
    ROLLING_BACK = "rolling-back"  # its rollback runs
    ROLLED_BACK = "rolled-back"  # then it is due again, in place
    ROLLBACK_FAILED = "rollback-failed"  # its rollback stopped: failed, or exited


PHASE_OF = {
    Stand.FRESH: Phase.DUE,
    Stand.IN_FLIGHT: Phase.IN_FLIGHT,
    Stand.FAILED: Phase.FAILED,
    Stand.ROLLING_BACK: Phase.ROLLBACK_FAILED,
}


class LineState(Enum):
    RUNNING = "running"
    OK = "ok"
    FAILED = "failed"


@dataclass
class Line:
    """One line of the step log: a step's run, or its undo or re-run."""

    step: Step
    action: Action
    started: float  # monotonic
    state: LineState = LineState.RUNNING
    detail: str = ""
    error: str = ""
    elapsed: float = 0.0  # seconds, once it ended
    attempt: int = 1  # its run this session: a Retry runs a failed line again

    def spent(self) -> float:
        return time.monotonic() - self.started if self.state is LineState.RUNNING else self.elapsed


@dataclass(eq=False)
class Item:
    rotation: Rotation
    phase: Phase
    screens: list[Screen]
    at: int  # the index of the step its plan is at
    lines: dict[tuple[str, Action], Line] = field(default_factory=dict)
    # The current operator screen's request, kept once answered while its silent steps run.
    request: object | None = None
    said: list[str] = field(default_factory=list)  # what its last run said: the lock, an error
    # What its rollback runs, once one is under way or stopped (Executor.rollback), and how many
    # of them finished.
    rollback: list[tuple[Step, Action]] | None = None
    undone: int = 0

    @classmethod
    def of(cls, rotation: Rotation) -> "Item":
        screens = collate(rotation.plan.steps)
        return cls(rotation, PHASE_OF[rotation.stand], screens, rotation.at)

    @property
    def id(self) -> str:
        """Its leaf with its keys, `leaf#key`: unique, as a key belongs to one plan of its leaf."""
        target = self.rotation.plan.target
        return f"{target.leaf}#{','.join(target.keys)}"

    @property
    def leaf(self) -> str:
        return self.rotation.plan.target.leaf

    @property
    def external(self) -> bool:
        return self.rotation.plan.target.kind == EXTERNAL

    @property
    def in_flight(self) -> bool:
        """Its plan is in flight, or about to be: the leaf's other plans wait on it."""
        return self.phase not in (Phase.DUE, Phase.DONE, Phase.ROLLED_BACK)

    @property
    def rolls_back(self) -> bool:
        return self.phase in (Phase.ROLLING_BACK, Phase.ROLLED_BACK, Phase.ROLLBACK_FAILED)

    @property
    def screen(self) -> Screen:
        return screen_of(self.screens, self.rotation.plan.steps[self.at])

    @property
    def operator_step(self) -> Step | None:
        """Its screen's operator step: the screen's title and instruction before its request
        comes."""
        return next((s for s in self.screen.steps if s.actor is Actor.OPERATOR), None)

    @property
    def remaining(self) -> int:
        """Seconds, from the step it is at; in a rollback, from the undo it is at."""
        if self.rolls_back:
            return sum(step.estimate for step, _ in self.rollback[self.undone :])
        return sum(step.estimate for step in self.rotation.plan.steps[self.at :])

    @property
    def estimate(self) -> int:
        """Seconds, for its information line: the whole plan while it is due, else what remained
        of it when listed, kept while the wizard runs (design §7.3)."""
        if self.phase is Phase.DUE:
            return sum(step.estimate for step in self.rotation.plan.steps)
        return self.rotation.estimate

    def stopped_at(self) -> str:
        """The line its plan stopped at: the undo its rollback is at, else its step."""
        if self.rolls_back:
            if self.undone < len(self.rollback):
                return label(*self.rollback[self.undone])
            return ""  # every undo ran: the staging leaf outlived it
        return self.rotation.plan.steps[self.at].title

    def due_again(self) -> None:
        """Back to due, its plan no longer in flight: a cancel, or a rollback done."""
        self.phase = Phase.DUE
        self.at = 0
        self.lines = {}
        self.request = None
        self.rollback = None
        self.undone = 0

    def visible(self) -> list[Line]:
        """The step log of its screen, in plan order: its tool steps' lines, a silent one's only
        once it failed."""
        lines = (self.lines.get((step.id, Action.RUN)) for step in self.screen.steps)
        return [
            line
            for line in lines
            if line is not None
            and line.step.actor is Actor.TOOL
            and (not line.step.silent or line.state is LineState.FAILED)
        ]

    def rollback_lines(self) -> list[Line]:
        """The rollback screen's log: the lines of its undos and re-runs this session, in their
        order."""
        lines = (self.lines.get((step.id, action)) for step, action in self.rollback or [])
        return [line for line in lines if line is not None]

    def waits_on_you(self, today: datetime.date) -> bool:
        """What the status bar counts (R90): in flight, failed, or due."""
        due_at = self.rotation.due_at
        due = self.phase is not Phase.DONE and due_at is not None and due_at <= today
        return self.in_flight or due
