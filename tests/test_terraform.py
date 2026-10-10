"""The terraform kind (design §6, §3.7; slice 048): a marker leaf whose args point at what re-mints
its credential: the deploy repo and its directory, the Argo CD Application whose PreSync hook
applies it, the keeper in rotation_epoch and the Secret Terraform writes. Its plan commits a new
keeper through GitHub's contents API under the kind's own token, waits for the Application's sync
of a revision that contains the commit, proves that the Secret changed (ruling F1), rewrites the
marker, restarts every workload that reads the Secret (ruling D1) and stamps. Nothing undoes the
commit, and a plan taken up past its commit by another process commits afresh before it proves
anything."""

import base64
import datetime
import hashlib
import json
import re

import pytest
from fake_cluster import HEAD, FakeCluster, application, pod_spec, secret, snapshot, workload
from fake_github import CONTENTS_TOKEN, FakeGitHub
from fixtures import annotated
from plans import Recorder, client, fake_of, flight_of, lock, run_state, state_of

from secret_rotator.argocdsteps import ArgocdSync
from secret_rotator.audit import Leaf, audit
from secret_rotator.cluster import Cluster, Workload
from secret_rotator.contract import MARKER_VALUE
from secret_rotator.executor import AbortRefused, Executor, Outcome
from secret_rotator.kinds.terraform import Terraform, keepers
from secret_rotator.kinds.terraform.steps import CONFLICT_TRIES, CREDENTIALS, NO_UNDO
from secret_rotator.model import Finished, Progress
from secret_rotator.plan import PlanError, make

KIND = "terraform"
LEAF = "rotator/terraform/iot/db"
REPO = "pvginkel/IotDeploy"
APP = "iot-prd"
NS = "iot-prd"
SECRET = "iotsupport-db"
FILE = "config/prd/rotation.tfvars"
ARGS = {"repo": REPO, "path": "", "app": APP, "keeper": "postgres", "secret": f"{NS}/{SECRET}"}
HOOK = {"repo": f"https://github.com/{REPO}.git", "revision": "$ARGOCD_APP_REVISION"} | {
    "stage": "prd",
    "namespace": NS,
}
EMPTY = "rotation_epoch = {}\n"
COMMIT = "terraform.commit_keeper:iot-prd/postgres"
SYNC = "argocd.sync:iot-prd"
PROVE = "terraform.prove_remint:iot-prd/iotsupport-db"
POD = "iot-prd/deployment/iotsupport"
PLAN = ["terraform.marker:password", COMMIT, SYNC, PROVE, "kv.write", f"k8s.rollout:{POD}"]
PLAN += ["kv.stamp"]
START = datetime.datetime(2026, 10, 5, 2, 0, tzinfo=datetime.UTC)
VALUE = re.compile(r"2026-10-0[56]T02:\d\d:\d\dZ")


def b64(text):
    return base64.b64encode(text.encode()).decode()


def clock(start=START):
    """An executor clock a second further on each call."""
    times = iter(start + datetime.timedelta(seconds=n) for n in range(10_000))
    return lambda: next(times)


def store_of(args=ARGS, activate="none"):
    """The marker leaf and the kind's token leaf."""
    entry = {"kind": KIND, "args": args, "interval": "14d", "activate": activate}
    token = {"kind": "manual", "args": {"type": "github-pat"}, "interval": "365d"}
    return {
        LEAF: Leaf(LEAF, {"password"}, annotated({"password": entry})),
        CREDENTIALS: Leaf(
            CREDENTIALS, {"token"}, annotated({"token": token | {"activate": "none"}})
        ),
    }


def objects(hook=HOOK, *, readers=True):
    """(resource, object) of the app as prd holds it: its Application, the Secret Terraform
    writes, a CronJob and, with readers, a Deployment that read it, and a Deployment that does
    not."""
    found = [
        ("applications", application(APP, hook=hook)),
        ("secrets", secret(NS, SECRET, username="iotsupport", password="SECRET-at-install")),
        ("deployments", workload("Deployment", NS, "other", pod_spec(env=["other"]), app=APP)),
        (
            "cronjobs",
            {
                "metadata": {"namespace": NS, "name": "backup"},
                "spec": {"jobTemplate": {"spec": {"template": {"spec": pod_spec(env=[SECRET])}}}},
            },
        ),
    ]
    if readers:
        reader = workload("Deployment", NS, "iotsupport", pod_spec(env=[SECRET]), app=APP)
        found.append(("deployments", reader))
    return found


