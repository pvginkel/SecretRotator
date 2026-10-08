"""One plan run to an end by a front end, the same in `run <path>` and in the UI (design §4.5,
§7.4): what the operator is offered where the plan stands — Resume, Retry, Abort behind its guard or
its refusal reason, Details — what each does, the break of a dead holder's lock, and the Telegram
message for every failure, of the plan or of its rollback (design R66). The front end shows the
run and asks the operator; a front end whose event loop answers the renderer runs the executor and
this off its own thread, and stops a running plan with Executor.stop."""

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum

from secret_rotator.executor import AbortRefused, Executor, Outcome, PlanMismatch, Stand
from secret_rotator.lock import Holder, LockError, LockHeld
from secret_rotator.model import label
from secret_rotator.openbao import OpenBaoError
from secret_rotator.telegram import TelegramError, failed


class Choice(StrEnum):
    RESUME = "resume"  # run a plan left in flight on from its step
    RETRY = "retry"  # run the failed step again, or the stopped rollback on
    ABORT = "abort"  # roll back, after its guard: abort_question
    DETAILS = "details"
    EXIT = "exit"  # leave the plan where it stands


@dataclass(frozen=True)
class Offer:
    choices: tuple[Choice, ...]
    blocker: str | None = None  # why Abort is not among them; None in a rollback, which is one


def abort_question(executor: Executor) -> str:
    """Abort's one question (design §4.5); its count is the steps the rollback runs."""
    n = len(executor.rollback())
    return f"Abort and roll back {n} step{'' if n == 1 else 's'}?" if n else "Abort?"


class Session:
    """say: a line the operator reads; confirm: their yes or no to a question. notify: where each
    failure is told in Telegram, None while no chat is committed; a message it cannot send is
    said. command: the command the plan runs in, which takes a plan left in flight up again."""

    def __init__(
        self,
        executor: Executor,
        *,
        say: Callable[[str], None],
        confirm: Callable[[str], bool],
        notify: Callable[[str], None] | None,
        command: str,
    ):
        self.executor = executor
        self.say = say
        self.confirm = confirm
        self.notify = notify
        self.command = command

    def offer(self, stand: Stand) -> Offer:
        """What the operator may do with the plan in flight, failed, or stopped in its rollback."""
        if stand is Stand.ROLLING_BACK:
            return Offer((Choice.RETRY, Choice.DETAILS, Choice.EXIT))
        blocker = self.executor.abort_blocker()
        abort = () if blocker else (Choice.ABORT,)
        if stand is Stand.FAILED:
            return Offer((Choice.RETRY, *abort, Choice.DETAILS, Choice.EXIT), blocker)
        return Offer((Choice.RESUME, *abort, Choice.EXIT), blocker)

    def details(self) -> str:
        """The technical detail of the last failure, or what the run state recorded of a failure
        taken up from an earlier run."""
        failure = self.executor.failure
        if failure is not None:
            return failure.technical.rstrip()
        state = self.executor.state.of(self.executor.leaf)
        return f"{state.last_run or '?'}: {state.last_error or 'no error recorded'}"

    def attempt(self, action: Callable[[], Outcome]) -> Outcome | None:
        """The executor's run or abort, run again once the operator has a dead holder's lock
        broken; None when it got to no outcome, which is said."""
        e = self.executor
        while True:
            try:
                outcome = action()
            except LockHeld as held:
                if not self.break_lock(held.holder):
                    return None
                continue
            except KeyboardInterrupt:  # Ctrl-C, which reaches a plan run on the main thread only
                self.say("")
                self.say(
                    f"Interrupted at: {e.plan.steps[e.at].title}. It is left in flight there: "
                    f"`{self.command}` takes it up, running that step again."
                )
                return None
            except (AbortRefused, PlanMismatch, OpenBaoError, LockError) as err:
                self.say(f"error: {err}")
                return None
            if self.notify is not None and outcome in (Outcome.FAILED, Outcome.ROLLBACK_FAILED):
                self.tell(outcome)
            return outcome

    def tell(self, outcome: Outcome) -> None:
        """The failure in Telegram."""
        failure, plan = self.executor.failure, self.executor.plan
        text = failed(
            plan.name,
            plan.target.keys,
            label(failure.step, failure.action),
            failure.error,
            rollback=outcome is Outcome.ROLLBACK_FAILED,
        )
        try:
            self.notify(f"In `{self.command}`: {text}")
        except (OpenBaoError, TelegramError) as err:
            self.say(f"The Telegram message about it is not sent: {err}")

    def break_lock(self, holder: Holder) -> bool:
        """The lock of a holder the operator says is gone, broken; False when it is not."""
        self.say(f"Another plan runs: {holder}.")
        if not self.confirm(f"Is {holder.who} gone? Break its lock?"):
            return False
        try:
            self.executor.lock.break_held(holder)
        except LockHeld as held:
            self.say(f"Not broken: it was taken since. {held.holder}.")
            return False
        except OpenBaoError as err:
            self.say(f"error: {err}")
            return False
        self.say("The lock is broken.")
        return True
