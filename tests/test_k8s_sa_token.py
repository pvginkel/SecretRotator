"""The k8s-sa-token kind (design §6) over the seed's keys: the KubeCoder catalog's kubeconfig,
kubecoder-ro on dev and on prd; kubeconfig-dev-write, dev's kubecoder-rw; kubeconfig-prd-write,
prd's kubecoder-rw; and the rotator's own token iac/rotator-k8s-token, secret-rotator's on prd. Its
args, the clusters a key's tokens are on; its plan, one per key, which mints a successor token
Secret on each of them before it writes, syncs and rolls out both stages' controllers through the
dev bag's copy, proves the new tokens and deletes the old ones last; its runs, which leave every
reader holding tokens the clusters take and the old ones ended, prd reached with the rotator's
running client and dev with the dev write token the catalog holds (ruling D1), the rotator's own
run calling prd with the new token from its delete on; the failures that stop it before a mint;
its rollbacks; the nightly run, which starts srvk8sdev for a plan on dev while it is off and shuts
it down after, skips the plan when dev does not come up and fails one dev refuses; how each cluster
is reached; and the tokens a key holds."""

import base64
import dataclasses
import datetime
import itertools
import json
import re
import ssl
import urllib.error
from pathlib import Path

import pytest
import yaml
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
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
from fake_pve import FakePve, FakeVm
from fake_telegram import CHAT, FakeTelegram
from fake_telegram import TOKEN as BOT
from fake_youtrack import TAG, FakeYouTrack
from fake_youtrack import TOKEN as JEEVES
from plans import NOW, Recorder, client, fake_of, flight_of, lock, put_state, run_state, state_of
from test_activation import ticking

from secret_rotator import annotate as ann
from secret_rotator import nightly
from secret_rotator.audit import audit
from secret_rotator.cluster import Cluster, Workload
from secret_rotator.executor import AbortRefused, Executor, Outcome
from secret_rotator.kinds.k8s_sa_token import K8sSaToken
from secret_rotator.kinds.k8s_sa_token.reach import DEV_WRITE, Dev, Prd, connect
from secret_rotator.kinds.k8s_sa_token.steps import (
    REVIEWS,
    Delete,
    Mint,
    Prove,
    Record,
    record_name,
)
from secret_rotator.kinds.k8s_sa_token.tokens import (
    SUFFIX,
    access,
    claims,
    replaced,
    successor,
    tokens_of,
)
from secret_rotator.kube import Kube
from secret_rotator.model import StepFailed, value_name
from secret_rotator.plan import PlanError, make, of_leaf
from secret_rotator.switches import Switches
from secret_rotator.telegram import Telegram
from secret_rotator.vmsteps import DEV_VM, Pve
from secret_rotator.youtrack import YouTrack

SEED = ann.load_seed(ann.DEFAULT_SEED)
KIND = "k8s-sa-token"
NS = "kube-system"
OWN = "iac/rotator-k8s-token"
CATALOG = "eso/prd/kubecoder/prd/catalog"
DEV_CATALOG = "eso/prd/kubecoder/dev/catalog"
BASE, DEV_WRITE_KEY, WRITE = "kubeconfig", "kubeconfig-dev-write", "kubeconfig-prd-write"
RO, RO_SECRET = "kubecoder-ro", "kubecoder-ro-token"
RW, RW_SECRET = "kubecoder-rw", "kubecoder-rw-token"
OWN_SA, OWN_SECRET = "secret-rotator", "secret-rotator-token"
SECRETS = f"/api/v1/namespaces/{NS}/secrets"
PRD_ES = "kubecoder-prd/kubecoder-secret-catalog"
DEV_ES = "kubecoder-dev/kubecoder-secret-catalog"
PRD_CTL = "kubecoder-prd/deployment/kubecoder-controller"
DEV_CTL = "kubecoder-dev/deployment/kubecoder-controller"
DEV_ADDR = "https://10.1.3.3:16443"
SERVERS = {"prd": "https://10.1.0.27:16443", "dev": DEV_ADDR}
MINT, PROVE, DELETE = "k8s.sa_token:prd", "k8s.sa_token.prove:prd", "k8s.sa_token.delete:prd"
DEV_MINT, DEV_PROVE, DEV_DELETE = (i.replace(":prd", ":dev") for i in (MINT, PROVE, DELETE))
ACTIVATION = [
    f"eso.sync:{PRD_ES}",
    f"eso.sync:{DEV_ES}",
    f"k8s.rollout:{PRD_CTL}",
    f"k8s.rollout:{DEV_CTL}",
]
OWN_PLAN = [MINT, "kv.write", PROVE, DELETE, "kv.stamp"]
NO_UNDO = "the old token ended with its Secret"
WHAT = f"{CATALOG}#{WRITE}"
DEV_WHAT = f"{CATALOG}#{DEV_WRITE_KEY}"
NOT_CLUSTERS = "clusters: not a list of distinct clusters of dev and prd"
DEV_START = f"vm.start:{DEV_VM}"
BOOT = 180  # seconds srvk8sdev takes to answer once started
NO_DEV = f"{DEV_VM} did not answer within 15 min: dev does not answer at {DEV_ADDR}: GET /version"


def catalog_plan(key, *clusters):
    """The step ids of the plan of a catalog kubeconfig whose tokens are on the clusters."""
    return [
        *([DEV_START] if "dev" in clusters else []),
        *(f"k8s.sa_token:{c}" for c in clusters),
        "kv.write",
        f"kv.copy:{DEV_CATALOG}#{key}",
        *ACTIVATION,
        *(f"k8s.sa_token.prove:{c}" for c in clusters),
        *(f"k8s.sa_token.delete:{c}" for c in clusters),
        "kv.stamp",
    ]


CATALOG_PLAN = catalog_plan(WRITE, "prd")


def pem(cluster):
    return f"-----BEGIN CERTIFICATE-----\n{cluster}\n-----END CERTIFICATE-----\n"


