"""The kinds as plugins (design §6): found through their entry points, random and manual with their
args, ask and description, manual one plan per key and on a marker leaf, the operator steps, and
the activation the primary's plan composes for its leaf and its copies' leaves (design §4.3)."""

import datetime
from importlib.metadata import EntryPoint

import pytest
from fixtures import compliant_store
from plans import COPY, LEAF, NOW, Recorder, client, fake_of, lock

from secret_rotator import registry
from secret_rotator.audit import Leaf, audit
from secret_rotator.contract import MARKER_VALUE
from secret_rotator.executor import Abandon, AbortRefused, Executor, Outcome
from secret_rotator.kinds.manual import Manual
from secret_rotator.kinds.random import Random
from secret_rotator.model import StepFailed, value_name
from secret_rotator.opsteps import (
    ConfirmRequest,
    CredentialRequest,
    OperatorConfirm,
    OperatorCredential,
    OperatorShow,
    ShowRequest,
)
from secret_rotator.plan import PlanError, make, of_leaf

KINDS = registry.load()
TRELLO = "eso/prd/trello/prd/trello"  # manual api-key and token (never), random bearer-token
WIFI = "shared/wifi"  # manual password, never, activate none
SEAL = "rotator/bootstrap/seal-key"


def store_of(**activate):
    """The compliant store with rotation_activate set per leaf (leaf path with / as __)."""
    store = compliant_store()
    for name, value in activate.items():
        store[name.replace("__", "/")].meta["rotation_activate"] = value
    store[SEAL] = Leaf(
        SEAL,
        {"seal-key"},
        {
            "rotation_mechanism": "manual",
            "rotation_interval": "never",
            "rotation_activate": "none",
            "notes": "the bootstrap tier, rotated by hand at its source",
        },
    )
    return store


ACTIVATE_NONE = {"eso__prd__app__prd__token": "none", "eso__prd__trello__prd__trello": "none"}


def plan_for(leaf, kind, keys, store=None):
    store = store or store_of(**ACTIVATE_NONE)
    return make(KINDS, leaf, kind, list(keys), store, audit(store))


def run(store, plan, *answers, data=None):
    bao = fake_of(store, data)
    r = Recorder(*answers)
    outcome = Executor(client(bao), plan, r, lock(bao), dry_run=False, clock=lambda: NOW).run()
    return bao, outcome, r


class TestTheRegistry:
    def test_it_finds_the_kinds_this_distribution_ships_through_their_entry_points(self):
        assert set(KINDS) == {"random", "manual"}
        assert isinstance(KINDS["random"], Random) and isinstance(KINDS["manual"], Manual)

    @pytest.mark.parametrize(
        ("found", "problem"),
        [
            ([("keycloak", "secret_rotator.kinds.random:Random")], "not a kind of design §6"),
            ([("approle", "secret_rotator.kinds.random:Random")], "registered as approle"),
            (
                [
                    ("random", "secret_rotator.kinds.random:Random"),
                    ("random", "secret_rotator.kinds.random:Random"),
                ],
                "has a plugin already",
            ),
        ],
    )
    def test_a_plugin_that_is_not_one_kind_of_design_6_is_refused(self, found, problem):
        eps = [EntryPoint(name, value, registry.GROUP) for name, value in found]
        with pytest.raises(registry.RegistryError, match=problem):
            registry.load(eps)

    def test_a_kind_without_a_plugin_has_no_plan(self):
        store = compliant_store()
        with pytest.raises(PlanError, match="keycloak-client is not a kind this install has"):
            make(
                KINDS,
                "eso/prd/app/prd/oidc",
                "keycloak-client",
                ["client_secret"],
                store,
                audit(store),
            )


