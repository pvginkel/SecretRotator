"""One way to run a plan for every front end (design §4.5, §7.4): what is offered where the plan
stands, Abort's guard and its refusal reason, Details, the break of a dead holder's lock, and the
Telegram message for every failure — also with the executor on a thread of its own, its operator
steps answered from the front end's thread and a running tool step stopped from there."""

import queue

from plans import (
    LEAF,
    Confirm,
    ConfirmFirst,
    Journal,
    RandomLike,
    Recorder,
    Tool,
    Waiting,
    Worker,
    client,
    fake,
    flight_of,
    plan_of,
    state_of,
)
from test_executor import OLD, executor

from secret_rotator.contract import LOCK_LEAF
from secret_rotator.executor import Abandon, Outcome, Stand
from secret_rotator.lock import Lock
from secret_rotator.session import Choice, Offer, Session, abort_question

UI = "secret-rotator ui"
REVOKED = "the old token is revoked"


class Front:
    """A front end's half of a session: what it is told, and its answers to the questions, each
    a bool or a function that returns one."""

    def __init__(self, *answers):
        self.said = []
        self.asked = []
        self.answers = list(answers)
        self.told = []

    def confirm(self, question):
        self.asked.append(question)
        answer = self.answers.pop(0)
        return answer() if callable(answer) else answer

    def session(self, e, *, notify=True):
        return Session(
            e,
            say=self.said.append,
            confirm=self.confirm,
            notify=self.told.append if notify else None,
            command=UI,
        )


class ToolFirst(RandomLike):
    """generate, then the extra steps, then the write."""

    def plan(self, leaf, ctx):
        return [*ctx.steps.generate("token"), *self.extra, *ctx.steps.write()]


def left(plan, *answers):
    """The plan run to where the answers leave it, then taken up by a new executor."""
    bao = fake()
    executor(bao, plan, Recorder(*answers)).run()
    e = executor(bao, plan, Recorder())
    return bao, e, e.load()


class TestOffer:
    def test_a_plan_in_flight_offers_resume_abort_and_exit(self):
        _, e, stand = left(plan_of(Confirm("check")), Abandon.EXIT)
        assert stand is Stand.IN_FLIGHT
        offer = Front().session(e).offer(stand)
        assert offer == Offer((Choice.RESUME, Choice.ABORT, Choice.EXIT))

    def test_once_a_finished_step_has_no_undo_abort_is_left_out_with_its_reason(self):
        plan = plan_of(Confirm("revoke", irreversible=REVOKED), Confirm("x"))
        _, e, stand = left(plan, {}, Abandon.EXIT)
        assert Front().session(e).offer(stand) == Offer((Choice.RESUME, Choice.EXIT), REVOKED)

    def test_a_failed_plan_offers_retry_abort_details_and_exit(self):
        _, e, stand = left(plan_of(Tool("t", Journal(), fail=1)))
        assert stand is Stand.FAILED
        offer = Front().session(e).offer(stand)
        assert offer == Offer((Choice.RETRY, Choice.ABORT, Choice.DETAILS, Choice.EXIT))

    def test_a_failed_step_without_an_undo_leaves_abort_out_with_its_reason(self):
        _, e, stand = left(plan_of(Tool("irrev", Journal(), fail=1, undoable=False)))
        assert Front().session(e).offer(stand) == Offer(
            (Choice.RETRY, Choice.DETAILS, Choice.EXIT), "irrev cannot be taken back"
        )

    def test_a_stopped_rollback_offers_retry_details_and_exit_and_never_abort(self):
        plan = plan_of(Tool("t", Journal(), undo_fail=1), Confirm("check"))
        _, e, stand = left(plan, Abandon.ABORT)
        assert stand is Stand.ROLLING_BACK
        assert Front().session(e).offer(stand) == Offer((Choice.RETRY, Choice.DETAILS, Choice.EXIT))


class TestAbortQuestion:
    def test_it_counts_the_steps_the_rollback_runs(self):
        _, e, _ = left(plan_of(Confirm("check")), Abandon.EXIT)
        assert abort_question(e) == "Abort and roll back 2 steps?"
        _, e, _ = left(plan_of(kind=ToolFirst(Tool("t", Journal()), Confirm("c"))), Abandon.EXIT)
        assert abort_question(e) == "Abort and roll back 1 step?"

    def test_while_nothing_mutated_it_asks_abort_only(self):
        _, e, _ = left(plan_of(kind=ConfirmFirst()), Abandon.EXIT)
        assert abort_question(e) == "Abort?"


class TestDetails:
    def test_it_is_the_technical_detail_of_the_failure(self):
        e = executor(fake(), plan_of(Tool("t", Journal(), fail=1)), Recorder())
        session = Front().session(e)
        assert session.attempt(e.run) is Outcome.FAILED
        assert session.details() == "the technical detail"

    def test_of_a_failure_taken_up_it_is_the_recorded_run_and_error(self):
        bao, e, _ = left(plan_of(Tool("t", Journal(), fail=1)))
        assert Front().session(e).details() == f"{state_of(bao, LEAF).last_run}: t failed"


