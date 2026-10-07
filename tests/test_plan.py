"""Building a plan (design §4.3): a Target from the entries of the keys it writes, the kind's steps
from the step factory, kv.stamp appended by the core, and step ids stable across rebuilds."""

import pytest
from fixtures import compliant_store, edit
from plans import COPY, LEAF, Confirm, Journal, RandomLike, Tool, plan_of

from secret_rotator.audit import audit
from secret_rotator.contract import Activator
from secret_rotator.kvsteps import KvStamp
from secret_rotator.plan import Activation, Copy, PlanError, build, target

TRELLO = "eso/prd/trello/prd/trello"


def target_of(leaf, kind, keys, store=None):
    store = store or compliant_store()
    return target(leaf, kind, keys, audit(store))


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
    edit(
        store["eso/prd/kc/prd/catalog"].meta,
        "app-token",
        kind=f"copy:{LEAF}#token",
        activate="auto",
    )
    t = target_of(LEAF, "random", ["token"], store)
    assert t.copies == (
        Copy("eso/prd/kc/prd/catalog", "app-token", "token"),
        Copy(COPY, "token", "token"),
    )
    assert target_of(TRELLO, "random", ["bearer-token"]).copies == ()


def test_each_key_carries_its_own_entry_s_args():
    store = compliant_store()
    edit(store[LEAF].meta, "token", args={"length": 20})
    assert target_of(LEAF, "random", ["token"], store).entries["token"].args == {"length": 20}
    edit(store[TRELLO].meta, "api-key", args={"vendor": "trello"})
    t = target_of(TRELLO, "random", ["bearer-token"], store)
    assert list(t.entries) == ["bearer-token"] and t.entries["bearer-token"].args == {}


def test_a_key_s_args_its_kind_refuses_refuse_the_plan_naming_the_entry():
    class Picky(RandomLike):
        def args_problems(self, args):
            return [f"{k}: not one of picky's" for k in args]

    store = compliant_store()
    store[LEAF].keys.add("other")
    edit(store[LEAF].meta, "other", kind="random", activate="none", args={"size": 3})
    edit(store[LEAF].meta, "token", args={"length": 20})
    with pytest.raises(PlanError) as e:
        build(Picky(), target_of(LEAF, "random", ["other", "token"], store))
    assert str(e.value) == (
        f"{LEAF}: rotation_other args: size: not one of picky's; "
        f"rotation_token args: length: not one of picky's"
    )


def test_the_activations_are_the_rotated_keys_entries_then_the_copies_leaf_by_leaf():
    store = compliant_store()
    store[LEAF].keys.add("second")
    edit(store[LEAF].meta, "second", kind="random", activate="eso")
    store[COPY].keys.add("second")
    edit(store[COPY].meta, "second", kind=f"copy:{LEAF}#second", activate="manual:say so")
    store["eso/prd/kc/prd/catalog"].keys.add("app-token")
    edit(
        store["eso/prd/kc/prd/catalog"].meta,
        "app-token",
        kind=f"copy:{LEAF}#token",
        activate="none",
    )
    t = target_of(LEAF, "random", ["second", "token"], store)
    assert t.activations == (
        Activation(LEAF, "second", (Activator("eso"),)),
        Activation(LEAF, "token", (Activator("eso"), Activator("k8s-rollout"))),
        Activation("eso/prd/kc/prd/catalog", "app-token", ()),
        Activation(COPY, "second", (Activator("manual", "say so"),)),
        Activation(COPY, "token", ()),
    )


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
    edit(store[TRELLO].meta, "bearer-token", interval="soon")
    with pytest.raises(PlanError, match="bearer-token is blocked by a finding"):
        target_of(TRELLO, "random", ["bearer-token"], store)


@pytest.mark.parametrize("key", ["a,b", "a/b"])
def test_a_key_named_with_a_comma_or_a_slash_is_planned(key):
    store = compliant_store()
    store[LEAF].keys.add(key)
    edit(store[LEAF].meta, key, kind="random", activate="auto")
    assert target_of(LEAF, "random", [key], store).keys == (key,)
