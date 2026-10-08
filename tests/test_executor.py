"""The executor (design §4.5): a plan runs under the lock, its place kept in its staging leaf and
its outcome in the run state; a failure stops it, Retry and a new process resume it, Abort rolls
it back or cancels it, a front end stops it from another thread, and a dry run touches nothing."""

import datetime
import time

import pytest
from fixtures import AUTO, annotated, compliant_store, data_of, edit, fields_of
from plans import (
    COPY,
    LEAF,
    NOW,
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
    lock,
    plan_of,
    put_flight,
    put_state,
    run_state,
    state_of,
)

from secret_rotator.audit import audit, live_store
from secret_rotator.contract import LOCK_LEAF, STATE_LEAF
from secret_rotator.executor import (
    Abandon,
    AbortRefused,
    Executor,
    Outcome,
    PlanMismatch,
    Stand,
)
from secret_rotator.kvsteps import URLSAFE
from secret_rotator.lock import Lock, LockHeld
from secret_rotator.model import Action, Skipped, Step, StepFailed
from secret_rotator.schedule import schedule
from secret_rotator.staging import NOT_LANDED, InFlight, staging_leaf
from secret_rotator.state import LeafState

STAGING = staging_leaf("random", LEAF)
OLD = data_of(LEAF)["token"]
TRELLO = "eso/prd/trello/prd/trello"  # manual api-key and token, random bearer-token
TRELLO_MANUAL = {"leaf": TRELLO, "of": "manual", "keys": ("api-key",)}
TRELLO_STAGING = staging_leaf("manual", TRELLO)


def executor(bao, plan, renderer, *, dry_run=False):
    return Executor(
        client(bao),
        plan,
        renderer,
        lock(bao),
        state=run_state(bao),
        dry_run=dry_run,
        clock=lambda: NOW,
    )


def run(bao, plan, *answers):
    r = Recorder(*answers)
    return executor(bao, plan, r).run(), r


def writes_to(bao, path):
    return [(m, p) for m, p, *_ in bao.writes() if p == path]


def due_at(bao, leaf=LEAF, key="token"):
    """When the key falls due, read from the store as the nightly run reads it."""
    live = live_store(client(bao), runs=True)
    entries = audit(live).entries[leaf]
    (found,) = [s for s in schedule(leaf, entries, live[leaf].state.stamps) if s.key == key]
    return found.due_at


class ExtraFirst(RandomLike):
    """random's shape with the extra steps between the generate and the write."""

    def plan(self, leaf, ctx):
        return [*ctx.steps.generate("token"), *self.extra, *ctx.steps.write()]


class Unrecorded(Step):
    """A step without an undo that did not land, and whose mark the staging leaf refuses."""

    type = "test.unrecorded"
    mutates = True
    no_undo = "unrecorded cannot be taken back"

    def __init__(self, bao):
        super().__init__("unrecorded", "do unrecorded")
        self.bao = bao

    def run(self, ctx):
        self.bao.broken["POST", f"kv/data/{STAGING}"] = TimeoutError("timed out")
        raise StepFailed("unrecorded failed", landed=False)


