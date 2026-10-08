"""The jenkins-job-token kind (design §6) over the seed's one row, iot's trigger URL: its args; its
plans, which sync every ExternalSecret that reads the leaf and activate before the job's token is
set, whatever the leaf's activate; the runs, which leave a URL in KV that carries the token the
job holds; their rollbacks, which put the old token back once the consumers hold the old URL; the
failures that stop a plan before it writes; and the URL and config.xml the kind edits."""

import datetime
import json
from pathlib import Path

import pytest
from fake_cluster import FakeCluster, externalsecret, pod_spec, snapshot, workload
from fake_jenkins import CREDENTIALS, IOT, TRIGGER_TOKEN, FakeJenkins, job_config
from fixtures import edit
from plans import NOW, Recorder, client, fake_of, lock, run_state, state_of
from test_activation import ticking

from secret_rotator import annotate as ann
from secret_rotator.audit import audit
from secret_rotator.cluster import Cluster
from secret_rotator.executor import Executor, Outcome
from secret_rotator.jenkins import ADDR
from secret_rotator.kinds.jenkins_job_token import JenkinsJobToken, trigger
from secret_rotator.kinds.jenkins_job_token.steps import Generate, SetToken
from secret_rotator.model import StepFailed, value_name
from secret_rotator.plan import PlanError, make

SEED = ann.load_seed(ann.DEFAULT_SEED)
KIND = "jenkins-job-token"
LEAF = "eso/prd/iot/prd/architecture-pipeline"
KEY = "trigger_url"
ES = "iot-prd/iot-architecture-pipeline"
ROLLED = "iot-prd/deployment/iotsupport"
CONFIG = "/job/AaC/job/IoTSupport/config.xml"
# The URL's shape: "Trigger builds remotely" of the job (IotDeploy config/prd/values.yaml), POSTed
# as it is by IoTSupport's ArchitecturePipelineTriggerService.
URL = f"{ADDR}/job/AaC/job/IoTSupport/build?token={TRIGGER_TOKEN}"


def seed_store():
    return ann.offline_store(Path(str(ann.DEFAULT_KEYS)), SEED, lambda line: None)


def iot_objects():
    """(resource, object) of the leaf's readers as prd holds them (2026-10-08): the iotsupport
    Deployment reads iot-architecture-pipeline's trigger_url into its env."""
    return [
        (
            "externalsecrets",
            externalsecret(
                "iot-prd",
                "iot-architecture-pipeline",
                data=[(LEAF, KEY)],
                target="iot-architecture-pipeline",
            ),
        ),
        (
            "deployments",
            workload(
                "Deployment", "iot-prd", "iotsupport", pod_spec(env=["iot-architecture-pipeline"])
            ),
        ),
    ]


class World:
    """The seed's store on the fake OpenBao, the leaf holding URL; Jenkins, whose AaC/IoTSupport
    holds the token URL carries; and the leaf's readers on the fake cluster."""

    def __init__(self, store=None, url=URL):
        self.store = store or seed_store()
        self.bao = fake_of(self.store, {LEAF: {KEY: url}, "rotator/jenkins": dict(CREDENTIALS)})
        self.jenkins = FakeJenkins()
        self.cluster = FakeCluster(iot_objects())
        self.kind = JenkinsJobToken()
        self.posted = []  # (the token posted, the ExternalSecret's syncs, iotsupport's generation)
        self.jenkins.before_config = lambda job, xml: self.posted.append(
            (trigger.auth_token(xml), self.cluster.syncs, self.generation())
        )

    def url(self):
        return self.bao.data(LEAF)[KEY]

    def plan(self, *, cluster=True, store=None):
        return make(
            {KIND: self.kind},
            LEAF,
            KIND,
            [KEY],
            audit(store or self.store),
            Cluster(self.cluster.kube()) if cluster else None,
            jenkins=self.jenkins.jenkins(),
        )

    def executor(self, *, day=0, store=None):
        self.recorder = Recorder()
        tick = ticking()
        return Executor(
            client(self.bao),
            self.plan(store=store),
            self.recorder,
            lock(self.bao),
            state=run_state(self.bao),
            dry_run=False,
            clock=lambda: tick() + datetime.timedelta(days=day),
        )

    def run(self, *, day=0, store=None):
        return self.executor(day=day, store=store).run()

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

    def generation(self):
        return self.cluster.get("deployments", "iot-prd", "iotsupport")["metadata"]["generation"]

    def config_posts(self):
        return [r for r in self.jenkins.requests if r[:2] == ("POST", CONFIG)]


def ids(plan):
    return [s.id for s in plan.steps]