class World:
    """The marker on the fake OpenBao with the kind's token, the deploy repo on the fake GitHub,
    whose pushes reach Argo CD's auto-sync, and the app on the fake cluster, whose PreSync hook's
    Terraform re-mints the password when the keeper it reads at the synced revision changed, as
    long as the keeper is wired to the password."""

    def __init__(
        self, *, args=ARGS, hook=HOOK, readers=True, activate="none", text=EMPTY, file=FILE
    ):
        self.store = store_of(args, activate)
        self.bao = fake_of(
            self.store,
            {LEAF: {"password": MARKER_VALUE}, CREDENTIALS: {"token": CONTENTS_TOKEN}},
        )
        self.file = file
        self.github = FakeGitHub()
        self.github.repo(REPO, {file: text, "config/prd/values.yaml": "image: 1\n"}, sha=HEAD)
        self.cluster = FakeCluster(objects(hook, readers=readers))
        self.github.on_commit.append(lambda repo, sha: self.cluster.push(APP, sha))
        self.cluster.presync[APP] = self.hook
        self.wired = True
        self.applied = None  # the keeper value the hook's Terraform last applied
        self.kind = Terraform()

    def hook(self, revision):
        held = self.github.file_at(REPO, revision, self.file)
        value = keepers.parse(held)[1].get("postgres")
        if self.wired and value != self.applied:
            self.applied = value
            self.secret()["data"]["password"] = b64(f"SECRET-minted-{value}")

    def secret(self):
        return self.cluster.get("secrets", NS, SECRET)

    def plan(self, cluster=None, derived=None):
        return make(
            {KIND: self.kind},
            LEAF,
            KIND,
            ["password"],
            audit(self.store),
            cluster or Cluster(self.cluster.kube()),
            derived=derived,
            github=self.github.github(),
        )

    def executor(self, plan=None, start=START):
        self.recorder = Recorder()
        return Executor(
            client(self.bao),
            plan or self.plan(),
            self.recorder,
            lock(self.bao),
            state=run_state(self.bao),
            dry_run=False,
            clock=clock(start),
        )

    def resumed(self, start):
        """The executor of another process that takes the leaf's plan in flight up."""
        self.kind = Terraform()
        return self.executor(self.plan(derived=flight_of(self.bao, LEAF).derived), start)

    def commits(self):
        """The commits on main after the first."""
        return self.github.repos[REPO][1:]

    def keeper(self, commit):
        return keepers.parse(commit["files"][self.file])[1]["postgres"]

    def restarted(self, name):
        template = self.cluster.get("deployments", NS, name)["spec"]["template"]
        return "kubectl.kubernetes.io/restartedAt" in template["metadata"]["annotations"]

    def failure(self):
        (failed,) = self.recorder.failures()
        return failed


def ids(plan):
    return [step.id for step in plan.steps]


def progress(world):
    return [e for e in world.recorder.events if isinstance(e, Progress)]


def finished(world):
    """The detail of each step's finished run, by its id."""
    return {e.step.id: e.detail for e in world.recorder.events if isinstance(e, Finished)}


def digest(data):
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


class TestTheArgs:
    def test_a_marker_names_the_repo_the_directory_the_application_the_keeper_and_the_secret(self):
        kind = Terraform()
        assert kind.args_problems(ARGS) == []
        assert kind.args_problems({k: v for k, v in ARGS.items() if k != "path"}) == []
        assert kind.args_problems(ARGS | {"path": "apps/iot"}) == []

    def test_what_is_wrong_with_them(self):
        bad = {"repo": "IotDeploy", "path": "/abs", "app": "Iot_prd", "keeper": "-x"}
        assert Terraform().args_problems(bad | {"secret": "iotsupport-db", "key": "x"}) == [
            "key: not one of terraform's repo, path, app, keeper, secret",
            "repo: not <owner>/<repo>",
            "app: not an Argo CD Application's name",
            "keeper: not a keeper's name",
            "secret: not <namespace>/<name>",
            "path: not a directory of the repo, nor empty for its root",
        ]