class TestRandom:
    def test_its_plan_generates_writes_copies_and_stamps(self):
        plan = plan_for(LEAF, "random", ["token"])
        assert [s.id for s in plan.steps] == [
            "random.generate:token",
            "kv.write",
            f"kv.copy:{COPY}#token",
            "kv.stamp",
        ]
        assert not plan.needs_operator and plan.ask == ""
        assert plan.description == (
            "The tool generates a new 43-character token and writes it to the leaf and its 1 copy."
        )

    def test_rotation_args_set_length_and_charset(self):
        store = store_of(**ACTIVATE_NONE)
        store[LEAF].meta["rotation_args"] = '{"length":20,"charset":"abc"}'
        bao, outcome, _ = run(store, plan_for(LEAF, "random", ["token"], store))
        assert outcome is Outcome.DONE
        new = bao.data(LEAF)["token"]
        assert len(new) == 20 and set(new) <= set("abc") and bao.data(COPY) == {"token": new}

    @pytest.mark.parametrize(
        ("args", "problem"),
        [
            ('{"size":20}', "size: not one of random's length, charset"),
            ('{"length":0}', "length: not a whole number from 1"),
            ('{"length":"20"}', "length: not a whole number from 1"),
            ('{"charset":"aa"}', "charset: not two or more distinct characters"),
        ],
    )
    def test_rotation_args_it_cannot_use_refuse_the_plan(self, args, problem):
        store = store_of(**ACTIVATE_NONE)
        store[LEAF].meta["rotation_args"] = args
        with pytest.raises(PlanError, match=f"rotation_args: {problem}"):
            plan_for(LEAF, "random", ["token"], store)


class TestManual:
    def test_a_leaf_with_several_manual_keys_has_one_plan_per_key(self):
        store = store_of(**ACTIVATE_NONE)
        plans, _ = of_leaf(TRELLO, store, audit(store), KINDS)
        assert [(p.kind, p.keys) for p in plans] == [
            ("random", ("bearer-token",)),
            ("manual", ("api-key",)),
            ("manual", ("token",)),
        ]

    def test_its_plan_asks_for_the_credential_writes_that_key_alone_and_stamps_it(self):
        store = store_of(**ACTIVATE_NONE)
        plan = plan_for(TRELLO, "manual", ["token"], store)
        assert [s.id for s in plan.steps] == ["operator.credential:token", "kv.write", "kv.stamp"]
        assert plan.needs_operator and plan.ask == "paste a new token"
        bao, outcome, r = run(store, plan, {"token": "NEW-trello-token"})
        assert outcome is Outcome.DONE
        ((_, request),) = r.asked
        assert isinstance(request, CredentialRequest)
        assert [f.key for f in request.fields] == ["token"] and request.fields[0].shape is None
        assert "Notes: api-key and token cannot be rotated" in request.instruction
        data = bao.data(TRELLO)
        assert data["token"] == "NEW-trello-token"
        assert data["api-key"] == f"SECRET-{TRELLO}-api-key"
        assert data["bearer-token"] == f"SECRET-{TRELLO}-bearer-token"
        (body,) = [b for m, p, _, b, _ in bao.requests if (m, p) == ("PATCH", f"kv/data/{TRELLO}")]
        assert body["data"] == {"token": "NEW-trello-token"}
        meta = bao.meta(TRELLO)
        assert meta["rotated_at_token"] == "2026-10-05"
        assert "rotated_at_api-key" not in meta and "rotated_at_bearer-token" not in meta

    def test_rotation_args_describe_the_credential_and_its_shape(self):
        store = store_of(**ACTIVATE_NONE)
        store[WIFI].meta["rotation_args"] = (
            '{"what":"Wi-Fi PSK","mint":"UniFi → WiFi → Password","prefix":"psk-"}'
        )
        plan = plan_for(WIFI, "manual", ["password"], store)
        credential = plan.steps[0]
        assert credential.title == "Mint a new Wi-Fi PSK and enter it"
        assert credential.instruction.startswith("UniFi → WiFi → Password")
        assert credential.shape.words == "starts with psk-"
        assert credential.shape.test("psk-1") and not credential.shape.test("1")
        assert plan.ask == "paste a new Wi-Fi PSK"

    @pytest.mark.parametrize(
        ("args", "problem"),
        [('{"vendor":"x"}', "vendor: not one of manual's"), ('{"what":""}', "what: not a text")],
    )
    def test_rotation_args_it_does_not_know_refuse_the_plan(self, args, problem):
        store = store_of(**ACTIVATE_NONE)
        store[WIFI].meta["rotation_args"] = args
        with pytest.raises(PlanError, match=problem):
            plan_for(WIFI, "manual", ["password"], store)

    def test_on_a_marker_leaf_it_confirms_at_the_source_and_rewrites_the_marker_key(self):
        store = store_of()
        plan = plan_for(SEAL, "manual", ["seal-key"], store)
        assert [s.id for s in plan.steps] == [
            "manual.marker:seal-key",
            "operator.confirm:source",
            "kv.write",
            "kv.stamp",
        ]
        assert plan.ask == "rotate it at its source"
        bao, outcome, r = run(store, plan, {}, data={SEAL: {"seal-key": MARKER_VALUE}})
        assert outcome is Outcome.DONE
        ((_, request),) = r.asked
        assert isinstance(request, ConfirmRequest)
        assert request.title == "Rotate seal-key at its source"
        assert request.instruction == "the bootstrap tier, rotated by hand at its source"
        assert bao.data(SEAL) == {"seal-key": f"{MARKER_VALUE}; rotated 2026-10-05T04:30:00+00:00"}
        assert bao.version(SEAL) == 2 and bao.meta(SEAL)["rotated_at_seal-key"] == "2026-10-05"

    def test_once_confirmed_at_the_source_a_marker_rotation_cannot_be_aborted(self):
        store = store_of()
        bao = fake_of(store, {SEAL: {"seal-key": MARKER_VALUE}})
        bao.refuse["PATCH", f"kv/data/{SEAL}"] = 403
        plan = plan_for(SEAL, "manual", ["seal-key"], store)
        e = Executor(client(bao), plan, Recorder({}), lock(bao), dry_run=False, clock=lambda: NOW)
        assert e.run() is Outcome.FAILED
        with pytest.raises(AbortRefused, match="seal-key was rotated at its source"):
            e.abort()

    def test_a_marker_leaf_that_holds_a_credential_is_not_overwritten(self):
        store = store_of()
        plan = plan_for(SEAL, "manual", ["seal-key"], store)
        bao, outcome, r = run(store, plan, data={SEAL: {"seal-key": "SECRET-the-seal-key"}})
        assert outcome is Outcome.FAILED and r.asked == []
        assert "does not hold the marker text" in r.failures()[0].error
        assert bao.data(SEAL) == {"seal-key": "SECRET-the-seal-key"}