def activate_none():
    store = seed_store()
    edit(store[LEAF].meta, KEY, activate="none")
    return store


class TestTheSeed:
    def test_the_trigger_url_names_its_job_and_rotates_yearly(self):
        (entry,) = audit(seed_store()).entries[LEAF].values()
        assert (entry.kind, entry.args, entry.interval) == (KIND, {"job": IOT}, 365)
        assert [str(a) for a in entry.activate] == ["eso", "k8s-rollout"]
        assert JenkinsJobToken().args_problems(entry.args) == []

    @pytest.mark.parametrize(
        ("args", "problem"),
        [
            ({}, "job: not a Jenkins job's full name"),
            ({"job": ""}, "job: not a Jenkins job's full name"),
            ({"job": "AaC/"}, "job: not a Jenkins job's full name"),
            ({"job": "AaC/ IoTSupport"}, "job: not a Jenkins job's full name"),
            ({"job": 3}, "job: not a Jenkins job's full name"),
        ],
    )
    def test_args_it_cannot_use_are_named(self, args, problem):
        assert JenkinsJobToken().args_problems(args) == [problem]

    def test_an_arg_it_does_not_know_is_named(self):
        assert JenkinsJobToken().args_problems({"job": "AaC/Home Assistant Fleet", "x": 1}) == [
            "x: not one of jenkins-job-token's job"
        ]


class TestThePlans:
    def test_it_syncs_and_rolls_out_before_the_job_s_token_is_set(self):
        assert ids(World().plan()) == [
            "jenkins_job_token.generate",
            "kv.write",
            f"eso.sync:{ES}",
            f"k8s.rollout:{ROLLED}",
            "jenkins_job_token.set",
            "kv.stamp",
        ]

    def test_the_externalsecret_syncs_before_the_set_though_the_activate_is_none(self):
        assert ids(World().plan(store=activate_none())) == [
            "jenkins_job_token.generate",
            "kv.write",
            f"eso.sync:{ES}",
            "jenkins_job_token.set",
            "kv.stamp",
        ]

    def test_offline_without_a_snapshot_it_has_no_plan(self):
        with pytest.raises(PlanError, match="offline plan without a snapshot does not reach"):
            World().plan(cluster=False)

    def test_against_a_snapshot_it_plans_without_reaching_jenkins(self, tmp_path):
        path = tmp_path / "snapshot.json"
        path.write_text(json.dumps(snapshot(iot_objects())))
        world = World()
        plan = make(
            {KIND: world.kind},
            LEAF,
            KIND,
            [KEY],
            audit(world.store),
            Cluster.of_snapshot(path),
            jenkins=world.jenkins.jenkins(),
        )
        assert ids(plan) == ids(world.plan())
        assert world.jenkins.requests == []

    def test_a_leaf_with_two_keys_of_the_kind_has_no_plan(self):
        store = seed_store()
        store[LEAF].keys.add("other_url")
        edit(
            store[LEAF].meta,
            "other_url",
            kind=KIND,
            args={"job": IOT},
            interval="365d",
            activate="auto",
        )
        with pytest.raises(PlanError, match="a jenkins-job-token plan rotates one key, not 2"):
            make(
                {KIND: JenkinsJobToken()},
                LEAF,
                KIND,
                [KEY, "other_url"],
                audit(store),
                Cluster(FakeCluster(iot_objects()).kube()),
            )

    def test_the_steps_and_the_description(self):
        plan = World().plan()
        generate, set_token = plan.steps[0], plan.steps[4]
        assert (generate.mutates, generate.silent) == (False, True)
        assert (set_token.mutates, set_token.activator, set_token.undo) == (True, True, None)
        assert generate.title == f"generate a new remote-trigger token for Jenkins job {IOT}"
        assert set_token.title == (
            f"set the remote-trigger token of Jenkins job {IOT} from {LEAF}#{KEY}"
        )
        assert not plan.needs_operator and plan.ask == ""
        assert plan.description == (
            f"The tool puts a new remote-trigger token for Jenkins job {IOT} in the URL the leaf "
            f"holds and writes it to the leaf and activates what reads it. Once every "
            f"ExternalSecret that reads the leaf has synced, it sets the token on the job and "
            f"re-reads the job."
        )


