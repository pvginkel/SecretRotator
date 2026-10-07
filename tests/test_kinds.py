"""The kinds as plugins (design §6): found through their entry points, random and manual with their
args, ask and description, manual one plan per key and on a marker leaf, the operator steps, and
the activation the primary's plan composes for its leaf and its copies' leaves (design §4.3)."""

import datetime
import re
from importlib.metadata import EntryPoint

import pytest
from fixtures import annotated, compliant_store, edit, fields_of, set_activate
from plans import COPY, LEAF, NOW, Recorder, client, fake_of, lock, run_state, state_of

from secret_rotator import registry
from secret_rotator.audit import Leaf, audit
from secret_rotator.contract import MARKER_VALUE
from secret_rotator.executor import Abandon, AbortRefused, Executor, Outcome
from secret_rotator.kinds.approle import AppRole
from secret_rotator.kinds.manual import TYPES, Manual, load_type
from secret_rotator.kinds.random import Random
from secret_rotator.model import StepFailed, expiry_name, value_name
from secret_rotator.opsteps import (
    EXPIRY,
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
    """The compliant store with the activate of every entry of a leaf set (leaf path with / as
    __)."""
    store = compliant_store()
    for name, value in activate.items():
        set_activate(store[name.replace("__", "/")].meta, value)
    store[SEAL] = Leaf(
        SEAL,
        {"seal-key"},
        annotated(
            {
                "seal-key": {
                    "kind": "manual",
                    "interval": "never",
                    "activate": "none",
                    "notes": "the bootstrap tier, rotated by hand at its source",
                }
            }
        ),
    )
    return store


ACTIVATE_NONE = {"eso__prd__app__prd__token": "none", "eso__prd__trello__prd__trello": "none"}


def plan_for(leaf, kind, keys, store=None):
    store = store or store_of(**ACTIVATE_NONE)
    return make(KINDS, leaf, kind, list(keys), audit(store))


def run(store, plan, *answers, data=None):
    bao = fake_of(store, data)
    r = Recorder(*answers)
    outcome = Executor(
        client(bao), plan, r, lock(bao), state=run_state(bao), dry_run=False, clock=lambda: NOW
    ).run()
    return bao, outcome, r


class TestTheRegistry:
    def test_it_finds_the_kinds_this_distribution_ships_through_their_entry_points(self):
        assert set(KINDS) == {"random", "manual", "approle"}
        assert isinstance(KINDS["random"], Random) and isinstance(KINDS["manual"], Manual)
        assert isinstance(KINDS["approle"], AppRole)

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

    def test_a_key_s_args_set_its_length_and_charset(self):
        store = store_of(**ACTIVATE_NONE)
        edit(store[LEAF].meta, "token", args={"length": 20, "charset": "abc"})
        bao, outcome, _ = run(store, plan_for(LEAF, "random", ["token"], store))
        assert outcome is Outcome.DONE
        new = bao.data(LEAF)["token"]
        assert len(new) == 20 and set(new) <= set("abc") and bao.data(COPY) == {"token": new}

    def test_each_key_is_generated_to_its_own_args(self):
        store = store_of(**ACTIVATE_NONE)
        store[LEAF].keys.add("pin")
        edit(
            store[LEAF].meta,
            "pin",
            kind="random",
            activate="none",
            args={"length": 6, "charset": "0123456789"},
        )
        plan = plan_for(LEAF, "random", ["pin", "token"], store)
        assert plan.description == (
            "The tool generates a new 6-character pin and a new 43-character token and writes "
            "it to the leaf and its 1 copy."
        )
        bao, outcome, _ = run(store, plan, data={LEAF: {"pin": "000000", "token": "old"}})
        assert outcome is Outcome.DONE
        pin, token = bao.data(LEAF)["pin"], bao.data(LEAF)["token"]
        assert len(pin) == 6 and pin.isdigit() and len(token) == 43

    @pytest.mark.parametrize(
        ("args", "problem"),
        [
            ({"size": 20}, "size: not one of random's length, charset"),
            ({"length": 0}, "length: not a whole number from 1"),
            ({"length": "20"}, "length: not a whole number from 1"),
            ({"charset": "aa"}, "charset: not two or more distinct characters"),
        ],
    )
    def test_args_it_cannot_use_refuse_the_plan(self, args, problem):
        store = store_of(**ACTIVATE_NONE)
        edit(store[LEAF].meta, "token", args=args)
        with pytest.raises(PlanError, match=f"rotation_token args: {problem}"):
            plan_for(LEAF, "random", ["token"], store)


class TestCredentialTypes:
    """manual's documents (design §6, ruling 054 D1): one per credential type of the catalog."""

    def test_the_plugin_documents_the_catalogs_ten_types(self):
        assert set(TYPES) == {
            "argocd-token",
            "github-pat",
            "grafana-api-key",
            "jenkins-basic-auth",
            "mouser-api-key",
            "openai-api-key",
            "ssh-private-key",
            "telegram-bot-token",
            "torguard-wireguard",
            "tvdb-api-key",
        }

    def test_every_document_says_what_the_credential_is_how_to_mint_it_and_its_shape(self):
        for name, doc in TYPES.items():
            assert doc.credential.strip() and doc.shape.words.strip(), name
            assert doc.instructions.strip() and not doc.instructions.startswith("---"), name

    def test_the_types_that_carry_an_expiry(self):
        assert {name for name, doc in TYPES.items() if doc.expires} == {
            "argocd-token",
            "github-pat",
            "grafana-api-key",
        }

    @pytest.mark.parametrize(
        ("name", "matches", "mismatches"),
        [
            ("github-pat", ["ghp_" + "a1" * 18, "github_pat_11AAB_x9"], ["gho_abc", "token"]),
            ("telegram-bot-token", ["123456789:AAH-x_9"], ["AAH-x_9", "123456789"]),
            ("openai-api-key", ["sk-proj-abc_9", "sk-abc"], ["pk-abc", "sk-"]),
            (
                "mouser-api-key",
                ["0f8fad5b-d9cb-469f-a165-70867728950e"],
                ["0f8fad5bd9cb469fa16570867728950e", "key"],
            ),
            ("jenkins-basic-auth", ["Basic dXNlcjp0b2tlbg=="], ["dXNlcjp0b2tlbg==", "Bearer x"]),
            (
                "tvdb-api-key",
                ["0F8FAD5B-D9CB-469F-A165-70867728950E"],
                ["0f8fad5b-d9cb-469f-a165", "key"],
            ),
            (
                "torguard-wireguard",
                ["[Interface]\nPrivateKey = x=\nAddress = 10.0.0.2/32\n\n[Peer]\nPublicKey = y=\n"],
                ["PrivateKey = x=", "[Peer]\n[Interface]\nPrivateKey = x="],
            ),
            ("argocd-token", ["eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ4In0.c2ln"], ["eyJhbGc", "a.b.c"]),
            ("grafana-api-key", ["glsa_AbC123_0a1b2c3d"], ["eyJrIjoi", "glsa"]),
            (
                "ssh-private-key",
                [
                    "-----BEGIN OPENSSH PRIVATE KEY-----\nb3\n-----END OPENSSH PRIVATE KEY-----\n",
                    "-----BEGIN PRIVATE KEY-----\nMIIE\n-----END PRIVATE KEY-----",
                ],
                ["ssh-ed25519 AAAAC3 pve-root", "-----BEGIN OPENSSH PRIVATE KEY-----\nb3Bl"],
            ),
        ],
    )
    def test_each_shape_tells_a_pasted_value_of_the_type_from_another(
        self, name, matches, mismatches
    ):
        shape = TYPES[name].shape
        assert [shape.test(v) for v in matches] == [True] * len(matches)
        assert [shape.test(v) for v in mismatches] == [False] * len(mismatches)

    def test_the_live_mint_facts_are_in_their_types_documents(self):
        assert "TorGuard control panel" in TYPES["torguard-wireguard"].instructions
        assert "TheTVDB's dashboard" in TYPES["tvdb-api-key"].instructions

    def test_a_document_is_its_front_matter_then_its_instructions(self):
        doc = load_type(
            "---\ncredential: Wi-Fi PSK\nshape:\n  words: starts with psk-\n  pattern: psk-.+\n"
            "expires: false\n---\nUniFi → WiFi → Password.\n\n---\nNot front matter.\n"
        )
        assert doc.credential == "Wi-Fi PSK" and doc.expires is False
        assert doc.instructions == "UniFi → WiFi → Password.\n\n---\nNot front matter."
        assert doc.shape.words == "starts with psk-"
        assert doc.shape.test("psk-1\n2") and not doc.shape.test("1psk-")


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
        assert not request.expires
        assert request.instruction == (
            f"Mint a new token for {TRELLO} where it is issued.\n\nNotes: cannot be rotated"
        )
        data = bao.data(TRELLO)
        assert data["token"] == "NEW-trello-token"
        assert data["api-key"] == f"SECRET-{TRELLO}-api-key"
        assert data["bearer-token"] == f"SECRET-{TRELLO}-bearer-token"
        (body,) = [b for m, p, _, b, _ in bao.requests if (m, p) == ("PATCH", f"kv/data/{TRELLO}")]
        assert body["data"] == {"token": "NEW-trello-token"}
        assert state_of(bao, TRELLO).stamps == {"token": "2026-10-05"}

    def test_a_type_shows_its_documents_instructions_with_the_key_s_notes_below(self):
        store = store_of(**ACTIVATE_NONE)
        edit(store[WIFI].meta, "password", args={"type": "github-pat"})
        plan = plan_for(WIFI, "manual", ["password"], store)
        credential = plan.steps[0]
        github = TYPES["github-pat"]
        assert credential.title == "Mint a new GitHub personal access token and enter it"
        assert credential.instruction == f"{github.instructions}\n\nNotes: PSK in every device"
        assert credential.shape is github.shape
        assert plan.ask == "paste a new GitHub personal access token"
        assert plan.description.startswith("You mint a new GitHub personal access token and enter")

    def test_a_type_that_expires_asks_the_expiry_with_the_value_and_the_stamp_writes_it(self):
        store = store_of(**ACTIVATE_NONE)
        edit(store[WIFI].meta, "password", args={"type": "github-pat"}, expires_at="2026-10-01")
        plan = plan_for(WIFI, "manual", ["password"], store)
        answer = {"password": "github_pat_SECRET", EXPIRY: "2027-01-04"}
        bao, outcome, r = run(store, plan, answer)
        assert outcome is Outcome.DONE
        ((_, request),) = r.asked
        assert request.expires
        assert fields_of(bao.meta(WIFI), "password")["expires_at"] == "2027-01-04"

    def test_a_blank_expiry_leaves_the_key_without_one(self):
        store = store_of(**ACTIVATE_NONE)
        edit(store[WIFI].meta, "password", args={"type": "github-pat"}, expires_at="2026-10-01")
        plan = plan_for(WIFI, "manual", ["password"], store)
        bao, outcome, _ = run(store, plan, {"password": "github_pat_SECRET", EXPIRY: ""})
        assert outcome is Outcome.DONE
        assert "expires_at" not in fields_of(bao.meta(WIFI), "password")

    def test_a_type_that_does_not_expire_asks_no_expiry_and_its_rotation_clears_one(self):
        store = store_of(**ACTIVATE_NONE)
        edit(store[WIFI].meta, "password", args={"type": "openai-api-key"}, expires_at="2027-01-01")
        plan = plan_for(WIFI, "manual", ["password"], store)
        bao, outcome, r = run(store, plan, {"password": "sk-SECRET"})
        assert outcome is Outcome.DONE
        ((_, request),) = r.asked
        assert not request.expires
        assert "expires_at" not in fields_of(bao.meta(WIFI), "password")

    def test_every_key_of_a_type_shows_the_same_instructions_untemplated(self):
        store = store_of(**ACTIVATE_NONE)
        for key in ("api-key", "token"):
            edit(store[TRELLO].meta, key, args={"type": "openai-api-key"}, notes=f"{key} notes")
        steps = [plan_for(TRELLO, "manual", [k], store).steps[0] for k in ("api-key", "token")]
        assert [s.instruction for s in steps] == [
            f"{TYPES['openai-api-key'].instructions}\n\nNotes: {key} notes"
            for key in ("api-key", "token")
        ]

    def test_without_notes_the_instructions_stand_alone(self):
        store = store_of(**ACTIVATE_NONE)
        edit(
            store[TRELLO].meta,
            "token",
            args={"type": "openai-api-key"},
            interval="365d",
            notes=None,
        )
        credential = plan_for(TRELLO, "manual", ["token"], store).steps[0]
        assert credential.instruction == TYPES["openai-api-key"].instructions

    @pytest.mark.parametrize(
        ("args", "problem"),
        [
            ({"vendor": "x"}, "vendor: not manual's; its one arg is type"),
            ({"type": "github-pat", "prefix": "ghp_"}, "prefix: not manual's; its one arg is type"),
            ({"what": "PSK"}, "what: not manual's; its one arg is type"),
            ({"type": "fax"}, "type: 'fax' is not a credential type manual documents"),
            ({"type": ["github-pat"]}, "type: ['github-pat'] is not a credential type"),
        ],
    )
    def test_args_it_does_not_know_refuse_the_plan(self, args, problem):
        store = store_of(**ACTIVATE_NONE)
        edit(store[WIFI].meta, "password", args=args)
        with pytest.raises(PlanError, match=re.escape(f"rotation_password args: {problem}")):
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
        assert bao.version(SEAL) == 2
        assert state_of(bao, SEAL).stamps == {"seal-key": "2026-10-05"}

    def test_once_confirmed_at_the_source_a_marker_rotation_cannot_be_aborted(self):
        store = store_of()
        bao = fake_of(store, {SEAL: {"seal-key": MARKER_VALUE}})
        bao.refuse["PATCH", f"kv/data/{SEAL}"] = 403
        plan = plan_for(SEAL, "manual", ["seal-key"], store)
        e = Executor(
            client(bao),
            plan,
            Recorder({}),
            lock(bao),
            state=run_state(bao),
            dry_run=False,
            clock=lambda: NOW,
        )
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
                f"{LEAF}: {COPY}'s rotation_token activate github-webhook:pvginkel/X/7: no step",
            ),
            (
                {"eso__prd__app__prd__token": "argocd-sync:app-prd"},
                f"{LEAF}: rotation_token activate argocd-sync:app-prd: no step",
            ),
        ],
    )
    def test_a_spec_no_step_is_built_for_refuses_the_plan(self, activate, problem):
        with pytest.raises(PlanError, match=problem):
            plan_for(LEAF, "random", ["token"], store_of(**activate))


