"""The k8s-sa-token kind (design §6) over the seed's keys on prd: the KubeCoder catalog's
kubeconfig-prd-write, prd's kubecoder-rw, and the rotator's own token iac/rotator-k8s-token,
secret-rotator's. Its args; its plan, one per key, which mints a successor token Secret before it
writes, syncs and rolls out both stages' controllers through the dev bag's copy, proves the new
token and deletes the old one last; its runs, which leave every reader holding a token prd takes
and the old one ended, the rotator's own run calling prd with the new token from its delete on;
the failures that stop it before the mint; its rollbacks; and the tokens a key holds."""

import dataclasses
import datetime
import json
import re
from pathlib import Path

import pytest
import yaml
from fake_cluster import (
    ADDR,
    ROTATOR,
    SA_NAME,
    SA_TOKEN,
    FakeCluster,
    b64url,
    externalsecret,
    legacy_token,
    pod_spec,
    snapshot,
    token_in,
    token_secret,
    workload,
)
from plans import NOW, Recorder, client, fake_of, lock, run_state, state_of
from test_activation import ticking

from secret_rotator import annotate as ann
from secret_rotator.audit import audit
from secret_rotator.cluster import Cluster, Workload
from secret_rotator.executor import AbortRefused, Executor, Outcome
from secret_rotator.kinds.k8s_sa_token import K8sSaToken
from secret_rotator.kinds.k8s_sa_token.steps import RECORD, Delete, Mint, Prove, Record
from secret_rotator.kinds.k8s_sa_token.tokens import (
    SUFFIX,
    claims,
    replaced,
    successor,
    token_of,
)
from secret_rotator.kube import Kube
from secret_rotator.model import StepFailed, value_name
from secret_rotator.plan import PlanError, make, of_leaf

SEED = ann.load_seed(ann.DEFAULT_SEED)
KIND = "k8s-sa-token"
NS = "kube-system"
OWN = "iac/rotator-k8s-token"
CATALOG = "eso/prd/kubecoder/prd/catalog"
DEV_CATALOG = "eso/prd/kubecoder/dev/catalog"
WRITE = "kubeconfig-prd-write"
RW, RW_SECRET = "kubecoder-rw", "kubecoder-rw-token"
OWN_SA, OWN_SECRET = "secret-rotator", "secret-rotator-token"
SECRETS = f"/api/v1/namespaces/{NS}/secrets"
PRD_ES = "kubecoder-prd/kubecoder-secret-catalog"
DEV_ES = "kubecoder-dev/kubecoder-secret-catalog"
PRD_CTL = "kubecoder-prd/deployment/kubecoder-controller"
DEV_CTL = "kubecoder-dev/deployment/kubecoder-controller"
MINT, PROVE, DELETE = "k8s.sa_token:prd", "k8s.sa_token.prove:prd", "k8s.sa_token.delete:prd"
CATALOG_PLAN = [
    MINT,
    "kv.write",
    f"kv.copy:{DEV_CATALOG}#{WRITE}",
    f"eso.sync:{PRD_ES}",
    f"eso.sync:{DEV_ES}",
    f"k8s.rollout:{PRD_CTL}",
    f"k8s.rollout:{DEV_CTL}",
    PROVE,
    DELETE,
    "kv.stamp",
]
OWN_PLAN = [MINT, "kv.write", PROVE, DELETE, "kv.stamp"]
CA = "LS0tLS1CRUdJTiBDRVJUSUZJQ0FURS0tLS0t"
NO_UNDO = "the old token ended with its Secret"
WHAT = f"{CATALOG}#{WRITE}"


def kubeconfig(token, *, cluster="prd", server="https://10.1.0.27:16443", user="kubecoder-rw-prd"):
    """A write kubeconfig as KubeCoder's remint doc assembles it: one cluster, user and context."""
    entry = f"{{server: {server}, certificate-authority-data: {CA}}}"
    return (
        "apiVersion: v1\nkind: Config\nclusters:\n"
        f"  - {{name: {cluster}, cluster: {entry}}}\n"
        f"users:\n  - {{name: {user}, user: {{token: {token}}}}}\n"
        f"contexts:\n  - {{name: {cluster}, context: {{cluster: {cluster}, user: {user}}}}}\n"
        f"current-context: {cluster}\n"
    )


