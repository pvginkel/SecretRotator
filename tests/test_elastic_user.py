"""The elastic-user kind (design §6) over the seed's five rows, the superuser elastic, whose leaf is
the kind's counterpart, among them: its args; its plans, which log in before they write, sync
every ExternalSecret that reads the leaf or a copy before the password is set, whatever the leaf's
activate, and roll the consumers after it (R102), Elasticsearch's own rollout given ten minutes;
the runs, which leave Elasticsearch taking the password KV holds; their rollbacks, which leave
Elasticsearch, KV and the Secrets on the password Elasticsearch held; the failures that stop a plan
before it writes; and the client's answers."""

import datetime
import io
import json
import urllib.error
from pathlib import Path

import pytest
from fake_cluster import FakeCluster, externalsecret, pod_spec, snapshot, workload
from fake_elasticsearch import (
    AUTHENTICATE,
    BASE,
    PASSWORDS,
    FakeElasticsearch,
    FakeResponse,
    password_path,
)
from fixtures import edit
from plans import NOW, Recorder, client, fake_of, lock, run_state, state_of
from test_activation import ticking

from secret_rotator import annotate as ann
from secret_rotator.audit import audit
from secret_rotator.cluster import Cluster
from secret_rotator.executor import Executor, Outcome
from secret_rotator.kinds import elastic_user
from secret_rotator.kinds.elastic_user import ElasticUser
from secret_rotator.kinds.elastic_user.elasticsearch import Elasticsearch, ElasticsearchError
from secret_rotator.kinds.elastic_user.steps import HELD, Login, SetPassword
from secret_rotator.model import StepFailed, value_name
from secret_rotator.plan import PlanError, make

SEED = ann.load_seed(ann.DEFAULT_SEED)
KIND = "elastic-user"
KEY = "password"
SUPER = "eso/prd/elasticsearch/prd/elastic"
READER = "eso/prd/elasticsearch/prd/filebeat-reader"
KIBANA = "eso/prd/elasticsearch/prd/kibana-system"
FILEBEAT = "eso/prd/filebeat/prd/elastic-credentials"
IOT = "eso/prd/iot/prd/elastic-credentials"
CATALOG = "eso/prd/kubecoder/prd/catalog"
USERS = {
    SUPER: "elastic",
    READER: "reader",
    KIBANA: "kibana_system",
    FILEBEAT: "filebeat_writer",
    IOT: "iotsupport",
}
LEAVES = list(USERS)
NAMED = (FILEBEAT, IOT)  # the leaves that hold their user's name as username
# The ExternalSecrets that read each leaf or its copy on prd (2026-10-09), and the workload each
# leaf's plan rolls out.
READERS = {
    SUPER: ["elasticsearch-prd/elasticsearch-elastic"],
    READER: ["elasticsearch-prd/elasticsearch-reader", "kubecoder-prd/kubecoder-secret-catalog"],
    KIBANA: ["elasticsearch-prd/elasticsearch-kibana-system"],
    FILEBEAT: [
        "elasticsearch-prd/elasticsearch-filebeat-writer",
        "filebeat-prd/filebeat-es-credentials",
    ],
    IOT: ["elasticsearch-prd/elasticsearch-iotsupport", "iot-prd/iot-elastic-credentials"],
}
ROLLED = {
    SUPER: "elasticsearch-prd/deployment/elasticsearch",
    READER: "kubecoder-prd/deployment/kubecoder-controller",
    KIBANA: "elasticsearch-prd/deployment/kibana",
    FILEBEAT: "filebeat-prd/daemonset/filebeat",
    IOT: "iot-prd/deployment/iotsupport",
}
LOGIN, SET = "elastic.login", "elastic.set_password"


def seed_store():
    return ann.offline_store(Path(str(ann.DEFAULT_KEYS)), SEED, lambda line: None)


