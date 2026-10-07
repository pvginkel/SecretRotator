"""jenkins.job and jenkins.credential (design §4.2): Jenkins reached with the admin's API token from
rotator/jenkins; a job triggered by its full name with its parameters and waited on for SUCCESS; a
credential's one secret field updated by id and the credential re-read; the jenkins-job: and
jenkins-credential: activators (design §4.3) built into plans, run, failed and rolled back."""

import datetime

import pytest
from fake_jenkins import APPROLE, CREDENTIALS, TOKEN, YT, FakeJenkins, string_credential
from fake_openbao import FakeOpenBao
from plans import (
    COPY,
    LEAF,
    NOW,
    Recorder,
    client,
    fake_of,
    flight_of,
    lock,
    run_state,
    state_of,
)
from test_kinds import KINDS, store_of

from secret_rotator import terminal
from secret_rotator.audit import audit
from secret_rotator.executor import Executor, Outcome
from secret_rotator.jenkins import ADDR, JenkinsError, credential_path, job_path
from secret_rotator.jenkinssteps import (
    JOB_BOUND,
    JenkinsCredential,
    JenkinsJob,
    parse_job,
    with_secret,
)
from secret_rotator.model import Action, Skipped, StepFailed
from secret_rotator.plan import PlanError, StepFactory, make, target

WEBHOOK = "eso/prd/yt/prd/webhook"  # random; jenkins-job:YouTrack/YouTrackConfiguration?…
WEBHOOK_COPY = "jenkins/youtrack"  # its copy, key webhook-token
YT_ID = f"jenkins.job:{YT}?ROTATE_TOKEN=true"


def bao_with(data=None):
    return FakeOpenBao({"rotator/jenkins": {"data": dict(data or CREDENTIALS), "meta": {}}})


class Ctx:
    def __init__(self, bao=None, staged=None):
        self.bao = client(bao or bao_with())
        self.now = NOW
        self.values = dict(staged or {})
        self.progressed = []

    def progress(self, detail):
        self.progressed.append(detail)

    def staged(self, name):
        return self.values.get(name)


class TestTheClient:
    def test_a_job_s_path_is_its_full_name_a_segment_per_folder(self):
        assert job_path(YT) == "/job/YouTrack/job/YouTrackConfiguration"
        assert job_path("A folder/b?c") == "/job/A%20folder/job/b%3Fc"

    def test_a_refusal_names_the_request_never_the_token(self):
        fake = FakeJenkins()
        jenkins = fake.jenkins()
        jenkins.authenticate("admin", "SECRET-a-wrong-token")
        with pytest.raises(JenkinsError) as e:
            jenkins.credential_xml(APPROLE)
        path = f"/credentials/store/system/domain/_/credential/{APPROLE}/config.xml"
        assert str(e.value) == f"GET {path}: HTTP 401" and e.value.status == 401

    def test_a_transport_failure_is_a_jenkins_error_without_status(self):
        fake = FakeJenkins()
        fake.broken["POST", "/job/Plain/job/Job/build"] = TimeoutError("timed out")
        jenkins = fake.jenkins()
        jenkins.authenticate("admin", TOKEN)
        with pytest.raises(JenkinsError, match="transport error") as e:
            jenkins.trigger("Plain/Job", {})
        assert e.value.status is None

    def test_an_answer_that_names_no_queue_item_is_an_error(self):
        fake = FakeJenkins()
        fake.no_location = True
        jenkins = fake.jenkins()
        jenkins.authenticate("admin", TOKEN)
        with pytest.raises(JenkinsError, match="names no queue item"):
            jenkins.trigger("Plain/Job", {})