class TestThePlan:
    def test_commit_sync_proof_marker_restart_and_stamp_with_no_operator_step(self):
        world = World()
        plan = world.plan()
        assert ids(plan) == PLAN
        assert not plan.needs_operator and plan.ask == ""
        assert world.kind.credential(plan.target) == "Terraform-minted credential"
        sync = plan.steps[2]
        assert isinstance(sync, ArgocdSync) and sync.pushed is plan.steps[1]
        assert not sync.healthy  # the restarts judge the Application's health
        assert plan.steps[-1].consumers == (POD,)
        assert plan.description == (
            "The tool commits a new postgres keeper to pvginkel/IotDeploy, waits for Argo CD to "
            "sync iot-prd, whose PreSync hook's Terraform re-mints the credential into Secret "
            "iot-prd/iotsupport-db, and proves that the Secret changed. It records the rotation "
            "on this marker leaf, restarts every workload that reads the Secret."
        )

    def test_a_secret_cron_jobs_alone_read_restarts_nothing_and_the_sync_judges_health(self):
        plan = World(readers=False).plan()
        assert ids(plan) == [step for step in PLAN if not step.startswith("k8s.rollout")]
        assert plan.steps[2].healthy

    def test_a_snapshot_of_prd_plans_the_same(self, tmp_path):
        path = tmp_path / "snapshot.json"
        path.write_text(json.dumps(snapshot(objects())))
        assert ids(World().plan(Cluster.of_snapshot(path))) == PLAN

    def test_an_offline_plan_without_a_snapshot_cannot_be_built(self):
        world = World()
        with pytest.raises(PlanError) as e:
            make({KIND: world.kind}, LEAF, KIND, ["password"], audit(world.store))
        assert str(e.value) == (
            "rotator/terraform/iot/db: its plan reads Argo Application iot-prd and Secret "
            "iot-prd/iotsupport-db on the cluster, which an offline plan without a snapshot does "
            "not reach"
        )

    def test_a_plan_in_flight_restarts_what_it_derived_when_it_started(self):
        world = World()
        plan = world.plan()
        assert plan.derived.workloads == {
            f"secret:{NS}/{SECRET}": [Workload(NS, "deployment", "iotsupport")]
        }
        late = workload("Deployment", NS, "late", pod_spec(env_from=[SECRET]))
        world.cluster.add("deployments", late)
        assert "k8s.rollout:iot-prd/deployment/late" in ids(world.plan())
        assert ids(world.plan(derived=plan.derived)) == PLAN

    def test_the_marker_s_activate_is_free_and_comes_last(self):
        plan = World(activate="argocd-sync:dashboard-prd").plan()
        assert ids(plan) == [*PLAN[:-1], "argocd.sync:dashboard-prd", "kv.stamp"]
        with pytest.raises(PlanError, match="repeats step id\\(s\\) argocd.sync:iot-prd"):
            World(activate=f"argocd-sync:{APP}").plan()


