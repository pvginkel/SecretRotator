"""The kubecoder-client kind (design §6) over the seed's row, Fieldnotes' credential for KubeCoder's
controller, client fieldnotes (049 F1): its args; its plan, which mints the credential with the one
the leaf holds before it writes, since the mint ends that one, then syncs and rolls Fieldnotes out
and proves the new credential; its runs, which leave Fieldnotes holding the credential the
controller takes for client fieldnotes; the failures that stop it before the mint; the failures
after the mint, which no Abort takes back; and the client's answers."""

import dataclasses
import datetime
import io
import json
import urllib.error
from pathlib import Path

import pytest
from fake_cluster import FakeCluster, externalsecret, pod_spec, snapshot, workload
from fake_kubecoder import BASE, CLIENT, CLIENTS, CREDENTIALS, OLD, FakeController, FakeResponse
from fixtures import edit
from plans import NOW, Recorder, client, fake_of, lock, run_state, state_of
from test_activation import ticking

from secret_rotator import annotate as ann
from secret_rotator.audit import audit
from secret_rotator.cluster import Cluster
from secret_rotator.executor import AbortRefused, Executor, Outcome
from secret_rotator.kinds import kubecoder_client
from secret_rotator.kinds.kubecoder_client import KubeCoderClient
from secret_rotator.kinds.kubecoder_client.kubecoder import KubeCoder, KubeCoderError
from secret_rotator.kinds.kubecoder_client.steps import Mint, Prove
from secret_rotator.model import StepFailed, value_name
from secret_rotator.plan import PlanError, make

SEED = ann.load_seed(ann.DEFAULT_SEED)
KIND = "kubecoder-client"
LEAF = "eso/prd/fieldnotes/prd/kubecoder-controller"
KEY = "token"
ES = "fieldnotes-prd/fieldnotes-kubecoder-controller"
POD = "fieldnotes-prd/deployment/fieldnotes"
MINT, PROVE = "kubecoder_client.mint", "kubecoder_client.prove"
PLAN = [MINT, "kv.write", f"eso.sync:{ES}", f"k8s.rollout:{POD}", PROVE, "kv.stamp"]
NO_UNDO = f"KubeCoder's controller ended the credential client {CLIENT} held when it minted"


def seed_store():
    return ann.offline_store(Path(str(ann.DEFAULT_KEYS)), SEED, lambda line: None)


def fieldnotes_objects():
    """(resource, object) of the leaf's readers as prd holds them (2026-10-09): one
    ExternalSecret, and the fieldnotes Deployment, whose API reads its Secret."""
    return [
        (
            "externalsecrets",
            externalsecret(
                "fieldnotes-prd",
                "fieldnotes-kubecoder-controller",
                data=[(LEAF, KEY)],
                target="fieldnotes-kubecoder-controller",
            ),
        ),
        (
            "deployments",
            workload(
                "Deployment",
                "fieldnotes-prd",
                "fieldnotes",
                pod_spec(env=["fieldnotes-kubecoder-controller"]),
            ),
        ),
    ]


class World:
    """The seed's store on the fake OpenBao, the leaf holding client fieldnotes's credential; the
    controller; and Fieldnotes on the fake cluster, which holds the credential KV held when its pod
    last rolled out."""

    def __init__(self, store=None, data=None):
        self.store = store or seed_store()
        self.controller = FakeController()
        self.bao = fake_of(self.store, {LEAF: {KEY: OLD}} | (data or {}))
        self.cluster = FakeCluster(fieldnotes_objects())
        self.pod = OLD
        self.during_rollout = []  # what happens as the pod rolls out
        rolled = self.cluster.roll_out

        def roll_out(resource, obj):
            rolled(resource, obj)
            self.pod = self.bao.data(LEAF)[KEY]
            for event in self.during_rollout:
                event()

        self.cluster.roll_out = roll_out
        self.minted = []  # (what KV holds, the syncs so far, what Fieldnotes holds), at each mint
        self.controller.before_mint = lambda client: self.minted.append(
            (self.held(), self.cluster.syncs, self.pod)
        )
        self.kind = KubeCoderClient(opener=self.controller)

    def held(self):
        return self.bao.data(LEAF)[KEY]

    def plan(self, *, cluster=True):
        return make(
            {KIND: self.kind},
            LEAF,
            KIND,
            [KEY],
            audit(self.store),
            Cluster(self.cluster.kube()) if cluster else None,
        )

    def executor(self, *, day=0):
        self.recorder = Recorder()
        tick = ticking()
        return Executor(
            client(self.bao),
            self.plan(),
            self.recorder,
            lock(self.bao),
            state=run_state(self.bao),
            dry_run=False,
            clock=lambda: tick() + datetime.timedelta(days=day),
        )

    def run(self, *, day=0):
        return self.executor(day=day).run()

    def failure(self):
        (failure,) = self.recorder.failures()
        return failure

    def texts(self):
        """Every detail, error and technical text the run reported."""
        return [
            getattr(e, name, "")
            for e in self.recorder.events
            for name in ("detail", "error", "technical")
        ]