def elastic_objects():
    """(resource, object) of the leaves' readers as prd holds them (2026-10-09): ElasticsearchDeploy
    delivers each leaf to its setup Job, the superuser's to Elasticsearch and kibana_system's to
    Kibana; FilebeatDeploy and IotDeploy their own leaf, username and password, to filebeat and
    iotsupport; the KubeCoder catalog extracts its whole leaf, which holds the reader's copy."""
    ns = "elasticsearch-prd"
    own = {
        "elasticsearch-elastic": SUPER,
        "elasticsearch-kibana-system": KIBANA,
        "elasticsearch-filebeat-writer": FILEBEAT,
        "elasticsearch-iotsupport": IOT,
        "elasticsearch-reader": READER,
    }
    objects = [
        ("externalsecrets", externalsecret(ns, name, data=[(leaf, KEY)], target=name))
        for name, leaf in own.items()
    ]
    for consumer, name, leaf in (
        ("filebeat-prd", "filebeat-es-credentials", FILEBEAT),
        ("iot-prd", "iot-elastic-credentials", IOT),
    ):
        data = [(leaf, "username"), (leaf, KEY)]
        objects.append(("externalsecrets", externalsecret(consumer, name, data=data, target=name)))
    objects.append(
        (
            "externalsecrets",
            externalsecret(
                "kubecoder-prd",
                "kubecoder-secret-catalog",
                extract=[CATALOG],
                target="kubecoder-secret-catalog",
            ),
        )
    )
    objects += [
        (
            "deployments",
            workload("Deployment", ns, "elasticsearch", pod_spec(env=["elasticsearch-elastic"])),
        ),
        (
            "deployments",
            workload("Deployment", ns, "kibana", pod_spec(env=["elasticsearch-kibana-system"])),
        ),
        (
            "daemonsets",
            workload(
                "DaemonSet", "filebeat-prd", "filebeat", pod_spec(env=["filebeat-es-credentials"])
            ),
        ),
        (
            "deployments",
            workload(
                "Deployment", "iot-prd", "iotsupport", pod_spec(env=["iot-elastic-credentials"])
            ),
        ),
        (
            "deployments",
            workload("Deployment", "kubecoder-prd", "kubecoder-controller", pod_spec()),
        ),
    ]
    return objects


def leaf_data(leaf, password=None):
    data = {KEY: PASSWORDS[USERS[leaf]] if password is None else password}
    return data | ({"username": USERS[leaf]} if leaf in NAMED else {})


class World:
    """The seed's store on the fake OpenBao, each leaf holding the password Elasticsearch takes for
    its user; Elasticsearch; and the leaves' readers on the fake cluster."""

    def __init__(self, store=None, data=None):
        self.store = store or seed_store()
        self.es = FakeElasticsearch()
        held = {leaf: leaf_data(leaf) for leaf in LEAVES} | (data or {})
        self.bao = fake_of(self.store, held)
        self.cluster = FakeCluster(elastic_objects())
        self.kind = ElasticUser(opener=self.es)
        # (user, the password set, what KV holds, the syncs so far, the rolled workload's
        # generation), at each set
        self.posted = []
        self.leaf = None
        self.es.before_post = lambda user, new: self.posted.append(
            (user, new, self.password(self.leaf), self.cluster.syncs, self.generation(self.leaf))
        )
        self.synced = []  # (ExternalSecret, what KV holds of the leaf it reads), at each sync
        eso_sync = self.cluster.eso_sync

        def synced(es):
            ref = f"{es['metadata']['namespace']}/{es['metadata']['name']}"
            read = CATALOG if ref.startswith("kubecoder-prd/") else self.leaf
            key = "elastic-password" if read == CATALOG else KEY
            self.synced.append((ref, self.bao.data(read)[key]))
            eso_sync(es)

        self.cluster.eso_sync = synced

    def password(self, leaf):
        return self.bao.data(leaf)[KEY]

    def generation(self, leaf):
        ns, kind, name = ROLLED[leaf].split("/")
        return self.cluster.get(f"{kind}s", ns, name)["metadata"]["generation"]

    def plan(self, leaf, *, cluster=True, store=None):
        return make(
            {KIND: self.kind},
            leaf,
            KIND,
            [KEY],
            audit(store or self.store),
            Cluster(self.cluster.kube()) if cluster else None,
        )

    def executor(self, leaf, *, day=0):
        self.leaf = leaf
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


