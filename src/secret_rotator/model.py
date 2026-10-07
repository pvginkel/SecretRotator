"""The plan model of design §4.1–§4.5: the one Step ABC every step type implements, the context a
step runs in, and the events its run reaches a renderer by."""

import abc
import datetime
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from secret_rotator.openbao import OpenBao
from secret_rotator.state import State


class Actor(StrEnum):
    TOOL = "tool"
    OPERATOR = "operator"


class Action(StrEnum):
    """What a line of the run is: a step run, an undo, or an activator re-run by a rollback."""

    RUN = "run"
    UNDO = "undo"
    RERUN = "rerun"


class Pace(Protocol):
    """The clock a step that waits reads and the sleep it waits by: a client's, so a test's fake
    paces both."""

    def clock(self) -> float: ...

    def sleep(self, seconds: float) -> None: ...


class StepFailed(Exception):
    """A step's own failure: error is one sentence, technical the detail behind it."""

    def __init__(self, error: str, technical: str = ""):
        super().__init__(error)
        self.error = error
        self.technical = technical


class Context(Protocol):
    """What a running step gets from the executor."""

    bao: OpenBao
    state: State  # the run state, which kv.stamp writes
    now: datetime.datetime  # UTC

    def progress(self, detail: str) -> None:
        """A live detail of the running step: `1/2 Ready`."""

    def stage(self, name: str, value: str) -> None:
        """Puts a value in the plan's staging leaf before anything uses it (design §4.5)."""

    def staged(self, name: str) -> str | None:
        """A value staged by this plan, also before an exit or a crash; None if none was."""

    def ask(self, request: object) -> dict[str, str]:
        """The operator's answer to an operator step's request, from the renderer. When the
        operator aborts or exits instead, the step is left there and the executor takes over."""


def wait(
    pace: Pace,
    ctx: Context,
    bound: int,
    poll: int,
    why_not: Callable[[], str | None],
    what: str,
) -> None:
    """Polls why_not() until it answers None, each answer a progress detail; StepFailed once the
    bound (seconds) has passed: `<what> within <n> min: <its last answer>`."""
    deadline = pace.clock() + bound
    while (why := why_not()) is not None:
        if pace.clock() >= deadline:
            raise StepFailed(f"{what} within {bound // 60} min: {why}")
        ctx.progress(why)
        pace.sleep(poll)


def value_name(key: str) -> str:
    """The staging name of the new value of a plan's data key."""
    return f"value:{key}"


class Step(abc.ABC):
    """One unit of work with one target (design R48). The class attributes are its type's
    defaults; an instance may override them, as an operator step whose action cannot be undone
    declares itself mutating."""

    type: str
    actor = Actor.TOOL
    silent = False  # no screen of its own (design §7.4)
    mutates = False  # changes something outside the executor (design §4.5)
    # Its undo is re-running it after the real undos: an eso.sync, a k8s.rollout (design §4.5).
    activator = False
    # A rollback undoes the step the plan stopped at too, landed or not: an undo must leave a step
    # that did not land as it is.
    undo: Callable[[Context], str | None] | None = None
    # Why a mutating step without an undo cannot be taken back, shown when Abort is refused.
    no_undo = ""

    def __init__(self, id: str, title: str, *, estimate: int = 0):
        self.id = id  # stable across rebuilds of the same plan
        self.title = title  # plain words
        self.estimate = estimate  # seconds

    def __repr__(self) -> str:
        return f"<{self.type} {self.id}>"

    @abc.abstractmethod
    def run(self, ctx: Context) -> str | None:
        """Does the step and returns the detail of its finished line. Raises to fail; a Retry
        runs it again, so it must be idempotent."""


@dataclass(frozen=True)
class Started:
    step: Step
    action: Action = Action.RUN


@dataclass(frozen=True)
class Progress:
    step: Step
    action: Action
    detail: str


@dataclass(frozen=True)
class Finished:
    step: Step
    action: Action
    ok: bool
    detail: str = ""
    error: str = ""  # one sentence, shown under a failed line
    technical: str = ""  # the stack trace, the API response: Details


@dataclass(frozen=True)
class Skipped:
    step: Step
    reason: str


Event = Started | Progress | Finished | Skipped