def ids(plan):
    return [s.id for s in plan.steps]


class TestTheSeed:
    def test_the_leaf_names_client_fieldnotes(self):
        entries = audit(seed_store()).entries
        entry = entries[LEAF][KEY]
        assert (entry.kind, entry.args, entry.interval) == (KIND, {"client": CLIENT}, 365)
        assert [s.name for s in entry.activate] == ["eso", "k8s-rollout"]
        assert KubeCoderClient().args_problems(entry.args) == []
        assert [leaf for leaf, by in entries.items() for e in by.values() if e.kind == KIND] == [
            LEAF
        ]

    def test_the_kind_has_no_counterpart_leaf(self):
        assert not any(leaf.startswith("rotator/kubecoder-client") for leaf in seed_store())

    @pytest.mark.parametrize(
        ("args", "problems"),
        [
            ({}, ["client: missing; the KubeCoder client whose credential it is"]),
            (
                {"client": "Fieldnotes"},
                ["client: not a KubeCoder client name as the controller stores it"],
            ),
            ({"client": "-x"}, ["client: not a KubeCoder client name as the controller stores it"]),
            (
                {"client": "a" * 41},
                ["client: not a KubeCoder client name as the controller stores it"],
            ),
            ({"client": 7}, ["client: not a KubeCoder client name as the controller stores it"]),
            ({"client": CLIENT, "url": BASE}, ["url: not one of kubecoder-client's client"]),
        ],
    )
    def test_its_args_name_one_client_in_the_controller_s_spelling(self, args, problems):
        assert KubeCoderClient().args_problems(args) == problems

    @pytest.mark.parametrize("name", ["a" * 40, "fieldnotes", "mac.book_2-x", "0"])
    def test_a_name_the_controller_stores_is_a_client(self, name):
        assert KubeCoderClient().args_problems({"client": name}) == []

    def test_it_reaches_the_controller_at_its_homelab_address(self):
        # KubeCoderDeploy config/prd/values.yaml; prd answered there on 2026-10-09.
        assert kubecoder_client.BASE == BASE


class TestThePlan:
    def test_it_mints_before_it_writes_and_proves_after_fieldnotes_rolls_out(self):
        assert ids(World().plan()) == PLAN

    def test_offline_without_a_snapshot_it_has_no_plan(self):
        with pytest.raises(PlanError, match="offline plan without a snapshot does not reach"):
            World().plan(cluster=False)

    def test_against_a_snapshot_it_plans_without_reaching_the_controller(self, tmp_path):
        path = tmp_path / "snapshot.json"
        path.write_text(json.dumps(snapshot(fieldnotes_objects())))
        world = World()
        plan = make(
            {KIND: world.kind}, LEAF, KIND, [KEY], audit(world.store), Cluster.of_snapshot(path)
        )
        assert ids(plan) == PLAN
        assert world.controller.requests == []

    def test_the_steps_and_the_description(self):
        world = World()
        plan = world.plan()
        mint, prove = plan.steps[0], plan.steps[plan.index(PROVE)]
        assert (mint.mutates, mint.undo, mint.no_undo.startswith(NO_UNDO)) == (True, None, True)
        assert (prove.mutates, prove.silent) == (False, True)
        assert mint.title == f"mint a new credential for KubeCoder client {CLIENT}"
        assert prove.title == "prove the new credential"
        assert not plan.needs_operator and plan.ask == ""
        assert plan.description == (
            f"KubeCoder's controller mints a new credential for client {CLIENT}, asked with the "
            f"one the leaf holds, which ends that one. The tool writes it to the leaf and "
            f"activates what reads it and proves the new one."
        )
        assert world.kind.credential(plan.target) == "KubeCoder client credential"

    def test_a_leaf_that_activates_nothing_is_written_and_proved(self):
        store = seed_store()
        edit(store[LEAF].meta, KEY, activate="none")
        plan = World(store=store).plan()
        assert ids(plan) == [MINT, "kv.write", PROVE, "kv.stamp"]
        assert "The tool writes it to the leaf and proves the new one." in plan.description

    def test_a_plan_of_two_keys_is_refused(self):
        world = World()
        two = dataclasses.replace(world.plan().target, keys=(KEY, "other"))
        with pytest.raises(PlanError, match=f"^{LEAF}: a kubecoder-client plan rotates one key, "):
            world.kind.plan(two, None)


