"""The youtrack-token kind (design §6) over the seed's rows, which take no args, its counterpart's
among them; its plans, which sync every ExternalSecret that reads the leaf before the proof and the
revoke, whatever the leaf's activate; the runs, which mint through Hub for the owner of the token
the leaf holds, with that token's scope, and revoke that token and no other, rotator/youtrack's
for another account than the counterpart's and the counterpart itself; the plans that stop before
they mint when they cannot tell which token the leaf holds; their rollbacks and the failures that
did not land; and Hub's client."""

import datetime
import json
from pathlib import Path

import pytest
from fake_cluster import FakeCluster, externalsecret, pod_spec, snapshot, workload
from fake_hub import HUB, YOUTRACK, FakeHub, value_of
from fixtures import edit
from plans import NOW, Recorder, client, fake_of, lock, run_state, state_of
from test_activation import ticking

from secret_rotator import annotate as ann
from secret_rotator.audit import audit
from secret_rotator.cluster import Cluster
from secret_rotator.executor import AbortRefused, Executor, Outcome
from secret_rotator.kinds.youtrack_token import YouTrackToken, hub, token_name
from secret_rotator.kinds.youtrack_token.steps import COUNTERPART, NEW, OLD, Mint
from secret_rotator.model import Action, Finished, StepFailed, value_name
from secret_rotator.plan import PlanError, make
from secret_rotator.youtrack import YouTrackError

SEED = ann.load_seed(ann.DEFAULT_SEED)
KIND = "youtrack-token"
FIELDNOTES = "eso/prd/fieldnotes/prd/youtrack-token"
MCP = "eso/prd/youtrack/prd/mcp"
BACKUP = "eso/prd/youtrack/prd/backup"
JENKINS = "jenkins/youtrack"
CATALOG = "eso/prd/kubecoder/prd/catalog"
CARD = "rotator/youtrack"
ADMIN = COUNTERPART[0]
# Each leaf's key of the kind.
LEAVES = {
    FIELDNOTES: "token",
    MCP: "youtrack-api-key",
    BACKUP: "token",
    JENKINS: "admin-token",
    CATALOG: "youtrack-api-key",
    CARD: "token",
    ADMIN: "token",
}
OPERATOR, JEEVES, CLAUDE = "u-operator", "u-jeeves", "u-claude"  # Hub ids
LOGINS = {OPERATOR: "operator", JEEVES: "jeeves", CLAUDE: "claude"}
# The token each leaf holds, as the operator made it: its owner, name and scope.
HAND_MADE = {
    FIELDNOTES: (CLAUDE, "Fieldnotes", (YOUTRACK,)),
    MCP: (CLAUDE, "YouTrack MCP", (YOUTRACK,)),
    BACKUP: (OPERATOR, "Backup", (YOUTRACK, HUB)),
    JENKINS: (OPERATOR, "Jenkins", (YOUTRACK,)),
    CATALOG: (CLAUDE, "KubeCoder", (YOUTRACK,)),
    CARD: (JEEVES, "Secret Rotator card", (YOUTRACK,)),
    ADMIN: (OPERATOR, "Secret Rotator", (HUB,)),
}
SYNCED = {
    FIELDNOTES: ["fieldnotes-prd/fieldnotes-youtrack-token"],
    MCP: ["intercom-prd/intercom-mcp-tokens", "youtrack-mcp-prd/youtrack-mcp"],
    BACKUP: ["youtrack-prd/youtrack-backup"],
    CATALOG: ["kubecoder-prd/kubecoder-secret-catalog"],
}
ROLLED = {
    FIELDNOTES: ["fieldnotes-prd/deployment/fieldnotes"],
    MCP: ["intercom-prd/deployment/intercom", "youtrack-mcp-prd/deployment/youtrack-mcp"],
    CATALOG: ["kubecoder-prd/deployment/kubecoder-controller"],
}
PROVE, REVOKE = "youtrack_token.prove", "youtrack_token.revoke"