def ids(plan):
    return [s.id for s in plan.steps]


def expected(leaf):
    copies = [f"kv.copy:{CATALOG}#elastic-password"] if leaf == READER else []
    return [
        LOGIN,
        f"random.generate:{KEY}",
        "kv.write",
        *copies,
        *(f"eso.sync:{es}" for es in READERS[leaf]),
        SET,
        f"k8s.rollout:{ROLLED[leaf]}",
        "kv.stamp",
    ]


class TestTheSeed:
    @pytest.mark.parametrize(
        ("leaf", "interval", "activate"),
        [
            (SUPER, 365, ["eso", "k8s-rollout"]),
            (READER, 14, []),
            (KIBANA, 14, ["eso", "k8s-rollout"]),
            (FILEBEAT, 14, ["eso", "k8s-rollout"]),
            (IOT, 14, ["eso", "k8s-rollout"]),
        ],
    )
    def test_each_user_s_leaf_names_its_user(self, leaf, interval, activate):
        entries = audit(seed_store()).entries[leaf]
        entry = entries[KEY]
        assert (entry.kind, entry.args, entry.interval) == (KIND, {"user": USERS[leaf]}, interval)
        assert [s.name for s in entry.activate] == activate
        assert ElasticUser().args_problems(entry.args) == []
        if leaf in NAMED:
            assert entries["username"].kind == "none"

    def test_the_reader_s_copy_in_the_kubecoder_catalog_rolls_its_controller(self):
        entry = audit(seed_store()).entries[CATALOG]["elastic-password"]
        assert entry.kind == f"copy:{READER}#{KEY}"
        assert [str(s) for s in entry.activate] == [
            "k8s-rollout:kubecoder-prd/deployment/kubecoder-controller"
        ]

    def test_the_counterpart_is_the_superuser_s_own_leaf_and_no_rotator_leaf(self):
        # 044 D3: elastic's password lives where ESO reads it, and nowhere else.
        assert elastic_user.COUNTERPART == SUPER
        assert not any(leaf.startswith("rotator/elastic-user") for leaf in seed_store())

    @pytest.mark.parametrize(
        ("args", "problems"),
        [
            ({}, ["user: missing; the Elasticsearch user whose password it is"]),
            ({"user": "a b"}, ["user: not an Elasticsearch user name"]),
            ({"user": 7}, ["user: not an Elasticsearch user name"]),
            (
                {"user": "reader", "url": BASE},
                ["url: not one of elastic-user's user"],
            ),
        ],
    )
    def test_its_args_name_one_user(self, args, problems):
        assert ElasticUser().args_problems(args) == problems

    def test_it_reaches_elasticsearch_at_the_readme_s_address(self):
        # ElasticsearchDeploy README.md; prd answered there on 2026-10-09.
        assert elastic_user.BASE == BASE