def kubeconfig(tokens, *, account="rw"):
    """A kubeconfig as KubeCoder's remint doc assembles one (cluster-identity-remint.md §4): for
    each cluster of tokens (cluster -> token) its server and CA, the user
    kubecoder-<account>-<cluster> with the token, and a context of both; current the last."""
    clusters = sorted(tokens)
    ca = {c: base64.b64encode(pem(c).encode()).decode() for c in clusters}
    user = {c: f"kubecoder-{account}-{c}" for c in clusters}
    lines = [
        "apiVersion: v1",
        "kind: Config",
        "clusters:",
        *(
            f"  - {{name: {c}, cluster: {{server: {SERVERS[c]}, "
            f"certificate-authority-data: {ca[c]}}}}}"
            for c in clusters
        ),
        "users:",
        *(f"  - {{name: {user[c]}, user: {{token: {tokens[c]}}}}}" for c in clusters),
        "contexts:",
        *(f"  - {{name: {c}, context: {{cluster: {c}, user: {user[c]}}}}}" for c in clusters),
        f"current-context: {clusters[-1]}",
    ]
    return "\n".join(lines) + "\n"


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
    token Secrets of kubecoder-ro, kubecoder-rw and secret-rotator."""
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
        ("secrets", token_secret(NS, RO_SECRET, RO)),
        ("secrets", token_secret(NS, RW_SECRET, RW)),
        ("secrets", token_secret(NS, OWN_SECRET, OWN_SA)),
    ]


def dev_objects():
    """The token Secrets of kubecoder-ro and kubecoder-rw on dev (remint doc :41)."""
    return [
        ("secrets", token_secret(NS, RO_SECRET, RO)),
        ("secrets", token_secret(NS, RW_SECRET, RW)),
    ]


class World:
    """The seed's store on the fake OpenBao, both catalog bags holding the three kubeconfigs of the
    clusters' tokens (held: what the prd bag holds instead), iac/rotator-k8s-token the rotator's
    token; prd and dev, each of which takes only its token Secrets' tokens; the rotator's running
    client, which calls prd with the rotator's token; and dev reached at the apiserver its write
    kubeconfig names, trusting the CA it names, on srvk8sdev, which runs on pve: a start has dev
    answer BOOT seconds later, unless boots is False, and a shutdown has it answer no more."""

    def __init__(self, held=None):
        self.store = seed_store()
        self.cluster = FakeCluster(kubecoder_objects())
        self.dev = FakeCluster(dev_objects(), addr=DEV_ADDR)
        self.cluster.static = set()
        self.dev.static = set()
        self.rw, self.ro, self.own = (self.token(n) for n in (RW_SECRET, RO_SECRET, OWN_SECRET))
        self.dev_rw, self.dev_ro = (self.token(n, self.dev) for n in (RW_SECRET, RO_SECRET))
        self.values = {
            BASE: kubeconfig({"dev": self.dev_ro, "prd": self.ro}, account="ro"),
            DEV_WRITE_KEY: kubeconfig({"dev": self.dev_rw}),
            WRITE: kubeconfig({"prd": self.rw}),
        }
        bags = {
            leaf: {k: f"SECRET-{leaf}-{k}" for k in sorted(self.store[leaf].keys)} | self.values
            for leaf in (CATALOG, DEV_CATALOG)
        }
        bags[CATALOG] |= held or {}
        rotator = {"rotator/youtrack": {"token": JEEVES}, "rotator/telegram": {"token": BOT}}
        self.bao = fake_of(self.store, bags | rotator | {OWN: {"token": self.own}})
        self.running = Cluster(self.cluster.kube(self.own))
        self.cas = set()  # the CAs dev's clients were made to trust
        self.kind = K8sSaToken(self.connect)
        self.boots = True
        self.vm = FakeVm(DEV_VM, 919, "pve", "running", started=self.boot, stopped=self.halt)
        self.pve_cluster = FakePve([self.vm])
        self.pve = Pve(self.pve_cluster, sleep=self.dev.sleep, clock=self.dev.clock)

    def boot(self):
        if self.boots:
            self.dev.later(BOOT, lambda: setattr(self.dev, "off", False))

    def halt(self):
        self.dev.off = True

    def power_off(self):
        """srvk8sdev stopped, as it is by default."""
        self.vm.status = "stopped"
        self.dev.off = True

    def connect(self, token, server, ca):
        """dev's client, as reach.connect makes one."""
        self.cas.add(ca)
        return Kube(token, server, opener=self.dev, sleep=self.dev.sleep, clock=self.dev.clock)

    def token(self, name, cluster=None):
        return token_in((cluster or self.cluster).get("secrets", NS, name))

    def tokens_of(self, account, cluster=None):
        """The names of the token Secrets the cluster (prd by default) holds for the account."""
        return sorted(
            name
            for (resource, _, name), obj in (cluster or self.cluster).objects.items()
            if resource == "secrets"
            and obj.get("type") == SA_TOKEN
            and obj["metadata"]["annotations"][SA_NAME] == account
        )

    def successor(self, account, old, cluster=None):
        (name,) = [n for n in self.tokens_of(account, cluster) if n != old]
        return name

    def plan(self, leaf=CATALOG, key=WRITE, *, cluster=True):
        running = self.running if cluster else None
        return make({KIND: self.kind}, leaf, KIND, [key], audit(self.store), running, pve=self.pve)

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

    def requests(self, cluster=None):
        return [(method, path) for method, path, _ in (cluster or self.cluster).requests]


def ids(plan):
    return [s.id for s in plan.steps]