class TestActivation:
    def test_none_activates_nothing(self):
        plan = plan_for(WIFI, "manual", ["password"])
        assert [s.type for s in plan.steps] == ["operator.credential", "kv.write", "kv.stamp"]

    def test_manual_text_becomes_a_confirm_for_the_primary_s_leaf_then_each_copy_s(self):
        store = store_of(
            eso__prd__app__prd__token="manual:restart the app by hand",
            iac__copy="manual:tell the copy's reader",
        )
        plan = plan_for(LEAF, "random", ["token"], store)
        confirms = [s for s in plan.steps if isinstance(s, OperatorConfirm)]
        assert [(s.id, s.title, s.instruction) for s in confirms] == [
            (f"operator.confirm:{LEAF}:1", "restart the app by hand", f"for {LEAF}"),
            (f"operator.confirm:{COPY}:1", "tell the copy's reader", f"for {COPY}"),
        ]
        assert [s.type for s in plan.steps][-3:] == ["operator.confirm"] * 2 + ["kv.stamp"]
        assert plan.ask == "restart the app by hand; tell the copy's reader"
        assert all(s.mutates and s.activator and s.undo is None for s in confirms)

    @pytest.mark.parametrize(
        ("activate", "problem"),
        [
            (
                {"eso__prd__app__prd__token": "none", "iac__copy": "github-webhook:pvginkel/X/7"},
                f"{LEAF}: {COPY}'s rotation_activate github-webhook:pvginkel/X/7: no step",
            ),
            (
                {"eso__prd__app__prd__token": "argocd-sync:app-prd"},
                f"{LEAF}: rotation_activate argocd-sync:app-prd: no step",
            ),
        ],
    )
    def test_a_spec_no_step_is_built_for_refuses_the_plan(self, activate, problem):
        with pytest.raises(PlanError, match=problem):
            plan_for(LEAF, "random", ["token"], store_of(**activate))