class TestJenkinsJob:
    def test_its_id_and_title_name_the_job_and_its_parameters(self):
        step = JenkinsJob(FakeJenkins().jenkins(), *parse_job(f"{YT}?ROTATE_TOKEN=true&X="))
        assert step.id == f"{YT_ID}&X="
        assert step.title == f"run Jenkins job {YT} with ROTATE_TOKEN=true, X="
        assert step.mutates and step.activator and step.undo is None
        assert JenkinsJob(FakeJenkins().jenkins(), "Plain/Job", {}).id == "jenkins.job:Plain/Job"

    def test_it_triggers_the_job_with_its_parameters_and_waits_for_success(self):
        fake = FakeJenkins()
        ctx = Ctx()
        step = JenkinsJob(fake.jenkins(), YT, {"ROTATE_TOKEN": "true"})
        assert step.run(ctx) == "build #1 SUCCESS"
        assert fake.requests[0][:3] == (
            "POST",
            "/job/YouTrack/job/YouTrackConfiguration/buildWithParameters",
            {"ROTATE_TOKEN": "true"},
        )
        assert fake.triggered() == [(YT, {"ROTATE_TOKEN": "true"})]
        assert ctx.progressed[0] == "queued: In the quiet period"
        assert ctx.progressed[-1] == "build #1 running"
        assert fake.now >= fake.queue_lag + fake.build_lag

    def test_a_job_without_parameters_is_built_by_build(self):
        fake = FakeJenkins()
        assert JenkinsJob(fake.jenkins(), "Plain/Job", {}).run(Ctx()) == "build #1 SUCCESS"
        assert fake.requests[0][:2] == ("POST", "/job/Plain/job/Job/build")

    @pytest.mark.parametrize("result", ["FAILURE", "UNSTABLE", "ABORTED"])
    def test_a_build_that_does_not_succeed_fails_the_step(self, result):
        fake = FakeJenkins()
        fake.jobs["Plain/Job"] = result
        with pytest.raises(StepFailed) as e:
            JenkinsJob(fake.jenkins(), "Plain/Job", {}).run(Ctx())
        assert e.value.error == f"Plain/Job build #1 ended {result}"
        assert e.value.technical == f"{ADDR}/job/Plain/job/Job/1/console"

    def test_a_cancelled_queue_item_fails_the_step(self):
        fake = FakeJenkins()
        fake.cancelling.add("Plain/Job")
        with pytest.raises(StepFailed, match="the queued build of Plain/Job was cancelled"):
            JenkinsJob(fake.jenkins(), "Plain/Job", {}).run(Ctx())

    def test_a_build_still_running_at_the_bound_fails_the_step(self):
        fake = FakeJenkins(build_lag=JOB_BOUND + 60)
        with pytest.raises(StepFailed) as e:
            JenkinsJob(fake.jenkins(), "Plain/Job", {}).run(Ctx())
        assert e.value.error == "Plain/Job did not succeed within 30 min: build #1 running"

    def test_a_retry_triggers_a_new_build(self):
        fake = FakeJenkins()
        step = JenkinsJob(fake.jenkins(), "Plain/Job", {})
        step.run(Ctx())
        assert step.run(Ctx()) == "build #2 SUCCESS"

    @pytest.mark.parametrize(
        ("data", "problem"),
        [
            (None, "rotator/jenkins cannot be read"),
            ({"user": "admin"}, "rotator/jenkins has no token"),
            ({"token": TOKEN}, "rotator/jenkins has no user"),
        ],
    )
    def test_without_the_admin_s_token_it_fails_before_asking_jenkins(self, data, problem):
        fake = FakeJenkins()
        bao = FakeOpenBao() if data is None else bao_with(data)
        with pytest.raises(StepFailed, match=problem):
            JenkinsJob(fake.jenkins(), "Plain/Job", {}).run(Ctx(bao))
        assert fake.requests == []