class TestTheSeed:
    def test_its_keys_are_the_catalog_s_three_kubeconfigs_and_the_rotator_s_own_token(self):
        entries = audit(seed_store()).entries
        found = {
            (leaf, key): e.args["clusters"]
            for leaf, by in entries.items()
            for key, e in by.items()
            if e.kind == KIND
        }
        assert found == {
            (CATALOG, BASE): ["dev", "prd"],
            (CATALOG, DEV_WRITE_KEY): ["dev"],
            (CATALOG, WRITE): ["prd"],
            (OWN, "token"): ["prd"],
        }
        for leaf, key in found:
            entry = entries[leaf][key]
            assert entry.interval == 365
            assert K8sSaToken().args_problems(entry.args) == []
        for key in (BASE, DEV_WRITE_KEY, WRITE):
            assert [str(s) for s in entries[CATALOG][key].activate] == [f"k8s-rollout:{PRD_CTL}"]
            assert [str(s) for s in entries[DEV_CATALOG][key].activate] == [
                f"k8s-rollout:{DEV_CTL}"
            ]
        assert not entries[OWN]["token"].activate
        assert DEV_WRITE == (CATALOG, DEV_WRITE_KEY)

    def test_the_kind_has_no_counterpart_leaf(self):
        assert not any(leaf.startswith("rotator/k8s-sa-token") for leaf in seed_store())

    @pytest.mark.parametrize(
        ("args", "problems"),
        [
            ({}, ["clusters: missing; the clusters whose tokens the key holds, of dev and prd"]),
            (
                {"clusters": ["prd"], "cluster": "prd"},
                ["cluster: not one of k8s-sa-token's clusters"],
            ),
            ({"clusters": "prd"}, [NOT_CLUSTERS]),
            ({"clusters": []}, [NOT_CLUSTERS]),
            ({"clusters": ["prd", "prd"]}, [NOT_CLUSTERS]),
            ({"clusters": ["stg"]}, [NOT_CLUSTERS]),
            ({"clusters": [["prd"]]}, [NOT_CLUSTERS]),
        ],
    )
    def test_its_one_arg_names_the_clusters_the_key_s_tokens_are_on(self, args, problems):
        assert K8sSaToken().args_problems(args) == problems