def seed_store():
    return ann.offline_store(Path(str(ann.DEFAULT_KEYS)), SEED, lambda line: None)


def youtrack_objects():
    """(resource, object) of the readers of the kind's leaves, as prd holds them (2026-10-09)."""
    cronjob = {
        "metadata": {"namespace": "youtrack-prd", "name": "youtrack-backup"},
        "spec": {
            "jobTemplate": {"spec": {"template": {"spec": pod_spec(env=["youtrack-backup"])}}}
        },
    }
    return [
        (
            "externalsecrets",
            externalsecret(
                "fieldnotes-prd",
                "fieldnotes-youtrack-token",
                data=[(FIELDNOTES, "token")],
                target="fieldnotes-youtrack-token",
            ),
        ),
        (
            "deployments",
            workload(
                "Deployment",
                "fieldnotes-prd",
                "fieldnotes",
                pod_spec(env=["fieldnotes-youtrack-token"]),
            ),
        ),
        (
            "externalsecrets",
            externalsecret(
                "youtrack-mcp-prd",
                "youtrack-mcp",
                data=[(MCP, "youtrack-api-key"), (MCP, "bearer-token")],
            ),
        ),
        (
            "deployments",
            workload(
                "Deployment", "youtrack-mcp-prd", "youtrack-mcp", pod_spec(env=["youtrack-mcp"])
            ),
        ),
        (
            "externalsecrets",
            externalsecret(
                "intercom-prd",
                "intercom-mcp-tokens",
                data=[
                    (MCP, "youtrack-api-key"),
                    ("eso/prd/jenkins-mcp/prd/config", "bearer-token"),
                ],
            ),
        ),
        (
            "deployments",
            workload(
                "Deployment", "intercom-prd", "intercom", pod_spec(env=["intercom-mcp-tokens"])
            ),
        ),
        (
            "externalsecrets",
            externalsecret("youtrack-prd", "youtrack-backup", data=[(BACKUP, "token")]),
        ),
        ("cronjobs", cronjob),
        (
            "externalsecrets",
            externalsecret(
                "kubecoder-prd",
                "kubecoder-secret-catalog",
                extract=[CATALOG],
                target="kubecoder-secret-catalog",
            ),
        ),
        (
            "deployments",
            workload(
                "Deployment",
                "kubecoder-prd",
                "kubecoder-controller",
                pod_spec(volume=["kubecoder-secret-catalog"]),
            ),
        ),
    ]


class World:
    """The seed's store on the fake OpenBao; Hub, where each leaf holds the token the operator
    made, and claude holds one more that no leaf does; and the readers of the leaves on the fake
    cluster."""

    def __init__(self):
        self.store = seed_store()
        self.hub = FakeHub()
        for user, login in LOGINS.items():
            self.hub.user(user, login, admin=user == OPERATOR)
        made = {leaf: self.hub.token(*HAND_MADE[leaf]) for leaf in LEAVES}
        self.laptop = self.hub.token(CLAUDE, "Laptop")
        data = {
            leaf: {k: f"SECRET-{leaf}-{k}" for k in sorted(self.store[leaf].keys)}
            | {key: made[leaf]}
            for leaf, key in LEAVES.items()
        }
        self.bao = fake_of(self.store, data)
        self.cluster = FakeCluster(youtrack_objects())
        self.kind = YouTrackToken(self.hub)
        self.initial = self.hub.values()

    def held(self, leaf):
        """The token the leaf holds."""
        return self.bao.data(leaf)[LEAVES[leaf]]

    def plan(self, leaf, cluster=None):
        return make(
            {KIND: self.kind},
            leaf,
            KIND,
            [LEAVES[leaf]],
            audit(self.store),
            cluster or Cluster(self.cluster.kube()),
        )

    def executor(self, leaf, *, day=0):
        """day: the run's day after NOW, so a later run's force-syncs differ from an earlier's."""
        self.recorder = Recorder()
        tick = ticking()
        return Executor(
            client(self.bao),
            self.plan(leaf),
            self.recorder,
            lock(self.bao),
            state=run_state(self.bao),
            dry_run=False,
            clock=lambda: tick() + datetime.timedelta(days=day),
        )

    def run(self, leaf, *, day=0):
        return self.executor(leaf, day=day).run()

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

    def generation(self, target):
        ns, _, name = target.split("/")
        return self.cluster.get("deployments", ns, name)["metadata"]["generation"]

    def synced(self, ref):
        """Whether the ExternalSecret has synced since the cluster was made."""
        ns, name = ref.split("/")
        es = self.cluster.get("externalsecrets", ns, name)
        return es["status"]["syncedResourceVersion"] != "1-0"

    def replace(self, leaf, value):
        """The leaf and its token on Hub hold value instead."""
        old = self.held(leaf)
        (token,) = [t for t in self.hub.tokens.values() if t["value"] == old]
        token["value"] = value
        self.bao.new_version(leaf, self.bao.data(leaf) | {LEAVES[leaf]: value})