class TestTheRuns:
    def test_a_rotation_leaves_a_url_that_carries_the_token_the_job_holds(self):
        world = World()
        assert world.run() is Outcome.DONE
        url = world.url()
        new = trigger.token_of(url)
        assert new != TRIGGER_TOKEN and len(new) == 43
        assert url == URL.replace(TRIGGER_TOKEN, new)
        assert world.jenkins.auth_token(IOT) == new
        assert state_of(world.bao, LEAF).stamps == {KEY: "2026-10-05"}
        assert not any(
            TRIGGER_TOKEN in text or new in text or ADDR in text for text in world.texts()
        )

    def test_the_job_s_token_is_set_only_once_the_secret_and_iotsupport_hold_the_new_url(self):
        world = World()
        assert world.run() is Outcome.DONE
        new = world.jenkins.auth_token(IOT)
        assert world.posted == [(new, 1, 2)]

    def test_with_activate_none_the_secret_syncs_before_the_set_and_nothing_rolls_out(self):
        world = World()
        assert world.run(store=activate_none()) is Outcome.DONE
        assert world.posted == [(world.jenkins.auth_token(IOT), 1, 1)]

    def test_the_next_rotation_replaces_the_token_the_first_set(self):
        world = World()
        assert world.run() is Outcome.DONE
        first = world.jenkins.auth_token(IOT)
        assert world.run(day=1) is Outcome.DONE
        assert world.jenkins.auth_token(IOT) == trigger.token_of(world.url()) != first

    def test_the_rest_of_the_job_s_config_is_as_it_was(self):
        world = World()
        before = world.jenkins.configs[IOT]
        assert world.run() is Outcome.DONE
        assert trigger.but_token(world.jenkins.configs[IOT]) == trigger.but_token(before)

    def test_a_set_jenkins_refuses_rolls_back_to_the_old_url_and_the_job_keeps_its_token(self):
        world = World()
        world.jenkins.refused["POST", CONFIG] = 403
        executor = world.executor()
        assert executor.run() is Outcome.FAILED
        failure = world.failure()
        assert failure.step.id == "jenkins_job_token.set"
        assert failure.error == f"POST {CONFIG}: HTTP 403"
        assert executor.abort_blocker() is None
        assert executor.abort() is Outcome.ROLLED_BACK
        assert world.url() == URL and world.jenkins.auth_token(IOT) == TRIGGER_TOKEN
        assert world.generation() == 3 and world.cluster.syncs == 2
        assert len(world.config_posts()) == 1

    def test_a_set_whose_re_read_differs_is_undone_once_the_consumers_hold_the_old_url(self):
        world = World()
        world.jenkins.mangle.add(IOT)
        executor = world.executor()
        assert executor.run() is Outcome.FAILED
        assert world.failure().error == (
            f"the re-read of Jenkins job {IOT} is not the job as read before but its token: "
            f"something else of it changed"
        )
        new = world.jenkins.auth_token(IOT)
        assert new == trigger.token_of(world.url()) != TRIGGER_TOKEN
        assert executor.abort() is Outcome.ROLLED_BACK
        assert world.url() == URL and world.jenkins.auth_token(IOT) == TRIGGER_TOKEN
        assert world.posted == [(new, 1, 2), (TRIGGER_TOKEN, 2, 3)]

    def test_a_set_whose_answer_is_lost_is_retried_and_finishes(self):
        world = World()
        world.jenkins.broken["POST", CONFIG] = TimeoutError("timed out")
        assert world.run() is Outcome.FAILED
        assert world.failure().step.id == "jenkins_job_token.set"
        world.jenkins.broken.clear()
        assert world.run() is Outcome.DONE
        assert world.jenkins.auth_token(IOT) == trigger.token_of(world.url()) != TRIGGER_TOKEN

    @pytest.mark.parametrize(
        ("held", "error"),
        [
            (
                "SECRET-another-token",
                f"Jenkins job {IOT} holds another remote-trigger token than the one {LEAF}#{KEY} "
                f"carries: the URL does not trigger that job",
            ),
            (
                None,
                f"Jenkins job {IOT} has no remote-trigger token: Trigger builds remotely is off",
            ),
        ],
    )
    def test_a_job_that_does_not_hold_the_url_s_token_stops_the_plan_before_it_writes(
        self, held, error
    ):
        world = World()
        world.jenkins.configs[IOT] = job_config(held)
        executor = world.executor()
        assert executor.run() is Outcome.FAILED
        assert world.failure().step.id == "jenkins_job_token.generate"
        assert world.failure().error == error
        assert world.url() == URL and world.config_posts() == []
        assert executor.abort() is Outcome.CANCELLED

    @pytest.mark.parametrize(
        ("url", "problem"),
        [
            (f"{ADDR}/job/AaC/job/IoTSupport/build", "has 0 token parameters, not one"),
            (f"{URL}&token={TRIGGER_TOKEN}", "has 2 token parameters, not one"),
            (f"{ADDR}/job/AaC/job/IoTSupport/build?token=", "has an empty token parameter"),
        ],
    )
    def test_a_url_that_carries_no_one_token_stops_the_plan_before_it_writes(self, url, problem):
        world = World(url=url)
        assert world.run() is Outcome.FAILED
        assert world.failure().error == f"the URL in {LEAF}#{KEY} {problem}"
        assert world.url() == url and world.jenkins.requests == []


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
    def test_a_generate_resumed_after_its_url_was_staged_keeps_it(self):
        world = World()
        ctx = Ctx(world.bao)
        generate = Generate(world.jenkins.jenkins(), IOT, LEAF, KEY)
        generate.run(ctx)
        staged = dict(ctx.values)
        assert list(staged) == [value_name(KEY)]
        assert generate.run(ctx) == f"a URL that carries a new token for Jenkins job {IOT}"
        assert ctx.values == staged

    def test_a_set_on_a_job_that_holds_the_token_already_posts_nothing(self):
        world = World()
        set_token = SetToken(world.jenkins.jenkins(), IOT, LEAF, KEY)
        assert set_token.run(Ctx(world.bao)) == f"Jenkins job {IOT} holds that token already"
        assert world.config_posts() == []

    def test_a_set_takes_its_token_from_the_url_kv_holds_when_it_runs(self):
        world = World()
        set_token = SetToken(world.jenkins.jenkins(), IOT, LEAF, KEY)
        world.bao.new_version(LEAF, {KEY: URL.replace(TRIGGER_TOKEN, "SECRET-new")})
        assert set_token.run(Ctx(world.bao)) == "set; the re-read of the job holds it"
        assert world.jenkins.auth_token(IOT) == "SECRET-new"
        world.bao.new_version(LEAF, {KEY: URL})
        set_token.run(Ctx(world.bao))
        assert world.jenkins.auth_token(IOT) == TRIGGER_TOKEN

    def test_a_set_whose_re_read_lacks_the_token_fails(self):
        world = World()
        world.bao.new_version(LEAF, {KEY: URL.replace(TRIGGER_TOKEN, "SECRET-new")})
        stored = world.jenkins.configs[IOT]
        original = world.jenkins.config

        def ignored(job, method, req):
            answer = original(job, method, req)
            world.jenkins.configs[IOT] = stored
            return answer

        world.jenkins.config = ignored
        set_token = SetToken(world.jenkins.jenkins(), IOT, LEAF, KEY)
        with pytest.raises(StepFailed) as e:
            set_token.run(Ctx(world.bao))
        assert str(e.value) == (
            f"the re-read of Jenkins job {IOT} does not hold the token {LEAF}#{KEY} carries"
        )

    def test_a_leaf_without_the_key_cannot_be_read(self):
        world = World()
        world.bao.new_version(LEAF, {"other": "x"})
        with pytest.raises(StepFailed, match=f"^{LEAF}#{KEY} cannot be read$"):
            SetToken(world.jenkins.jenkins(), IOT, LEAF, KEY).run(Ctx(world.bao))