class TestARotation:
    def test_it_commits_the_keeper_syncs_proves_the_re_mint_restarts_and_stamps(self):
        world = World()
        before = dict(world.secret()["data"])
        executor = world.executor()
        assert executor.run() is Outcome.DONE, world.recorder.failures()
        (commit,) = world.commits()
        value = world.keeper(commit)
        assert VALUE.fullmatch(value)
        assert commit["message"] == "rotate postgres (secret-rotator)"
        assert commit["files"][FILE] == f'rotation_epoch = {{\n  "postgres" = "{value}"\n}}\n'
        after = world.secret()["data"]
        assert after["password"] == b64(f"SECRET-minted-{value}") != before["password"]
        assert world.restarted("iotsupport") and not world.restarted("other")
        assert world.bao.data(LEAF)["password"].startswith(f"{MARKER_VALUE}; rotated ")
        state = state_of(world.bao, LEAF)
        assert (state.status, state.stamps, state.consumers) == (
            "ok",
            {"password": "2026-10-05"},
            (POD,),
        )
        assert flight_of(world.bao, LEAF) is None
        details = finished(world)
        assert details[COMMIT] == f"{commit['sha'][:7]}: postgres = {value} in {FILE}"
        assert details[PROVE] == "Secret iot-prd/iotsupport-db changed"
        # The fingerprints are the process's alone: not staged, not in the run state, not shown.
        written = json.dumps(world.bao.requests) + repr(world.recorder.events)
        assert digest(before) not in written and digest(dict(after)) not in written

    def test_the_file_keeps_its_other_keepers_and_comments_and_stays_canonical(self):
        text = '# Written by the secret rotator.\nrotation_epoch = {\n  s3 = "2026-09-21"\n}\n'
        world = World(text=text)
        assert world.executor().run() is Outcome.DONE, world.recorder.failures()
        (commit,) = world.commits()
        assert commit["files"][FILE] == (
            "# Written by the secret rotator.\nrotation_epoch = {\n"
            f'  "postgres" = "{world.keeper(commit)}"\n  "s3"       = "2026-09-21"\n}}\n'
        )

    def test_an_app_in_a_directory_of_its_repo_takes_the_keeper_file_there(self):
        file = f"apps/iot/{FILE}"
        args = ARGS | {"path": "apps/iot"}
        world = World(args=args, hook=HOOK | {"path": "apps/iot"}, file=file)
        assert world.executor().run() is Outcome.DONE, world.recorder.failures()
        (commit,) = world.commits()
        assert set(commit["files"]) == {file, "config/prd/values.yaml"}
        assert VALUE.fullmatch(world.keeper(commit))

    def test_a_push_to_another_file_meanwhile_does_not_conflict(self):
        world = World()
        world.github.racing.append(
            lambda repo: world.github.push(repo, "config/prd/values.yaml", "image: 2\n")
        )
        assert world.executor().run() is Outcome.DONE, world.recorder.failures()
        pin, keeper = world.commits()
        assert keeper["files"]["config/prd/values.yaml"] == "image: 2\n"
        puts = [r for r in world.github.requests if r[0] == "PUT"]
        assert len(puts) == 1

    def test_a_push_to_the_file_meanwhile_is_read_again_and_kept(self):
        world = World()
        theirs = 'rotation_epoch = {\n  "s3" = "2026-10-04T00:00:00Z"\n}\n'
        world.github.racing.append(lambda repo: world.github.push(repo, FILE, theirs))
        assert world.executor().run() is Outcome.DONE, world.recorder.failures()
        _, ours = world.commits()
        assert keepers.parse(ours["files"][FILE])[1] == {
            "postgres": world.keeper(ours),
            "s3": "2026-10-04T00:00:00Z",
        }
        progressed = [e.detail for e in progress(world) if e.step.id == COMMIT]
        assert f"{FILE} changed as it was committed: reading it again" in progressed

    def test_a_file_that_conflicts_at_every_try_fails_it_without_a_commit(self):
        world = World()
        for n in range(CONFLICT_TRIES):
            text = f'rotation_epoch = {{\n  "s3" = "2026-10-04T00:00:0{n}Z"\n}}\n'
            world.github.racing.append(lambda repo, text=text: world.github.push(repo, FILE, text))
        executor = world.executor()
        assert executor.run() is Outcome.FAILED
        assert world.failure().error == (
            f"pvginkel/IotDeploy {FILE} changed under each of {CONFLICT_TRIES} commits"
        )
        assert all("rotate postgres" not in c["message"] for c in world.commits())
        assert executor.abort() is Outcome.CANCELLED

    def test_a_commit_github_refuses_did_not_land(self):
        world = World()
        world.github.refused["PUT", f"/repos/{REPO}/contents/{FILE}"] = 403
        executor = world.executor()
        assert executor.run() is Outcome.FAILED
        assert world.failure().error.startswith(f"PUT /repos/{REPO}/contents/{FILE}: HTTP 403")
        assert world.commits() == []
        assert executor.abort() is Outcome.CANCELLED

    @pytest.mark.parametrize(
        ("knob", "answer", "error"),
        [
            ("broken", ConnectionResetError(), "transport error"),
            ("refused", 502, "HTTP 502"),  # a 5xx can come after GitHub wrote the commit
        ],
    )
    def test_a_commit_whose_answer_is_lost_counts_as_landed_and_is_not_rolled_back(
        self, knob, answer, error
    ):
        world = World()
        getattr(world.github, knob)["PUT", f"/repos/{REPO}/contents/{FILE}"] = answer
        executor = world.executor()
        assert executor.run() is Outcome.FAILED
        assert error in world.failure().error
        with pytest.raises(AbortRefused, match=NO_UNDO):
            executor.abort()

    @pytest.mark.parametrize(
        ("args", "hook", "why"),
        [
            (
                ARGS,
                HOOK | {"repo": "https://github.com/pvginkel/HomelabAppsDeploy.git"},
                "Argo Application iot-prd's hook applies "
                "https://github.com/pvginkel/HomelabAppsDeploy.git, not the marker's "
                "pvginkel/IotDeploy",
            ),
            (
                ARGS,
                HOOK | {"path": "apps/iot"},
                "Argo Application iot-prd's hook applies apps/iot of pvginkel/IotDeploy, not "
                "the marker's root",
            ),
            (
                ARGS | {"path": "apps/iot"},
                HOOK,
                "Argo Application iot-prd's hook applies the root of pvginkel/IotDeploy, not "
                "the marker's apps/iot",
            ),
            (ARGS, None, "Argo Application iot-prd has no hook.stage: no PreSync hook applies it"),
        ],
    )
    def test_a_marker_the_application_s_hook_disagrees_with_fails_before_its_commit(
        self, args, hook, why
    ):
        world = World(args=args, hook=hook)
        executor = world.executor()
        assert executor.run() is Outcome.FAILED
        assert world.failure().step.id == COMMIT and world.failure().error == why
        assert world.github.requests == [] and world.commits() == []
        assert executor.abort() is Outcome.CANCELLED

    def test_an_application_on_another_branch_fails_before_its_commit(self):
        world = World()
        world.cluster.get("applications", "argocd-prd", APP)["spec"]["source"]["targetRevision"] = (
            "release"
        )
        assert world.executor().run() is Outcome.FAILED
        assert world.failure().error == "Argo Application iot-prd tracks release, not main"
        assert world.github.requests == []

    @pytest.mark.parametrize(
        ("text", "why"),
        [
            (None, f"pvginkel/IotDeploy has no {FILE} on main"),
            (
                'stage = "prd"\n',
                f"pvginkel/IotDeploy {FILE}: not one `rotation_epoch = {{ … }}` map",
            ),
            (
                'rotation_epoch = {\n  "postgres" = "2099-01-01T00:00:00Z"\n}\n',
                f"pvginkel/IotDeploy {FILE} holds postgres = 2099-01-01T00:00:00Z, which does not "
                "sort before the new 2026-10-05T02:00:01Z: a value it held re-mints nothing",
            ),
        ],
    )
    def test_a_keeper_file_it_cannot_take_fails_it_before_its_commit(self, text, why):
        world = World(text="")
        world.github.repos[REPO][0]["files"] = {} if text is None else {FILE: text}
        executor = world.executor()
        assert executor.run() is Outcome.FAILED
        assert world.failure().error == why and world.commits() == []
        assert executor.abort() is Outcome.CANCELLED

    def test_without_the_kind_s_token_nothing_is_committed(self):
        world = World()
        del world.bao.leaves[CREDENTIALS]
        executor = world.executor()
        assert executor.run() is Outcome.FAILED
        assert world.failure().error == (
            f"{CREDENTIALS} cannot be read: no such leaf, or its current version is deleted"
        )
        assert executor.abort() is Outcome.CANCELLED


