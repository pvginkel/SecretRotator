"""The plan model of design §4.1–§4.5: the one Step ABC every step type implements, the context a
step runs in, and the events its run reaches a renderer by."""

import abc
import datetime
import traceback
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from secret_rotator.github import GitHubError
from secret_rotator.jenkins import JenkinsError
from secret_rotator.kube import KubeError
from secret_rotator.openbao import OpenBao, OpenBaoError
from secret_rotator.state import State
from secret_rotator.youtrack import YouTrackError


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
    """A step's own failure: error is one sentence, technical the detail behind it. landed False:
    what the step does is known never to have taken effect (Step.no_undo)."""

    def __init__(self, error: str, technical: str = "", *, landed: bool = True):
        super().__init__(error)
        self.error = error
        self.technical = technical
        self.landed = landed


def failure(e: Exception) -> tuple[str, str]:
    """A failure's one sentence and its technical detail. An OpenBaoError, a KubeError, a
    JenkinsError, a GitHubError or a YouTrackError names its request, and a transport error as
    such."""
    technical = "".join(traceback.format_exception(e))
    if isinstance(e, StepFailed):
        return e.error, e.technical or technical
    if isinstance(e, OpenBaoError | KubeError | JenkinsError | GitHubError | YouTrackError):
        return str(e), technical
    return f"{type(e).__name__}: {e}", technical


def not_landed(e: Exception) -> StepFailed:
    """The failure e, its sentence and detail kept, as one of a step that did not land."""
    error, technical = failure(e)
    return StepFailed(error, technical, landed=False)


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


def expiry_name(key: str) -> str:
    """The staging name of the ISO date the new credential of a plan's data key expires, which
    kv.stamp writes as the key's expires_at."""
    return f"expires-at:{key}"


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
    # Why a mutating step without an undo cannot be taken back, shown when Abort is refused. Abort
    # is refused once such a step ran, a failed run included, unless every run of it failed with a
    # StepFailed with landed False. A step raises that only where what it does is known never to
    # have taken effect, and its docstring names those failures. A step that did not land counts
    # as not having run: nothing of it is undone or re-run, and only the steps before it gate
    # Abort (design §4.5).
    no_undo = ""

    def __init__(self, id: str, title: str, *, estimate: int = 0):
        self.id = id  # stable across rebuilds of the same plan
        self.title = title  # plain words
        self.estimate = estimate  # seconds

    def __repr__(self) -> str:
        return f"<{self.type} {self.id}>"

    def unanswered(self, bao: OpenBao) -> str | None:
        """Why what the step acts on does not answer: a system that may be off, as the dev cluster
        is by default; None when it answers or is no such system. The nightly run asks it before it
        starts the plan. A system that answers and refuses is no reason: the step fails on it."""
        return None

    @abc.abstractmethod
    def run(self, ctx: Context) -> str | None:
        """Does the step and returns the detail of its finished line. Raises to fail; a Retry
        runs it again, so it must be idempotent."""


def label(step: Step, action: Action) -> str:
    """A line's words: the step's title, marked as an undo or a re-run by a rollback."""
    return {Action.RUN: "", Action.UNDO: "undo: ", Action.RERUN: "again: "}[action] + step.title


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
