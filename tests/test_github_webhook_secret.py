"""The github-webhook-secret kind (design §6) over the seed's row: Fieldnotes' hook secret, whose
entry names the hand-made hook, and the core's GitHub token beside it; its plan, which syncs and
rolls Fieldnotes out before the hook's change whatever the order or the absence of auto; its runs,
in which the ping proves the new secret and a delivery Fieldnotes refused meanwhile is redelivered;
and its rollback, which puts the old secret back on the hook."""

import datetime
import json
from pathlib import Path

import pytest
from fake_cluster import FakeCluster, externalsecret, pod_spec, snapshot, workload
from fake_github import CREDENTIALS, HOOK, OLD, REPO, SPEC, START, FakeGitHub
from fixtures import set_activate
from plans import Recorder, client, fake_of, lock, run_state, state_of
from test_activation import ticking

from secret_rotator import annotate as ann
from secret_rotator.audit import audit
from secret_rotator.cluster import Cluster
from secret_rotator.executor import Executor, Outcome
from secret_rotator.githubsteps import CREDENTIALS as TOKEN_LEAF
from secret_rotator.kinds.github_webhook_secret import GitHubWebhookSecret
from secret_rotator.model import Action
from secret_rotator.plan import PlanError, make

SEED = ann.load_seed(ann.DEFAULT_SEED)
KIND = "github-webhook-secret"
LEAF = "eso/prd/fieldnotes/prd/github-webhook-secret"
ES = "fieldnotes-prd/fieldnotes-github-webhook-secret"
POD = "fieldnotes-prd/deployment/fieldnotes"
HOOK_STEP = f"github.webhook:{SPEC}"
PLAN = [
    "random.generate:secret",
    "kv.write",
    f"eso.sync:{ES}",
    f"k8s.rollout:{POD}",
    HOOK_STEP,
    "kv.stamp",
]


def seed_store():
    return ann.offline_store(Path(str(ann.DEFAULT_KEYS)), SEED, lambda line: None)


def fieldnotes_objects():
    """(resource, object) of the leaf's readers as prd holds them (2026-10-09): one
    ExternalSecret, and the fieldnotes Deployment, whose API and webhook-relay containers read its
    Secret."""
    return [
        (
            "externalsecrets",
            externalsecret(
                "fieldnotes-prd",
                "fieldnotes-github-webhook-secret",
                data=[(LEAF, "secret")],
                target="fieldnotes-github-webhook-secret",
            ),
        ),
        (
            "deployments",
            workload(
                "Deployment",
                "fieldnotes-prd",
                "fieldnotes",
                pod_spec(env=["fieldnotes-github-webhook-secret"]),
            ),
        ),
    ]


class World:
    """The seed's store on the fake OpenBao, the hook on the fake GitHub holding the leaf's
    secret, and Fieldnotes on the fake cluster, whose receiver holds the secret KV held when its
    pod last rolled out."""

    def __init__(self, activate=None):
        self.store = seed_store()
        if activate is not None:
            set_activate(self.store[LEAF].meta, activate)
        self.bao = fake_of(self.store, {LEAF: {"secret": OLD}, TOKEN_LEAF: dict(CREDENTIALS)})
        self.bao.now = START
        self.github = FakeGitHub()
        self.cluster = FakeCluster(fieldnotes_objects())
        self.pod = OLD
        self.github.held = lambda: self.pod
        self.during_rollout = []  # what happens as the pod rolls out
        rolled = self.cluster.roll_out

        def roll_out(resource, obj):
            rolled(resource, obj)
            self.pod = self.bao.data(LEAF)["secret"]
            for event in self.during_rollout:
                event()

        self.cluster.roll_out = roll_out
        self.kind = GitHubWebhookSecret()

    def plan(self, cluster=None):
        return make(
            {KIND: self.kind},
            LEAF,
            KIND,
            ["secret"],
            audit(self.store),
            cluster or Cluster(self.cluster.kube()),
            github=self.github.github(),
        )

    def executor(self):
        self.recorder = Recorder()
        return Executor(
            client(self.bao),
            self.plan(),
            self.recorder,
            lock(self.bao),
            state=run_state(self.bao),
            dry_run=False,
            clock=ticking(),
        )


def ids(plan):
    return [s.id for s in plan.steps]


class TestTheSeed:
    def test_the_leaf_names_the_hand_made_hook_after_its_sync_and_rollout(self):
        entries = audit(seed_store()).entries
        (key,) = [k for k, e in entries[LEAF].items() if e.kind == KIND]
        entry = entries[LEAF][key]
        assert (key, entry.interval, entry.args) == ("secret", 14, {})
        assert [str(a) for a in entry.activate] == ["eso", "k8s-rollout", f"github-webhook:{SPEC}"]
        assert [leaf for leaf, by in entries.items() for e in by.values() if e.kind == KIND] == [
            LEAF
        ]

    def test_the_core_s_github_token_is_a_manual_key_limited_to_the_hook_s_repository(self):
        entries = audit(seed_store()).entries[TOKEN_LEAF]
        assert list(entries) == ["token"]
        entry = entries["token"]
        assert (entry.kind, entry.args, entry.interval, entry.activate) == (
            "manual",
            {"type": "github-pat"},
            365,
            (),
        )
        assert (
            "fine-grained, on pvginkel, repository access only pvginkel/Fieldnotes" in entry.notes
        )
        assert "permission Webhooks read and write and nothing more" in entry.notes

    def test_it_takes_no_args(self):
        assert GitHubWebhookSecret().args_problems({"repo": REPO}) == [
            "repo: github-webhook-secret takes no args"
        ]