def ids(plan):
    return [s.id for s in plan.steps]


def requests_to(world, method, route):
    return [r for r in world.hub.requests if r[0] == method and world.hub.route(r[1]) == route]


class TestTheSeed:
    def test_each_leaf_of_the_kind_has_one_key_of_it_and_no_args(self):
        entries = audit(seed_store()).entries
        found = {
            leaf: (key, entry.args)
            for leaf, by_key in entries.items()
            for key, entry in by_key.items()
            if entry.kind == KIND
        }
        assert found == {leaf: (key, {}) for leaf, key in LEAVES.items()}
        intervals = {leaf: entries[leaf][key].interval for leaf, key in LEAVES.items()}
        assert intervals == {leaf: 365 if leaf in (CARD, ADMIN) else 14 for leaf in LEAVES}
        activations = {
            leaf: [str(a) for a in entries[leaf][key].activate] for leaf, key in LEAVES.items()
        }
        assert activations == {
            FIELDNOTES: ["eso", "k8s-rollout"],
            MCP: ["eso", "k8s-rollout"],
            BACKUP: [],
            JENKINS: [],
            CATALOG: ["k8s-rollout:kubecoder-prd/deployment/kubecoder-controller"],
            CARD: [],
            ADMIN: [],
        }

    def test_the_counterpart_is_a_hub_token_of_the_operator_s_rotated_by_the_kind(self):
        (entry,) = audit(seed_store()).entries[ADMIN].values()
        assert (entry.kind, entry.interval, entry.activate) == (KIND, 365, ())
        assert "operator's YouTrack admin account with Hub's scope" in " ".join(entry.notes.split())

    def test_args_it_cannot_use_are_named(self):
        assert YouTrackToken().args_problems({"user": "jeeves"}) == [
            "user: youtrack-token takes no args"
        ]