def bounded_token():
    """A token of the TokenRequest API: it names no Secret and expires."""
    claims = {
        "iss": "https://kubernetes.default.svc",
        "exp": 1_800_000_000,
        "kubernetes.io": {"namespace": NS, "serviceaccount": {"name": RW, "uid": "u"}},
        "sub": f"system:serviceaccount:{NS}:{RW}",
    }
    return f"{b64url({'alg': 'RS256'})}.{b64url(claims)}.c2lnbmF0dXJl"


def seed_store():
    return ann.offline_store(Path(str(ann.DEFAULT_KEYS)), SEED, lambda line: None)


def kubecoder_objects():
    """(resource, object) of the catalog bags' readers on prd (2026-10-09): each stage's
    ExternalSecret extracting its bag whole and its controller, which reads its Secret; and the
    token Secrets of kubecoder-rw and secret-rotator."""
    objects = []
    for stage, leaf in (("prd", CATALOG), ("dev", DEV_CATALOG)):
        ns = f"kubecoder-{stage}"
        catalog = "kubecoder-secret-catalog"
        objects += [
            ("externalsecrets", externalsecret(ns, catalog, extract=[leaf], target=catalog)),
            (
                "deployments",
                workload("Deployment", ns, "kubecoder-controller", pod_spec(volume=[catalog])),
            ),
        ]
    return [
        *objects,
        ("secrets", token_secret(NS, RW_SECRET, RW)),
        ("secrets", token_secret(NS, OWN_SECRET, OWN_SA)),
    ]


class World:
    """The seed's store on the fake OpenBao, both catalog bags holding prd's kubecoder-rw
    kubeconfig (write: what the prd bag holds instead) and iac/rotator-k8s-token the rotator's
    token; prd, which takes only its token Secrets' tokens; and the rotator's running client, which
    calls prd with the rotator's token."""

    def __init__(self, *, write=None):
        self.store = seed_store()
        self.cluster = FakeCluster(kubecoder_objects())
        self.cluster.static = set()
        self.rw = self.token(RW_SECRET)
        self.own = self.token(OWN_SECRET)
        bags = {
            leaf: {k: f"SECRET-{leaf}-{k}" for k in sorted(self.store[leaf].keys)}
            | {WRITE: kubeconfig(self.rw)}
            for leaf in (CATALOG, DEV_CATALOG)
        }
        if write is not None:
            bags[CATALOG][WRITE] = write
        self.bao = fake_of(self.store, bags | {OWN: {"token": self.own}})
        self.running = Cluster(self.cluster.kube(self.own))
        self.kind = K8sSaToken()

    def token(self, name):
        return token_in(self.cluster.get("secrets", NS, name))

    def tokens_of(self, account):
        """The names of the token Secrets prd holds for the ServiceAccount."""
        return sorted(
            name
            for (resource, _, name), obj in self.cluster.objects.items()
            if resource == "secrets"
            and obj.get("type") == SA_TOKEN
            and obj["metadata"]["annotations"][SA_NAME] == account
        )

    def successor(self, account, old):
        (name,) = [n for n in self.tokens_of(account) if n != old]
        return name

    def plan(self, leaf=CATALOG, key=WRITE, *, cluster=True):
        running = self.running if cluster else None
        return make({KIND: self.kind}, leaf, KIND, [key], audit(self.store), running)

    def executor(self, leaf=CATALOG, key=WRITE, *, day=0):
        self.recorder = Recorder()
        tick = ticking()
        return Executor(
            client(self.bao),
            self.plan(leaf, key),
            self.recorder,
            lock(self.bao),
            state=run_state(self.bao),
            dry_run=False,
            clock=lambda: tick() + datetime.timedelta(days=day),
        )

    def run(self, leaf=CATALOG, key=WRITE, *, day=0):
        return self.executor(leaf, key, day=day).run()

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

    def requests(self):
        return [(method, path) for method, path, _ in self.cluster.requests]


