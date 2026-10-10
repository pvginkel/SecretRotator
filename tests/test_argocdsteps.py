"""argocd.sync (design §4.2): an activator with one Application, synced through its object. With a
pushed commit it waits for a sync that succeeded at a revision that contains the commit, the
commit or a later head, left to auto-sync for a grace; without one, for any sync that started once
it ran. It requests a sync itself only while no operation runs, of the tracked branch's head, never
a pinned revision, and verifies Synced and, unless told to leave it, Healthy (slice 048, ruling
F2)."""

import pytest
from fake_cluster import ARGO, HEAD, RETRY, FakeCluster
from test_k8ssteps import NOW

from secret_rotator.argocdsteps import AUTO_SYNC_GRACE, ArgocdSync
from secret_rotator.cluster import Cluster
from secret_rotator.kube import KubeError
from secret_rotator.model import StepFailed

APP = "app-prd"
PATH = f"{ARGO}/{APP}"
COMMIT = "b2c4e6a8f0d1c3e5a7b9d1f3e5c7a9b1d3f5e7c9"  # the commit a step before it pushed
LATER = "d4e6f8a0b2c4d6e8f0a2b4c6d8e0f2a4b6c8d0e2"  # a later head, which has it
OTHER = "0e1f2a3b4c5d6e7f8a9b0c1d2e3f4a5b6c7d8e9f"  # a head that does not have it
REQUESTED = "requested a sync of the head of the branch it tracks"


class Ctx:
    def __init__(self, staged=None):
        self.now = NOW
        self.progressed = []
        self.values = dict(staged or {})

    def progress(self, detail):
        self.progressed.append(detail)

    def stage(self, name, value):
        self.values[name] = value

    def staged(self, name):
        return self.values.get(name)


class Pushed:
    """COMMIT, staged as commit; whether a revision contains it is answered from history."""

    staged = "commit"

    def __init__(self, history=None):
        self.history = history or {LATER: {COMMIT}}  # revision -> the commits it has
        self.asked = []

    def contains(self, ctx, revision, commit):
        self.asked.append(revision)
        return commit in self.history.get(revision, ())


def pushed_sync(fake, **kwargs):
    pushed = Pushed()
    return ArgocdSync(Cluster(fake.kube()), APP, pushed, **kwargs), pushed, Ctx({"commit": COMMIT})


def requested(fake):
    """The operations the step requested, as patched."""
    return [body for path, body in fake.patches() if path == PATH and "operation" in body]


class TestTheStep:
    def test_it_is_an_activator_with_one_target(self):
        step = ArgocdSync(Cluster(FakeCluster().kube()), APP)
        assert (step.id, step.title) == ("argocd.sync:app-prd", "sync Argo CD Application app-prd")
        assert step.mutates and step.activator and step.undo is None

    def test_an_application_that_is_gone_counts_as_done_with_a_line_saying_so(self):
        fake = FakeCluster()
        del fake.objects["applications", "argocd-prd", APP]
        step, _, ctx = pushed_sync(fake)
        assert step.run(ctx) == "does not exist: nothing to sync, counted as done"
        assert fake.patches() == [] and ctx.progressed == []

    def test_an_application_deleted_while_it_waits_fails_it(self):
        fake = FakeCluster()
        fake.push(APP, COMMIT, webhook=False)
        fake.later(10, lambda: fake.objects.pop(("applications", "argocd-prd", APP)))
        step, _, ctx = pushed_sync(fake)
        with pytest.raises(StepFailed, match="Argo Application app-prd does not exist"):
            step.run(ctx)

    def test_a_commit_that_is_not_staged_fails_it(self):
        step, _, _ = pushed_sync(FakeCluster())
        with pytest.raises(StepFailed, match="no commit is staged as commit"):
            step.run(Ctx())