class TestOperatorSteps:
    class Ctx:
        now = NOW

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

    def test_a_credential_that_expires_stages_its_expiry_for_each_key_before_the_values(self):
        step = OperatorCredential(("a", "b"), "t", "i", expires=True)
        ctx = self.Ctx({"a": "SECRET-a", "b": "SECRET-bb", EXPIRY: "2027-01-04"})
        assert step.run(ctx) == "a: 8 characters, b: 9 characters; expires 2027-01-04"
        assert ctx.asked[0].expires
        assert ctx.values == {
            expiry_name("a"): "2027-01-04",
            expiry_name("b"): "2027-01-04",
            value_name("a"): "SECRET-a",
            value_name("b"): "SECRET-bb",
        }
        assert list(ctx.values)[:2] == [expiry_name("a"), expiry_name("b")]

    def test_a_blank_expiry_is_staged_as_none(self):
        ctx = self.Ctx({"a": "SECRET-a", EXPIRY: ""})
        assert OperatorCredential(("a",), "t", "i", expires=True).run(ctx).endswith("; no expiry")
        assert ctx.values[expiry_name("a")] == ""

    @pytest.mark.parametrize(
        "expiry, problem",
        [
            ("soon", "'soon' is not an ISO date"),
            ("2026-10-05", "2026-10-05 is not after today, 2026-10-05"),
        ],
    )
    def test_an_expiry_that_is_no_date_after_today_fails_and_stages_nothing(self, expiry, problem):
        ctx = self.Ctx({"a": "SECRET-a", EXPIRY: expiry})
        with pytest.raises(StepFailed, match=f"the expiry entered is no expiry: {problem}"):
            OperatorCredential(("a",), "t", "i", expires=True).run(ctx)
        assert ctx.values == {}

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
        # The staging leaf holds the plan's record, in flight at the credential, and no value.
        assert set(bao.data("rotator/staging/manual/shared/wifi")) == {"keys", "step", "derived"}