def ids(plan):
    return [s.id for s in plan.steps]


class TestTheSeed:
    def test_its_keys_are_the_catalog_s_three_kubeconfigs_and_the_rotator_s_own_token(self):
        entries = audit(seed_store()).entries
        found = sorted(
            (leaf, key) for leaf, by in entries.items() for key, e in by.items() if e.kind == KIND
        )
        assert found == [
            (CATALOG, "kubeconfig"),
            (CATALOG, "kubeconfig-dev-write"),
            (CATALOG, WRITE),
            (OWN, "token"),
        ]
        for leaf, key in found:
            entry = entries[leaf][key]
            assert (entry.args, entry.interval) == ({}, 365)
            assert K8sSaToken().args_problems(entry.args) == []
        assert [str(s) for s in entries[CATALOG][WRITE].activate] == [f"k8s-rollout:{PRD_CTL}"]
        assert [str(s) for s in entries[DEV_CATALOG][WRITE].activate] == [f"k8s-rollout:{DEV_CTL}"]
        assert not entries[OWN]["token"].activate

    def test_the_kind_has_no_counterpart_leaf_and_takes_no_args(self):
        assert not any(leaf.startswith("rotator/k8s-sa-token") for leaf in seed_store())
        assert K8sSaToken().args_problems({"cluster": "prd"}) == [
            "cluster: k8s-sa-token takes no args"
        ]


class TestThePlan:
    def test_a_kubeconfig_is_minted_first_written_to_both_bags_and_its_old_token_deleted_last(self):
        assert ids(World().plan()) == CATALOG_PLAN

    def test_the_rotator_s_own_token_has_no_reader_to_sync(self):
        assert ids(World().plan(OWN, "token")) == OWN_PLAN

    def test_each_kubeconfig_is_a_plan_of_its_own(self):
        world = World()
        result = audit(world.store)
        plans, _ = of_leaf(CATALOG, world.store, result, {KIND: world.kind}, world.running)
        assert [p.keys for p in plans] == [("kubeconfig",), ("kubeconfig-dev-write",), (WRITE,)]
        assert all(p.plan is not None for p in plans)

    @pytest.mark.parametrize(("leaf", "key"), [(CATALOG, WRITE), (OWN, "token")])
    def test_offline_without_a_snapshot_it_has_no_plan(self, leaf, key):
        with pytest.raises(PlanError, match="offline plan without a snapshot does not reach"):
            World().plan(leaf, key, cluster=False)

    def test_against_a_snapshot_it_plans_without_reaching_prd(self, tmp_path):
        path = tmp_path / "snapshot.json"
        path.write_text(json.dumps(snapshot(kubecoder_objects())))
        world = World()
        result = audit(world.store)
        for leaf, key, want in ((CATALOG, WRITE, CATALOG_PLAN), (OWN, "token", OWN_PLAN)):
            plan = make({KIND: world.kind}, leaf, KIND, [key], result, Cluster.of_snapshot(path))
            assert ids(plan) == want
        assert world.cluster.requests == []

    def test_the_steps_and_the_description(self):
        world = World()
        plan = world.plan()
        mint, prove, delete = (plan.steps[plan.index(i)] for i in (MINT, PROVE, DELETE))
        assert (mint.mutates, mint.undo is not None) == (True, True)
        assert (prove.mutates, prove.silent) == (False, True)
        assert (delete.mutates, delete.undo, delete.no_undo.startswith(NO_UNDO)) == (
            True,
            None,
            True,
        )
        assert [s.title for s in (mint, prove, delete)] == [
            "mint a new ServiceAccount token on prd",
            "prove the new token on prd",
            "delete the old ServiceAccount token on prd",
        ]
        assert not plan.needs_operator and plan.ask == ""
        assert plan.description == (
            "The tool mints a new token on prd for the ServiceAccount of the token the key holds "
            "and writes it to the leaf and its 1 copy and activates what reads it; in a "
            "kubeconfig only the token changes. Once every ExternalSecret that reads the leaf "
            "has synced, it proves prd takes the new token and deletes the Secret of the token it "
            "replaced."
        )
        assert world.kind.credential(plan.target) == "Kubernetes ServiceAccount token"

    def test_a_plan_of_two_keys_is_refused(self):
        world = World()
        two = dataclasses.replace(world.plan().target, keys=(WRITE, "kubeconfig"))
        with pytest.raises(PlanError, match=f"^{CATALOG}: a k8s-sa-token plan rotates one key, "):
            world.kind.plan(two, None)


