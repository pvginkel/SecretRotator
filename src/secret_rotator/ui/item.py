"""One box of the list: a listed rotation and the state its box shows (design §7.3), with what its
wizard has shown this session: where its plan is, the step log, the operator step's request."""

import datetime
import time
from dataclasses import dataclass, field
from enum import Enum

from secret_rotator.executor import Stand
from secret_rotator.listing import Rotation
from secret_rotator.model import Action, Actor, Step
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
    ROLLBACK_FAILED = "rollback-failed"


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
        return self.phase not in (Phase.DUE, Phase.DONE)

    @property
    def screen(self) -> Screen:
        return screen_of(self.screens, self.rotation.plan.steps[self.at])

    @property
    def remaining(self) -> int:
        """Seconds, from the step it is at."""
        return sum(step.estimate for step in self.rotation.plan.steps[self.at :])

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

    def waits_on_you(self, today: datetime.date) -> bool:
        """What the status bar counts (R90): in flight, failed, or due."""
        due_at = self.rotation.due_at
        due = self.phase is Phase.DUE and due_at is not None and due_at <= today
        return self.in_flight or due