class TestTheLeafsPlans:
    def test_every_other_key_says_why_it_has_none(self):
        store = store_of(**ACTIVATE_NONE)
        edit(
            store["eso/prd/kc/prd/catalog"].meta,
            "jenkins-user",
            kind="kubecoder-client",
            interval="14d",
            activate="none",
        )
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
        edit(store[TRELLO].meta, "bearer-token", interval="soon")
        plans, unplanned = of_leaf(TRELLO, store, audit(store), KINDS)
        assert unplanned == {
            "bearer-token": "blocked: rotation_bearer-token: interval: 'soon' is not <n>d or never"
        }
        assert [p.keys for p in plans] == [("api-key",), ("token",)]

    def test_a_key_blocked_by_its_copy_s_entry_says_so(self):
        store = store_of(**ACTIVATE_NONE)
        edit(store[COPY].meta, "token", activate=None)
        _, unplanned = of_leaf(LEAF, store, audit(store), KINDS)
        assert unplanned == {"token": "blocked: a leaf it is copied into has a finding"}

    def test_due_plans_come_first_and_one_that_cannot_be_built_says_why(self):
        store = store_of()
        store[TRELLO].state.stamps["bearer-token"] = "2026-09-30"
        plans, _ = of_leaf(TRELLO, store, audit(store), KINDS)
        assert [(p.keys, p.due_at) for p in plans] == [
            (("bearer-token",), datetime.date(2026, 10, 14)),
            (("api-key",), None),
            (("token",), None),
        ]
        offline = "its activation is read from the cluster, which an offline plan does not reach"
        assert all(p.plan is None and offline in p.error for p in plans)