class TestTheProof:
    def test_a_secret_terraform_did_not_re_mint_fails_it_before_any_restart(self):
        world = World()
        world.wired = False
        executor = world.executor()
        assert executor.run() is Outcome.FAILED
        (commit,) = world.commits()
        failed = world.failure()
        assert failed.step.id == PROVE
        assert failed.error == (
            f"Secret iot-prd/iotsupport-db did not change: the sync of {commit['sha'][:7]} took "
            f"postgres = {world.keeper(commit)} in pvginkel/IotDeploy {FILE}, and its Terraform "
            f"re-minted nothing"
        )
        assert not world.restarted("iotsupport")
        assert world.bao.data(LEAF)["password"] == MARKER_VALUE
        assert state_of(world.bao, LEAF).status == "failed"
        with pytest.raises(AbortRefused, match=NO_UNDO):
            executor.abort()

    def test_a_retry_in_the_process_that_committed_proves_it_against_a_later_sync(self):
        world = World()
        world.wired = False
        assert world.executor().run() is Outcome.FAILED
        world.wired = True  # the operator wires the keeper and pushes it
        world.github.push(REPO, "terraform/main.tf", "wired\n", "wire the keeper")
        executor = world.executor(world.plan(derived=flight_of(world.bao, LEAF).derived))
        assert executor.run() is Outcome.DONE, world.recorder.failures()
        assert [c["message"] for c in world.commits()] == [
            "rotate postgres (secret-rotator)",
            "wire the keeper",
        ]

    def test_another_process_past_the_commit_commits_afresh_before_it_proves_anything(self):
        world = World()
        world.cluster.sync_failing[APP] = "one or more synchronization tasks completed"
        executor = world.executor()
        assert executor.run() is Outcome.FAILED
        assert world.failure().step.id == SYNC
        assert state_of(world.bao, LEAF).status == "failed-activation"
        with pytest.raises(AbortRefused, match=NO_UNDO):
            executor.abort()
        del world.cluster.sync_failing[APP]
        resumed = world.resumed(START + datetime.timedelta(days=1))
        assert resumed.run() is Outcome.DONE, world.recorder.failures()
        first, fresh = world.commits()
        assert world.keeper(first) < world.keeper(fresh)
        assert world.secret()["data"]["password"] == b64(f"SECRET-minted-{world.keeper(fresh)}")
        (detail, *_) = [e.detail for e in progress(world) if e.step.id == PROVE]
        assert detail == f"{first['sha'][:7]} was committed by another process: committing afresh"
        assert world.restarted("iotsupport")
        assert state_of(world.bao, LEAF).stamps == {"password": "2026-10-06"}