class TestJenkinsCredential:
    def test_from_staged_it_writes_the_one_secret_field_and_re_reads_the_credential(self):
        fake = FakeJenkins()
        step = JenkinsCredential(fake.jenkins(), APPROLE, staged="value:secret_id")
        ctx = Ctx(staged={"value:secret_id": "SECRET-new<&>secret-id"})
        assert step.run(ctx) == "updated; the re-read is the same credential"
        assert fake.secret(APPROLE) == "SECRET-new<&>secret-id"
        assert "<roleId>role-id-of-jenkins</roleId>" in fake.credentials[APPROLE]["xml"]
        assert step.run(ctx) == "updated; the re-read is the same credential"  # a retry

    def test_from_staged_it_mutates_and_has_no_undo(self):
        step = JenkinsCredential(FakeJenkins().jenkins(), APPROLE, staged="value:secret_id")
        assert (step.id, step.title) == (
            f"jenkins.credential:{APPROLE}",
            f"update Jenkins credential {APPROLE}",
        )
        assert step.mutates and not step.activator and step.undo is None
        assert step.no_undo == (
            f"Jenkins does not give a credential's value back: the one {APPROLE} held cannot be "
            f"written back"
        )

    def test_from_kv_it_takes_the_key_as_kv_holds_it_and_is_an_activator(self):
        fake = FakeJenkins()
        fake.add_string("app-token", "SECRET-old")
        bao = bao_with()
        bao.leaves[LEAF] = {"data": {"token": "SECRET-in-kv"}, "meta": {}}
        step = JenkinsCredential(fake.jenkins(), "app-token", kv=(LEAF, "token"))
        assert step.title == f"update Jenkins credential app-token from {LEAF}#token"
        assert step.mutates and step.activator and step.undo is None
        step.run(Ctx(bao))
        assert fake.secret("app-token") == "SECRET-in-kv"
        with pytest.raises(StepFailed, match=f"{LEAF}#nope cannot be read"):
            JenkinsCredential(fake.jenkins(), "app-token", kv=(LEAF, "nope")).run(Ctx(bao))

    def test_it_takes_its_value_from_exactly_one_place(self):
        with pytest.raises(ValueError):
            JenkinsCredential(FakeJenkins().jenkins(), APPROLE)
        with pytest.raises(ValueError):
            JenkinsCredential(FakeJenkins().jenkins(), APPROLE, kv=(LEAF, "token"), staged="v")

    def test_no_staged_value_fails_before_asking_jenkins_and_did_not_land(self):
        fake = FakeJenkins()
        with pytest.raises(StepFailed, match="no value is staged as value:secret_id") as e:
            JenkinsCredential(fake.jenkins(), APPROLE, staged="value:secret_id").run(Ctx())
        assert fake.requests == [] and not e.value.landed

    def test_without_the_admin_s_token_a_staged_value_did_not_land(self):
        fake = FakeJenkins()
        step = JenkinsCredential(fake.jenkins(), APPROLE, staged="v")
        with pytest.raises(StepFailed, match="rotator/jenkins cannot be read") as e:
            step.run(Ctx(FakeOpenBao(), staged={"v": "SECRET-x"}))
        assert fake.requests == [] and not e.value.landed

    @pytest.mark.parametrize(("credential", "n"), [("a-certificate", 2), ("a-username", 0)])
    def test_a_credential_without_exactly_one_secret_field_is_refused(self, credential, n):
        fake = FakeJenkins()
        step = JenkinsCredential(fake.jenkins(), credential, staged="v")
        with pytest.raises(StepFailed, match=f"has {n} secret fields, not one") as e:
            step.run(Ctx(staged={"v": "SECRET-x"}))
        assert [r for r in fake.requests if r[0] == "POST"] == [] and not e.value.landed

    def test_a_re_read_that_is_not_the_credential_written_fails_the_step(self):
        fake = FakeJenkins()
        fake.mangle.add(APPROLE)
        step = JenkinsCredential(fake.jenkins(), APPROLE, staged="v")
        with pytest.raises(StepFailed, match="is not the credential written") as e:
            step.run(Ctx(staged={"v": "SECRET-x"}))
        assert "SECRET" not in e.value.technical and e.value.landed

    def test_no_such_credential_is_jenkins_answer_and_a_staged_value_did_not_land(self):
        step = JenkinsCredential(FakeJenkins().jenkins(), "gone", staged="v")
        with pytest.raises(StepFailed) as e:
            step.run(Ctx(staged={"v": "SECRET-x"}))
        assert e.value.error == f"GET {credential_path('gone')}/config.xml: HTTP 404"
        assert not e.value.landed
        assert isinstance(e.value.__cause__, JenkinsError)
        assert "JenkinsError" in e.value.technical

    def test_a_write_jenkins_refuses_did_not_land(self):
        fake = FakeJenkins()
        fake.refused["POST", f"{credential_path(APPROLE)}/config.xml"] = 403
        step = JenkinsCredential(fake.jenkins(), APPROLE, staged="v")
        with pytest.raises(StepFailed) as e:
            step.run(Ctx(staged={"v": "SECRET-x"}))
        assert e.value.error == f"POST {credential_path(APPROLE)}/config.xml: HTTP 403"
        assert not e.value.landed
        assert fake.secret(APPROLE) == "SECRET-old-secret-id"

    @pytest.mark.parametrize(
        ("fault", "error"),
        [
            ({"refused": 502}, "HTTP 502"),
            ({"broken": TimeoutError("timed out")}, "transport error"),
        ],
    )
    def test_a_write_whose_outcome_is_unknown_counts_as_landed(self, fault, error):
        fake = FakeJenkins()
        request = ("POST", f"{credential_path(APPROLE)}/config.xml")
        if "refused" in fault:
            fake.refused[request] = fault["refused"]
        else:
            fake.broken[request] = fault["broken"]
        step = JenkinsCredential(fake.jenkins(), APPROLE, staged="v")
        with pytest.raises(JenkinsError, match=error):
            step.run(Ctx(staged={"v": "SECRET-x"}))

    def test_from_kv_a_failure_is_reported_as_it_is(self):
        fake = FakeJenkins()
        fake.add_string("app-token", "SECRET-old")
        fake.refused["POST", f"{credential_path('app-token')}/config.xml"] = 403
        bao = bao_with()
        bao.leaves[LEAF] = {"data": {"token": "SECRET-in-kv"}, "meta": {}}
        step = JenkinsCredential(fake.jenkins(), "app-token", kv=(LEAF, "token"))
        with pytest.raises(JenkinsError, match="HTTP 403"):
            step.run(Ctx(bao))
        with pytest.raises(JenkinsError, match="HTTP 404"):
            JenkinsCredential(fake.jenkins(), "gone", kv=(LEAF, "token")).run(Ctx(bao))

    def test_with_secret_puts_the_value_in_place_of_the_redaction_escaped(self):
        xml = with_secret(string_credential("x"), "a<b&c", "x")
        assert "<secret>a&lt;b&amp;c</secret>" in xml and "secret-redacted" not in xml