class TestARun:
    def test_it_writes_the_new_value_and_its_copies_and_stamps_the_keys(self):
        bao = fake()
        outcome, r = run(bao, plan_of())
        assert outcome is Outcome.DONE
        new = bao.data(LEAF)["token"]
        assert new != OLD and len(new) == 43 and set(new) <= set(URLSAFE)
        assert bao.data(COPY) == {"token": new}
        state = state_of(bao, LEAF)
        assert state.stamps == {"token": "2026-10-05"} and state.status == "ok"
        assert flight_of(bao, LEAF) is None
        assert bao.meta(LEAF) == fake().meta(LEAF)
        assert not [w for w in bao.writes() if w[1].startswith("kv/metadata/") and w[0] != "DELETE"]
        assert r.lines() == [
            (state, step, Action.RUN)
            for step in ("random.generate:token", "kv.write", "kv.copy:iac/copy#token", "kv.stamp")
            for state in ("started", "ok")
        ]

    def test_a_rotation_past_the_key_s_expiry_leaves_it_due_at_its_stamp_plus_its_interval(self):
        # 045 close-out B2: an expiry that has passed kept a freshly rotated key due every run.
        bao = fake()
        edit(bao.meta(LEAF), "token", expires_at="2026-10-01")
        put_state(bao, LEAF, stamps={"token": "2026-09-30"})
        assert due_at(bao) == datetime.date(2026, 9, 24)
        assert run(bao, plan_of())[0] is Outcome.DONE
        assert "expires_at" not in fields_of(bao.meta(LEAF), "token")
        assert due_at(bao) == datetime.date(2026, 10, 19)

    def test_the_staging_leaf_is_destroyed_and_the_lock_released_not_deleted(self):
        bao = fake()
        run(bao, plan_of())
        assert STAGING not in bao.leaves
        assert ("DELETE", f"kv/metadata/{STAGING}") in writes_to(bao, f"kv/metadata/{STAGING}")
        assert bao.data(LOCK_LEAF) == {}
        assert not [w for w in bao.writes() if w[1] == f"kv/metadata/{LOCK_LEAF}"]

    def test_the_value_is_staged_before_anything_uses_it(self):
        bao = fake()
        run(bao, plan_of())
        paths = [p for _, p, *_ in bao.writes()]
        assert paths.index(f"kv/data/{STAGING}") < paths.index(f"kv/data/{LEAF}")

    def test_each_step_is_recorded_in_the_staging_leaf_before_it_runs(self):
        bao = fake()
        run(bao, plan_of())
        records, done = [], []
        for _, path, _, body, _ in bao.writes():
            if path == f"kv/data/{STAGING}":
                assert body["data"]["keys"] == '["token"]'
                records.append(body["data"]["step"])
            elif path not in (f"kv/data/{LOCK_LEAF}", f"kv/metadata/{STAGING}"):
                done.append((path, records[-1]))
        steps = ["random.generate:token", "kv.write", "kv.copy:iac/copy#token", "kv.stamp"]
        assert list(dict.fromkeys(records)) == steps
        assert done == [
            (f"kv/data/{LEAF}", "kv.write"),
            (f"kv/data/{COPY}", "kv.copy:iac/copy#token"),
            (f"kv/data/{STATE_LEAF}", "kv.stamp"),
        ]

    def test_no_event_state_or_lock_carries_a_value(self):
        bao = fake()
        _, r = run(bao, plan_of())
        new = bao.data(LEAF)["token"]
        texts = [str(getattr(e, f, "")) for e in r.events for f in ("detail", "error")]
        assert not [t for t in texts if new in t or OLD in t]
        assert new not in str(bao.meta(LEAF)) and new not in str(bao.leaves[LOCK_LEAF])
        assert new not in str(bao.leaves[STATE_LEAF])

    def test_a_staging_leaf_left_after_its_stamp_ends_its_plan_at_the_stamp(self):
        bao = fake()
        put_flight(bao, "random", LEAF, ["token"], "kv.stamp", **{"value:token": "STALE"})
        e = executor(bao, plan_of(), Recorder())
        assert e.load() is Stand.IN_FLIGHT
        assert e.run() is Outcome.DONE
        assert STAGING not in bao.leaves and bao.data(LEAF)["token"] == OLD
        assert state_of(bao, LEAF).stamps == {"token": "2026-10-05"}
        run(bao, plan_of())
        assert bao.data(LEAF)["token"] not in ("STALE", OLD)

    def test_the_bag_s_other_keys_are_never_rewritten(self):
        bao = fake()
        bao.leaves["eso/prd/kc/prd/catalog"]["data"]["app-token"] = "SECRET-bag-copy"
        bag_before = dict(bao.data("eso/prd/kc/prd/catalog"))
        store = compliant_store()
        bag = store["eso/prd/kc/prd/catalog"]
        bag.keys.add("app-token")
        edit(bag.meta, "app-token", kind=f"copy:{LEAF}#token", activate="auto")
        outcome, _ = run(bao, plan_of(store=store))
        assert outcome is Outcome.DONE
        new = bao.data(LEAF)["token"]
        assert bao.data("eso/prd/kc/prd/catalog") == bag_before | {"app-token": new}
        (_, _, _, body, ctype) = next(
            r for r in bao.requests if r[:2] == ("PATCH", "kv/data/eso/prd/kc/prd/catalog")
        )
        assert body["data"] == {"app-token": new} and ctype == "application/merge-patch+json"