class TestContains:
    def test_a_revision_has_the_commit_when_it_is_it_or_a_later_head(self):
        world = World()
        plan = world.plan()
        commit = plan.steps[1]
        made = world.github.push(REPO, FILE, 'rotation_epoch = {\n  "postgres" = "x"\n}\n')
        later = world.github.push(REPO, "config/prd/values.yaml", "image: 2\n")

        class Ctx:
            bao = client(world.bao)

        assert commit.contains(Ctx, made, made) and commit.contains(Ctx, later, made)
        assert not commit.contains(Ctx, HEAD, made)


def test_the_keeper_file_is_canonical_as_terraform_fmt_leaves_it():
    """Each form checked with `terraform fmt -check` (Terraform 1.16.3)."""
    assert keepers.render("", {}) == EMPTY
    assert keepers.render("", {"s3": "b", "iotsupport-db": "a"}) == (
        'rotation_epoch = {\n  "iotsupport-db" = "a"\n  "s3"            = "b"\n}\n'
    )
    text = '# kept\n\nrotation_epoch   =  {\n  postgres="a"\n\n  "s3" = "b"   \n}\n\n'
    assert keepers.parse(text) == ("# kept\n\n", {"postgres": "a", "s3": "b"})
    for bad in ("rotation_epoch = {\n  a = b\n}\n", "x = {}\n", "rotation_epoch = {}\nx = 1\n"):
        with pytest.raises(ValueError):
            keepers.parse(bad)