class TestThePlan:
    def test_a_kubeconfig_is_minted_first_written_to_both_bags_and_its_old_token_deleted_last(self):
        assert ids(World().plan()) == CATALOG_PLAN

    def test_the_dev_write_kubeconfig_is_minted_proved_and_its_old_token_deleted_on_dev(self):
        assert ids(World().plan(key=DEV_WRITE_KEY)) == catalog_plan(DEV_WRITE_KEY, "dev")

    def test_the_base_kubeconfig_is_minted_on_dev_then_prd_and_its_old_tokens_deleted_last(self):
        assert ids(World().plan(key=BASE)) == catalog_plan(BASE, "dev", "prd")

    def test_the_rotator_s_own_token_has_no_reader_to_sync(self):
        assert ids(World().plan(OWN, "token")) == OWN_PLAN

    def test_each_kubeconfig_is_a_plan_of_its_own(self):
        world = World()
        result = audit(world.store)
        plans, _ = of_leaf(CATALOG, world.store, result, {KIND: world.kind}, world.running)
        assert [p.keys for p in plans] == [(BASE,), (DEV_WRITE_KEY,), (WRITE,)]
        assert all(p.plan is not None for p in plans)

    @pytest.mark.parametrize(("leaf", "key"), [(CATALOG, WRITE), (CATALOG, BASE), (OWN, "token")])
    def test_offline_without_a_snapshot_it_has_no_plan(self, leaf, key):
        with pytest.raises(PlanError, match="offline plan without a snapshot does not reach"):
            World().plan(leaf, key, cluster=False)

    def test_against_a_snapshot_it_plans_without_reaching_either_cluster(self, tmp_path):
        path = tmp_path / "snapshot.json"
        path.write_text(json.dumps(snapshot(kubecoder_objects())))
        world = World()
        result = audit(world.store)
        for leaf, key, want in (
            (CATALOG, WRITE, CATALOG_PLAN),
            (CATALOG, DEV_WRITE_KEY, catalog_plan(DEV_WRITE_KEY, "dev")),
            (CATALOG, BASE, catalog_plan(BASE, "dev", "prd")),
            (OWN, "token", OWN_PLAN),
        ):
            plan = make({KIND: world.kind}, leaf, KIND, [key], result, Cluster.of_snapshot(path))
            assert ids(plan) == want
        assert world.cluster.requests == [] and world.dev.requests == [] and world.cas == set()

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

    def test_the_steps_on_dev_and_the_descriptions_of_the_plans_that_reach_it(self):
        world = World()
        plan = world.plan(key=BASE)
        assert [s.title for s in plan.steps if s.type.startswith("k8s.sa_token")] == [
            "mint a new ServiceAccount token on dev",
            "mint a new ServiceAccount token on prd",
            "prove the new token on dev",
            "prove the new token on prd",
            "delete the old ServiceAccount token on dev",
            "delete the old ServiceAccount token on prd",
        ]
        assert plan.description == (
            "The tool mints a new token on dev and on prd for the ServiceAccount of each token "
            "the key holds and writes it to the leaf and its 1 copy and activates what reads it; "
            "in the kubeconfig only the tokens change. Once every ExternalSecret that reads the "
            "leaf has synced, it proves each cluster takes its new token and deletes the Secrets "
            "of the tokens it replaced. It reaches dev with the dev write token the catalog holds."
        )
        assert world.plan(key=DEV_WRITE_KEY).description == (
            "The tool mints a new token on dev for the ServiceAccount of the token the key holds "
            "and writes it to the leaf and its 1 copy and activates what reads it; in a "
            "kubeconfig only the token changes. Once every ExternalSecret that reads the leaf "
            "has synced, it proves dev takes the new token and deletes the Secret of the token it "
            "replaced. It reaches dev with the dev write token the catalog holds."
        )
        assert world.dev.requests == [] and world.cas == set()

    def test_a_plan_of_two_keys_is_refused(self):
        world = World()
        two = dataclasses.replace(world.plan().target, keys=(WRITE, BASE))
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
        assert world.dev.requests == []
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
        assert world.bao.data(CATALOG)[WRITE] == kubeconfig({"prd": world.token(second)})

    def test_the_dev_write_token_mints_its_successor_on_dev_and_the_new_one_deletes_it(self):
        world = World()
        before = world.values[DEV_WRITE_KEY]
        assert world.run(key=DEV_WRITE_KEY) is Outcome.DONE
        name = world.successor(RW, RW_SECRET, world.dev)
        new = world.token(name, world.dev)
        for leaf in (CATALOG, DEV_CATALOG):
            assert world.bao.data(leaf)[DEV_WRITE_KEY] == before.replace(world.dev_rw, new)
        assert world.tokens_of(RW, world.dev) == [name]
        assert world.dev.whose(new) == f"system:serviceaccount:{NS}:{RW}"
        assert world.dev.whose(world.dev_rw) is None
        requests = world.requests(world.dev)
        create = requests.index(("POST", SECRETS))
        delete = requests.index(("DELETE", f"{SECRETS}/{RW_SECRET}"))
        assert world.dev.bearers[create] == world.dev_rw and world.dev.bearers[delete] == new
        assert world.cas == {pem("dev")}
        assert ("POST", SECRETS) not in world.requests() and world.tokens_of(RW) == [RW_SECRET]
        assert world.running.kube.token == world.own
        assert state_of(world.bao, CATALOG).stamps == {DEV_WRITE_KEY: "2026-10-05"}
        assert not any(world.dev_rw in t or new in t for t in world.texts())

    def test_the_base_kubeconfig_gets_a_new_token_on_each_cluster_dev_s_by_the_dev_write_one(self):
        world = World()
        before = world.values[BASE]
        assert world.run(key=BASE) is Outcome.DONE
        dev_name = world.successor(RO, RO_SECRET, world.dev)
        prd_name = world.successor(RO, RO_SECRET)
        dev_new, prd_new = world.token(dev_name, world.dev), world.token(prd_name)
        want = before.replace(world.dev_ro, dev_new).replace(world.ro, prd_new)
        assert world.bao.data(CATALOG)[BASE] == want == world.bao.data(DEV_CATALOG)[BASE]
        assert tokens_of(want, ("dev", "prd"), "it") == {"dev": dev_new, "prd": prd_new}
        assert world.tokens_of(RO, world.dev) == [dev_name] and world.tokens_of(RO) == [prd_name]
        account = f"system:serviceaccount:{NS}:{RO}"
        assert world.dev.whose(dev_new) == world.cluster.whose(prd_new) == account
        assert world.dev.whose(world.dev_ro) is None and world.cluster.whose(world.ro) is None
        assert set(world.dev.bearers) == {world.dev_rw, dev_new}
        assert world.tokens_of(RW, world.dev) == [RW_SECRET]
        assert world.bao.data(CATALOG)[DEV_WRITE_KEY] == world.values[DEV_WRITE_KEY]
        assert state_of(world.bao, CATALOG).stamps == {BASE: "2026-10-05"}

    @pytest.mark.parametrize(
        ("held", "error"),
        [
            (
                kubeconfig({"prd": bounded_token()}),
                f"the token {WHAT} holds on prd names no token Secret: it is not a "
                f"ServiceAccount token Secret's token",
            ),
            (
                kubeconfig({"prd": legacy_token(NS, RW, "kubecoder-rw-gone")}),
                f"prd holds no Secret {NS}/kubecoder-rw-gone, which the token {WHAT} holds on "
                f"prd names",
            ),
            (
                kubeconfig({"prd": legacy_token(NS, RW, RW_SECRET)}),
                f"prd's Secret {NS}/{RW_SECRET} does not hold the token {WHAT} holds on prd",
            ),
            (
                kubeconfig({"prd": legacy_token(NS, RO, RW_SECRET)}),
                f"prd's Secret {NS}/{RW_SECRET} is not a token Secret of ServiceAccount "
                f"kubecoder-ro",
            ),
            (
                kubeconfig({"dev": legacy_token(NS, RW, RW_SECRET), "prd": "x.y.z"}),
                f"{WHAT} holds a kubeconfig of cluster(s) dev, prd, not prd",
            ),
            ("SECRET-not-a-token", f"{WHAT} holds neither a token nor a kubeconfig"),
        ],
        ids=["bounded", "no-such-secret", "dev-token", "other-account", "two-clusters", "neither"],
    )
    def test_a_failure_before_the_mint_stops_the_plan_before_it_creates_anything(self, held, error):
        world = World({WRITE: held})
        executor = world.executor()
        assert executor.run() is Outcome.FAILED
        assert (world.failure().step.id, world.failure().error) == (MINT, error)
        assert world.bao.data(CATALOG)[WRITE] == held
        assert ("POST", SECRETS) not in world.requests()
        assert executor.abort() is Outcome.CANCELLED

    @pytest.mark.parametrize(
        ("held", "refused", "step", "error"),
        [
            (
                kubeconfig({"dev": legacy_token(NS, RW, RW_SECRET)}),
                None,
                DEV_MINT,
                f"GET {SECRETS}/{RW_SECRET}: HTTP 401: Unauthorized",
            ),
            (None, 403, DEV_MINT, f"POST {SECRETS}: HTTP 403: no"),
            (
                kubeconfig({"prd": legacy_token(NS, RW, RW_SECRET)}),
                None,
                DEV_START,
                f"{DEV_WHAT} holds a kubeconfig without cluster dev",
            ),
        ],
        ids=["token-refused", "create-refused", "no-dev-cluster"],
    )
    def test_dev_refusing_or_out_of_reach_fails_the_plan_before_the_mint_creates_anything(
        self, held, refused, step, error
    ):
        world = World({DEV_WRITE_KEY: held} if held else None)
        if refused:
            world.dev.refused["POST", SECRETS] = refused
        executor = world.executor(key=DEV_WRITE_KEY)
        assert executor.run() is Outcome.FAILED
        assert (world.failure().step.id, world.failure().error) == (step, error)
        assert executor.abort() is Outcome.CANCELLED
        assert world.tokens_of(RW, world.dev) == [RW_SECRET]
        assert world.bao.data(CATALOG)[DEV_WRITE_KEY] == (held or world.values[DEV_WRITE_KEY])

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
        assert world.bao.data(CATALOG)[WRITE] == world.values[WRITE]

    def test_a_failed_rollout_is_rolled_back_onto_the_old_token_which_still_works(self):
        world = World()
        world.cluster.stuck.add("kubecoder-dev/kubecoder-controller")
        executor = world.executor()
        assert executor.run() is Outcome.FAILED
        assert world.failure().step.id == f"k8s.rollout:{DEV_CTL}"
        world.cluster.stuck.clear()
        assert executor.abort() is Outcome.ROLLED_BACK
        assert world.bao.data(CATALOG)[WRITE] == world.values[WRITE]
        assert world.bao.data(DEV_CATALOG)[WRITE] == world.values[WRITE]
        assert world.tokens_of(RW) == [RW_SECRET]
        assert world.cluster.whose(world.rw) == f"system:serviceaccount:{NS}:{RW}"

    def test_a_dev_write_rollback_deletes_its_successor_with_the_old_token_written_back(self):
        world = World()
        world.cluster.stuck.add("kubecoder-dev/kubecoder-controller")
        executor = world.executor(key=DEV_WRITE_KEY)
        assert executor.run() is Outcome.FAILED
        name = world.successor(RW, RW_SECRET, world.dev)
        world.cluster.stuck.clear()
        assert executor.abort() is Outcome.ROLLED_BACK
        for leaf in (CATALOG, DEV_CATALOG):
            assert world.bao.data(leaf)[DEV_WRITE_KEY] == world.values[DEV_WRITE_KEY]
        assert world.tokens_of(RW, world.dev) == [RW_SECRET]
        delete = world.requests(world.dev).index(("DELETE", f"{SECRETS}/{name}"))
        assert world.dev.bearers[delete] == world.dev_rw
        assert world.dev.whose(world.dev_rw) == f"system:serviceaccount:{NS}:{RW}"

    def test_a_base_plan_whose_prd_mint_is_refused_deletes_the_successor_it_made_on_dev(self):
        world = World()
        world.cluster.refused["POST", SECRETS] = 403
        executor = world.executor(key=BASE)
        assert executor.run() is Outcome.FAILED
        assert world.failure().step.id == MINT
        assert len(world.tokens_of(RO, world.dev)) == 2
        assert executor.abort() is Outcome.ROLLED_BACK
        assert world.tokens_of(RO, world.dev) == [RO_SECRET] and world.tokens_of(RO) == [RO_SECRET]
        assert world.bao.data(CATALOG)[BASE] == world.values[BASE]

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


