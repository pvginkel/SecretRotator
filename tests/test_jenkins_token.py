"""The jenkins-token kind (design §6) over the seed's rows, which take no args; its plans, which
sync every ExternalSecret that reads the leaf before the login and the revoke, whatever the leaf's
activate; the runs, which mint on the admin account, revoke the token of the leaf's name the last
rotation minted and leave one the operator made by hand, and rotate rotator/jenkins itself; their
rollbacks and the failures that did not land; and the security page the kind finds the tokens
on."""

import datetime
import json
from pathlib import Path

import pytest
from fake_cluster import FakeCluster, externalsecret, pod_spec, snapshot, workload
from fake_jenkins import CREDENTIALS, PROPERTY, TOKEN, USER, FakeJenkins, security_page
from fixtures import edit
from plans import NOW, Recorder, client, fake_of, lock, run_state, state_of
from test_activation import ticking

from secret_rotator import annotate as ann
from secret_rotator import audit as aud
from secret_rotator.audit import audit
from secret_rotator.cluster import Cluster
from secret_rotator.contract import KINDS
from secret_rotator.executor import AbortRefused, Executor, Outcome
from secret_rotator.jenkins import Jenkins, JenkinsError
from secret_rotator.kinds.jenkins_token import JenkinsToken, token_name, tokens
from secret_rotator.kinds.jenkins_token.steps import UUID, Login, Mint
from secret_rotator.model import Finished, StepFailed, value_name
from secret_rotator.plan import PlanError, make

SEED = ann.load_seed(ann.DEFAULT_SEED)
KIND = "jenkins-token"
MCP = "eso/prd/jenkins-mcp/prd/config"
BOT = "eso/prd/jenkins-telegram-bot/prd/config"
POLLER = "eso/prd/version-poller/prd/jenkins"
CATALOG = "eso/prd/kubecoder/prd/catalog"
STATS = "eso/prd/infra-statistics/prd/jenkins"
ROTATOR = "rotator/jenkins"
ADMIN_PASSWORD = "shared/jenkins/admin-password"
# Each leaf's key of the kind.
LEAVES = {
    MCP: "token",
    BOT: "jenkins-token",
    POLLER: "token",
    CATALOG: "jenkins-token",
    STATS: "token",
    ROTATOR: "token",
}
DATA = {
    MCP: {"bearer-token": "SECRET-bearer", "token": "SECRET-old-mcp", "user": USER},
    BOT: {
        "jenkins-token": "SECRET-old-bot",
        "telegram-bot-token": "SECRET-bot",
        "telegram-chat-id": "-100",
    },
    POLLER: {"token": "SECRET-old-poller", "user": USER},
    STATS: {"token": "SECRET-old-stats"},
    ROTATOR: dict(CREDENTIALS),
}
ROLLED = {
    MCP: "jenkins-prd/deployment/jenkins-mcp",
    BOT: "jenkins-prd/deployment/jenkins-telegram-bot",
    CATALOG: "kubecoder-prd/deployment/kubecoder-controller",
    STATS: "infra-statistics-prd/deployment/infra-statistics",
}
SYNCED = {
    MCP: "jenkins-prd/jenkins-mcp-secrets",
    BOT: "jenkins-prd/jenkins-telegram-bot-secrets",
    POLLER: "version-poller-prd/version-poller-jenkins",
    CATALOG: "kubecoder-prd/kubecoder-secret-catalog",
    STATS: "infra-statistics-prd/infra-statistics-secrets",
}


def seed_store():
    return ann.offline_store(Path(str(ann.DEFAULT_KEYS)), SEED, lambda line: None)