class TestTheRuns:
    def test_a_rotation_leaves_fieldnotes_holding_the_credential_the_controller_takes(self):
        world = World()
        assert world.run() is Outcome.DONE
        new = world.held()
        assert new != OLD and world.pod == new
        assert world.controller.whose(new) == CLIENT
        assert world.controller.whose(OLD) is None
        assert world.controller.credentials == CREDENTIALS | {CLIENT: new}
        assert state_of(world.bao, LEAF).stamps == {KEY: "2026-10-05"}
        assert not any(OLD in text or new in text for text in world.texts())

    def test_the_mint_is_asked_with_the_leaf_s_credential_before_anything_is_written(self):
        world = World()
        assert world.run() is Outcome.DONE
        assert world.controller.mints() == [("POST", CLIENTS, CLIENT)]
        assert world.minted == [(OLD, 0, OLD)]

    def test_the_proof_asks_with_the_new_credential_after_the_rollout(self):
        world = World()
        asked = []
        world.during_rollout.append(lambda: asked.append(len(world.controller.requests)))
        assert world.run() is Outcome.DONE
        (at,) = asked
        assert world.controller.requests[at:] == [("GET", CLIENTS, CLIENT)]

    def test_the_next_rotation_mints_with_the_credential_the_first_minted(self):
        world = World()
        assert world.run() is Outcome.DONE
        first = world.held()
        assert world.run(day=365) is Outcome.DONE
        assert world.minted[1][0] == first
        assert world.held() == world.pod != first
        assert world.controller.whose(world.held()) == CLIENT

    @pytest.mark.parametrize(
        ("held", "dropped", "error"),
        [
            (
                "SECRET-not-the-controller-s",
                (),
                f"KubeCoder's controller refuses the credential {LEAF}#{KEY} holds",
            ),
            ("", (), f"{LEAF}#{KEY} holds no credential"),
            ("SECRET-macbook", (CLIENT,), f"KubeCoder's controller has no client {CLIENT}"),
        ],
    )
    def test_a_failure_before_the_mint_stops_the_plan_before_it_writes(self, held, dropped, error):
        world = World(data={LEAF: {KEY: held}})
        for name in dropped:
            del world.controller.credentials[name]
        before = dict(world.bao.data(LEAF))
        executor = world.executor()
        assert executor.run() is Outcome.FAILED
        assert (world.failure().step.id, world.failure().error) == (MINT, error)
        assert world.bao.data(LEAF) == before and world.controller.mints() == []
        assert executor.abort() is Outcome.CANCELLED

    def test_a_static_client_is_not_minted(self):
        store = seed_store()
        edit(store[LEAF].meta, KEY, args={"client": "bot"})
        world = World(store=store)
        assert world.run() is Outcome.FAILED
        assert world.failure().error == (
            "KubeCoder's client bot is static, not minted: POST /clients mints no credential for it"
        )
        assert world.controller.mints() == []

    def test_a_mint_the_controller_refuses_did_not_land(self):
        world = World()
        world.controller.unreadable = True
        executor = world.executor()
        assert executor.run() is Outcome.FAILED
        assert world.failure().error == (
            f"POST {CLIENTS}: HTTP 409: the minted client store is unreadable"
        )
        assert world.held() == OLD and world.controller.whose(OLD) == CLIENT
        assert executor.abort() is Outcome.CANCELLED

    def test_an_unreachable_controller_stops_the_plan_before_it_writes(self):
        world = World()
        refused = ConnectionRefusedError(111, "Connection refused")
        world.controller.broken["GET", CLIENTS] = refused
        executor = world.executor()
        assert executor.run() is Outcome.FAILED
        assert world.failure().error == (
            f"GET {CLIENTS}: transport error: ConnectionRefusedError(111, 'Connection refused')"
        )
        assert world.held() == OLD
        assert executor.abort() is Outcome.CANCELLED

    def test_a_rollout_that_fails_after_the_mint_cannot_be_aborted_and_a_retry_finishes(self):
        world = World()
        world.cluster.stuck.add("fieldnotes-prd/fieldnotes")
        executor = world.executor()
        assert executor.run() is Outcome.FAILED
        assert world.failure().step.id == f"k8s.rollout:{POD}"
        with pytest.raises(AbortRefused, match=NO_UNDO):
            executor.abort()
        world.cluster.stuck.clear()
        assert world.run() is Outcome.DONE
        assert world.controller.minted == 1
        assert world.pod == world.held() and world.controller.whose(world.held()) == CLIENT

    def test_a_mint_whose_answer_is_lost_cannot_be_aborted_and_the_retry_says_why(self):
        world = World()
        world.controller.lost["POST", CLIENTS] = 502
        executor = world.executor()
        assert executor.run() is Outcome.FAILED
        assert world.failure().error == f"POST {CLIENTS}: HTTP 502"
        with pytest.raises(AbortRefused, match=NO_UNDO):
            executor.abort()
        world.controller.lost.clear()
        assert world.run() is Outcome.FAILED
        assert world.failure().error == (
            f"KubeCoder's controller refuses the credential {LEAF}#{KEY} holds"
        )
        assert world.held() == OLD and world.controller.minted == 1

    def test_a_leaf_holding_another_client_s_credential_stops_after_the_mint(self):
        world = World(data={LEAF: {KEY: "SECRET-macbook"}})
        executor = world.executor()
        assert executor.run() is Outcome.FAILED
        assert world.failure().error == (
            f"KubeCoder's controller still takes the credential {LEAF}#{KEY} holds: it is not "
            f"client {CLIENT}'s, whose credential the mint ended"
        )
        assert world.held() == "SECRET-macbook"
        with pytest.raises(AbortRefused, match=NO_UNDO):
            executor.abort()

    def test_a_credential_re_minted_during_the_rollout_fails_the_proof(self):
        world = World()
        world.during_rollout.append(
            lambda: world.controller.credentials.update({CLIENT: "SECRET-someone-else-s"})
        )
        executor = world.executor()
        assert executor.run() is Outcome.FAILED
        assert (world.failure().step.id, world.failure().error) == (
            PROVE,
            "KubeCoder's controller refuses the new credential",
        )
        with pytest.raises(AbortRefused, match=NO_UNDO):
            executor.abort()