class TestThePlan:
    def test_it_syncs_and_rolls_fieldnotes_out_then_changes_the_hook(self):
        plan = World().plan()
        assert ids(plan) == PLAN
        assert plan.description == (
            "The tool generates a new secret and writes it to the leaf and activates what reads "
            "it. Once every ExternalSecret that reads the leaf has synced, it sets the secret on "
            f"GitHub hook {SPEC}, proves it with a ping and redelivers each delivery that failed "
            "meanwhile."
        )
        assert (plan.ask, plan.needs_operator) == ("", False)

    def test_the_hook_s_change_comes_last_whatever_the_activate_s_order(self):
        assert ids(World(f"github-webhook:{SPEC},eso,k8s-rollout").plan()) == PLAN

    def test_without_a_rollout_it_still_syncs_before_the_hook_s_change(self):
        assert ids(World(f"github-webhook:{SPEC}").plan()) == [
            "random.generate:secret",
            "kv.write",
            f"eso.sync:{ES}",
            HOOK_STEP,
            "kv.stamp",
        ]

    def test_without_a_hook_it_has_no_plan(self):
        with pytest.raises(PlanError) as e:
            World("auto").plan()
        assert str(e.value) == (
            f"{LEAF}: no entry the plan writes names the GitHub hook "
            f"(github-webhook:<owner>/<repo>/<hook-id>) whose secret it is"
        )

    def test_against_a_snapshot_it_plans_without_reaching_github(self, tmp_path):
        path = tmp_path / "snapshot.json"
        path.write_text(json.dumps(snapshot(fieldnotes_objects())))
        world = World()
        assert ids(world.plan(Cluster.of_snapshot(path))) == PLAN
        assert world.github.requests == []


class TestTheRuns:
    def test_it_rotates_the_secret_fieldnotes_holding_it_before_the_hook(self):
        world = World()
        assert world.executor().run() is Outcome.DONE
        new = world.bao.data(LEAF)["secret"]
        assert new != OLD and world.github.secret() == new == world.pod
        (ping,) = world.github.hooks[REPO, HOOK]["deliveries"]
        assert (ping["event"], ping["status_code"]) == ("ping", 200)
        assert state_of(world.bao, LEAF).status == "ok"
        assert state_of(world.bao, LEAF).consumers == (
            "fieldnotes-prd/externalsecret/fieldnotes-github-webhook-secret",
            POD,
        )

    def test_a_push_fieldnotes_refused_meanwhile_is_redelivered_under_the_new_secret(self):
        world = World()
        pushes = []
        world.during_rollout.append(lambda: pushes.append(world.github.deliver("push")))
        assert world.executor().run() is Outcome.DONE
        (push,) = pushes
        assert push["status_code"] == 401
        assert world.github.attempts(push["guid"]) == [401, 200]
        (done,) = [
            e for e in world.recorder.events if getattr(e, "ok", False) and e.step.id == HOOK_STEP
        ]
        assert done.detail.endswith(f"; redelivered 1: {push['guid']}")

    def test_a_ping_fieldnotes_refuses_rolls_back_to_the_old_secret_on_the_hook(self):
        world = World()
        world.github.held = lambda: OLD  # Fieldnotes never took the new secret
        executor = world.executor()
        assert executor.run() is Outcome.FAILED
        assert state_of(world.bao, LEAF).last_error.startswith(f"the ping of hook {SPEC} failed")
        assert world.github.secret() != OLD
        world.recorder.events.clear()
        assert executor.abort() is Outcome.ROLLED_BACK
        assert [(line[1], line[2]) for line in world.recorder.lines() if line[0] == "ok"] == [
            ("kv.write", Action.UNDO),
            (f"eso.sync:{ES}", Action.RERUN),
            (f"k8s.rollout:{POD}", Action.RERUN),
            (HOOK_STEP, Action.RERUN),
        ]
        assert world.github.secret() == OLD == world.bao.data(LEAF)["secret"]

    def test_a_dry_run_asks_github_nothing(self):
        world = World()
        recorder = Recorder()
        executor = Executor(
            client(world.bao),
            world.plan(),
            recorder,
            lock(world.bao),
            state=run_state(world.bao),
            dry_run=True,
            clock=lambda: START + datetime.timedelta(hours=1),
        )
        assert executor.run() is Outcome.DRY_RUN
        assert world.github.requests == [] and world.bao.writes() == []