class Nights:
    """Nightly runs over a world's store, the kind alone enabled and the rotator's own token
    stamped on the first night, so the catalog's three kubeconfigs are due."""

    def __init__(self, world):
        self.world = world
        self.youtrack = FakeYouTrack()
        self.telegram = FakeTelegram()
        self.lines = []
        self.ticks = itertools.count()
        put_state(world.bao, OWN, stamps={"token": NOW.date().isoformat()})

    def __call__(self, days=0):
        self.lines.clear()
        now = NOW + datetime.timedelta(days=days)
        switches = Switches(
            dry_run=False,
            paused=False,
            kinds_enabled=frozenset({KIND}),
            max_rotations_per_run=10,
            card_tag=TAG,
            telegram_chat_id=CHAT,
        )
        return nightly.run(
            client(self.world.bao),
            self.world.running,
            {KIND: self.world.kind},
            switches,
            youtrack=lambda token: YouTrack(token, opener=self.youtrack),
            telegram=lambda token, chat: Telegram(token, chat, opener=self.telegram),
            out=self.lines.append,
            holder="run on srviac, pid 7",
            now=lambda: now + datetime.timedelta(microseconds=next(self.ticks)),
            pve=self.world.pve,
        )

    def card(self):
        (issue,) = self.youtrack.open()
        return issue["description"]