class TestTheRuns:
    def test_both_bags_hold_the_kubeconfig_with_only_its_token_changed_to_one_prd_takes(self):
        world = World()
        before = world.bao.data(CATALOG)[WRITE]
        assert world.run() is Outcome.DONE
        name = world.successor(RW, RW_SECRET)
        assert re.fullmatch(f"{RW}-token-[{SUFFIX}]{{5}}", name)
        assert world.tokens_of(RW) == [name]
        new = world.token(name)
        assert world.bao.data(CATALOG)[WRITE] == before.replace(world.rw, new)
        assert world.bao.data(DEV_CATALOG)[WRITE] == before.replace(world.rw, new)
        assert world.cluster.whose(new) == f"system:serviceaccount:{NS}:{RW}"
        assert world.cluster.whose(world.rw) is None
        assert world.running.kube.token == world.own
        assert state_of(world.bao, CATALOG).stamps == {WRITE: "2026-10-05"}
        assert not any(world.rw in t or new in t for t in world.texts())

    def test_the_old_token_is_deleted_after_both_stages_synced_and_rolled_out_on_the_new(self):
        world = World()
        assert world.run() is Outcome.DONE
        requests = world.requests()
        create = requests.index(("POST", SECRETS))
        delete = requests.index(("DELETE", f"{SECRETS}/{RW_SECRET}"))
        patched = [i for i, (method, _) in enumerate(requests) if method == "PATCH"]
        assert len(patched) == 4 and create < min(patched) and max(patched) < delete

    def test_the_rotator_s_own_rotation_calls_prd_with_the_new_token_from_its_delete_on(self):
        world = World()
        assert world.run(OWN, "token") is Outcome.DONE
        name = world.successor(OWN_SA, OWN_SECRET)
        new = world.token(name)
        assert world.bao.data(OWN) == {"token": new}
        assert world.tokens_of(OWN_SA) == [name]
        assert world.cluster.whose(world.own) is None and world.cluster.whose(new) == ROTATOR
        assert world.running.kube.token == new
        delete = world.requests().index(("DELETE", f"{SECRETS}/{OWN_SECRET}"))
        assert set(world.cluster.bearers[delete:]) == {new}
        assert world.running.health(Workload.parse(PRD_CTL)) is None

    def test_the_next_rotation_replaces_the_token_the_first_minted(self):
        world = World()
        assert world.run() is Outcome.DONE
        first = world.successor(RW, RW_SECRET)
        assert world.run(day=365) is Outcome.DONE
        (second,) = world.tokens_of(RW)
        assert second != first
        assert world.bao.data(CATALOG)[WRITE] == kubeconfig(world.token(second))

    @pytest.mark.parametrize(
        ("held", "error"),
        [
            (
                kubeconfig(bounded_token()),
                f"the token {WHAT} holds names no token Secret: it is not a ServiceAccount token "
                f"Secret's token",
            ),
            (
                kubeconfig(legacy_token(NS, RW, "kubecoder-rw-gone")),
                f"prd holds no Secret {NS}/kubecoder-rw-gone, which the token {WHAT} holds names",
            ),
            (
                kubeconfig(legacy_token(NS, RW, RW_SECRET), server="https://10.1.3.3:16443"),
                f"prd's Secret {NS}/{RW_SECRET} does not hold the token {WHAT} holds",
            ),
            (
                kubeconfig(legacy_token(NS, "kubecoder-ro", RW_SECRET)),
                f"prd's Secret {NS}/{RW_SECRET} is not a token Secret of ServiceAccount "
                f"kubecoder-ro",
            ),
            (
                kubeconfig("x.y.z").replace("users:\n", "users:\n  - {name: two, user: {}}\n"),
                f"{WHAT} holds a kubeconfig of 2 users, not one",
            ),
            ("SECRET-not-a-token", f"{WHAT} holds neither a token nor a kubeconfig"),
        ],
        ids=["bounded", "no-such-secret", "dev-token", "other-account", "two-users", "neither"],
    )
    def test_a_failure_before_the_mint_stops_the_plan_before_it_creates_anything(self, held, error):
        world = World(write=held)
        executor = world.executor()
        assert executor.run() is Outcome.FAILED
        assert (world.failure().step.id, world.failure().error) == (MINT, error)
        assert world.bao.data(CATALOG)[WRITE] == held
        assert ("POST", SECRETS) not in world.requests()
        assert executor.abort() is Outcome.CANCELLED

    def test_a_create_prd_refuses_did_not_land(self):
        world = World()
        world.cluster.refused["POST", SECRETS] = 403
        executor = world.executor()
        assert executor.run() is Outcome.FAILED
        assert world.failure().error == f"POST {SECRETS}: HTTP 403: no"
        assert executor.abort() is Outcome.CANCELLED
        assert world.tokens_of(RW) == [RW_SECRET]

    def test_a_token_never_filled_fails_the_mint_and_abort_deletes_the_successor(self):
        world = World()
        world.cluster.token_controller = False
        executor = world.executor()
        assert executor.run() is Outcome.FAILED
        name = world.successor(RW, RW_SECRET)
        assert world.failure().error == (
            f"Secret {NS}/{name} got no token within 1 min: waiting for the token controller to "
            f"fill its token"
        )
        assert executor.abort() is Outcome.ROLLED_BACK
        assert world.tokens_of(RW) == [RW_SECRET]
        assert world.bao.data(CATALOG)[WRITE] == kubeconfig(world.rw)

    def test_a_failed_rollout_is_rolled_back_onto_the_old_token_which_still_works(self):
        world = World()
        world.cluster.stuck.add("kubecoder-dev/kubecoder-controller")
        executor = world.executor()
        assert executor.run() is Outcome.FAILED
        assert world.failure().step.id == f"k8s.rollout:{DEV_CTL}"
        world.cluster.stuck.clear()
        assert executor.abort() is Outcome.ROLLED_BACK
        assert world.bao.data(CATALOG)[WRITE] == kubeconfig(world.rw)
        assert world.bao.data(DEV_CATALOG)[WRITE] == kubeconfig(world.rw)
        assert world.tokens_of(RW) == [RW_SECRET]
        assert world.cluster.whose(world.rw) == f"system:serviceaccount:{NS}:{RW}"

    def test_the_rotator_s_own_delete_refused_after_the_switch_is_rolled_back_onto_its_token(self):
        world = World()
        world.cluster.refused["DELETE", f"{SECRETS}/{OWN_SECRET}"] = 403
        executor = world.executor(OWN, "token")
        assert executor.run() is Outcome.FAILED
        assert world.failure().step.id == DELETE
        new = world.token(world.successor(OWN_SA, OWN_SECRET))
        assert world.running.kube.token == new
        assert executor.abort() is Outcome.ROLLED_BACK
        assert world.running.kube.token == world.own
        assert world.tokens_of(OWN_SA) == [OWN_SECRET]
        assert world.bao.data(OWN) == {"token": world.own}
        assert world.cluster.whose(new) is None

    def test_a_delete_whose_answer_is_lost_cannot_be_aborted_and_a_retry_finishes(self):
        world = World()
        path = f"{SECRETS}/{RW_SECRET}"
        world.cluster.broken["DELETE", path] = ConnectionResetError(104, "Connection reset")
        executor = world.executor()
        assert executor.run() is Outcome.FAILED
        assert world.failure().error == (
            f"DELETE {path}: transport error: ConnectionResetError(104, 'Connection reset')"
        )
        with pytest.raises(AbortRefused, match=NO_UNDO):
            executor.abort()
        del world.cluster.broken["DELETE", path]
        assert world.run() is Outcome.DONE
        assert RW_SECRET not in world.tokens_of(RW) and len(world.tokens_of(RW)) == 1


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