class TestThePlans:
    @pytest.mark.parametrize("leaf", LEAVES)
    def test_it_syncs_before_the_set_and_rolls_out_after_it(self, leaf):
        assert ids(World().plan(leaf)) == expected(leaf)

    @pytest.mark.parametrize("leaf", LEAVES)
    def test_only_elasticsearch_s_rollout_waits_ten_minutes(self, leaf):
        (rollout,) = [s for s in World().plan(leaf).steps if s.type == "k8s.rollout"]
        assert rollout.bound == (600 if leaf == SUPER else 300)

    def test_offline_without_a_snapshot_it_has_no_plan(self):
        with pytest.raises(PlanError, match="offline plan without a snapshot does not reach"):
            World().plan(READER, cluster=False)

    def test_against_a_snapshot_it_plans_without_reaching_elasticsearch(self, tmp_path):
        path = tmp_path / "snapshot.json"
        path.write_text(json.dumps(snapshot(elastic_objects())))
        world = World()
        for leaf in LEAVES:
            plan = make(
                {KIND: world.kind}, leaf, KIND, [KEY], audit(world.store), Cluster.of_snapshot(path)
            )
            assert ids(plan) == expected(leaf)
        assert world.es.requests == []

    @pytest.mark.parametrize(("leaf", "user"), [(READER, "elastic"), (SUPER, "reader")])
    def test_the_superuser_s_password_is_its_own_leaf_s_alone(self, leaf, user):
        store = seed_store()
        edit(store[leaf].meta, KEY, args={"user": user})
        with pytest.raises(PlanError) as e:
            World().plan(leaf, store=store)
        assert str(e.value) == (
            f"{leaf}: user elastic's password is {SUPER}'s, the kind's counterpart, and no "
            f"other leaf's"
        )

    def test_the_steps_and_the_descriptions(self):
        world = World()
        plan = world.plan(READER)
        login, set_password = plan.steps[0], plan.steps[plan.index(SET)]
        assert (login.mutates, login.silent) == (False, True)
        assert (set_password.mutates, set_password.activator) == (True, False)
        assert set_password.undo is not None
        assert login.title == (
            "log in to Elasticsearch as reader with the password the leaf holds, and as elastic "
            "with the one its leaf holds"
        )
        assert set_password.title == "set the password of Elasticsearch user reader to the new one"
        assert not plan.needs_operator and plan.ask == ""
        assert plan.description == (
            "The tool generates a new password for Elasticsearch user reader and writes it to the "
            "leaf and its 1 copy and activates what reads it. Once every ExternalSecret that "
            "reads the leaf has synced and before it activates, it sets the password in "
            f"Elasticsearch as user elastic, logged in with the password {SUPER} holds, and logs "
            "in with the new one."
        )
        superuser = world.plan(SUPER)
        assert superuser.steps[0].title == (
            "log in to Elasticsearch as elastic with the password the leaf holds"
        )
        assert superuser.description == (
            "The tool generates a new password for Elasticsearch user elastic and writes it to "
            "the leaf and activates what reads it. Once every ExternalSecret that reads the leaf "
            "has synced and before it activates, it sets the password in Elasticsearch as user "
            "elastic, logged in with the one it held, and logs in with the new one."
        )
        assert world.kind.credential(plan.target) == "Elasticsearch user password"

    def test_a_plan_that_activates_nothing_still_syncs_before_the_set(self):
        store = seed_store()
        edit(store[CATALOG].meta, "elastic-password", activate="none")
        plan = World().plan(READER, store=store)
        assert ids(plan) == [*expected(READER)[:-2], "kv.stamp"]
        assert plan.description == (
            "The tool generates a new password for Elasticsearch user reader and writes it to the "
            "leaf and its 1 copy. Once every ExternalSecret that reads the leaf has synced, it "
            f"sets the password in Elasticsearch as user elastic, logged in with the password "
            f"{SUPER} holds, and logs in with the new one."
        )