class TestTheNight:
    def test_while_srvk8sdev_is_off_the_night_starts_it_for_each_dev_plan_and_shuts_it_down(self):
        world = World()
        world.power_off()
        nights = Nights(world)
        assert nights() == 0
        assert state_of(world.bao, CATALOG).stamps == {
            BASE: "2026-10-05",
            DEV_WRITE_KEY: "2026-10-05",
            WRITE: "2026-10-05",
        }
        assert RO_SECRET not in world.tokens_of(RO, world.dev)
        assert RW_SECRET not in world.tokens_of(RW, world.dev)
        assert world.pve_cluster.commands() == [("pve", "start"), ("pve", "shutdown")] * 2
        assert world.vm.status == "stopped" and world.dev.off
        assert nights.lines.count(f"    {DEV_VM} shut down on pve") == 2
        assert "Rotated 3:" in nights.telegram.messages[-1]

    def test_a_srvk8sdev_that_does_not_come_up_skips_its_plans_onto_the_card_and_prd_s_rotates(
        self,
    ):
        world = World()
        world.power_off()
        world.boots = False
        nights = Nights(world)
        assert nights() == 0
        assert state_of(world.bao, CATALOG).stamps == {WRITE: "2026-10-05"}
        assert state_of(world.bao, CATALOG).failed_nights == 0
        for key in (BASE, DEV_WRITE_KEY):
            assert world.bao.data(CATALOG)[key] == world.values[key]
        assert world.tokens_of(RO) == [RO_SECRET] and flight_of(world.bao, CATALOG) is None
        why = f"{NO_DEV}: transport error: [Errno 113] No route to host"
        for key in (BASE, DEV_WRITE_KEY):
            assert f"- `{CATALOG}`: its {KIND} plan of {key}: {why}" in nights.card()
            assert f"    skipped: {why}" in nights.lines
        (message,) = nights.telegram.messages
        assert "Rotated 1:" in message and "Failed" not in message
        assert world.vm.status == "stopped"
        world.boots = True
        assert nights(days=1) == 0
        assert state_of(world.bao, CATALOG).stamps == {
            WRITE: "2026-10-05",
            BASE: "2026-10-06",
            DEV_WRITE_KEY: "2026-10-06",
        }
        assert RO_SECRET not in world.tokens_of(RO, world.dev)
        assert RW_SECRET not in world.tokens_of(RW, world.dev)
        assert "Rotated 2:" in nights.telegram.messages[-1]
        assert world.vm.status == "stopped"

    def test_a_running_srvk8sdev_is_left_running_by_the_night(self):
        world = World()
        nights = Nights(world)
        assert nights() == 0
        assert "Rotated 3:" in nights.telegram.messages[-1]
        assert world.pve_cluster.commands() == [] and world.vm.status == "running"

    def test_a_dev_plan_the_night_rolls_back_has_srvk8sdev_up_for_its_undo_then_shut_down(self):
        world = World()
        world.power_off()
        world.dev.refused["POST", REVIEWS] = 403
        nights = Nights(world)
        assert nights() == 0
        failed = [m for m in nights.telegram.messages if "The run rolls it back" in m]
        assert len(failed) == 2 and all("prove the new token on dev" in m for m in failed)
        assert world.tokens_of(RO, world.dev) == [RO_SECRET] and world.tokens_of(RO) == [RO_SECRET]
        assert world.tokens_of(RW, world.dev) == [RW_SECRET]
        assert world.pve_cluster.commands() == [("pve", "start"), ("pve", "shutdown")] * 2
        assert world.vm.status == "stopped"
        assert "Failed 2:" in nights.telegram.messages[-1]

    def test_a_dev_that_answers_and_refuses_fails_its_plans_which_the_night_rolls_back(self):
        world = World()
        world.dev.refused["POST", SECRETS] = 403
        nights = Nights(world)
        assert nights() == 0
        failed = [m for m in nights.telegram.messages if "The run rolls it back" in m]
        assert len(failed) == 2 and all(f"POST {SECRETS}: HTTP 403: no" in m for m in failed)
        assert "Failed 2:" in nights.telegram.messages[-1]
        assert "Rotated 1:" in nights.telegram.messages[-1]
        assert world.tokens_of(RO, world.dev) == [RO_SECRET] and world.tokens_of(RO) == [RO_SECRET]
        assert world.tokens_of(RW, world.dev) == [RW_SECRET]
        assert state_of(world.bao, CATALOG).stamps == {WRITE: "2026-10-05"}


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


PRD_ONLY = ("prd",)
RECORD = record_name("prd")


class TestTheSteps:
    def test_a_mint_stages_its_record_before_it_creates_and_a_re_run_creates_no_other(self):
        world = World()
        ctx = Ctx(world.bao)
        world.cluster.broken["POST", SECRETS] = ConnectionResetError(104, "Connection reset")
        mint = Mint(Prd(world.running), CATALOG, WRITE, PRD_ONLY)
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
        assert ctx.values[value_name(WRITE)] == kubeconfig({"prd": world.token(record.new)})

    def test_the_mints_of_the_base_kubeconfig_each_replace_their_cluster_s_token_in_the_value(self):
        world = World()
        ctx = Ctx(world.bao)
        clusters = ("dev", "prd")
        Mint(Dev(world.connect), CATALOG, BASE, clusters).run(ctx)
        dev_record = Record.load(ctx.values[record_name("dev")])
        dev_new = world.token(dev_record.new, world.dev)
        assert (dev_record.account, dev_record.old) == (RO, RO_SECRET)
        assert ctx.values[value_name(BASE)] == world.values[BASE].replace(world.dev_ro, dev_new)
        prd = Mint(Prd(world.running), CATALOG, BASE, clusters)
        prd.run(ctx)
        prd_new = world.token(Record.load(ctx.values[RECORD]).new)
        both = world.values[BASE].replace(world.dev_ro, dev_new).replace(world.ro, prd_new)
        assert ctx.values[value_name(BASE)] == both
        prd.run(ctx)
        assert ctx.values[value_name(BASE)] == both
        assert world.requests().count(("POST", SECRETS)) == 1
        assert world.requests(world.dev).count(("POST", SECRETS)) == 1

    def test_a_mint_undo_after_nothing_or_a_successor_gone_deletes_nothing(self):
        world = World()
        mint = Mint(Prd(world.running), CATALOG, WRITE, PRD_ONLY)
        assert mint.undo(Ctx(world.bao)) == "nothing was minted"
        record = Record(NS, RW, RW_SECRET, f"uid-{RW_SECRET}", "kubecoder-rw-token-bcdfg")
        ctx = Ctx(world.bao, {RECORD: record.dump()})
        assert mint.undo(ctx) == f"Secret {NS}/kubecoder-rw-token-bcdfg does not exist"

    def test_a_mint_undo_with_the_client_on_the_new_token_and_the_old_gone_fails(self):
        world = World()
        ctx = Ctx(world.bao)
        Mint(Prd(world.running), OWN, "token", PRD_ONLY).run(ctx)
        record = Record.load(ctx.values[RECORD])
        world.running.kube.token = world.token(record.new)
        del world.cluster.objects["secrets", NS, OWN_SECRET]
        with pytest.raises(StepFailed) as e:
            Mint(Prd(world.running), OWN, "token", PRD_ONLY).undo(ctx)
        assert str(e.value) == (
            f"the rotator calls prd with the new token, and Secret {NS}/{OWN_SECRET} of the old "
            f"one is gone"
        )
        assert record.new in world.tokens_of(OWN_SA)

    def test_a_delete_of_a_secret_gone_is_done_and_of_one_replaced_did_not_land(self):
        world = World()
        record = Record(NS, RW, RW_SECRET, "uid-other", "kubecoder-rw-token-bcdfg")
        ctx = Ctx(world.bao, {RECORD: record.dump()})
        delete = Delete(Prd(world.running), CATALOG, WRITE, PRD_ONLY)
        with pytest.raises(StepFailed) as e:
            delete.run(ctx)
        assert str(e.value) == (
            f"Secret {NS}/{RW_SECRET} is not the one the plan verified: it was replaced"
        )
        assert e.value.landed is False
        assert world.tokens_of(RW) == [RW_SECRET]
        del world.cluster.objects["secrets", NS, RW_SECRET]
        assert delete.run(ctx) == f"Secret {NS}/{RW_SECRET} is gone already"

    def test_a_delete_with_nothing_staged_or_dev_out_of_reach_did_not_land(self):
        world = World()
        with pytest.raises(StepFailed) as e:
            Delete(Prd(world.running), CATALOG, WRITE, PRD_ONLY).run(Ctx(world.bao))
        assert (str(e.value), e.value.landed) == ("no new token is staged", False)
        world.dev.off = True
        record = Record(NS, RW, RW_SECRET, f"uid-{RW_SECRET}", "kubecoder-rw-token-bcdfg")
        ctx = Ctx(world.bao, {record_name("dev"): record.dump()})
        with pytest.raises(StepFailed) as e:
            Delete(Dev(world.connect), CATALOG, DEV_WRITE_KEY, ("dev",)).run(ctx)
        assert e.value.landed is False and "No route to host" in str(e.value)

    def test_the_proof_needs_the_staged_value_and_prd_taking_its_token_as_the_account(self):
        world = World()
        prove = Prove(Prd(world.running), CATALOG, WRITE, PRD_ONLY)
        with pytest.raises(StepFailed, match="^no new token is staged$"):
            prove.run(Ctx(world.bao))
        record = Record(NS, RW, RW_SECRET, f"uid-{RW_SECRET}", "kubecoder-rw-token-bcdfg")
        held = world.values[WRITE]
        other = Ctx(
            world.bao, {RECORD: record.dump(), value_name(WRITE): kubeconfig({"prd": "a.b.c"})}
        )
        with pytest.raises(StepFailed, match=f"^{WHAT} does not hold the value the plan staged$"):
            prove.run(other)
        staged = {RECORD: record.dump(), value_name(WRITE): held}
        assert prove.run(Ctx(world.bao, staged)) == (
            f"prd takes it as system:serviceaccount:{NS}:{RW}"
        )
        ro = Record(NS, RO, RW_SECRET, "u", "kubecoder-ro-token-bcdfg")
        with pytest.raises(StepFailed) as e:
            prove.run(Ctx(world.bao, staged | {RECORD: ro.dump()}))
        assert str(e.value) == (
            f"prd takes the new token as system:serviceaccount:{NS}:{RW}, not "
            f"system:serviceaccount:{NS}:{RO}"
        )
        del world.cluster.objects["secrets", NS, RW_SECRET]
        with pytest.raises(StepFailed, match="^prd refuses the new token$"):
            prove.run(Ctx(world.bao, staged))

    def test_the_proof_on_dev_reviews_dev_s_token_of_the_base_kubeconfig_there(self):
        world = World()
        record = Record(NS, RO, RO_SECRET, f"uid-{RO_SECRET}", "kubecoder-ro-token-bcdfg")
        staged = {record_name("dev"): record.dump(), value_name(BASE): world.values[BASE]}
        prove = Prove(Dev(world.connect), CATALOG, BASE, ("dev", "prd"))
        assert prove.run(Ctx(world.bao, staged)) == (
            f"dev takes it as system:serviceaccount:{NS}:{RO}"
        )
        assert world.dev.bearers == [world.dev_ro]