class TestTheSteps:
    def test_a_mint_stages_its_record_before_it_creates_and_a_re_run_creates_no_other(self):
        world = World()
        ctx = Ctx(world.bao)
        world.cluster.broken["POST", SECRETS] = ConnectionResetError(104, "Connection reset")
        mint = Mint(world.running, CATALOG, WRITE)
        with pytest.raises(Exception, match="transport error") as e:
            mint.run(ctx)
        assert getattr(e.value, "landed", True) is not False
        record = Record.load(ctx.values[RECORD])
        assert (record.namespace, record.account, record.old) == (NS, RW, RW_SECRET)
        assert world.tokens_of(RW) == [RW_SECRET]
        del world.cluster.broken["POST", SECRETS]
        assert mint.run(ctx) == f"ServiceAccount {RW}: Secret {NS}/{record.new} minted"
        assert mint.run(ctx) == f"ServiceAccount {RW}: Secret {NS}/{record.new} minted"
        assert world.tokens_of(RW) == sorted([RW_SECRET, record.new])
        assert world.requests().count(("POST", SECRETS)) == 2
        assert ctx.values[value_name(WRITE)] == kubeconfig(world.token(record.new))

    def test_a_mint_undo_after_nothing_or_a_successor_gone_deletes_nothing(self):
        world = World()
        mint = Mint(world.running, CATALOG, WRITE)
        assert mint.undo(Ctx(world.bao)) == "nothing was minted"
        record = Record(NS, RW, RW_SECRET, f"uid-{RW_SECRET}", "kubecoder-rw-token-bcdfg")
        ctx = Ctx(world.bao, {RECORD: record.dump()})
        assert mint.undo(ctx) == f"Secret {NS}/kubecoder-rw-token-bcdfg does not exist"

    def test_a_mint_undo_with_the_client_on_the_new_token_and_the_old_gone_fails(self):
        world = World()
        ctx = Ctx(world.bao)
        Mint(world.running, OWN, "token").run(ctx)
        record = Record.load(ctx.values[RECORD])
        world.running.kube.token = world.token(record.new)
        del world.cluster.objects["secrets", NS, OWN_SECRET]
        with pytest.raises(StepFailed) as e:
            Mint(world.running, OWN, "token").undo(ctx)
        assert str(e.value) == (
            f"the rotator calls prd with the new token, and Secret {NS}/{OWN_SECRET} of the old "
            f"one is gone"
        )
        assert record.new in world.tokens_of(OWN_SA)

    def test_a_delete_of_a_secret_gone_is_done_and_of_one_replaced_did_not_land(self):
        world = World()
        record = Record(NS, RW, RW_SECRET, "uid-other", "kubecoder-rw-token-bcdfg")
        ctx = Ctx(world.bao, {RECORD: record.dump()})
        delete = Delete(world.running, CATALOG, WRITE)
        with pytest.raises(StepFailed) as e:
            delete.run(ctx)
        assert str(e.value) == (
            f"Secret {NS}/{RW_SECRET} is not the one the plan verified: it was replaced"
        )
        assert e.value.landed is False
        assert world.tokens_of(RW) == [RW_SECRET]
        del world.cluster.objects["secrets", NS, RW_SECRET]
        assert delete.run(ctx) == f"Secret {NS}/{RW_SECRET} is gone already"

    def test_a_delete_with_nothing_staged_did_not_land(self):
        world = World()
        with pytest.raises(StepFailed) as e:
            Delete(world.running, CATALOG, WRITE).run(Ctx(world.bao))
        assert (str(e.value), e.value.landed) == ("no new token is staged", False)

    def test_the_proof_needs_the_staged_value_and_prd_taking_its_token_as_the_account(self):
        world = World()
        prove = Prove(world.running, CATALOG, WRITE)
        with pytest.raises(StepFailed, match="^no new token is staged$"):
            prove.run(Ctx(world.bao))
        record = Record(NS, RW, RW_SECRET, f"uid-{RW_SECRET}", "kubecoder-rw-token-bcdfg")
        held = kubeconfig(world.rw)
        other = Ctx(world.bao, {RECORD: record.dump(), value_name(WRITE): kubeconfig("a.b.c")})
        with pytest.raises(StepFailed, match=f"^{WHAT} does not hold the value the plan staged$"):
            prove.run(other)
        staged = {RECORD: record.dump(), value_name(WRITE): held}
        assert prove.run(Ctx(world.bao, staged)) == (
            f"prd takes it as system:serviceaccount:{NS}:{RW}"
        )
        ro = Record(NS, "kubecoder-ro", RW_SECRET, "u", "kubecoder-ro-token-bcdfg")
        with pytest.raises(StepFailed) as e:
            prove.run(Ctx(world.bao, staged | {RECORD: ro.dump()}))
        assert str(e.value) == (
            f"prd takes the new token as system:serviceaccount:{NS}:{RW}, not "
            f"system:serviceaccount:{NS}:kubecoder-ro"
        )
        del world.cluster.objects["secrets", NS, RW_SECRET]
        with pytest.raises(StepFailed, match="^prd refuses the new token$"):
            prove.run(Ctx(world.bao, staged))