class TestAFailure:
    def failed(self):
        bao = fake()
        bao.refuse["PATCH", f"kv/data/{COPY}"] = 403
        outcome, r = run(bao, plan_of())
        return bao, outcome, r

    def test_it_stops_the_plan_at_the_failed_step_and_records_it(self):
        bao, outcome, r = self.failed()
        assert outcome is Outcome.FAILED
        state = state_of(bao, LEAF)
        assert state.status == "failed" and "HTTP 403" in state.last_error
        assert state.last_run == "2026-10-05T04:30:00+00:00" and state.stamps == {}
        assert flight_of(bao, LEAF) == InFlight("random", ("token",), "kv.copy:iac/copy#token")
        (failure,) = r.failures()
        assert failure.step.id == "kv.copy:iac/copy#token" and "Traceback" in failure.technical
        assert r.lines()[-1] == ("failed", "kv.copy:iac/copy#token", Action.RUN)
        assert bao.data(LOCK_LEAF) == {} and STAGING in bao.leaves

    def test_retry_runs_the_failed_step_again_and_carries_on(self):
        bao, _, _ = self.failed()
        new = bao.data(LEAF)["token"]
        bao.refuse.clear()
        e = executor(bao, plan_of(), Recorder())
        assert e.load() is Stand.FAILED
        assert e.run() is Outcome.DONE
        assert bao.data(LEAF)["token"] == new and bao.data(COPY) == {"token": new}
        assert len(writes_to(bao, f"kv/data/{LEAF}")) == 1  # kv.write did not run again
        assert state_of(bao, LEAF).status == "ok"

    def test_a_transport_error_is_the_step_s_failure_and_named_as_one(self):
        bao = fake()
        bao.broken["PATCH", f"kv/data/{LEAF}"] = TimeoutError("timed out")
        outcome, r = run(bao, plan_of())
        assert outcome is Outcome.FAILED
        (failure,) = r.failures()
        assert failure.step.id == "kv.write" and "transport error" in failure.error
        assert "transport error" in state_of(bao, LEAF).last_error

    def test_a_record_write_that_fails_is_the_step_s_failure(self):
        bao = fake()
        bao.broken["POST", f"kv/data/{STAGING}"] = TimeoutError("timed out")
        outcome, r = run(bao, plan_of())
        assert outcome is Outcome.FAILED
        (failure,) = r.failures()
        assert failure.step.id == "random.generate:token" and "transport error" in failure.error
        assert state_of(bao, LEAF).status == "failed" and flight_of(bao, LEAF) is None

    def test_a_failure_the_run_state_cannot_take_says_so(self):
        bao = fake()
        bao.refuse["PATCH", f"kv/data/{COPY}"] = 403
        bao.broken["POST", f"kv/data/{STATE_LEAF}"] = TimeoutError("timed out")
        outcome, r = run(bao, plan_of())
        assert outcome is Outcome.FAILED
        (failure,) = r.failures()
        assert "HTTP 403" in failure.error
        assert "The failure is not recorded in the run state" in failure.error
        assert "transport error" in failure.error

    def test_a_failed_activator_is_a_failed_activation(self):
        bao = fake()
        outcome, _ = run(bao, plan_of(Tool("act", Journal(), fail=1, activator=True)))
        assert outcome is Outcome.FAILED
        assert state_of(bao, LEAF).status == "failed-activation"

    def test_a_plan_whose_step_is_gone_from_its_rebuild_is_refused(self):
        bao = fake()
        put_flight(bao, "random", LEAF, ["token"], "no.such:step")
        with pytest.raises(PlanMismatch, match="no.such:step"):
            run(bao, plan_of())
        assert bao.data(LEAF)["token"] == OLD and bao.data(LOCK_LEAF) == {}

    def test_another_kind_s_plan_in_flight_on_the_leaf_is_not_resumed_as_this_one(self):
        bao = fake()
        flight = put_flight(bao, "manual", LEAF, ["token"], "kv.write")
        e = executor(bao, plan_of(), Recorder())
        with pytest.raises(
            PlanMismatch, match="in flight in its manual plan of token, at kv.write"
        ):
            e.run()
        with pytest.raises(PlanMismatch):
            e.abort()
        assert bao.data(LEAF)["token"] == OLD and bao.data(LOCK_LEAF) == {}
        assert flight_of(bao, LEAF) == flight and STAGING not in bao.leaves


