"""The lock kv/rotator/lock (design §4.3): taken by a check-and-set write, released by a write
that clears its holder and never deleted, and a dead holder's lock broken through the rotator."""

import pytest
from plans import NOW, client, fake

from secret_rotator.contract import LOCK_LEAF
from secret_rotator.lock import Lock, LockError, LockHeld, holder_name


def lock(bao, who="run eso/x on host-a, pid 1"):
    return Lock(client(bao), who, clock=lambda: NOW)


def test_it_is_taken_by_a_check_and_set_write_naming_holder_since_and_plan():
    bao = fake()
    lock(bao).take("random plan of eso/x")
    (write,) = bao.writes()
    assert write[:2] == ("POST", f"kv/data/{LOCK_LEAF}")
    assert write[3] == {
        "data": {
            "holder": "run eso/x on host-a, pid 1",
            "since": "2026-10-05T04:30:00+00:00",
            "plan": "random plan of eso/x",
        },
        "options": {"cas": 0},
    }


def test_a_released_lock_is_taken_at_its_current_version_and_never_deleted():
    bao = fake()
    first = lock(bao)
    with first.held("p1"):
        pass
    assert bao.data(LOCK_LEAF) == {}
    with lock(bao).held("p2"):
        assert bao.data(LOCK_LEAF)["plan"] == "p2"
    casses = [w[3]["options"]["cas"] for w in bao.writes()]
    assert casses == [0, 1, 2, 3]
    assert not [w for w in bao.writes() if w[0] == "DELETE"]


def test_a_held_lock_refuses_and_names_its_holder():
    bao = fake()
    lock(bao, "the nightly run").take("random plan of eso/a")
    with pytest.raises(LockHeld) as e:
        lock(bao).take("p")
    assert str(e.value) == (
        "another plan runs: the nightly run holds it since 2026-10-05T04:30:00+00:00 "
        "for the random plan of eso/a"
    )


def test_a_holder_that_takes_it_in_between_wins_the_check_and_set():
    bao = fake()
    real = bao.get_data

    def racing(leaf, body, query, req):
        bao.get_data = real
        try:
            return real(leaf, body, query, req)
        finally:
            lock(bao, "the other one").take("theirs")

    bao.get_data = racing
    with pytest.raises(LockHeld, match="the other one"):
        lock(bao).take("mine")
    assert bao.data(LOCK_LEAF)["holder"] == "the other one"


def test_a_dead_holder_s_lock_is_broken_through_the_rotator():
    bao = fake()
    lock(bao, "a run that died").take("p")
    mine = lock(bao)
    seen = mine.holder()
    mine.break_held(seen)
    assert mine.holder() is None
    with mine.held("mine"):
        assert mine.holder().who == mine.who


def test_breaking_is_refused_when_another_holder_took_it_since():
    bao = fake()
    lock(bao, "a run that died").take("p")
    seen = lock(bao).holder()
    lock(bao).break_held(seen)
    lock(bao, "a new holder").take("q")
    with pytest.raises(LockHeld, match="a new holder"):
        lock(bao).break_held(seen)
    assert bao.data(LOCK_LEAF)["holder"] == "a new holder"


def test_a_release_after_the_lock_was_broken_and_retaken_does_not_clear_the_new_holder():
    bao = fake()
    mine = lock(bao)
    mine.take("mine")
    other = lock(bao, "the operator")
    other.break_held(other.holder())
    other.take("theirs")
    with pytest.raises(LockError, match="broken while this plan held it"):
        mine.release()
    assert bao.data(LOCK_LEAF)["holder"] == "the operator"


def test_the_holder_name_says_the_command_and_where_it_runs():
    assert holder_name("run eso/x").startswith("run eso/x on ")