class TestTheRuns:
    @pytest.mark.parametrize("leaf", LEAVES)
    def test_a_rotation_leaves_elasticsearch_taking_the_password_kv_holds(self, leaf):
        world, user = World(), USERS[leaf]
        assert world.run(leaf) is Outcome.DONE
        new = world.password(leaf)
        assert new != PASSWORDS[user] and len(new) == 43
        assert world.es.password(user) == new
        assert world.es.failed == 0
        assert state_of(world.bao, leaf).stamps == {KEY: "2026-10-05"}
        if leaf in NAMED:
            assert world.bao.data(leaf)["username"] == user
        assert not any(PASSWORDS[user] in text or new in text for text in world.texts())

    @pytest.mark.parametrize("leaf", LEAVES)
    def test_the_password_is_set_once_every_secret_holds_it_and_before_the_rollout(self, leaf):
        world = World()
        assert world.run(leaf) is Outcome.DONE
        new = world.password(leaf)
        assert world.posted == [(USERS[leaf], new, new, len(READERS[leaf]), 1)]
        assert world.synced == [(es, new) for es in READERS[leaf]]
        assert world.generation(leaf) == 2

    def test_the_superuser_sets_its_password_logged_in_with_the_one_it_held(self):
        world = World()
        assert world.run(SUPER) is Outcome.DONE
        assert world.es.posts() == [("POST", password_path("elastic"), "elastic")]
        assert world.es.password("elastic") == world.password(SUPER) != PASSWORDS["elastic"]

    def test_a_user_s_rotation_logs_in_with_the_superuser_s_password_kv_holds_now(self):
        world = World()
        assert world.run(SUPER) is Outcome.DONE
        assert world.run(KIBANA, day=1) is Outcome.DONE
        assert world.es.password("kibana_system") == world.password(KIBANA)
        assert world.es.failed == 0

    @pytest.mark.parametrize("leaf", [SUPER, READER])
    def test_the_next_rotation_logs_in_with_the_password_the_first_set(self, leaf):
        world = World()
        assert world.run(leaf) is Outcome.DONE
        first = world.password(leaf)
        assert world.run(leaf, day=14) is Outcome.DONE
        assert world.es.password(USERS[leaf]) == world.password(leaf) != first
        assert world.es.failed == 0

    def test_elasticsearch_that_does_not_come_up_fails_after_ten_minutes_and_rolls_back(self):
        world = World()
        world.cluster.stuck.add("elasticsearch-prd/elasticsearch")
        executor = world.executor(SUPER)
        assert executor.run() is Outcome.FAILED
        failure = world.failure()
        assert failure.step.id == f"k8s.rollout:{ROLLED[SUPER]}"
        assert failure.error.startswith(f"{ROLLED[SUPER]} not Ready within 10 min")
        assert 600 <= world.cluster.now < 700
        world.cluster.stuck.clear()
        assert executor.abort() is Outcome.ROLLED_BACK
        assert world.password(SUPER) == world.es.password("elastic") == PASSWORDS["elastic"]
        assert world.synced[-1] == ("elasticsearch-prd/elasticsearch-elastic", PASSWORDS["elastic"])

    def test_every_other_rollout_keeps_five_minutes(self):
        world = World()
        world.cluster.stuck.add("filebeat-prd/filebeat")
        assert world.run(FILEBEAT) is Outcome.FAILED
        assert world.failure().error.startswith(f"{ROLLED[FILEBEAT]} not Ready within 5 min")
        assert 300 <= world.cluster.now < 400

    @pytest.mark.parametrize("leaf", [SUPER, IOT])
    def test_a_set_elasticsearch_refuses_rolls_back_and_leaves_everything_on_the_old_password(
        self, leaf
    ):
        world, user = World(), USERS[leaf]
        world.es.refused["POST", password_path(user)] = 500
        executor = world.executor(leaf)
        assert executor.run() is Outcome.FAILED
        failure = world.failure()
        assert failure.step.id == SET
        assert failure.error == f"POST {password_path(user)}: HTTP 500: refused"
        assert executor.abort() is Outcome.ROLLED_BACK
        assert world.password(leaf) == world.es.password(user) == PASSWORDS[user]
        assert world.synced[-len(READERS[leaf]) :] == [
            (es, PASSWORDS[user]) for es in READERS[leaf]
        ]
        assert len(world.es.posts()) == 1

    @pytest.mark.parametrize("leaf", [SUPER, KIBANA])
    def test_a_set_whose_answer_is_lost_is_undone_before_kv_holds_the_old_password_again(
        self, leaf
    ):
        world, user = World(), USERS[leaf]
        world.es.lost["POST", password_path(user)] = 502
        executor = world.executor(leaf)
        assert executor.run() is Outcome.FAILED
        assert world.failure().error == f"POST {password_path(user)}: HTTP 502"
        new = world.password(leaf)
        assert world.es.password(user) == new
        world.es.lost.clear()
        assert executor.abort() is Outcome.ROLLED_BACK
        assert world.password(leaf) == world.es.password(user) == PASSWORDS[user]
        syncs = len(READERS[leaf])
        assert [p[:4] for p in world.posted] == [
            (user, new, new, syncs),
            (user, PASSWORDS[user], new, syncs),
        ]
        # The undo logs elastic in with the password Elasticsearch takes then: the new one on
        # the superuser's plan, the counterpart's on any other.
        assert [login for _, _, login in world.es.posts()] == ["elastic", "elastic"]
        assert not any(PASSWORDS[user] in text or new in text for text in world.texts())

    def test_a_set_whose_answer_is_lost_is_retried_and_finishes(self):
        world = World()
        world.es.lost["POST", password_path("reader")] = 502
        assert world.run(READER) is Outcome.FAILED
        world.es.lost.clear()
        assert world.run(READER) is Outcome.DONE
        assert world.es.password("reader") == world.password(READER) != PASSWORDS["reader"]
        assert len(world.es.posts()) == 1 and world.es.failed == 1
        assert state_of(world.bao, READER).stamps == {KEY: "2026-10-05"}

    def test_a_set_that_did_not_reach_elasticsearch_is_left_as_it_is_by_the_rollback(self):
        world = World()
        world.es.broken["POST", password_path("reader")] = TimeoutError("timed out")
        executor = world.executor(READER)
        assert executor.run() is Outcome.FAILED
        assert world.failure().error == (
            f"POST {password_path('reader')}: transport error: TimeoutError('timed out')"
        )
        assert executor.abort() is Outcome.ROLLED_BACK
        assert world.password(READER) == world.es.password("reader") == PASSWORDS["reader"]
        assert world.posted == [] and world.es.failed == 1

    def test_a_rollback_whose_superuser_login_elasticsearch_refuses_fails(self):
        world = World()
        world.es.refused["POST", password_path("reader")] = 500
        executor = world.executor(READER)
        assert executor.run() is Outcome.FAILED
        world.es.users["elastic"]["password"] = "SECRET-someone-else-s"
        world.es.users["reader"]["password"] = "SECRET-neither"
        world.es.refused.clear()
        assert executor.abort() is Outcome.ROLLBACK_FAILED
        assert world.recorder.failures()[-1].error == (
            f"Elasticsearch refuses user elastic with the password {SUPER} holds"
        )

    @pytest.mark.parametrize(
        ("leaf", "data", "error"),
        [
            (
                READER,
                {READER: leaf_data(READER, "SECRET-not-elasticsearch-s")},
                f"Elasticsearch refuses user reader with the password {READER} holds",
            ),
            (
                KIBANA,
                {SUPER: leaf_data(SUPER, "SECRET-not-elasticsearch-s")},
                f"Elasticsearch refuses user elastic with the password {SUPER} holds",
            ),
            (
                SUPER,
                {SUPER: leaf_data(SUPER, "SECRET-not-elasticsearch-s")},
                f"Elasticsearch refuses user elastic with the password {SUPER} holds",
            ),
            (
                FILEBEAT,
                {FILEBEAT: leaf_data(FILEBEAT) | {"username": "iotsupport"}},
                f"{FILEBEAT} holds username iotsupport, not filebeat_writer, the user its args "
                f"name",
            ),
            (IOT, {IOT: leaf_data(IOT, "")}, f"{IOT} holds no {KEY}"),
            (IOT, {SUPER: leaf_data(SUPER, "")}, f"{SUPER} holds no {KEY}"),
        ],
    )
    def test_a_login_elasticsearch_does_not_take_stops_the_plan_before_it_writes(
        self, leaf, data, error
    ):
        world = World(data=data)
        before = dict(world.bao.data(leaf))
        executor = world.executor(leaf)
        assert executor.run() is Outcome.FAILED
        assert world.failure().step.id == LOGIN
        assert world.failure().error == error
        assert world.bao.data(leaf) == before and world.es.posts() == []
        assert executor.abort() is Outcome.CANCELLED

    def test_an_unreachable_elasticsearch_stops_the_plan_before_it_writes(self):
        world = World()
        refused = ConnectionRefusedError(111, "Connection refused")
        world.es.broken["GET", AUTHENTICATE] = refused
        assert world.run(KIBANA) is Outcome.FAILED
        assert world.failure().error == (
            f"GET {AUTHENTICATE}: transport error: ConnectionRefusedError(111, 'Connection "
            f"refused')"
        )
        assert world.password(KIBANA) == PASSWORDS["kibana_system"]


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