class TestTheLeafsPlanInFlight:
    """A leaf has one plan in flight, named by its kind and its keys: a plan of the same kind for
    other keys is not it (the per-key manual plans; a due set that grew since it stopped)."""

    def test_a_plan_for_another_key_of_the_kind_is_refused_and_touches_nothing(self):
        bao = fake()
        api_key = plan_of(Tool("act", Journal(), fail=1, activator=True), **TRELLO_MANUAL)
        assert run(bao, api_key)[0] is Outcome.FAILED
        before = (dict(bao.data(TRELLO)), dict(bao.meta(TRELLO)), dict(bao.data(TRELLO_STAGING)))
        token = plan_of(Tool("act", Journal()), **TRELLO_MANUAL | {"keys": ("token",)})
        e = executor(bao, token, Recorder())
        with pytest.raises(PlanMismatch, match="in flight in its manual plan of api-key, at act"):
            e.run()
        with pytest.raises(PlanMismatch):
            e.abort()
        assert (bao.data(TRELLO), bao.meta(TRELLO), bao.data(TRELLO_STAGING)) == before
        assert "token" not in state_of(bao, TRELLO).stamps

    def test_a_plan_for_a_due_set_that_grew_is_refused(self):
        store = compliant_store()
        edit(store[TRELLO].meta, "token", kind="random")
        bao = fake()
        grown = {"leaf": TRELLO, "keys": ("bearer-token", "token"), "store": store}
        first = plan_of(
            Tool("eso.sync:x", Journal(), fail=1, activator=True),
            **grown | {"keys": ("bearer-token",)},
        )
        assert run(bao, first)[0] is Outcome.FAILED
        with pytest.raises(PlanMismatch, match="random plan of bearer-token, at eso.sync:x"):
            run(bao, plan_of(Tool("eso.sync:x", Journal()), **grown))
        assert bao.data(TRELLO)["token"] == data_of(TRELLO)["token"]
        assert "token" not in state_of(bao, TRELLO).stamps

    def test_the_plan_rebuilt_from_what_is_in_flight_resumes(self):
        bao = fake()
        api_key = plan_of(Tool("act", Journal(), fail=1, activator=True), **TRELLO_MANUAL)
        run(bao, api_key)
        flight = flight_of(bao, TRELLO)
        assert flight == InFlight("manual", ("api-key",), "act")
        again = plan_of(Tool("act", Journal()), **TRELLO_MANUAL | {"keys": flight.keys})
        assert run(bao, again)[0] is Outcome.DONE
        assert state_of(bao, TRELLO).stamps == {"api-key": "2026-10-05"}
        assert flight_of(bao, TRELLO) is None

    def test_its_record_keeps_a_key_named_with_a_comma_or_a_slash(self):
        store = compliant_store()
        store[LEAF].keys |= {"a,b", "c/d"}
        store[LEAF].meta |= annotated({key: {"kind": "random", **AUTO} for key in ("a,b", "c/d")})
        bao = fake()
        keys = {"store": store, "keys": ("a,b", "c/d")}
        assert run(bao, plan_of(Tool("act", Journal(), fail=1), **keys))[0] is Outcome.FAILED
        assert flight_of(bao, LEAF) == InFlight("random", ("a,b", "c/d"), "act")
        assert run(bao, plan_of(Tool("act", Journal()), **keys))[0] is Outcome.DONE
        assert state_of(bao, LEAF).stamps == {"a,b": "2026-10-05", "c/d": "2026-10-05"}


class TestResume:
    def test_an_exit_at_an_operator_step_leaves_the_plan_in_flight_there(self):
        bao = fake()
        outcome, r = run(bao, plan_of(Confirm("revoke")), Abandon.EXIT)
        assert outcome is Outcome.EXITED
        assert r.asked == [("revoke", "please revoke")]
        assert flight_of(bao, LEAF) == InFlight("random", ("token",), "revoke")
        assert bao.data(LOCK_LEAF) == {}

    def test_a_new_process_resumes_where_the_plan_stopped_with_its_staged_value(self):
        bao = fake()
        plan = plan_of(Confirm("revoke"))
        run(bao, plan, Abandon.EXIT)
        new = bao.data(LEAF)["token"]
        e = executor(bao, plan_of(Confirm("revoke")), Recorder({}))
        assert e.load() is Stand.IN_FLIGHT
        assert e.run() is Outcome.DONE
        assert bao.data(LEAF)["token"] == new and state_of(bao, LEAF).status == "ok"

    def test_an_exit_before_anything_was_written_keeps_the_generated_value(self):
        bao = fake()
        run(bao, plan_of(kind=ConfirmFirst()), Abandon.EXIT)
        assert bao.data(LEAF)["token"] == OLD
        staged = bao.data(STAGING)["value:token"]
        assert run(bao, plan_of(kind=ConfirmFirst()), {})[0] is Outcome.DONE
        assert bao.data(LEAF)["token"] == staged

    def test_a_second_plan_is_refused_while_another_holds_the_lock(self):
        bao = fake()
        Lock(client(bao), "the nightly run").take("a plan")
        with pytest.raises(LockHeld, match="the nightly run"):
            run(bao, plan_of())
        assert bao.data(LEAF)["token"] == OLD