class TestThePlans:
    @pytest.mark.parametrize("leaf", [FIELDNOTES, MCP, CATALOG])
    def test_an_activated_leaf_syncs_and_rolls_out_before_the_proof_and_the_revoke(self, leaf):
        assert ids(World().plan(leaf)) == [
            "youtrack_token.mint",
            "kv.write",
            *(f"eso.sync:{es}" for es in SYNCED[leaf]),
            *(f"k8s.rollout:{w}" for w in ROLLED[leaf]),
            PROVE,
            REVOKE,
            "kv.stamp",
        ]

    def test_the_backup_s_externalsecret_syncs_though_its_activate_is_none(self):
        assert ids(World().plan(BACKUP)) == [
            "youtrack_token.mint",
            "kv.write",
            f"eso.sync:{SYNCED[BACKUP][0]}",
            PROVE,
            REVOKE,
            "kv.stamp",
        ]

    @pytest.mark.parametrize("leaf", [JENKINS, CARD, ADMIN])
    def test_no_externalsecret_reads_the_jenkins_leaf_or_the_rotator_s_own(self, leaf):
        assert ids(World().plan(leaf)) == [
            "youtrack_token.mint",
            "kv.write",
            PROVE,
            REVOKE,
            "kv.stamp",
        ]

    def test_offline_without_a_snapshot_no_leaf_has_a_plan(self):
        world = World()
        for leaf, key in LEAVES.items():
            with pytest.raises(PlanError, match="offline plan without a snapshot does not reach"):
                make({KIND: world.kind}, leaf, KIND, [key], audit(world.store), None)

    def test_against_a_snapshot_it_plans_without_reaching_hub(self, tmp_path):
        path = tmp_path / "snapshot.json"
        path.write_text(json.dumps(snapshot(youtrack_objects())))
        world = World()
        for leaf in LEAVES:
            assert ids(world.plan(leaf, Cluster.of_snapshot(path))) == ids(world.plan(leaf)), leaf
        assert world.hub.requests == []

    def test_a_leaf_with_two_keys_of_the_kind_has_no_plan(self):
        store = seed_store()
        edit(store[MCP].meta, "bearer-token", kind=KIND)
        with pytest.raises(PlanError, match="a youtrack-token plan rotates one key, not 2"):
            make(
                {KIND: YouTrackToken()},
                MCP,
                KIND,
                ["bearer-token", "youtrack-api-key"],
                audit(store),
                Cluster(FakeCluster(youtrack_objects()).kube()),
            )

    def test_the_steps_and_the_descriptions(self):
        world = World()
        plan = world.plan(FIELDNOTES)
        mint, prove, revoke = plan.steps[0], plan.steps[4], plan.steps[5]
        assert (mint.mutates, mint.undo is not None, mint.no_undo) == (True, True, "")
        assert (prove.mutates, prove.silent) == (False, True)
        assert (revoke.mutates, revoke.undo) == (True, None)
        assert revoke.no_undo == "a revoked YouTrack permanent token cannot be restored"
        assert mint.title == f"mint a new YouTrack permanent token named {FIELDNOTES}#token"
        assert revoke.title == "revoke the token the leaf held"
        assert not plan.needs_operator and plan.ask == ""
        assert plan.description == (
            f"The tool mints a new YouTrack permanent token named {FIELDNOTES}#token for the "
            f"owner of the token the leaf holds, with that token's scope, and writes it to the "
            f"leaf and activates what reads it. Once every ExternalSecret that reads the leaf has "
            f"synced, it proves the new token and revokes the one the leaf held."
        )
        assert world.kind.credential(plan.target) == "YouTrack permanent token"
        assert token_name(CARD, "token") == "rotator/youtrack#token"