class TestTheReach:
    def test_dev_is_reached_at_the_server_and_ca_of_the_catalog_s_dev_write_kubeconfig(self):
        world = World()
        kube = Dev(world.connect).kube(client(world.bao))
        assert (kube.addr, kube.token) == (DEV_ADDR, world.dev_rw)
        assert world.cas == {pem("dev")}
        broken = World({DEV_WRITE_KEY: kubeconfig({"prd": world.rw})})
        with pytest.raises(
            StepFailed, match=f"^{DEV_WHAT} holds a kubeconfig without cluster dev$"
        ):
            Dev(broken.connect).kube(client(broken.bao))

    def test_dev_off_does_not_answer_and_dev_up_answers_even_when_it_refuses(self):
        world = World()
        dev, bao = Dev(world.connect), client(world.bao)
        assert dev.unanswered(bao) is None
        del world.dev.objects["secrets", NS, RW_SECRET]
        assert dev.unanswered(bao) is None
        world.dev.broken["GET", "/version"] = urllib.error.URLError(
            ssl.SSLCertVerificationError(1, "certificate verify failed")
        )
        assert dev.unanswered(bao) is None
        world.dev.off = True
        assert dev.unanswered(bao) == (
            f"dev does not answer at {DEV_ADDR}: GET /version: transport error: [Errno 113] No "
            f"route to host"
        )
        assert Prd(world.running).unanswered(bao) is None

    def test_every_step_on_dev_is_on_srvk8sdev_and_asks_whether_dev_answers_and_no_other(self):
        world = World()
        world.dev.off = True
        bao = client(world.bao)
        plan = world.plan(key=BASE)
        built = len(world.cluster.requests)
        on_dev = [DEV_MINT, DEV_PROVE, DEV_DELETE]
        assert [s.id for s in plan.steps if s.unanswered(bao)] == on_dev
        assert [s.id for s in plan.steps if s.vm == DEV_VM] == on_dev
        assert [s.id for s in plan.steps[0].steps] == on_dev
        assert len(world.cluster.requests) == built

    def test_dev_s_client_trusts_the_ca_its_kubeconfig_names_and_no_other(self):
        key = ec.generate_private_key(ec.SECP256R1())
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "microk8s dev")])
        certificate = (
            x509.CertificateBuilder()
            .subject_name(name)
            .issuer_name(name)
            .public_key(key.public_key())
            .serial_number(1)
            .not_valid_before(datetime.datetime(2026, 1, 1))
            .not_valid_after(datetime.datetime(2036, 1, 1))
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
            .sign(key, hashes.SHA256())
        )
        ca = certificate.public_bytes(serialization.Encoding.PEM).decode()
        kube = connect("SECRET-dev-write", DEV_ADDR, ca)
        assert (kube.addr, kube.token) == (DEV_ADDR, "SECRET-dev-write")
        trusted = kube.open.keywords["context"].get_ca_certs()
        assert [c["subject"] for c in trusted] == [((("commonName", "microk8s dev"),),)]