class TestTheTokens:
    def test_a_bare_token_is_the_value_and_is_replaced_whole(self):
        token = legacy_token(NS, OWN_SA, OWN_SECRET)
        assert token_of(f"{token}\n", "it") == token
        assert replaced(f"{token}\n", token, "n.e.w", "it") == "n.e.w\n"

    def test_in_a_kubeconfig_only_the_token_changes(self):
        old, new = legacy_token(NS, RW, RW_SECRET), legacy_token(NS, RW, "next")
        value = kubeconfig(old)
        assert token_of(value, "it") == old
        result = replaced(value, old, new, "it")
        assert result == kubeconfig(new)
        doc = yaml.safe_load(value)
        doc["users"][0]["user"]["token"] = new
        assert yaml.safe_load(result) == doc

    @pytest.mark.parametrize(
        ("value", "error"),
        [
            (kubeconfig("x.y.z").replace("{token: x.y.z}", "{}"), "the user of the kubeconfig"),
            ("apiVersion: v1\nkind: Config\n", "it holds a kubeconfig of 0 users, not one"),
            ("{", "it holds neither a token nor a kubeconfig"),
            ("kind: Secret\n", "it holds neither a token nor a kubeconfig"),
        ],
    )
    def test_a_value_without_one_token_is_refused(self, value, error):
        with pytest.raises(StepFailed, match=f"^{re.escape(error)}"):
            token_of(value, "it")

    def test_a_token_not_held_verbatim_cannot_be_replaced(self):
        value = kubeconfig("x.y.z").replace("{token: x.y.z}", '{token: "\\x78.y.z"}')
        assert token_of(value, "it") == "x.y.z"
        with pytest.raises(StepFailed, match="^it does not hold its token verbatim"):
            replaced(value, "x.y.z", "n.e.w", "it")

    def test_a_token_secret_s_token_names_its_secret_and_a_bounded_one_none(self):
        assert claims(legacy_token(NS, RW, RW_SECRET), "it") == (NS, RW, RW_SECRET)
        for token in (bounded_token(), "a.bm90IGpzb24.c"):
            with pytest.raises(StepFailed, match="^it names no token Secret"):
                claims(token, "it")

    def test_a_successor_is_named_as_generate_name_names_one(self):
        names = {successor(RW) for _ in range(20)}
        assert all(re.fullmatch(f"{RW}-token-[{SUFFIX}]{{5}}", n) for n in names)
        assert len(names) > 1


def test_a_client_bearing_another_token_reaches_the_same_apiserver_at_the_same_pace():
    fake = FakeCluster()
    kube = fake.kube()
    other = kube.bearing("SECRET-other")
    assert (other.addr, other.open, other.sleep, other.clock) == (
        ADDR,
        kube.open,
        kube.sleep,
        kube.clock,
    )
    assert other.token == "SECRET-other" and kube.token != other.token
    assert isinstance(other, Kube)