class Ctx:
    def __init__(self, bao, staged=None):
        self.bao = client(bao)
        self.now = NOW
        self.values = dict(staged or {})

    def progress(self, detail):
        pass

    def stage(self, name, value):
        self.values[name] = value

    def staged(self, name):
        return self.values.get(name)


def controller(world):
    return KubeCoder(BASE, world.controller)


class TestTheSteps:
    def test_a_mint_stages_the_new_credential_and_a_re_run_mints_none(self):
        world = World()
        ctx = Ctx(world.bao)
        mint = Mint(controller(world), LEAF, KEY, CLIENT)
        assert mint.run(ctx) == (
            f"client {CLIENT}: minted; the controller refuses the credential it held"
        )
        new = ctx.values[value_name(KEY)]
        assert world.controller.whose(new) == CLIENT
        assert mint.run(ctx).startswith(f"client {CLIENT}: minted")
        assert ctx.values[value_name(KEY)] == new and world.controller.minted == 1

    def test_a_re_run_whose_staged_credential_the_controller_refuses_fails(self):
        world = World()
        ctx = Ctx(world.bao, {value_name(KEY): "SECRET-lost"})
        with pytest.raises(StepFailed) as e:
            Mint(controller(world), LEAF, KEY, CLIENT).run(ctx)
        assert str(e.value) == "KubeCoder's controller refuses the new credential"
        assert e.value.landed is not False
        assert world.controller.minted == 0

    def test_a_failure_before_the_mint_did_not_land_only_on_a_fresh_run(self):
        world = World(data={LEAF: {KEY: ""}})
        with pytest.raises(StepFailed) as e:
            Mint(controller(world), LEAF, KEY, CLIENT).run(Ctx(world.bao))
        assert e.value.landed is False
        with pytest.raises(StepFailed) as e:
            Mint(controller(world), LEAF, KEY, CLIENT).run(
                Ctx(world.bao, {value_name(KEY): "SECRET-staged"})
            )
        assert e.value.landed is not False

    @pytest.mark.parametrize(("status", "landed"), [(409, False), (500, True)])
    def test_a_mint_refused_did_not_land_and_a_server_error_may_have(self, status, landed):
        world = World()
        problem = {"type": "x", "title": "no", "status": status}
        world.controller.refused["POST", CLIENTS] = (status, problem)
        with pytest.raises(StepFailed) as e:
            Mint(controller(world), LEAF, KEY, CLIENT).run(Ctx(world.bao))
        assert str(e.value) == f"POST {CLIENTS}: HTTP {status}: no"
        assert (e.value.landed is not False) is landed

    def test_the_proof_needs_the_leaf_to_hold_the_staged_credential(self):
        world = World()
        prove = Prove(controller(world), LEAF, KEY, CLIENT)
        with pytest.raises(StepFailed, match="^no new credential is staged$"):
            prove.run(Ctx(world.bao))
        with pytest.raises(StepFailed) as e:
            prove.run(Ctx(world.bao, {value_name(KEY): "SECRET-other"}))
        assert str(e.value) == f"{LEAF}#{KEY} does not hold the credential the plan minted"
        assert prove.run(Ctx(world.bao, {value_name(KEY): OLD})) == (
            f"taken; client {CLIENT} is minted"
        )


