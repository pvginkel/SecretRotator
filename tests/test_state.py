"""The run state (design §3.4): one data leaf, kv/rotator/state, each secret leaf's state a JSON
object under its path; every write a check-and-set started again from a fresh read when another
write came first; a leaf gone from the store dropped at the next write; and the plans in flight
found by their staging leaves."""

import json

import pytest
from fake_openbao import FakeOpenBao
from plans import LEAF, client, fake, put_flight, put_state, run_state, state_of

from secret_rotator.contract import STATE_LEAF
from secret_rotator.openbao import OpenBaoError
from secret_rotator.staging import InFlight, flights
from secret_rotator.state import LeafState, State

WIFI = "shared/wifi"


def mark(**fields):
    def change(state):
        for name, value in fields.items():
            setattr(state, name, value)

    return change


class TestALeafsState:
    def test_it_is_stored_as_the_json_object_of_its_fields_that_are_set(self):
        state = LeafState(stamps={"token": "2026-10-05"}, status="ok", consumers=("a", "b"))
        assert json.loads(state.dump()) == {
            "consumers": ["a", "b"],
            "stamps": {"token": "2026-10-05"},
            "status": "ok",
        }
        assert LeafState.load(state.dump()) == state
        assert LeafState().dump() == "{}"


class TestAWrite:
    def test_the_first_creates_the_state_leaf_by_check_and_set(self):
        bao = fake()
        written = run_state(bao).update(LEAF, mark(status="ok"))
        assert written == LeafState(status="ok")
        ((method, path, _, body, _),) = bao.writes()
        assert (method, path) == ("POST", f"kv/data/{STATE_LEAF}")
        assert body["options"] == {"cas": 0}
        assert body["data"] == {LEAF: '{"status":"ok"}'}

    def test_it_changes_only_its_leaf_and_writes_against_the_version_it_read(self):
        bao = fake()
        put_state(bao, WIFI, status="manual-due")
        put_state(bao, LEAF, stamps={"token": "2026-09-01"})
        run_state(bao).update(LEAF, mark(status="failed"))
        assert bao.writes()[0][3]["options"] == {"cas": 1}
        assert state_of(bao, WIFI) == LeafState(status="manual-due")
        assert state_of(bao, LEAF) == LeafState(stamps={"token": "2026-09-01"}, status="failed")
        assert bao.version(STATE_LEAF) == 2

    def test_a_write_that_came_first_is_kept_and_the_change_made_again_on_it(self):
        bao = fake()
        put_state(bao, LEAF, failed_nights=1)
        nightly, run = run_state(bao), run_state(bao)
        calls = []

        def count(state):
            calls.append(state.failed_nights)
            if len(calls) == 1:  # the operator's run writes between this read and its write
                run.update(WIFI, mark(status="manual-due"))
            state.failed_nights += 1

        assert nightly.update(LEAF, count).failed_nights == 2
        assert calls == [1, 1]
        assert state_of(bao, WIFI).status == "manual-due"
        assert state_of(bao, LEAF).failed_nights == 2

    def test_a_refusal_that_is_no_check_and_set_conflict_is_raised(self):
        bao = fake()
        bao.refuse["POST", f"kv/data/{STATE_LEAF}"] = 400
        with pytest.raises(OpenBaoError) as e:
            run_state(bao).update(LEAF, mark(status="ok"))
        assert e.value.status == 400
        assert len(bao.writes()) == 1

    def test_it_drops_the_state_of_a_leaf_the_store_no_longer_holds(self):
        bao = fake()
        put_state(bao, "eso/prd/gone/prd/leaf", status="ok")
        put_state(bao, WIFI, status="manual-due")
        State(client(bao), {WIFI, LEAF}).update(LEAF, mark(status="ok"))
        assert set(bao.data(STATE_LEAF)) == {WIFI, LEAF}
        assert not [w for w in bao.writes() if w[0] == "DELETE"]

    def test_it_keeps_the_state_of_a_leaf_made_since_its_process_read_the_store(self):
        bao = fake()
        nightly = run_state(bao)
        new = "eso/prd/newapp/prd/token"
        bao.leaves[new] = {"data": {"token": "v"}, "meta": {}}
        run_state(bao).update(new, mark(stamps={"token": "2026-10-01"}))
        nightly.update(LEAF, mark(status="ok"))
        assert state_of(bao, new).stamps == {"token": "2026-10-01"}
        assert state_of(bao, LEAF).status == "ok"

    def test_it_keeps_the_leaf_it_writes_and_drops_a_state_left_empty(self):
        bao = fake()
        put_state(bao, WIFI, held_by="ANS-1")
        State(client(bao), {WIFI}).update(LEAF, mark(status="ok"))
        assert set(bao.data(STATE_LEAF)) == {WIFI, LEAF}
        State(client(bao), {WIFI, LEAF}).update(WIFI, mark(held_by=None))
        assert set(bao.data(STATE_LEAF)) == {LEAF}

    def test_a_leaf_without_state_reads_as_none_set(self):
        assert run_state(FakeOpenBao()).of(LEAF) == LeafState()


class TestThePlansInFlight:
    def test_each_is_read_from_its_staging_leaf_whose_path_names_its_kind_and_leaf(self):
        bao = fake()
        put_flight(bao, "random", LEAF, ["token"], "kv.write", **{"value:token": "SECRET-new"})
        put_flight(bao, "manual", WIFI, ["a,b", "c/d"], "operator.credential:a,b")
        assert flights(client(bao)) == {
            LEAF: InFlight("random", ("token",), "kv.write"),
            WIFI: InFlight("manual", ("a,b", "c/d"), "operator.credential:a,b"),
        }

    def test_none_without_a_staging_leaf(self):
        assert flights(client(fake())) == {}