class TestTheTrigger:
    def test_a_new_token_keeps_every_other_part_of_the_url_as_written(self):
        url = "https://jenkins.example/job/A%20B/job/C/buildWithParameters?cause=a+b&TOKEN=x&token=old#f"
        assert trigger.token_problem(url) is None and trigger.token_of(url) == "old"
        assert trigger.with_token(url, "new") == url.replace("token=old", "token=new")

    def test_the_token_is_read_decoded_and_written_encoded(self):
        url = "https://jenkins.example/job/C/build?to%6Ben=a%2Fb"
        assert trigger.token_of(url) == "a/b"
        assert trigger.with_token(url, "c/d") == "https://jenkins.example/job/C/build?to%6Ben=c%2Fd"

    def test_the_job_s_token_is_the_authtoken_under_its_root(self):
        xml = job_config("SECRET-t")
        assert trigger.auth_token(xml) == "SECRET-t"
        assert trigger.auth_token(job_config(None)) is None
        assert trigger.auth_token(b"<project><x><authToken>no</authToken></x></project>") is None

    def test_a_new_authtoken_leaves_the_rest_of_the_config_as_it_was(self):
        xml = job_config("SECRET-old")
        changed = trigger.with_auth_token(xml, "SECRET-new&<")
        assert trigger.auth_token(changed) == "SECRET-new&<"
        assert trigger.but_token(changed) == trigger.but_token(xml)
        assert trigger.but_token(changed) != trigger.but_token(job_config(None))