class TestOperatorSteps:
    class Ctx:
        def __init__(self, answer=None, staged=None):
            self.answer = answer
            self.values = dict(staged or {})
            self.asked = []

        def ask(self, request):
            self.asked.append(request)
            return self.answer

        def stage(self, name, value):
            self.values[name] = value

        def staged(self, name):
            return self.values.get(name)

    def test_a_credential_stages_each_value_and_keeps_one_entered_before(self):
        step = OperatorCredential(("a", "b"), "enter them", "at the vendor")
        ctx = self.Ctx({"a": "SECRET-a", "b": "SECRET-bb"})
        assert step.run(ctx) == "a: 8 characters, b: 9 characters"
        assert ctx.values == {value_name("a"): "SECRET-a", value_name("b"): "SECRET-bb"}
        assert step.run(ctx) == "entered before" and len(ctx.asked) == 1

    def test_a_credential_answered_without_a_value_fails(self):
        with pytest.raises(StepFailed, match="no value was entered for b"):
            OperatorCredential(("a", "b"), "t", "i").run(self.Ctx({"a": "x", "b": ""}))

    def test_show_hands_the_staged_value_to_the_renderer_and_never_prints_it(self):
        ctx = self.Ctx({}, {"minted": "SECRET-minted"})
        assert OperatorShow("minted", "store it", "in RoboForm").run(ctx) == "done"
        (request,) = ctx.asked
        assert isinstance(request, ShowRequest) and request.value == "SECRET-minted"
        assert "SECRET" not in repr(request)
        with pytest.raises(StepFailed, match="no value is staged as other"):
            OperatorShow("other", "t", "i").run(ctx)

    def test_an_irreversible_confirm_mutates_and_has_no_undo(self):
        plain = OperatorConfirm("x", "do it")
        assert not plain.mutates and plain.undo is None
        revoke = OperatorConfirm("revoke", "revoke the old one", irreversible="it is revoked")
        assert revoke.mutates and revoke.no_undo == "it is revoked" and revoke.undo is None

    def test_the_operator_s_exit_at_a_credential_stages_nothing(self):
        store = store_of(**ACTIVATE_NONE)
        bao, outcome, _ = run(store, plan_for(WIFI, "manual", ["password"], store), Abandon.EXIT)
        assert outcome is Outcome.EXITED
        assert "rotator/staging/manual/shared/wifi" not in bao.leaves


class TestTheLeafsPlans:
    def test_every_other_key_says_why_it_has_none(self):
        store = store_of(**ACTIVATE_NONE)
        store["eso/prd/kc/prd/catalog"].meta["key_jenkins-user"] = "kubecoder-client"
        store["eso/prd/kc/prd/catalog"].meta["rotation_interval"] = "14d"
        _, unplanned = of_leaf("eso/prd/kc/prd/catalog", store, audit(store), KINDS)
        assert unplanned == {
            "client-id": "a copy of eso/prd/app/prd/oidc#client_id, written by its primary's plan",
            "client-secret": (
                "a copy of eso/prd/app/prd/oidc#client_secret, written by its primary's plan"
            ),
            "jenkins-user": "kubecoder-client: a kind this install has no plugin for yet",
        }
        _, unplanned = of_leaf("eso/prd/bot/prd/config", store, audit(store), KINDS)
        assert unplanned["telegram-chat-id"] == "none: not a secret, never rotated"

    def test_a_blocked_key_says_what_blocks_it(self):
        store = store_of(**ACTIVATE_NONE)
        store[TRELLO].meta["interval_bearer-token"] = "soon"
        plans, unplanned = of_leaf(TRELLO, store, audit(store), KINDS)
        assert unplanned == {
            "bearer-token": "blocked: interval_bearer-token: 'soon' is not <n>d or never"
        }
        assert [p.keys for p in plans] == [("api-key",), ("token",)]

    def test_a_key_blocked_by_a_leaf_it_is_copied_into_says_so(self):
        store = store_of(**ACTIVATE_NONE)
        del store[COPY].meta["rotation_activate"]
        _, unplanned = of_leaf(LEAF, store, audit(store), KINDS)
        assert unplanned == {"token": "blocked: a leaf it is copied into has a finding"}

    def test_due_plans_come_first_and_one_that_cannot_be_built_says_why(self):
        store = store_of()
        store[TRELLO].meta["rotated_at_bearer-token"] = "2026-09-30"
        plans, _ = of_leaf(TRELLO, store, audit(store), KINDS)
        assert [(p.keys, p.due_at) for p in plans] == [
            (("bearer-token",), datetime.date(2026, 10, 14)),
            (("api-key",), None),
            (("token",), None),
        ]
        offline = "its activation is read from the cluster, which an offline plan does not reach"
        assert all(p.plan is None and offline in p.error for p in plans)