class TestTheRuns:
    @pytest.mark.parametrize("leaf", [FIELDNOTES, MCP, BACKUP, JENKINS, CATALOG])
    def test_a_rotation_mints_for_the_owner_with_the_scope_and_revokes_the_old_token(self, leaf):
        world = World()
        key = LEAVES[leaf]
        owner, _, scope = HAND_MADE[leaf]
        old = world.held(leaf)
        assert world.run(leaf) is Outcome.DONE
        new = world.held(leaf)
        assert new != old and new == value_of(LOGINS[owner], f"{leaf}#{key}", "SECRET-minted-1")
        (minted,) = [t for t in world.hub.tokens.values() if t["value"] == new]
        assert (minted["user"], minted["scope"]) == (owner, list(scope))
        assert world.hub.values() == world.initial - {old} | {new}
        for es in SYNCED.get(leaf, []):
            assert world.synced(es), es
        for target in ROLLED.get(leaf, []):
            assert world.generation(target) == 2, target
        assert state_of(world.bao, leaf).stamps == {key: "2026-10-05"}
        assert not any(new in text or old in text for text in world.texts())

    def test_the_next_rotation_revokes_the_token_the_first_minted(self):
        world = World()
        assert world.run(FIELDNOTES) is Outcome.DONE
        first = world.held(FIELDNOTES)
        assert world.run(FIELDNOTES, day=1) is Outcome.DONE
        assert world.hub.owned(CLAUDE) == sorted(
            [
                ("Laptop", world.laptop),
                (f"{FIELDNOTES}#token", world.held(FIELDNOTES)),
                ("YouTrack MCP", world.held(MCP)),
                ("KubeCoder", world.held(CATALOG)),
            ]
        )
        assert first not in world.hub.values()

    def test_the_backup_s_secret_syncs_before_the_revoke_and_nothing_rolls_out(self):
        world = World()
        seen = []
        world.hub.before_revoke = lambda token: seen.append(world.synced(SYNCED[BACKUP][0]))
        assert world.run(BACKUP) is Outcome.DONE
        assert seen == [True]
        assert not any(
            method == "PATCH" and "/deployments/" in path
            for method, path, _ in world.cluster.requests
        )

    def test_the_card_token_is_minted_for_jeeves_through_the_operator_s_counterpart(self):
        world = World()
        old = world.held(CARD)
        assert world.run(CARD) is Outcome.DONE
        assert world.hub.owned(JEEVES) == [("rotator/youtrack#token", world.held(CARD))]
        assert old not in world.hub.values()
        assert world.held(ADMIN) in world.initial

    def test_the_counterpart_rotates_itself_and_its_steps_after_the_write_use_the_new_one(self):
        world = World()
        old = world.held(ADMIN)
        assert world.run(ADMIN) is Outcome.DONE
        new = world.held(ADMIN)
        assert old not in world.hub.values()
        routes = [(r[0], world.hub.route(r[1])) for r in world.hub.requests]
        minted = routes.index(("POST", "tokens"))
        assert set(world.hub.bearers[: minted + 2]) == {old}  # the mint and its verification
        assert set(world.hub.bearers[minted + 2 :]) == {new}  # the proof and the revoke
        (minted,) = [t for t in world.hub.tokens.values() if t["value"] == new]
        assert (minted["user"], minted["name"], minted["scope"]) == (
            OPERATOR,
            f"{ADMIN}#token",
            [HUB],
        )
        assert world.run(FIELDNOTES) is Outcome.DONE

    @pytest.mark.parametrize(
        "change, error",
        [
            (
                lambda w: w.hub.token(CLAUDE, "Fieldnotes"),
                "2 of claude's tokens are named Fieldnotes",
            ),
            (
                lambda w: w.replace(FIELDNOTES, "SECRET-opaque-token"),
                f"the token {FIELDNOTES}#token holds is not of the form "
                "perm:<login>.<name>.<secret>",
            ),
            (
                lambda w: w.replace(FIELDNOTES, value_of("someone", "Fieldnotes", "SECRET-x")),
                f"the token {FIELDNOTES}#token holds carries login someone, but is claude's",
            ),
            (
                lambda w: w.replace(FIELDNOTES, value_of("claude", "Renamed", "SECRET-x")),
                "0 of claude's tokens are named Renamed",
            ),
        ],
    )
    def test_a_token_it_cannot_tell_among_the_owner_s_stops_the_plan_before_it_mints(
        self, change, error
    ):
        world = World()
        change(world)
        before = world.hub.values()
        executor = world.executor(FIELDNOTES)
        assert executor.run() is Outcome.FAILED
        failure = world.failure()
        assert failure.step.id == "youtrack_token.mint"
        assert failure.error == (
            f"{error}: which of claude's tokens {FIELDNOTES}#token holds cannot be told"
        )
        assert world.hub.minted == 0 and requests_to(world, "POST", "tokens") == []
        assert executor.abort_blocker() is None
        assert executor.abort() is Outcome.CANCELLED
        assert world.hub.values() == before

    def test_a_token_neither_youtrack_nor_hub_takes_stops_the_plan_before_it_mints(self):
        world = World()
        world.bao.new_version(FIELDNOTES, {"token": value_of("claude", "Gone", "SECRET-gone")})
        executor = world.executor(FIELDNOTES)
        assert executor.run() is Outcome.FAILED
        assert world.failure().error == (
            f"neither YouTrack nor Hub takes the token {FIELDNOTES}#token holds: GET "
            "/hub/api/rest/users/me: HTTP 401: can't be used here"
        )
        assert world.hub.minted == 0 and executor.abort_blocker() is None

    def test_a_mint_hub_refuses_mints_nothing_and_its_rollback_revokes_nothing(self):
        world = World()
        world.hub.refused["POST", "tokens"] = 403
        executor = world.executor(FIELDNOTES)
        assert executor.run() is Outcome.FAILED
        assert world.failure().error == (
            f"POST /hub/api/rest/users/{CLAUDE}/permanenttokens: HTTP 403: refused"
        )
        assert executor.abort_blocker() is None
        assert executor.abort() is Outcome.CANCELLED
        assert requests_to(world, "DELETE", "token") == []
        assert world.hub.values() == world.initial

    def test_a_mint_whose_answer_carries_no_token_is_revoked_by_the_rollback(self):
        world = World()
        world.hub.mint_answer = lambda made: {"id": made["id"]}
        executor = world.executor(FIELDNOTES)
        assert executor.run() is Outcome.FAILED
        assert world.failure().error == "Hub's answer to the mint of token m-1 carries no token"
        assert executor.abort() is Outcome.ROLLED_BACK
        assert "m-1" not in world.hub.tokens and world.hub.values() == world.initial
        assert world.held(FIELDNOTES) in world.initial

    @pytest.mark.parametrize(
        "answered, error",
        [
            (
                "perm:Y2xhdWRl.Qm9ndXM=.SECRET-bogus",
                "neither YouTrack nor Hub takes the new token: GET /hub/api/rest/users/me: "
                "HTTP 401: can't be used here",
            ),
            (None, "the new token is jeeves's, not claude's"),
        ],
    )
    def test_a_new_token_not_taken_as_the_owner_rolls_back_and_revokes_nothing_old(
        self, answered, error
    ):
        world = World()
        card = world.held(CARD)
        world.hub.mint_answer = lambda made: made | {"token": answered or card}
        executor = world.executor(FIELDNOTES)
        assert executor.run() is Outcome.FAILED
        failure = world.failure()
        assert (failure.step.id, failure.error) == (PROVE, error)
        assert requests_to(world, "DELETE", "token") == []
        assert executor.abort() is Outcome.ROLLED_BACK
        assert [(line[1], line[2]) for line in world.recorder.lines() if line[0] == "ok"][-4:] == [
            ("kv.write", Action.UNDO),
            ("youtrack_token.mint", Action.UNDO),
            (f"eso.sync:{SYNCED[FIELDNOTES][0]}", Action.RERUN),
            (f"k8s.rollout:{ROLLED[FIELDNOTES][0]}", Action.RERUN),
        ]
        assert world.held(FIELDNOTES) in world.initial
        assert world.hub.values() == world.initial

    def test_a_revoke_hub_refuses_did_not_land_and_can_be_rolled_back(self):
        world = World()
        world.hub.refused["DELETE", "token"] = 403
        executor = world.executor(FIELDNOTES)
        assert executor.run() is Outcome.FAILED
        assert world.failure().error.endswith("HTTP 403: refused")
        assert executor.abort_blocker() is None
        del world.hub.refused["DELETE", "token"]
        assert executor.abort() is Outcome.ROLLED_BACK
        assert world.held(FIELDNOTES) in world.initial
        assert world.hub.values() == world.initial

    def test_a_revoke_whose_answer_is_lost_cannot_be_aborted_and_a_retry_finishes(self):
        world = World()
        old = world.held(FIELDNOTES)
        world.hub.lost.add(("DELETE", "token"))
        executor = world.executor(FIELDNOTES)
        assert executor.run() is Outcome.FAILED
        assert world.failure().error.endswith("transport error: TimeoutError('timed out')")
        with pytest.raises(AbortRefused, match="a revoked YouTrack permanent token cannot be"):
            executor.abort()
        world.hub.lost.clear()
        assert world.run(FIELDNOTES) is Outcome.DONE
        (revoke,) = [
            e
            for e in world.recorder.events
            if isinstance(e, Finished) and e.step.id == REVOKE and e.ok
        ]
        assert revoke.detail == "token t-1 of claude was revoked already"
        assert old not in world.hub.values() and world.hub.minted == 1

    def test_a_token_revoked_meanwhile_is_gone_as_the_revoke_wants(self):
        world = World()
        lists = []

        def meanwhile():
            lists.append(True)
            if len(lists) == 3:  # the revoke's, after the mint's two
                del world.hub.tokens["t-1"]

        world.hub.after["GET", "tokens"] = meanwhile
        assert world.run(FIELDNOTES) is Outcome.DONE
        (delete,) = requests_to(world, "DELETE", "token")
        assert delete[1].endswith("/permanenttokens/t-1") and "t-1" not in world.hub.tokens

    def test_a_token_hub_still_lists_after_its_revoke_fails_the_step_that_landed(self):
        world = World()
        world.hub.kept.add("t-1")
        executor = world.executor(FIELDNOTES)
        assert executor.run() is Outcome.FAILED
        assert world.failure().error == "Hub still lists token t-1 of claude after its revoke"
        with pytest.raises(AbortRefused, match="a revoked YouTrack permanent token cannot be"):
            executor.abort()

    def test_a_new_token_hub_no_longer_lists_revokes_nothing(self):
        world = World()
        proved = []

        def gone():
            proved.append(True)
            if len(proved) == 2:  # the proof's, after the mint's lookup of the old token
                del world.hub.tokens["m-1"]

        world.hub.after["GET", "me"] = gone
        executor = world.executor(FIELDNOTES)
        assert executor.run() is Outcome.FAILED
        failure = world.failure()
        assert (failure.step.id, failure.error) == (
            REVOKE,
            "Hub lists no token m-1 of claude, the one the plan minted: token t-1 stays",
        )
        assert requests_to(world, "DELETE", "token") == []
        assert executor.abort_blocker() is None

    def test_a_dry_run_asks_hub_nothing(self):
        world = World()
        executor = Executor(
            client(world.bao),
            world.plan(FIELDNOTES),
            Recorder(),
            lock(world.bao),
            state=run_state(world.bao),
            dry_run=True,
            clock=lambda: NOW + datetime.timedelta(hours=1),
        )
        assert executor.run() is Outcome.DRY_RUN
        assert world.hub.requests == [] and world.bao.writes() == []