class TestAbort:
    def test_it_undoes_in_reverse_then_re_runs_the_activators_in_order(self):
        bao = fake()
        j = Journal()
        steps = (Tool("a1", j, activator=True), Tool("t", j), Tool("a2", j, activator=True))
        outcome, r = run(bao, plan_of(*steps, Confirm("check")), Abandon.ABORT)
        assert outcome is Outcome.ROLLED_BACK
        assert j == [
            ("run", "a1"),
            ("run", "t"),
            ("run", "a2"),
            ("undo", "t"),
            ("run", "a1"),
            ("run", "a2"),
        ]
        assert [(i, a) for state, i, a in r.lines() if a is not Action.RUN and state == "ok"] == [
            ("t", Action.UNDO),
            ("kv.copy:iac/copy#token", Action.UNDO),
            ("kv.write", Action.UNDO),
            ("a1", Action.RERUN),
            ("a2", Action.RERUN),
        ]
        assert bao.data(LEAF)["token"] == OLD and bao.data(COPY) == data_of(COPY)
        assert flight_of(bao, LEAF) is None and STAGING not in bao.leaves

    def test_a_rotation_rolled_back_leaves_the_key_s_expires_at_as_it_was(self):
        bao = fake()
        edit(bao.meta(LEAF), "token", expires_at="2026-10-01")
        meta = dict(bao.meta(LEAF))
        executor_ = executor(bao, plan_of(Tool("activate", Journal(), fail=1)), Recorder())
        assert executor_.run() is Outcome.FAILED
        assert executor_.abort() is Outcome.ROLLED_BACK
        assert bao.meta(LEAF) == meta

    def test_the_kv_undos_restore_the_copies_before_the_primary(self):
        bao = fake()
        run(bao, plan_of(Confirm("check")), Abandon.ABORT)
        patches = [p for m, p, *_ in bao.writes() if m == "PATCH" and p.startswith("kv/data/")]
        assert patches == [f"kv/data/{p}" for p in (LEAF, COPY, COPY, LEAF)]

    def test_while_nothing_mutated_it_is_a_cancel_and_the_leaf_is_as_it_was(self):
        bao = fake()
        meta = dict(bao.meta(LEAF))
        outcome, _ = run(bao, plan_of(kind=ConfirmFirst()), Abandon.ABORT)
        assert outcome is Outcome.CANCELLED
        assert bao.meta(LEAF) == meta and bao.data(LEAF)["token"] == OLD
        assert STAGING not in bao.leaves and STATE_LEAF not in bao.leaves
        assert not writes_to(bao, f"kv/data/{LEAF}")

    def test_it_is_refused_once_a_finished_mutating_step_has_no_undo(self):
        bao = fake()
        j = Journal()
        plan = plan_of(Tool("irrev", j, undoable=False), Confirm("check"))
        r = Recorder(Abandon.ABORT)
        e = executor(bao, plan, r)
        with pytest.raises(AbortRefused, match="irrev cannot be taken back"):
            e.run()
        assert e.abort_blocker() == "irrev cannot be taken back"
        assert flight_of(bao, LEAF).step == "check" and bao.data(LOCK_LEAF) == {}

    def test_an_operator_step_that_cannot_be_undone_refuses_it_once_done(self):
        bao = fake()
        plan = plan_of(Confirm("revoke", irreversible="the old token is revoked"), Confirm("x"))
        e = executor(bao, plan, Recorder({}, Abandon.EXIT))
        assert e.run() is Outcome.EXITED
        e.load()
        assert e.abort_blocker() == "the old token is revoked"
        with pytest.raises(AbortRefused):
            e.abort()

    def test_a_failed_step_without_an_undo_counts_as_landed(self):
        bao = fake()
        j = Journal()
        e = executor(bao, plan_of(Tool("irrev", j, undoable=False, fail=1)), Recorder())
        assert e.run() is Outcome.FAILED
        with pytest.raises(AbortRefused):
            e.abort()
        assert flight_of(bao, LEAF).step == "irrev"

    def test_a_failed_step_that_did_not_land_rolls_back_the_steps_before_it(self):
        bao = fake()
        j = Journal()
        plan = plan_of(Tool("t", j), Tool("irrev", j, undoable=False, fail=1, landed=False))
        e = executor(bao, plan, Recorder())
        assert e.run() is Outcome.FAILED
        assert e.abort_blocker() is None
        assert [(step.id, action) for step, action in e.rollback()] == [
            ("t", Action.UNDO),
            ("kv.copy:iac/copy#token", Action.UNDO),
            ("kv.write", Action.UNDO),
        ]
        assert e.abort() is Outcome.ROLLED_BACK
        assert j == [("run", "t"), ("run", "irrev"), ("undo", "t")]
        assert bao.data(LEAF)["token"] == OLD and flight_of(bao, LEAF) is None

    def test_that_a_step_did_not_land_is_kept_with_the_plan_in_flight(self):
        bao = fake()
        j = Journal()
        plan = plan_of(Tool("irrev", j, undoable=False, fail=1, landed=False))
        assert run(bao, plan)[0] is Outcome.FAILED
        assert bao.data(STAGING)[NOT_LANDED] == "irrev"
        e = executor(bao, plan, Recorder())
        assert e.load() is Stand.FAILED and e.abort_blocker() is None
        assert e.abort() is Outcome.ROLLED_BACK
        assert j == [("run", "irrev")]

    def test_a_retry_counts_the_step_as_landed_until_it_says_otherwise(self):
        bao = fake()
        j = Journal()
        irrev = Tool("irrev", j, undoable=False, fail=2, landed=False)
        e = executor(bao, plan_of(irrev), Recorder())
        assert e.run() is Outcome.FAILED
        irrev.landed = True
        assert e.run() is Outcome.FAILED
        assert NOT_LANDED not in bao.data(STAGING)
        assert e.abort_blocker() == "irrev cannot be taken back"

    def test_a_retry_that_did_not_land_leaves_an_earlier_attempt_landed(self):
        bao = fake()
        irrev = Tool("irrev", Journal(), undoable=False, fail=3)
        e = executor(bao, plan_of(irrev), Recorder())
        assert e.run() is Outcome.FAILED
        irrev.landed = False
        for _ in range(2):
            assert e.run() is Outcome.FAILED
            assert NOT_LANDED not in bao.data(STAGING)
            assert e.abort_blocker() == "irrev cannot be taken back"

    def test_a_retry_that_did_not_land_after_attempts_that_did_not_either_rolls_back(self):
        bao = fake()
        j = Journal()
        irrev = Tool("irrev", j, undoable=False, fail=2, landed=False)
        assert run(bao, plan_of(irrev))[0] is Outcome.FAILED
        e = executor(bao, plan_of(irrev), Recorder())
        assert e.run() is Outcome.FAILED
        assert bao.data(STAGING)[NOT_LANDED] == "irrev"
        assert e.abort() is Outcome.ROLLED_BACK
        assert j == [("run", "irrev"), ("run", "irrev")]

    def test_with_nothing_else_mutated_it_is_a_cancel(self):
        bao = fake()
        kind = ExtraFirst(Tool("irrev", Journal(), undoable=False, fail=1, landed=False))
        e = executor(bao, plan_of(kind=kind), Recorder())
        assert e.run() is Outcome.FAILED
        assert e.abort() is Outcome.CANCELLED
        assert STAGING not in bao.leaves and bao.data(LEAF)["token"] == OLD

    def test_a_mark_the_staging_leaf_cannot_take_counts_the_step_as_landed_and_says_so(self):
        bao = fake()
        r = Recorder()
        e = executor(bao, plan_of(Unrecorded(bao)), r)
        assert e.run() is Outcome.FAILED
        (failure,) = r.failures()
        assert failure.error.startswith(
            "unrecorded failed That it did not land is not recorded, so it counts as landed: "
        )
        assert "transport error" in failure.error
        assert state_of(bao, LEAF).last_error == failure.error
        del bao.broken["POST", f"kv/data/{STAGING}"]
        e = executor(bao, plan_of(Unrecorded(bao)), Recorder())
        e.load()
        assert e.abort_blocker() == "unrecorded cannot be taken back"

    def test_an_activator_without_an_undo_does_not_refuse_it(self):
        bao = fake()
        j = Journal()
        plan = plan_of(Tool("act", j, fail=1, activator=True, undoable=False))
        assert run(bao, plan)[0] is Outcome.FAILED
        e = executor(bao, plan, Recorder())
        e.load()
        assert e.abort_blocker() is None
        assert e.abort() is Outcome.ROLLED_BACK
        assert j == [("run", "act"), ("run", "act")]
        assert bao.data(LEAF)["token"] == OLD

    def test_after_a_failure_it_rolls_back_the_failed_activator_too(self):
        bao = fake()
        j = Journal()
        plan = plan_of(Tool("act", j, fail=1, activator=True))
        outcome, _ = run(bao, plan)
        assert outcome is Outcome.FAILED
        assert executor(bao, plan, Recorder()).abort() is Outcome.ROLLED_BACK
        assert j == [("run", "act"), ("run", "act")]
        assert bao.data(LEAF)["token"] == OLD
        assert state_of(bao, LEAF).status == "failed-activation" and flight_of(bao, LEAF) is None

    def test_a_failing_undo_stops_the_rollback_and_retry_continues_it(self):
        bao = fake()
        j = Journal()
        plan = plan_of(Tool("t", j, undo_fail=1), Confirm("check"))
        outcome, r = run(bao, plan, Abandon.ABORT)
        assert outcome is Outcome.ROLLBACK_FAILED
        assert r.lines()[-1] == ("failed", "t", Action.UNDO)
        assert bao.data(LEAF)["token"] != OLD  # the KV undos come after t's
        state = state_of(bao, LEAF)
        assert state.status == "failed"
        assert state.last_error.startswith("rollback: the undo of t failed")
        assert flight_of(bao, LEAF).step == "check"
        e = executor(bao, plan, Recorder())
        assert e.load() is Stand.ROLLING_BACK
        with pytest.raises(AbortRefused, match="Retry"):
            e.abort()
        assert e.run() is Outcome.ROLLED_BACK
        assert j == [("run", "t"), ("undo", "t"), ("undo", "t")]
        assert bao.data(LEAF)["token"] == OLD and flight_of(bao, LEAF) is None

    def test_a_rollback_resumed_in_a_new_process_skips_the_undos_it_did(self):
        bao = fake()
        j = Journal()
        plan = plan_of(Tool("t", j, undo_fail=1), Tool("u", j), Confirm("check"))
        assert run(bao, plan, Abandon.ABORT)[0] is Outcome.ROLLBACK_FAILED
        assert executor(bao, plan, Recorder()).run() is Outcome.ROLLED_BACK
        assert j == [("run", "t"), ("run", "u"), ("undo", "u"), ("undo", "t"), ("undo", "t")]

    def test_a_plan_not_started_has_nothing_to_roll_back(self):
        bao = fake()
        e = executor(bao, plan_of(Tool("irrev", Journal(), undoable=False)), Recorder())
        assert e.load() is Stand.FRESH
        assert e.rollback() == [] and e.abort_blocker() is None
        assert e.abort() is Outcome.CANCELLED
        assert bao.meta(LEAF) == fake().meta(LEAF) and state_of(bao, LEAF) == LeafState()