def es(world):
    return Elasticsearch(BASE, world.es)


def set_step(world, leaf=READER):
    world.leaf = leaf
    return SetPassword(es(world), leaf, KEY, USERS[leaf])


def staged_for(user):
    return {HELD: PASSWORDS[user], value_name(KEY): "SECRET-new"}


class TestTheSteps:
    def test_a_login_stages_the_password_elasticsearch_held_and_a_re_run_keeps_it(self):
        world = World()
        ctx = Ctx(world.bao)
        login = Login(es(world), READER, KEY, "reader")
        assert login.run(ctx) == "Elasticsearch takes reader and elastic"
        assert ctx.values == {HELD: PASSWORDS["reader"]}
        ctx.values[HELD] = "SECRET-staged-before"
        login.run(ctx)
        assert ctx.values == {HELD: "SECRET-staged-before"}

    def test_the_superuser_s_login_is_its_own(self):
        world = World()
        assert Login(es(world), SUPER, KEY, "elastic").run(Ctx(world.bao)) == (
            "Elasticsearch takes elastic"
        )
        assert world.es.requests == [("GET", AUTHENTICATE, "elastic")]

    def test_a_set_on_an_elasticsearch_that_takes_the_new_password_already_sets_nothing(self):
        world = World()
        world.es.users["reader"]["password"] = "SECRET-new"
        ctx = Ctx(world.bao, staged_for("reader"))
        assert set_step(world).run(ctx) == "Elasticsearch takes the new password already"
        assert world.es.posts() == [] and world.es.failed == 1

    def test_a_set_on_a_user_that_takes_neither_password_sets_the_new_one(self):
        world = World()
        world.es.users["reader"]["password"] = "SECRET-someone-else-s"
        ctx = Ctx(world.bao, staged_for("reader"))
        assert set_step(world).run(ctx) == "set; Elasticsearch takes a login with it"
        assert world.es.password("reader") == "SECRET-new"

    def test_a_set_logs_in_with_the_new_password_after_it_sets_it(self):
        world = World()
        world.es.set = lambda login, user, req: FakeResponse(200, b"{}")
        ctx = Ctx(world.bao, staged_for("reader"))
        with pytest.raises(StepFailed) as e:
            set_step(world).run(ctx)
        assert str(e.value) == "Elasticsearch refuses user reader with the new password"

    def test_a_set_elasticsearch_refuses_the_superuser_for_names_the_password(self):
        world = World()
        world.es.users["elastic"]["password"] = "SECRET-someone-else-s"
        with pytest.raises(StepFailed) as e:
            set_step(world).run(Ctx(world.bao, staged_for("reader")))
        assert str(e.value) == f"Elasticsearch refuses user elastic with the password {SUPER} holds"
        with pytest.raises(StepFailed) as e:
            set_step(world, SUPER).run(Ctx(world.bao, staged_for("elastic")))
        assert str(e.value) == "Elasticsearch refuses user elastic with the password it held"

    def test_a_login_elasticsearch_answers_otherwise_than_refusing_is_its_failure(self):
        world = World()
        world.es.refused["GET", AUTHENTICATE] = 503
        with pytest.raises(ElasticsearchError) as e:
            set_step(world).run(Ctx(world.bao, staged_for("reader")))
        assert str(e.value) == f"GET {AUTHENTICATE}: HTTP 503: refused"
        assert world.es.requests == [("GET", AUTHENTICATE, "reader")]

    def test_an_undo_on_an_elasticsearch_that_takes_the_held_password_sets_nothing(self):
        world = World()
        ctx = Ctx(world.bao, staged_for("reader"))
        assert set_step(world).undo(ctx) == (
            "Elasticsearch takes the password it held: nothing to set back"
        )
        assert world.es.posts() == []

    @pytest.mark.parametrize("leaf", [READER, SUPER])
    def test_an_undo_sets_the_held_password_back(self, leaf):
        world, user = World(), USERS[leaf]
        ctx = Ctx(world.bao, staged_for(user))
        step = set_step(world, leaf)
        assert step.run(ctx) == "set; Elasticsearch takes a login with it"
        assert world.es.password(user) == "SECRET-new"
        assert step.undo(ctx) == "set back; Elasticsearch takes a login with the password it held"
        assert world.es.password(user) == PASSWORDS[user]
        assert world.es.failed == 0

    @pytest.mark.parametrize(
        ("staged", "error"),
        [
            (
                {value_name(KEY): "SECRET-new"},
                "no password Elasticsearch held before the plan is staged",
            ),
            ({HELD: PASSWORDS["reader"]}, "no new password is staged"),
        ],
    )
    def test_a_set_and_its_undo_need_both_passwords_staged(self, staged, error):
        world = World()
        step = set_step(world)
        for do in (step.run, step.undo):
            with pytest.raises(StepFailed, match=f"^{error}$"):
                do(Ctx(world.bao, staged))
        assert world.es.requests == []