def ticking():
    times = iter(NOW + datetime.timedelta(seconds=n) for n in range(10_000))
    return lambda: next(times)


def run(bao, plan, *, dry_run=False, recorder=None):
    executor = Executor(
        client(bao),
        plan,
        recorder or Recorder(),
        lock(bao),
        state=run_state(bao),
        dry_run=dry_run,
        clock=ticking(),
    )
    return executor, executor.run()


def store_and_bao(**activate):
    store = store_of(**activate)
    bao = fake_of(store)
    bao.leaves["rotator/jenkins"] = {"data": dict(CREDENTIALS), "meta": {}}
    return store, bao


def plan_of(store, fake, leaf=LEAF):
    return make(KINDS, leaf, "random", ["token"], store, audit(store), jenkins=fake.jenkins())


class TestTheYouTrackWebhookToken:
    """The catalog's first jenkins-job: user: a random token, copied into jenkins/youtrack, which
    YouTrack/YouTrackConfiguration fans out to every project with ROTATE_TOKEN=true."""

    def test_its_plan_writes_the_copy_then_runs_the_job(self):
        store, _ = store_and_bao()
        plan = plan_of(store, FakeJenkins(), WEBHOOK)
        assert [s.id for s in plan.steps] == [
            "random.generate:token",
            "kv.write",
            f"kv.copy:{WEBHOOK_COPY}#webhook-token",
            YT_ID,
            "kv.stamp",
        ]
        line = (
            f"  4  tool  jenkins.job                   run Jenkins job {YT} with ROTATE_TOKEN=true"
        )
        assert line in terminal.plan_lines(plan)

    def test_it_runs_to_done(self):
        fake = FakeJenkins()
        store, bao = store_and_bao()
        _, outcome = run(bao, plan_of(store, fake, WEBHOOK))
        assert outcome is Outcome.DONE
        assert fake.triggered() == [(YT, {"ROTATE_TOKEN": "true"})]
        assert state_of(bao, WEBHOOK).status == "ok"

    def test_a_failed_build_stops_the_plan_and_abort_undoes_kv_then_runs_the_job_again(self):
        fake = FakeJenkins()
        fake.jobs[YT] = "FAILURE"
        store, bao = store_and_bao()
        old = bao.data(WEBHOOK_COPY)["webhook-token"]
        recorder = Recorder()
        executor, outcome = run(bao, plan_of(store, fake, WEBHOOK), recorder=recorder)
        assert outcome is Outcome.FAILED
        state = state_of(bao, WEBHOOK)
        assert state.status == "failed-activation"
        assert flight_of(bao, WEBHOOK).step == YT_ID
        assert state.last_error == f"{YT} build #1 ended FAILURE"
        fake.jobs[YT] = "SUCCESS"
        recorder.events.clear()
        assert executor.abort() is Outcome.ROLLED_BACK
        assert [(line[1], line[2]) for line in recorder.lines() if line[0] == "ok"] == [
            (f"kv.copy:{WEBHOOK_COPY}#webhook-token", Action.UNDO),
            ("kv.write", Action.UNDO),
            (YT_ID, Action.RERUN),
        ]
        assert bao.data(WEBHOOK_COPY)["webhook-token"] == old
        assert len(fake.triggered()) == 2

    def test_a_refusal_by_jenkins_is_the_step_s_failure_naming_the_request(self):
        fake = FakeJenkins()
        store, bao = store_and_bao()
        bao.leaves["rotator/jenkins"]["data"]["token"] = "SECRET-revoked"
        _, outcome = run(bao, plan_of(store, fake, WEBHOOK))
        assert outcome is Outcome.FAILED
        error = state_of(bao, WEBHOOK).last_error
        assert error == f"POST {job_path(YT)}/buildWithParameters?ROTATE_TOKEN=true: HTTP 401"

    def test_a_dry_run_asks_jenkins_nothing(self):
        fake = FakeJenkins()
        store, bao = store_and_bao()
        recorder = Recorder()
        _, outcome = run(bao, plan_of(store, fake, WEBHOOK), dry_run=True, recorder=recorder)
        assert outcome is Outcome.DRY_RUN
        assert all(isinstance(e, Skipped) for e in recorder.events)
        assert fake.requests == [] and bao.writes() == []