class TestTheTokens:
    def test_a_bare_token_is_the_value_and_is_replaced_whole(self):
        token = legacy_token(NS, OWN_SA, OWN_SECRET)
        assert tokens_of(f"{token}\n", PRD_ONLY, "it") == {"prd": token}
        assert replaced(f"{token}\n", PRD_ONLY, "prd", "n.e.w", "it") == "n.e.w\n"

    def test_in_a_kubeconfig_only_the_token_changes(self):
        old, new = legacy_token(NS, RW, RW_SECRET), legacy_token(NS, RW, "next")
        value = kubeconfig({"prd": old})
        assert tokens_of(value, PRD_ONLY, "it") == {"prd": old}
        result = replaced(value, PRD_ONLY, "prd", new, "it")
        assert result == kubeconfig({"prd": new})
        doc = yaml.safe_load(value)
        doc["users"][0]["user"]["token"] = new
        assert yaml.safe_load(result) == doc

    def test_in_the_base_kubeconfig_each_cluster_s_token_is_its_context_s_and_replaced_alone(self):
        dev, prd, new = (legacy_token(NS, RO, n) for n in ("dev", "prd", "next"))
        value = kubeconfig({"dev": dev, "prd": prd}, account="ro")
        both = ("dev", "prd")
        assert tokens_of(value, both, "it") == {"dev": dev, "prd": prd}
        result = replaced(value, both, "dev", new, "it")
        assert result == kubeconfig({"dev": new, "prd": prd}, account="ro")
        swapped = value.replace("user: kubecoder-ro-dev}", "user: kubecoder-ro-x}").replace(
            "user: kubecoder-ro-prd}", "user: kubecoder-ro-dev}"
        )
        swapped = swapped.replace("user: kubecoder-ro-x}", "user: kubecoder-ro-prd}")
        assert tokens_of(swapped, both, "it") == {"dev": prd, "prd": dev}

    @pytest.mark.parametrize(
        ("value", "clusters", "error"),
        [
            (
                kubeconfig({"prd": "x.y.z"}).replace("{token: x.y.z}", "{}"),
                PRD_ONLY,
                "the user of it's context on cluster prd has no token",
            ),
            (
                "apiVersion: v1\nkind: Config\n",
                PRD_ONLY,
                "it holds a kubeconfig of cluster(s) none, not prd",
            ),
            (
                kubeconfig({"dev": "x.y.z", "prd": "x.y.z"}),
                PRD_ONLY,
                "it holds a kubeconfig of cluster(s) dev, prd, not prd",
            ),
            ("a.b.c", ("dev", "prd"), "it holds one bare token, not one per cluster of dev, prd"),
            (
                kubeconfig({"dev": "x.y.z", "prd": "x.y.z"}).replace(
                    "  - {name: dev, context: {cluster: dev, user: kubecoder-rw-dev}}\n", ""
                ),
                ("dev", "prd"),
                "it holds a kubeconfig of 0 contexts on cluster dev, not one",
            ),
            (
                kubeconfig({"prd": "x.y.z"}).replace(
                    "users:\n", "users:\n  - {name: kubecoder-rw-prd}\n"
                ),
                PRD_ONLY,
                "it holds a kubeconfig whose users are not each named once",
            ),
            ("{", PRD_ONLY, "it holds neither a token nor a kubeconfig"),
            ("kind: Secret\n", PRD_ONLY, "it holds neither a token nor a kubeconfig"),
        ],
        ids=[
            "no-token",
            "no-cluster",
            "other-clusters",
            "bare",
            "no-context",
            "named-twice",
            "yaml",
            "kind",
        ],
    )
    def test_a_value_without_a_token_per_cluster_is_refused(self, value, clusters, error):
        with pytest.raises(StepFailed, match=f"^{re.escape(error)}$"):
            tokens_of(value, clusters, "it")

    def test_a_token_not_held_verbatim_cannot_be_replaced(self):
        value = kubeconfig({"prd": "x.y.z"}).replace("{token: x.y.z}", '{token: "\\x78.y.z"}')
        assert tokens_of(value, PRD_ONLY, "it") == {"prd": "x.y.z"}
        with pytest.raises(StepFailed, match="^it does not hold its token on prd verbatim"):
            replaced(value, PRD_ONLY, "prd", "n.e.w", "it")

    def test_a_kubeconfig_names_a_cluster_s_apiserver_its_ca_and_its_token(self):
        value = kubeconfig({"dev": "d.e.v", "prd": "p.r.d"}, account="ro")
        assert access(value, "dev", "it") == (DEV_ADDR, pem("dev"), "d.e.v")

    @pytest.mark.parametrize(
        ("value", "error"),
        [
            ("a.b.c", "it holds a bare token, not a kubeconfig naming dev's apiserver"),
            (kubeconfig({"prd": "x.y.z"}), "it holds a kubeconfig without cluster dev"),
            (
                kubeconfig({"dev": "x.y.z"}).replace(DEV_ADDR, "http://10.1.3.3:16443"),
                "it's cluster dev names no https server",
            ),
            (
                kubeconfig({"dev": "x.y.z"}).replace(
                    base64.b64encode(pem("dev").encode()).decode(), "bm90IGEgY2VydGlmaWNhdGU="
                ),
                "it's cluster dev has no certificate-authority-data",
            ),
        ],
        ids=["bare", "no-dev", "http", "no-ca"],
    )
    def test_a_kubeconfig_that_does_not_name_dev_s_apiserver_and_ca_is_refused(self, value, error):
        with pytest.raises(StepFailed, match=f"^{re.escape(error)}$"):
            access(value, "dev", "it")

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