class StopsAsItEnds(Step):
    """A mutating tool step during whose run, past its last progress detail, a front end asks
    the executor to stop."""

    type = "test.stops"
    mutates = True

    def __init__(self, id, journal, choice):
        super().__init__(id, f"do {id}")
        self.journal = journal
        self.choice = choice
        self.executor = None

    def run(self, ctx):
        self.journal.append(("run", self.id))
        self.executor.stop(self.choice)
        return f"{self.id} done"

    def undo(self, ctx):
        self.journal.append(("undo", self.id))
        return f"{self.id} undone"


class TestStop:
    """A front end that runs the executor off its own thread stops a running tool step (design
    §4.5): Abort rolls the plan back with that step's undo, an exit leaves it in flight there."""

    def started(self, bao, step, *more):
        e = executor(bao, plan_of(step, *more), Recorder())
        worker = Worker(e.run)
        assert step.waiting.wait(10)
        return e, worker

    def test_abort_while_a_tool_step_runs_stops_it_and_its_undo_runs(self):
        bao = fake()
        j = Journal()
        e, worker = self.started(bao, Waiting("rollout", j), Confirm("check"))
        assert [(step.id, action) for step, action in e.rollback()] == [
            ("rollout", Action.UNDO),
            ("kv.copy:iac/copy#token", Action.UNDO),
            ("kv.write", Action.UNDO),
        ]
        e.stop(Abandon.ABORT)
        assert worker.join() is Outcome.ROLLED_BACK
        assert j == [("run", "rollout"), ("undo", "rollout")]
        assert bao.data(LEAF)["token"] == OLD and flight_of(bao, LEAF) is None
        assert bao.data(LOCK_LEAF) == {} and state_of(bao, LEAF) == LeafState()

    def test_exit_while_a_tool_step_runs_leaves_the_plan_in_flight_there_and_a_resume_reruns_it(
        self,
    ):
        bao = fake()
        j = Journal()
        e, worker = self.started(bao, Waiting("rollout", j), Confirm("check"))
        e.stop(Abandon.EXIT)
        assert worker.join() is Outcome.EXITED
        assert flight_of(bao, LEAF) == InFlight("random", ("token",), "rollout")
        assert bao.data(LOCK_LEAF) == {} and state_of(bao, LEAF) == LeafState()
        new = bao.data(LEAF)["token"]
        e = executor(bao, plan_of(Tool("rollout", j), Confirm("check")), Recorder({}))
        assert e.load() is Stand.IN_FLIGHT
        assert e.run() is Outcome.DONE
        assert j == [("run", "rollout"), ("run", "rollout")]
        assert bao.data(LEAF)["token"] == new

    def test_abort_is_refused_while_a_step_without_an_undo_runs(self):
        bao = fake()
        e, worker = self.started(bao, Waiting("irrev", Journal(), undoable=False))
        assert e.abort_blocker() == "irrev cannot be taken back"
        e.stop(Abandon.ABORT)
        with pytest.raises(AbortRefused, match="irrev cannot be taken back"):
            worker.join()
        assert flight_of(bao, LEAF).step == "irrev" and bao.data(LOCK_LEAF) == {}

    @pytest.mark.parametrize("choice", [Abandon.ABORT, Abandon.EXIT])
    def test_a_stop_asked_for_as_a_step_finishes_lands_before_the_next_step(self, choice):
        bao = fake()
        j = Journal()
        step = StopsAsItEnds("t", j, choice)
        e = executor(bao, plan_of(step, Tool("next", j)), Recorder())
        step.executor = e
        if choice is Abandon.ABORT:
            assert e.run() is Outcome.ROLLED_BACK
            assert j == [("run", "t"), ("undo", "t")] and flight_of(bao, LEAF) is None
        else:
            assert e.run() is Outcome.EXITED
            assert j == [("run", "t")] and flight_of(bao, LEAF).step == "t"
        assert bao.data(LOCK_LEAF) == {}

    def test_a_rollback_stops_on_exit_only_and_a_resume_continues_it(self):
        bao = fake()
        j = Journal()
        step = Waiting("t", j, run_waits=False, undo_waits=True)
        e = executor(bao, plan_of(step, Confirm("check")), Recorder(Abandon.ABORT))
        worker = Worker(e.run)
        assert step.waiting.wait(10)  # its undo, in the rollback the Abort at check began
        e.stop(Abandon.ABORT)
        time.sleep(0.05)
        assert step.waiting.is_set()
        e.stop(Abandon.EXIT)
        assert worker.join() is Outcome.EXITED
        assert flight_of(bao, LEAF).step == "check" and bao.data(LOCK_LEAF) == {}
        e = executor(bao, plan_of(Tool("t", j), Confirm("check")), Recorder())
        assert e.load() is Stand.ROLLING_BACK
        assert e.run() is Outcome.ROLLED_BACK
        assert j == [("run", "t"), ("undo", "t"), ("undo", "t")]
        assert bao.data(LEAF)["token"] == OLD and flight_of(bao, LEAF) is None


class TestDryRun:
    def test_it_runs_nothing_and_reads_or_writes_nothing(self):
        bao = fake()
        r = Recorder()
        e = executor(bao, plan_of(Tool("t", Journal())), r, dry_run=True)
        assert e.run() is Outcome.DRY_RUN
        assert e.abort() is Outcome.DRY_RUN
        assert bao.requests == []
        assert all(isinstance(ev, Skipped) and ev.reason == "dry run" for ev in r.events)
        assert len(r.events) == 5