class Ctx:
    def __init__(self, bao):
        self.bao = client(bao)
        self.now = NOW
        self.values = {}

    def progress(self, detail):
        pass

    def stage(self, name, value):
        self.values[name] = value

    def staged(self, name):
        return self.values.get(name)


class TestTheSteps:
    def test_a_mint_resumed_after_its_token_was_staged_mints_no_other(self):
        world = World()
        ctx = Ctx(world.bao)
        mint = Mint(world.hub, FIELDNOTES, "token", f"{FIELDNOTES}#token")
        assert mint.run(ctx) == "token m-1 of claude, with the scope of token t-1"
        staged = dict(ctx.values)
        assert (staged[OLD], staged[NEW]) == ("t-1", "m-1") and value_name("token") in staged
        assert mint.run(ctx) == "token m-1 of claude, with the scope of token t-1"
        assert world.hub.minted == 1 and ctx.values == staged

    def test_a_mint_undone_revokes_the_token_it_minted_and_nothing_once_it_is_gone(self):
        world = World()
        ctx = Ctx(world.bao)
        mint = Mint(world.hub, FIELDNOTES, "token", f"{FIELDNOTES}#token")
        assert mint.undo(ctx) == "nothing was minted"
        mint.run(ctx)
        assert mint.undo(ctx) == "token m-1 revoked"
        assert "m-1" not in world.hub.tokens
        assert mint.undo(ctx) == "token m-1 was revoked already"

    def test_a_counterpart_the_store_lacks_stops_the_mint_before_it_reaches_hub(self):
        world = World()
        del world.bao.leaves[ADMIN]
        ctx = Ctx(world.bao)
        with pytest.raises(StepFailed, match=f"{ADMIN} cannot be read") as e:
            Mint(world.hub, FIELDNOTES, "token", f"{FIELDNOTES}#token").run(ctx)
        assert e.value.landed is False and world.hub.requests == []