def jenkins_objects():
    """(resource, object) of the readers of the kind's leaves, as prd holds them (2026-10-08) with
    JenkinsDeploy's and InfraStatisticsDeploy's ExternalSecrets as slice 047 has them read."""
    cronjob = {
        "metadata": {"namespace": "version-poller-prd", "name": "version-poller"},
        "spec": {
            "jobTemplate": {
                "spec": {"template": {"spec": pod_spec(env_from=["version-poller-jenkins"])}}
            }
        },
    }
    return [
        (
            "externalsecrets",
            externalsecret(
                "jenkins-prd",
                "jenkins-mcp-secrets",
                data=[(MCP, "bearer-token"), (MCP, "user"), (MCP, "token")],
            ),
        ),
        (
            "deployments",
            workload(
                "Deployment", "jenkins-prd", "jenkins-mcp", pod_spec(env=["jenkins-mcp-secrets"])
            ),
        ),
        (
            "externalsecrets",
            externalsecret(
                "jenkins-prd",
                "jenkins-telegram-bot-secrets",
                data=[(BOT, "jenkins-token"), (BOT, "telegram-bot-token")],
            ),
        ),
        (
            "deployments",
            workload(
                "Deployment",
                "jenkins-prd",
                "jenkins-telegram-bot",
                pod_spec(env_from=["jenkins-telegram-bot-secrets"]),
            ),
        ),
        (
            "externalsecrets",
            externalsecret(
                "version-poller-prd",
                "version-poller-jenkins",
                data=[(POLLER, "user"), (POLLER, "token")],
            ),
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
        (
            "externalsecrets",
            externalsecret(
                "infra-statistics-prd", "infra-statistics-secrets", data=[(STATS, "token")]
            ),
        ),
        (
            "deployments",
            workload(
                "Deployment",
                "infra-statistics-prd",
                "infra-statistics",
                pod_spec(env=["infra-statistics-secrets"]),
            ),
        ),
    ]


def own_names():
    """The names the kind gives the leaves' tokens."""
    return sorted(token_name(leaf, key) for leaf, key in LEAVES.items())


class World:
    """The seed's store on the fake OpenBao; Jenkins, whose admin account holds each leaf's token
    by the kind's name for it, as after the leaf's first rotation; and the readers of the leaves
    on the fake cluster."""

    def __init__(self, store=None):
        self.store = store or seed_store()
        self.bao = fake_of(self.store, DATA)
        self.jenkins = FakeJenkins()
        (own,) = self.jenkins.tokens.values()
        own["name"] = token_name(ROTATOR, LEAVES[ROTATOR])
        for leaf, key in LEAVES.items():
            if leaf != ROTATOR:
                self.jenkins.add_token(token_name(leaf, key), self.held(leaf))
        self.cluster = FakeCluster(jenkins_objects())
        self.kind = JenkinsToken()

    def held(self, leaf):
        """The token the leaf holds."""
        return self.bao.data(leaf)[LEAVES[leaf]]

    def hand_made(self, leaf, name):
        """Names the token the leaf holds as the operator made it, before the leaf's first
        rotation."""
        for token in self.jenkins.tokens.values():
            if token["value"] == self.held(leaf):
                token["name"] = name

    def values(self):
        """The values of the admin account's tokens."""
        return {token["value"] for token in self.jenkins.tokens.values()}

    def held_values(self):
        """The tokens the leaves hold."""
        return {self.held(leaf) for leaf in LEAVES}

    def plan(self, leaf, *, cluster=True):
        return make(
            {KIND: self.kind},
            leaf,
            KIND,
            [LEAVES[leaf]],
            audit(self.store),
            Cluster(self.cluster.kube()) if cluster else None,
            jenkins=self.jenkins.jenkins(),
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


def ids(plan):
    return [s.id for s in plan.steps]


def requests_to(world, path):
    return [r for r in world.jenkins.requests if r[1] == path]


GENERATE = f"{PROPERTY}/generateNewToken"
REVOKE = f"{PROPERTY}/revoke"
PAGE = f"/user/{USER}/security/"


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
        assert intervals == {leaf: 365 if leaf == ROTATOR else 14 for leaf in LEAVES}

    def test_jenkins_mcp_holds_a_user_and_a_token_and_no_header(self):
        entries = audit(seed_store()).entries[MCP]
        assert sorted(entries) == ["bearer-token", "token", "user"]
        assert (entries["token"].kind, entries["user"].kind) == (KIND, "none")
        assert entries["token"].activate == entries["bearer-token"].activate

    def test_infra_statistics_reads_a_token_leaf_of_its_own(self):
        (entry,) = audit(seed_store()).entries[STATS].values()
        assert (entry.kind, [str(a) for a in entry.activate]) == (KIND, ["eso", "k8s-rollout"])

    def test_the_admin_password_leaf_is_on_a_kind_that_never_rotates_it(self):
        store = seed_store()
        result = audit(store)
        entry = result.entries[ADMIN_PASSWORD]["password"]
        assert (entry.kind, entry.interval) == ("manual", None)
        assert "jenkins-admin-password" not in KINDS
        due = aud.due_keys(store, result, datetime.date(2099, 1, 1))
        assert ADMIN_PASSWORD not in {s.leaf for s in due}

    def test_args_it_cannot_use_are_named(self):
        assert JenkinsToken().args_problems({"legacy": "Claude"}) == [
            "legacy: jenkins-token takes no args"
        ]


class TestThePlans:
    @pytest.mark.parametrize("leaf", [MCP, BOT, CATALOG, STATS])
    def test_an_activated_leaf_syncs_and_rolls_out_before_the_login_and_the_revoke(self, leaf):
        assert ids(World().plan(leaf)) == [
            "jenkins_token.mint",
            "kv.write",
            f"eso.sync:{SYNCED[leaf]}",
            f"k8s.rollout:{ROLLED[leaf]}",
            "jenkins_token.login",
            "jenkins_token.revoke",
            "kv.stamp",
        ]

    def test_version_poller_s_externalsecret_syncs_though_its_activate_is_none(self):
        assert ids(World().plan(POLLER)) == [
            "jenkins_token.mint",
            "kv.write",
            f"eso.sync:{SYNCED[POLLER]}",
            "jenkins_token.login",
            "jenkins_token.revoke",
            "kv.stamp",
        ]

    def test_no_externalsecret_reads_rotator_jenkins(self):
        assert ids(World().plan(ROTATOR)) == [
            "jenkins_token.mint",
            "kv.write",
            "jenkins_token.login",
            "jenkins_token.revoke",
            "kv.stamp",
        ]

    def test_offline_without_a_snapshot_no_leaf_has_a_plan(self):
        for leaf in LEAVES:
            with pytest.raises(PlanError, match="offline plan without a snapshot does not reach"):
                World().plan(leaf, cluster=False)

    def test_against_a_snapshot_it_plans_without_reaching_jenkins(self, tmp_path):
        path = tmp_path / "snapshot.json"
        path.write_text(json.dumps(snapshot(jenkins_objects())))
        world = World()
        for leaf, key in LEAVES.items():
            plan = make(
                {KIND: world.kind},
                leaf,
                KIND,
                [key],
                audit(world.store),
                Cluster.of_snapshot(path),
                jenkins=world.jenkins.jenkins(),
            )
            assert ids(plan) == ids(world.plan(leaf)), leaf
        assert world.jenkins.requests == []

    def test_a_leaf_with_two_keys_of_the_kind_has_no_plan(self):
        store = seed_store()
        edit(store[POLLER].meta, "user", kind=KIND, interval="14d", activate="none")
        with pytest.raises(PlanError, match="a jenkins-token plan rotates one key, not 2"):
            make(
                {KIND: JenkinsToken()},
                POLLER,
                KIND,
                ["token", "user"],
                audit(store),
                Cluster(FakeCluster(jenkins_objects()).kube()),
            )

    def test_the_steps_and_the_descriptions(self):
        world = World()
        plan = world.plan(MCP)
        mint, login, revoke = plan.steps[0], plan.steps[4], plan.steps[5]
        assert (mint.mutates, mint.undo is not None, mint.no_undo) == (True, True, "")
        assert (login.mutates, login.silent) == (False, True)
        assert (revoke.mutates, revoke.undo) == (True, None)
        assert revoke.no_undo == "a revoked Jenkins API token cannot be restored"
        assert mint.title == f"mint a new Jenkins API token named {MCP}#token"
        assert revoke.title == f"revoke any other token named {MCP}#token"
        assert not plan.needs_operator and plan.ask == ""
        assert plan.description == (
            f"The tool mints a new Jenkins API token named {MCP}#token and writes it to the leaf "
            f"and activates what reads it. Once every ExternalSecret that reads the leaf has "
            f"synced, it logs in with the new token and revokes any other token named "
            f"{MCP}#token."
        )
        assert world.plan(ROTATOR).description == (
            "The tool mints a new Jenkins API token named rotator/jenkins#token and writes it to "
            "the leaf. Once every ExternalSecret that reads the leaf has synced, it logs in with "
            "the new token and revokes any other token named rotator/jenkins#token."
        )
        assert token_name(CATALOG, "jenkins-token") == f"{CATALOG}#jenkins-token"


class TestTheRuns:
    @pytest.mark.parametrize("leaf", [MCP, BOT, CATALOG, STATS])
    def test_a_rotation_activates_the_new_token_and_revokes_the_old_one(self, leaf):
        world = World()
        key = LEAVES[leaf]
        old = world.held(leaf)
        assert world.run(leaf) is Outcome.DONE
        new = world.held(leaf)
        assert new != old and new.startswith("SECRET-minted-token-")
        assert old not in world.values()
        assert {
            t["value"] for t in world.jenkins.tokens.values() if t["name"] == f"{leaf}#{key}"
        } == {new}
        assert world.synced(SYNCED[leaf]) and world.generation(ROLLED[leaf]) == 2
        assert state_of(world.bao, leaf).stamps == {key: "2026-10-05"}
        assert not any(new in text or old in text for text in world.texts())

    def test_the_next_rotation_revokes_the_token_the_first_minted(self):
        world = World()
        assert world.run(MCP) is Outcome.DONE
        first = world.held(MCP)
        assert world.run(MCP, day=1) is Outcome.DONE
        mine = [t for t in world.jenkins.tokens.values() if t["name"] == f"{MCP}#token"]
        assert [t["value"] for t in mine] == [world.held(MCP)] != [first]
        assert world.jenkins.token_names().count(f"{ROTATOR}#token") == 1

    def test_version_poller_s_secret_syncs_before_the_revoke_and_nothing_rolls_out(self):
        world = World()
        seen = []
        world.jenkins.before_revoke = lambda uuid: seen.append(world.synced(SYNCED[POLLER]))
        assert world.run(POLLER) is Outcome.DONE
        assert seen == [True]
        assert "SECRET-old-poller" not in world.values()
        assert not any(
            method == "PATCH" and "/deployments/" in path
            for method, path, _ in world.cluster.requests
        )

    def test_rotator_jenkins_rotates_the_token_its_own_steps_log_in_with(self):
        world = World()
        assert world.run(ROTATOR) is Outcome.DONE
        new = world.held(ROTATOR)
        assert new != TOKEN and world.bao.data(ROTATOR)["user"] == USER
        assert TOKEN not in world.values()
        assert world.run(MCP) is Outcome.DONE
        assert world.run(ROTATOR, day=1) is Outcome.DONE
        mine = [
            t["value"] for t in world.jenkins.tokens.values() if t["name"] == f"{ROTATOR}#token"
        ]
        assert mine == [world.held(ROTATOR)] != [new]

    def test_a_refused_login_rolls_back_to_the_old_token_and_revokes_the_new(self):
        world = World()
        world.jenkins.refused["GET", "/whoAmI/api/json"] = 401
        executor = world.executor(MCP)
        assert executor.run() is Outcome.FAILED
        failure = world.failure()
        assert failure.step.id == "jenkins_token.login"
        assert failure.error == (
            "Jenkins refuses the login as admin with the new token: GET /whoAmI/api/json: HTTP 401"
        )
        assert world.generation(ROLLED[MCP]) == 2
        assert executor.abort() is Outcome.ROLLED_BACK
        assert world.held(MCP) == "SECRET-old-mcp"
        assert world.jenkins.token_names() == own_names()
        assert world.generation(ROLLED[MCP]) == 3

    @pytest.mark.parametrize(
        "taken_as, error",
        [
            (
                None,
                "Jenkins refuses the login as admin with the new token: GET /whoAmI/api/json: "
                "HTTP 401",
            ),
            ("someone-else", "Jenkins takes the new token as someone-else, not admin"),
        ],
    )
    def test_a_new_token_jenkins_does_not_take_as_the_account_revokes_no_old_one(
        self, taken_as, error
    ):
        world = World()
        world.jenkins.mint_as = taken_as
        executor = world.executor(MCP)
        assert executor.run() is Outcome.FAILED
        failure = world.failure()
        assert failure.step.id == "jenkins_token.login" and failure.error == error
        assert requests_to(world, REVOKE) == [] and "SECRET-old-mcp" in world.values()
        assert executor.abort() is Outcome.ROLLED_BACK
        assert world.held(MCP) == "SECRET-old-mcp"
        assert world.jenkins.token_names() == own_names()

    def test_a_mint_jenkins_refuses_mints_nothing_and_its_rollback_revokes_nothing(self):
        world = World()
        world.jenkins.refused["POST", GENERATE] = 403
        executor = world.executor(MCP)
        assert executor.run() is Outcome.FAILED
        assert world.failure().error == f"POST {GENERATE}: HTTP 403"
        assert executor.abort_blocker() is None
        assert executor.abort() is Outcome.ROLLED_BACK
        assert world.held(MCP) == "SECRET-old-mcp" and world.jenkins.minted == 0
        assert requests_to(world, REVOKE) == []

    def test_a_revoke_jenkins_refuses_did_not_land_and_can_be_rolled_back(self):
        world = World()
        world.jenkins.refused["POST", REVOKE] = 403
        executor = world.executor(MCP)
        assert executor.run() is Outcome.FAILED
        assert world.failure().error == f"POST {REVOKE}: HTTP 403"
        assert executor.abort_blocker() is None
        del world.jenkins.refused["POST", REVOKE]
        assert executor.abort() is Outcome.ROLLED_BACK
        assert world.held(MCP) == "SECRET-old-mcp"
        assert world.values() == world.held_values()

    def test_a_revoke_whose_answer_is_lost_cannot_be_aborted_and_a_retry_finishes(self):
        world = World()
        world.jenkins.broken["POST", REVOKE] = TimeoutError("timed out")
        executor = world.executor(MCP)
        assert executor.run() is Outcome.FAILED
        with pytest.raises(AbortRefused, match="a revoked Jenkins API token cannot be restored"):
            executor.abort()
        world.jenkins.broken.clear()
        assert world.run(MCP) is Outcome.DONE
        assert "SECRET-old-mcp" not in world.values()
        assert world.jenkins.minted == 1

    def test_a_page_the_kind_cannot_read_fails_the_mint_before_anything_is_written(self):
        world = World()
        world.jenkins.page = lambda found: "<html><body><table id='tokens'></table></body></html>"
        executor = world.executor(MCP)
        assert executor.run() is Outcome.FAILED
        assert world.failure().step.id == "jenkins_token.mint"
        assert world.failure().error.startswith(
            "the security page of Jenkins account admin lists no token "
        )
        assert world.held(MCP) == "SECRET-old-mcp"
        assert executor.abort() is Outcome.ROLLED_BACK
        assert world.values() == world.held_values()

    def test_a_page_that_no_longer_lists_the_new_token_revokes_nothing(self):
        world = World()
        executor = world.executor(MCP)
        world.jenkins.before_revoke = lambda uuid: pytest.fail("revoked")
        original = world.jenkins.page

        def unlisted(found):
            if requests_to(world, "/whoAmI/api/json"):
                return original({k: v for k, v in found.items() if v["name"] != f"{MCP}#token"})
            return original(found)

        world.jenkins.page = unlisted
        assert executor.run() is Outcome.FAILED
        failure = world.failure()
        assert failure.step.id == "jenkins_token.revoke"
        assert failure.error.endswith(
            "the one the plan minted: which tokens it replaces is not known"
        )
        assert executor.abort_blocker() is None

    def test_a_token_the_page_still_lists_after_its_revoke_fails_the_step_that_landed(self):
        world = World()
        (uuid,) = [k for k, t in world.jenkins.tokens.items() if t["value"] == "SECRET-old-mcp"]
        old = world.jenkins.tokens[uuid]
        original = world.jenkins.page
        world.jenkins.page = lambda found: original(found | {uuid: old})
        executor = world.executor(MCP)
        assert executor.run() is Outcome.FAILED
        assert world.failure().error == (
            f"the security page of Jenkins account admin still lists token(s) {uuid} after "
            f"their revoke"
        )
        with pytest.raises(AbortRefused, match="a revoked Jenkins API token cannot be restored"):
            executor.abort()

    def test_a_first_rotation_leaves_the_token_the_operator_made_by_hand(self):
        world = World()
        world.hand_made(STATS, "OpenBao")
        assert world.run(STATS) is Outcome.DONE
        assert "OpenBao" in world.jenkins.token_names()
        assert requests_to(world, REVOKE) == []
        (revoke,) = [
            e
            for e in world.recorder.events
            if isinstance(e, Finished) and e.step.id == "jenkins_token.revoke"
        ]
        assert revoke.detail == (f"nothing to revoke: no other token is named {STATS}#token")


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
    def test_a_mint_resumed_after_its_token_was_staged_mints_no_other(self):
        world = World()
        ctx = Ctx(world.bao)
        mint = Mint(world.jenkins.jenkins(), f"{MCP}#token", "token")
        mint.run(ctx)
        staged = dict(ctx.values)
        assert set(staged) == {UUID, value_name("token")}
        assert mint.run(ctx) == f"token {staged[UUID]} of Jenkins account admin"
        assert world.jenkins.minted == 1 and ctx.values == staged

    def test_a_mint_undone_revokes_the_token_it_minted_and_nothing_once_it_is_gone(self):
        world = World()
        ctx = Ctx(world.bao)
        mint = Mint(world.jenkins.jenkins(), f"{MCP}#token", "token")
        assert mint.undo(ctx) == "nothing was minted"
        mint.run(ctx)
        uuid = ctx.values[UUID]
        assert mint.undo(ctx) == f"token {uuid} revoked"
        assert uuid not in world.jenkins.tokens
        assert mint.undo(ctx) == f"token {uuid} revoked"

    def test_a_login_fails_unless_the_leaf_holds_the_token_the_plan_minted(self):
        world = World()
        ctx = Ctx(world.bao)
        Mint(world.jenkins.jenkins(), f"{MCP}#token", "token").run(ctx)
        login = Login(world.jenkins.jenkins(), MCP, "token")
        with pytest.raises(StepFailed) as e:
            login.run(ctx)
        assert str(e.value) == f"{MCP}#token does not hold the token the plan minted"
        world.bao.new_version(MCP, DATA[MCP] | {"token": ctx.values[value_name("token")]})
        assert login.run(ctx) == "logged in as admin"

    def test_jenkins_s_own_refusal_of_a_mint_is_its_message(self):
        fake = FakeJenkins()

        def expired(req):
            """The one mint Jenkins answers with status error: a custom expiration in the past."""
            if req.full_url.endswith(GENERATE):
                req.data += b"&expirationDuration=custom&tokenExpiration=2000-01-01"
            return fake(req)

        jenkins = Jenkins(opener=expired, sleep=fake.sleep, clock=fake.clock)
        jenkins.authenticate(USER, TOKEN)
        with pytest.raises(JenkinsError) as e:
            tokens.generate(jenkins, USER, f"{MCP}#token")
        assert str(e.value) == f"POST {GENERATE}: Jenkins answers Expiration date is in the past."
        assert e.value.status == 200
        assert fake.minted == 0


class TestTheSecurityPage:
    def test_each_token_s_name_and_uuid_and_never_the_template_card(self):
        fake = FakeJenkins()
        fake.add_token("Claude", "SECRET-claude")
        fake.add_token("a <b> & c", "SECRET-other")
        jenkins = fake.jenkins()
        jenkins.authenticate(USER, TOKEN)
        found = tokens.listed(jenkins, USER)
        assert [(t.uuid, t.name) for t in found] == [
            (uuid, token["name"]) for uuid, token in fake.tokens.items()
        ]
        assert [t.name for t in found] == ["secret-rotator", "Claude", "a <b> & c"]
        assert 'id="api-token-row-template"' in security_page(fake.tokens)

    def test_a_page_of_another_layout_lists_no_token(self):
        fake = FakeJenkins()
        fake.page = lambda found: "<html><body><ul><li>secret-rotator</li></ul></body></html>"
        jenkins = fake.jenkins()
        jenkins.authenticate(USER, TOKEN)
        assert tokens.listed(jenkins, USER) == []
