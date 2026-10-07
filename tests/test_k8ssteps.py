"""eso.sync and k8s.rollout (design §4.2): activators with one target each; eso.sync verifies the
ExternalSecret Ready with a new syncedResourceVersion within 2 min, k8s.rollout every pod Ready and
the owning Argo Application Healthy within 5 min; a target that does not exist counts as done, with
a line saying so (design §4.5)."""

import datetime

import pytest
from fake_cluster import FakeCluster

from secret_rotator.cluster import Cluster, Ref, Workload
from secret_rotator.k8ssteps import FORCE_SYNC, RESTARTED_AT, EsoSync, K8sRollout
from secret_rotator.kube import KubeError
from secret_rotator.model import StepFailed

NOW = datetime.datetime(2026, 10, 5, 4, 30, 0, 123456, tzinfo=datetime.UTC)
ES = Ref("app-prd", "app-token")
APP = Workload("app-prd", "deployment", "app")


class Ctx:
    def __init__(self, now=NOW):
        self.now = now
        self.progressed = []

    def progress(self, detail):
        self.progressed.append(detail)


def cluster_of():
    fake = FakeCluster()
    return fake, Cluster(fake.kube())


class TestEsoSync:
    def test_it_is_an_activator_with_one_target(self):
        _, cluster = cluster_of()
        step = EsoSync(cluster, ES)
        assert (step.id, step.title) == (
            "eso.sync:app-prd/app-token",
            "sync ExternalSecret app-prd/app-token",
        )
        assert step.mutates and step.activator and step.undo is None

    def test_it_forces_a_sync_and_waits_for_a_new_synced_resource_version(self):
        fake, cluster = cluster_of()
        ctx = Ctx()
        assert EsoSync(cluster, ES).run(ctx) == "synced, Ready"
        es = fake.get("externalsecrets", "app-prd", "app-token")
        assert es["metadata"]["annotations"][FORCE_SYNC] == "2026-10-05T04:30:00.123456+00:00"
        assert es["status"]["syncedResourceVersion"] == "1-1"
        assert set(ctx.progressed) == {"waiting for ESO to sync it"}

    def test_a_retry_writes_a_new_value_so_eso_syncs_again(self):
        fake, cluster = cluster_of()
        step = EsoSync(cluster, ES)
        step.run(Ctx())
        step.run(Ctx(NOW + datetime.timedelta(microseconds=1)))
        assert fake.syncs == 2

    def test_a_sync_that_does_not_succeed_fails_with_eso_s_message_after_two_minutes(self):
        fake, cluster = cluster_of()
        fake.eso_failing.add("app-prd/app-token")
        with pytest.raises(StepFailed) as e:
            EsoSync(cluster, ES).run(Ctx())
        assert e.value.error == (
            "app-prd/app-token did not sync within 2 min: could not get secret data from provider"
        )
        assert 120 <= fake.now < 125

    def test_an_external_secret_that_is_gone_counts_as_done_with_a_line_saying_so(self):
        fake, cluster = cluster_of()
        detail = EsoSync(cluster, Ref("app-prd", "gone")).run(Ctx())
        assert detail == "does not exist: nothing to sync, counted as done"
        assert fake.patches() == []


class TestK8sRollout:
    def test_it_is_an_activator_with_one_target(self):
        _, cluster = cluster_of()
        step = K8sRollout(cluster, APP)
        assert (step.id, step.title) == (
            "k8s.rollout:app-prd/deployment/app",
            "roll out app-prd/deployment/app",
        )
        assert step.mutates and step.activator and step.undo is None

    def test_it_restarts_the_workload_and_waits_for_its_pods_and_its_application(self):
        fake, cluster = cluster_of()
        ctx = Ctx()
        assert K8sRollout(cluster, APP).run(ctx) == "Ready, Application app-prd Healthy"
        deployment = fake.get("deployments", "app-prd", "app")
        restarted = deployment["spec"]["template"]["metadata"]["annotations"][RESTARTED_AT]
        assert restarted == "2026-10-05T04:30:00.123456+00:00"
        assert (
            deployment["metadata"]["generation"] == deployment["status"]["observedGeneration"] == 2
        )
        assert ctx.progressed[0] == "waiting for its controller to see the change"

    def test_a_workload_whose_pods_do_not_come_up_fails_it_after_five_minutes(self):
        fake, cluster = cluster_of()
        fake.stuck.add("app-prd/app")
        with pytest.raises(StepFailed) as e:
            K8sRollout(cluster, APP).run(Ctx())
        assert e.value.error == "app-prd/deployment/app not Ready within 5 min: 1/2 Ready"
        assert 300 <= fake.now < 310

    def test_an_application_that_is_not_healthy_fails_it(self):
        fake, cluster = cluster_of()
        fake.get("applications", "argocd-prd", "app-prd")["status"]["health"]["status"] = "Degraded"
        with pytest.raises(StepFailed, match="its Argo Application app-prd is Degraded"):
            K8sRollout(cluster, APP).run(Ctx())

    def test_a_statefulset_and_a_daemonset_roll_out_the_same_way(self):
        fake, cluster = cluster_of()
        assert (
            K8sRollout(cluster, Workload("app-prd", "statefulset", "app-db")).run(Ctx()) == "Ready"
        )
        assert (
            K8sRollout(cluster, Workload("app-prd", "daemonset", "app-agent")).run(Ctx()) == "Ready"
        )
        assert fake.get("statefulsets", "app-prd", "app-db")["status"]["currentRevision"] == "rev-2"

    def test_a_workload_that_is_gone_counts_as_done_with_a_line_saying_so(self):
        fake, cluster = cluster_of()
        ctx = Ctx()
        detail = K8sRollout(cluster, Workload("app-prd", "deployment", "gone")).run(ctx)
        assert detail == "does not exist: nothing to roll out, counted as done"
        assert ctx.progressed == [] and fake.now == 0

    def test_any_other_refusal_fails_it(self):
        fake = FakeCluster()
        with pytest.raises(KubeError, match="HTTP 401: Unauthorized"):
            K8sRollout(Cluster(fake.kube("not-the-token")), APP).run(Ctx())