class TestTheClient:
    def test_it_logs_in_by_basic_auth_and_sends_the_password_as_json(self):
        world = World()
        world.leaf = READER
        world.es.users["elastic"]["password"] = "SECRET-a:b"
        client = es(world)
        assert client.authenticate("elastic", "SECRET-a:b")["username"] == "elastic"
        client.set_password("elastic", "SECRET-a:b", "reader", 'SECRET-"c"')
        assert world.es.password("reader") == 'SECRET-"c"'

    def test_a_refused_login_is_no_failure_of_takes_and_names_the_user_otherwise(self):
        client = es(World())
        assert client.takes("reader", PASSWORDS["reader"]) is True
        assert client.takes("reader", "SECRET-wrong") is False
        with pytest.raises(ElasticsearchError) as e:
            client.authenticate("reader", "SECRET-wrong")
        assert str(e.value) == (
            f"GET {AUTHENTICATE}: HTTP 401: unable to authenticate user [reader] for REST "
            f"request [{AUTHENTICATE}]"
        )
        assert e.value.status == 401

    @pytest.mark.parametrize(
        ("raised", "error"),
        [
            (
                urllib.error.HTTPError(BASE, 502, "Bad Gateway", {}, io.BytesIO(b"<html>")),
                f"GET {AUTHENTICATE}: HTTP 502",
            ),
            (
                urllib.error.URLError("Name or service not known"),
                f"GET {AUTHENTICATE}: transport error: Name or service not known",
            ),
        ],
    )
    def test_a_failure_names_the_request(self, raised, error):
        def opener(req):
            raise raised

        with pytest.raises(ElasticsearchError) as e:
            Elasticsearch(BASE, opener).authenticate("elastic", "SECRET-x")
        assert str(e.value) == error

    def test_an_answer_that_is_not_json_is_a_failure(self):
        with pytest.raises(
            ElasticsearchError, match=f"^GET {AUTHENTICATE}: HTTP 200, not a JSON answer$"
        ):
            Elasticsearch(BASE, lambda req: FakeResponse(200, b"<html>")).authenticate(
                "elastic", "SECRET-x"
            )