class TestTheClient:
    def test_it_asks_with_the_bearer_and_sends_the_name_as_json(self):
        world = World()
        kubecoder = controller(world)
        assert kubecoder.clients(OLD) == {
            "bot": "static",
            CLIENT: "minted",
            "macbook": "minted",
            "mcp": "static",
        }
        new = kubecoder.mint("SECRET-macbook", "Fieldnotes ")
        assert world.controller.whose(new) == CLIENT
        assert world.controller.requests == [
            ("GET", CLIENTS, CLIENT),
            ("POST", CLIENTS, "macbook"),
        ]

    def test_a_refused_bearer_is_no_failure_of_takes_and_names_the_title_otherwise(self):
        kubecoder = controller(World())
        assert kubecoder.takes(OLD) is True
        assert kubecoder.takes("SECRET-wrong") is False
        with pytest.raises(KubeCoderError) as e:
            kubecoder.clients("SECRET-wrong")
        assert str(e.value) == f"GET {CLIENTS}: HTTP 401: Missing or invalid API token"
        assert e.value.status == 401

    def test_a_static_name_is_refused_by_its_title(self):
        with pytest.raises(KubeCoderError) as e:
            controller(World()).mint(OLD, "Bot")
        assert str(e.value) == f"POST {CLIENTS}: HTTP 409: that client is chart-provisioned"

    @pytest.mark.parametrize(
        ("raised", "error"),
        [
            (
                urllib.error.HTTPError(BASE, 502, "Bad Gateway", {}, io.BytesIO(b"<html>")),
                f"GET {CLIENTS}: HTTP 502",
            ),
            (
                urllib.error.URLError("Name or service not known"),
                f"GET {CLIENTS}: transport error: Name or service not known",
            ),
        ],
    )
    def test_a_failure_names_the_request(self, raised, error):
        def opener(req):
            raise raised

        with pytest.raises(KubeCoderError) as e:
            KubeCoder(BASE, opener).clients(OLD)
        assert str(e.value) == error

    def test_an_answer_that_is_not_json_is_a_failure(self):
        with pytest.raises(KubeCoderError, match=f"^GET {CLIENTS}: HTTP 200, not a JSON answer$"):
            KubeCoder(BASE, lambda req: FakeResponse(200, b"<html>")).clients(OLD)