class TestAttempt:
    def test_each_failure_of_the_plan_and_of_its_rollback_is_told_and_nothing_else(self):
        e = executor(fake(), plan_of(Tool("t", Journal(), fail=1, undo_fail=1)), Recorder())
        front = Front()
        session = front.session(e)
        assert session.attempt(e.run) is Outcome.FAILED
        assert session.attempt(e.abort) is Outcome.ROLLBACK_FAILED
        assert session.attempt(e.run) is Outcome.ROLLED_BACK
        assert front.told == [
            f"In `{UI}`: The random plan of {LEAF} (token) failed at do t: t failed",
            f"In `{UI}`: The rollback of the random plan of {LEAF} (token) failed at undo: do t: "
            "the undo of t failed",
        ]

    def test_without_a_chat_a_failure_is_told_nowhere(self):
        e = executor(fake(), plan_of(Tool("t", Journal(), fail=1)), Recorder())
        front = Front()
        assert front.session(e, notify=False).attempt(e.run) is Outcome.FAILED
        assert front.told == []

    def test_a_refusal_is_said_and_gets_to_no_outcome(self):
        plan = plan_of(Confirm("revoke", irreversible=REVOKED), Confirm("x"))
        _, e, _ = left(plan, {}, Abandon.EXIT)
        front = Front()
        assert front.session(e).attempt(e.abort) is None
        assert front.said == [f"error: {REVOKED}"]


class TestTheLock:
    def held(self):
        bao = fake()
        Lock(client(bao), "run x on gone, pid 1").take("a plan")
        return bao, executor(bao, plan_of(), Recorder())

    def test_a_dead_holder_s_lock_is_broken_on_yes_and_the_plan_runs(self):
        bao, e = self.held()
        front = Front(True)
        assert front.session(e).attempt(e.run) is Outcome.DONE
        assert front.said[0].startswith("Another plan runs: run x on gone, pid 1 holds it since")
        assert front.asked == ["Is run x on gone, pid 1 gone? Break its lock?"]
        assert front.said[1:] == ["The lock is broken."]
        assert bao.data(LEAF)["token"] != OLD and bao.data(LOCK_LEAF) == {}

    def test_on_no_nothing_runs_and_the_lock_stays(self):
        bao, e = self.held()
        assert Front(False).session(e).attempt(e.run) is None
        assert bao.data(LOCK_LEAF)["holder"] == "run x on gone, pid 1"
        assert bao.data(LEAF)["token"] == OLD

    def test_a_lock_taken_since_is_not_broken(self):
        bao, e = self.held()

        def taken():
            other = Lock(client(bao), "the nightly run")
            other.break_held(other.holder())
            other.take("its plan")
            return True

        front = Front(taken)
        assert front.session(e).attempt(e.run) is None
        assert front.said[1].startswith("Not broken: it was taken since. the nightly run holds it")
        assert bao.data(LOCK_LEAF)["holder"] == "the nightly run"
        assert bao.data(LEAF)["token"] == OLD


class Bridge:
    """The renderer of a front end with an event loop: each ask handed to the loop's thread, and
    the answer it puts back awaited."""

    def __init__(self):
        self.events = []
        self.asks = queue.Queue()
        self.answers = queue.Queue()

    def event(self, event):
        self.events.append(event)

    def ask(self, step, request):
        self.asks.put((step.id, request))
        return self.answers.get(timeout=10)


class TestOffItsOwnThread:
    def test_its_asks_are_answered_from_the_front_end_and_abort_stops_a_running_step(self):
        bao = fake()
        j = Journal()
        rollout = Waiting("rollout", j)
        bridge = Bridge()
        e = executor(bao, plan_of(Confirm("check"), rollout), bridge)
        front = Front()
        session = front.session(e)
        worker = Worker(lambda: session.attempt(e.run))
        assert bridge.asks.get(timeout=10) == ("check", "please check")
        bridge.answers.put({})
        assert rollout.waiting.wait(10)
        assert abort_question(e) == "Abort and roll back 3 steps?"
        e.stop(Abandon.ABORT)
        assert worker.join() is Outcome.ROLLED_BACK
        assert j == [("run", "rollout"), ("undo", "rollout")]
        assert bao.data(LEAF)["token"] == OLD and flight_of(bao, LEAF) is None
        assert front.told == [] and front.said == []

    def test_quitting_while_a_step_runs_leaves_it_in_flight_with_the_lock_free_and_resume_reruns_it(
        self,
    ):
        bao = fake()
        j = Journal()
        rollout = Waiting("rollout", j)
        e = executor(bao, plan_of(rollout), Bridge())
        session = Front().session(e)
        worker = Worker(lambda: session.attempt(e.run))
        assert rollout.waiting.wait(10)
        e.stop(Abandon.EXIT)
        assert worker.join() is Outcome.EXITED
        assert flight_of(bao, LEAF).step == "rollout" and bao.data(LOCK_LEAF) == {}
        e = executor(bao, plan_of(Tool("rollout", j)), Bridge())
        session = Front().session(e)
        assert Choice.RESUME in session.offer(e.load()).choices
        assert session.attempt(e.run) is Outcome.DONE
        assert j == [("run", "rollout"), ("run", "rollout")]