class TestAPushedCommit:
    def test_auto_sync_syncs_it_and_nothing_is_requested(self):
        fake = FakeCluster()
        fake.push(APP, COMMIT)
        step, pushed, ctx = pushed_sync(fake)
        assert step.run(ctx) == "b2c4e6a synced by auto-sync: Synced, Healthy"
        assert requested(fake) == []
        # The commit itself is never asked about; the earlier sync's revision once.
        assert pushed.asked == [HEAD]
        assert ctx.progressed[0] == "waiting for auto-sync to start a sync of b2c4e6a"
        assert (
            "syncing b2c4e6a: waiting for completion of hook batch/Job/terraform-presync"
            in ctx.progressed
        )

    def test_a_later_head_that_has_it_will_do(self):
        fake = FakeCluster()
        fake.push(APP, COMMIT, webhook=False)
        fake.push(APP, LATER)
        step, pushed, ctx = pushed_sync(fake)
        assert step.run(ctx) == "d4e6f8a synced by auto-sync: Synced, Healthy"
        assert pushed.asked == [HEAD, LATER] and requested(fake) == []

    def test_a_head_that_moves_during_its_sync_is_synced_too(self):
        fake = FakeCluster()
        fake.push(APP, COMMIT)  # synced from 6 s to 86 s
        fake.webhook_lag = 72
        fake.later(20, lambda: fake.push(APP, LATER))
        step, _, ctx = pushed_sync(fake)
        assert step.run(ctx) == "d4e6f8a synced by auto-sync: Synced, Healthy"
        assert "OutOfSync after the sync of b2c4e6a" in ctx.progressed
        assert "waiting for the operation requested after it" in ctx.progressed
        assert requested(fake) == []

    def test_without_auto_sync_within_the_grace_it_requests_the_head_of_the_branch(self):
        fake = FakeCluster()
        fake.push(APP, COMMIT, webhook=False)
        step, _, ctx = pushed_sync(fake)
        assert step.run(ctx) == "b2c4e6a synced by secret-rotator: Synced, Healthy"
        waits = AUTO_SYNC_GRACE // 5
        auto_sync = "waiting for auto-sync to start a sync of b2c4e6a"
        assert ctx.progressed[:waits] == [auto_sync] * waits
        assert ctx.progressed[waits] == REQUESTED
        assert requested(fake) == [
            {
                "metadata": {"resourceVersion": "2"},
                "operation": {
                    "initiatedBy": {"username": "secret-rotator"},
                    "sync": {"prune": True},
                    "retry": RETRY,
                },
            }
        ]

    def test_an_application_that_does_not_auto_sync_is_requested_at_once(self):
        fake = FakeCluster()
        del fake.get("applications", "argocd-prd", APP)["spec"]["syncPolicy"]["automated"]
        fake.push(APP, COMMIT)
        step, _, ctx = pushed_sync(fake)
        assert step.run(ctx) == "b2c4e6a synced by secret-rotator: Synced, Healthy"
        assert ctx.progressed[0] == REQUESTED
        (body,) = requested(fake)
        assert body["operation"]["sync"] == {"prune": False}

    def test_it_requests_nothing_while_another_operation_runs(self):
        fake = FakeCluster()
        fake.push(APP, OTHER)  # auto-synced from 4 s, for 80 s
        fake.push(APP, COMMIT, webhook=False)
        step, pushed, ctx = pushed_sync(fake)
        assert step.run(ctx) == "b2c4e6a synced by secret-rotator: Synced, Healthy"
        assert pushed.asked == [HEAD, OTHER]
        assert "waiting for Argo CD to start the requested operation" in ctx.progressed
        under_way = "waiting for the sync of 0e1f2a3 under way to end"
        last = len(ctx.progressed) - 1 - ctx.progressed[::-1].index(under_way)
        assert ctx.progressed.index(REQUESTED) == last + 1
        assert len(requested(fake)) == 1

    def test_an_application_changed_as_it_requests_is_read_again(self):
        fake = FakeCluster()
        del fake.get("applications", "argocd-prd", APP)["spec"]["syncPolicy"]["automated"]
        fake.push(APP, COMMIT)
        fake.refused["PATCH", PATH] = 409
        fake.later(1, fake.refused.clear)
        step, _, ctx = pushed_sync(fake)
        assert step.run(ctx) == "b2c4e6a synced by secret-rotator: Synced, Healthy"
        assert ctx.progressed[:2] == [
            "the Application changed as the sync was requested: reading it again",
            REQUESTED,
        ]

    def test_a_stale_resource_version_is_refused(self):
        fake = FakeCluster()
        kube = fake.kube()
        with pytest.raises(KubeError, match="HTTP 409"):
            kube.merge_patch(PATH, {"metadata": {"resourceVersion": "0"}, "operation": {}})

    def test_a_failed_sync_fails_it_with_argo_cd_s_message_and_a_retry_requests_another(self):
        fake = FakeCluster()
        fake.sync_failing[APP] = "one or more synchronization tasks completed unsuccessfully"
        fake.push(APP, COMMIT)
        step, _, ctx = pushed_sync(fake)
        with pytest.raises(StepFailed) as e:
            step.run(ctx)
        assert e.value.error == (
            "the sync of app-prd at b2c4e6a ended Failed: one or more synchronization tasks "
            "completed unsuccessfully"
        )
        assert requested(fake) == []
        failed = fake.get("applications", "argocd-prd", APP)["status"]["operationState"]
        assert ctx.values["argocd.sync:app-prd:failed"] == failed["startedAt"]
        del fake.sync_failing[APP]
        ctx.progressed.clear()
        assert step.run(ctx) == "b2c4e6a synced by secret-rotator: Synced, Healthy"
        assert ctx.progressed[0] == REQUESTED and len(requested(fake)) == 1

    def test_a_retry_whose_new_sync_fails_too_fails_it_at_once_with_argo_cd_s_message(self):
        fake = FakeCluster()
        fake.sync_failing[APP] = "one or more synchronization tasks completed unsuccessfully"
        fake.push(APP, COMMIT)
        step, _, ctx = pushed_sync(fake)
        with pytest.raises(StepFailed):
            step.run(ctx)
        first = ctx.values["argocd.sync:app-prd:failed"]
        fake.sync_failing[APP] = "the PreSync hook failed again"
        with pytest.raises(StepFailed) as e:
            step.run(ctx)
        assert e.value.error == (
            "the sync of app-prd at b2c4e6a ended Failed: the PreSync hook failed again"
        )
        assert len(requested(fake)) == 1
        failed = fake.get("applications", "argocd-prd", APP)["status"]["operationState"]
        assert ctx.values["argocd.sync:app-prd:failed"] == failed["startedAt"] != first

    def test_health_is_verified_unless_left_to_the_steps_after_it(self):
        fake = FakeCluster()
        fake.get("applications", "argocd-prd", APP)["status"]["health"]["status"] = "Degraded"
        fake.push(APP, COMMIT)
        step, _, ctx = pushed_sync(fake, healthy=False)
        assert step.run(ctx) == "b2c4e6a synced by auto-sync: Synced"
        step, _, ctx = pushed_sync(fake)
        with pytest.raises(StepFailed) as e:
            step.run(ctx)
        assert e.value.error == (
            "Argo Application app-prd not Synced and Healthy within 15 min: Synced, Degraded"
        )


class TestWithoutACommit:
    def step(self, fake):
        return ArgocdSync(Cluster(fake.kube()), APP)

    def test_it_requests_a_sync_at_once_and_waits_for_it(self):
        fake = FakeCluster()
        ctx = Ctx()
        assert self.step(fake).run(ctx) == "5a1f3c9 synced by secret-rotator: Synced, Healthy"
        assert ctx.progressed[0] == REQUESTED and len(requested(fake)) == 1

    def test_a_sync_under_way_when_it_starts_is_waited_out_and_another_requested(self):
        fake = FakeCluster()
        fake.push(APP, OTHER)
        fake.sleep(5)
        fake.sleep(5)  # auto-synced from 7 s
        ctx = Ctx()
        assert self.step(fake).run(ctx) == "0e1f2a3 synced by secret-rotator: Synced, Healthy"
        assert ctx.progressed[0] == "waiting for the sync of 0e1f2a3 under way to end"
        assert len(requested(fake)) == 1