class TestHub:
    def test_the_owner_of_a_youtrack_token_is_youtrack_s_and_of_a_hub_token_hub_s(self):
        world = World()
        assert hub.owner(world.held(FIELDNOTES), world.hub) == hub.Owner(CLAUDE, "claude")
        assert [r[1] for r in world.hub.requests] == ["/api/users/me"]
        assert hub.owner(world.held(ADMIN), world.hub) == hub.Owner(OPERATOR, "operator")
        assert [r[1] for r in world.hub.requests][1:] == [
            "/api/users/me",
            "/hub/api/rest/users/me",
        ]

    def test_a_token_no_service_takes_is_hub_s_refusal(self):
        with pytest.raises(YouTrackError) as e:
            hub.owner("perm:eA==.eQ==.SECRET-z", World().hub)
        assert e.value.status == 401 and "SECRET" not in str(e.value)

    def test_the_tokens_are_read_page_by_page(self):
        world = World()
        world.hub.top = 2
        for n in range(3):
            world.hub.token(CLAUDE, f"Extra {n}")
        admin = hub.client(world.held(ADMIN), world.hub)
        names = [t.name for t in hub.tokens(admin, CLAUDE)]
        assert names == [
            "Fieldnotes",
            "YouTrack MCP",
            "KubeCoder",
            "Laptop",
            *(f"Extra {n}" for n in range(3)),
        ]
        assert [r[2]["$skip"] for r in world.hub.requests] == ["0", "2", "4", "6"]

    def test_a_user_without_tokens_has_none(self):
        world = World()
        world.hub.user("u-new", "new")
        admin = hub.client(world.held(ADMIN), world.hub)
        assert hub.tokens(admin, "u-new") == []

    def test_a_user_lists_only_their_own_tokens(self):
        world = World()
        mine = hub.client(world.held(BACKUP), world.hub)  # the operator's own, with Hub's scope
        assert [t.name for t in hub.tokens(mine, OPERATOR)] == [
            "Backup",
            "Jenkins",
            "Secret Rotator",
        ]
        world.hub.users[OPERATOR]["admin"] = False
        with pytest.raises(YouTrackError, match="HTTP 403"):
            hub.tokens(mine, CLAUDE)

    @pytest.mark.parametrize(
        "value, found",
        [
            (value_of("jeeves", "Card", "SECRET-s"), ("jeeves", "Card")),
            (value_of("j", "a.b#c", "SECRET-s"), ("j", "a.b#c")),
            ("SECRET-plain", None),
            ("perm:SECRET-plain", None),
            ("perm:amVldmVz.Q2FyZA==.", None),
            ("perm:amVldmVz.Q2FyZA==.SECRET.more", None),
            ("perm:not*base64.Q2FyZA==.SECRET-s", None),
            ("perm:/w==.Q2FyZA==.SECRET-s", None),
            ("token:amVldmVz.Q2FyZA==.SECRET-s", None),
        ],
    )
    def test_the_login_and_name_a_token_carries(self, value, found):
        assert hub.carried(value) == found
