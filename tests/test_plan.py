"""Building a plan (design §4.3): a Target from the leaf's resolved annotations, the kind's steps
from the step factory, kv.stamp appended by the core, and step ids stable across rebuilds."""

import pytest
from fixtures import compliant_store
from plans import COPY, LEAF, Confirm, Journal, RandomLike, Tool, plan_of

from secret_rotator.audit import audit
from secret_rotator.kvsteps import KvStamp
from secret_rotator.plan import Copy, PlanError, target

TRELLO = "eso/prd/trello/prd/trello"


def target_of(leaf, kind, keys, store=None):
    store = store or compliant_store()
    return target(leaf, kind, keys, store, audit(store))


def test_the_core_appends_kv_stamp_of_the_plan_s_keys_last():
    plan = plan_of()
    assert [s.id for s in plan.steps] == [
        "random.generate:token",
        "kv.write",
        f"kv.copy:{COPY}#token",
        "kv.stamp",
    ]
    stamp = plan.steps[-1]
    assert isinstance(stamp, KvStamp) and (stamp.leaf, stamp.keys) == (LEAF, ("token",))


def test_step_ids_are_stable_across_rebuilds():
    assert [s.id for s in plan_of().steps] == [s.id for s in plan_of().steps]


def test_a_kind_cannot_add_its_own_stamp_or_repeat_an_id():
    class Stamping(RandomLike):
        def plan(self, leaf, ctx):
            return [*super().plan(leaf, ctx), KvStamp(leaf.leaf, leaf.keys)]

    with pytest.raises(PlanError, match="repeats step id"):
        plan_of(kind=Stamping())
    with pytest.raises(PlanError, match="repeats step id"):
        plan_of(Tool("t", Journal()), Tool("t", Journal()))


def test_needs_operator_is_any_operator_step():
    assert not plan_of().needs_operator
    assert plan_of(Confirm("c")).needs_operator


def test_the_target_has_the_copies_of_the_rotated_keys_only():
    store = compliant_store()
    store["eso/prd/kc/prd/catalog"].keys.add("app-token")
    store["eso/prd/kc/prd/catalog"].meta["key_app-token"] = f"copy:{LEAF}#token"
    t = target_of(LEAF, "random", ["token"], store)
    assert t.copies == (
        Copy("eso/prd/kc/prd/catalog", "app-token", "token"),
        Copy(COPY, "token", "token"),
    )
    assert target_of(TRELLO, "random", ["bearer-token"]).copies == ()


def test_rotation_args_belong_to_the_leaf_s_kind_only():
    store = compliant_store()
    store[LEAF].meta["rotation_args"] = '{"length":20}'
    assert target_of(LEAF, "random", ["token"], store).args == {"length": 20}
    store[TRELLO].meta["rotation_args"] = '{"vendor":"trello"}'
    assert target_of(TRELLO, "random", ["bearer-token"], store).args == {}


@pytest.mark.parametrize(
    "leaf, kind, keys, problem",
    [
        (TRELLO, "random", ["api-key"], "api-key is not a random key"),
        (LEAF, "random", [], "no key to rotate"),
        ("no/such/leaf", "random", ["token"], "no such leaf"),
        (COPY, f"copy:{LEAF}#token", ["token"], "not a kind the rotator"),
        (COPY, "none", ["token"], "not a kind the rotator"),
    ],
)
def test_a_target_the_annotations_do_not_give_is_refused(leaf, kind, keys, problem):
    with pytest.raises(PlanError, match=problem):
        target_of(leaf, kind, keys)


def test_a_key_a_finding_blocks_is_refused():
    store = compliant_store()
    store[TRELLO].meta["interval_bearer-token"] = "soon"
    with pytest.raises(PlanError, match="bearer-token is blocked by a finding"):
        target_of(TRELLO, "random", ["bearer-token"], store)


@pytest.mark.parametrize("key", ["a,b", "a/b"])
def test_a_key_rotator_step_cannot_name_is_refused(key):
    store = compliant_store()
    store[LEAF].keys.add(key)
    with pytest.raises(PlanError, match="cannot be named in rotator_step"):
        target_of(LEAF, "random", [key], store)