class TestActivators:
    def test_a_job_two_leaves_name_runs_once_at_the_first_one_s_place(self):
        store, _ = store_and_bao(
            eso__prd__app__prd__token="jenkins-job:Plain/Job,manual:look",
            iac__copy="jenkins-job:Plain/Job,jenkins-job:Other/Job",
        )
        plan = plan_of(store, FakeJenkins())
        assert [s.id for s in plan.steps][3:] == [
            "jenkins.job:Plain/Job",
            f"operator.confirm:{LEAF}:2",
            "jenkins.job:Other/Job",
            "kv.stamp",
        ]

    def test_a_credential_takes_the_one_key_the_plan_writes_to_its_leaf(self):
        store, _ = store_and_bao(
            eso__prd__app__prd__token="jenkins-credential:app-token",
            iac__copy="jenkins-credential:copy-token",
        )
        steps = plan_of(store, FakeJenkins()).steps
        creds = [s for s in steps if isinstance(s, JenkinsCredential)]
        assert [(s.id, s.kv) for s in creds] == [
            ("jenkins.credential:app-token", (LEAF, "token")),
            ("jenkins.credential:copy-token", (COPY, "token")),
        ]

    def test_a_credential_on_a_leaf_the_plan_writes_two_keys_to_refuses_the_plan(self):
        store, _ = store_and_bao(
            eso__prd__app__prd__token="none", iac__copy="jenkins-credential:copy-token"
        )
        store[COPY].keys.add("token2")
        store[COPY].meta["key_token2"] = f"copy:{LEAF}#token"
        with pytest.raises(
            PlanError,
            match=f"{LEAF}: {COPY}'s rotation_activate jenkins-credential:copy-token: the plan "
            f"writes token, token2 to {COPY}, and the spec does not say which one the credential "
            f"takes",
        ):
            plan_of(store, FakeJenkins())

    def test_abort_writes_the_restored_value_into_the_credential(self):
        fake = FakeJenkins()
        fake.add_string("app-token", "SECRET-held")
        fake.jobs["Plain/Job"] = "FAILURE"
        store, bao = store_and_bao(
            eso__prd__app__prd__token="jenkins-credential:app-token,jenkins-job:Plain/Job"
        )
        old = bao.data(LEAF)["token"]
        executor, outcome = run(bao, plan_of(store, fake))
        assert outcome is Outcome.FAILED
        assert fake.secret("app-token") == bao.data(LEAF)["token"] != old
        fake.jobs["Plain/Job"] = "SUCCESS"
        assert executor.abort() is Outcome.ROLLED_BACK
        assert bao.data(LEAF)["token"] == old and fake.secret("app-token") == old


class TestTheFactory:
    def test_it_builds_a_staged_credential_and_a_job_for_a_kind(self):
        store, _ = store_and_bao()
        fake = FakeJenkins()
        steps = StepFactory(
            target(LEAF, "random", ["token"], store, audit(store)), jenkins=fake.jenkins()
        )
        [cred] = steps.jenkins_credential(APPROLE, "value:secret_id")
        assert (cred.staged, cred.undo, cred.activator) == ("value:secret_id", None, False)
        [job] = steps.jenkins_job("Plain/Job", {"A": "1"})
        assert job.id == "jenkins.job:Plain/Job?A=1" and job.jenkins is cred.jenkins
