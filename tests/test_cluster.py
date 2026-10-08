"""What the rotator reads of the cluster: the one match from leaf to ExternalSecret (ruling B1),
auto's rollout targets derived through the Secrets pod templates read, never a bare pod, and a
workload's health as `kubectl rollout status` and Argo CD judge it. The ServiceAccount manifest."""

import json
from pathlib import Path

import pytest
import yaml
from fake_cluster import FakeCluster, externalsecret, pod_spec, snapshot, workload

from secret_rotator.cluster import (
    SNAPSHOT,
    SNAPSHOT_KINDS,
    Cluster,
    Ref,
    SnapshotError,
    Workload,
    leaves_of,
    pending,
)

LEAF = "eso/prd/app/prd/token"
CATALOG = "eso/prd/kc/prd/catalog"


def cluster_of(fake=None):
    fake = fake or FakeCluster()
    return fake, Cluster(fake.kube())


class TestMatch:
    def test_a_remote_ref_or_an_extract_names_a_leaf(self):
        es = externalsecret("ns", "es", data=[("a/b", "k")], extract=["c/d"])
        assert leaves_of(es) == {"a/b", "c/d"}

    def test_a_generator_references_no_leaf(self):
        _, cluster = cluster_of()
        generated = next(
            es for es in cluster.externalsecrets if es["metadata"]["name"] == "generated"
        )
        assert leaves_of(generated) == set()

    def test_every_leaf_an_external_secret_references(self):
        _, cluster = cluster_of()
        assert cluster.referenced() == {
            "eso/prd/app/prd/oidc",
            LEAF,
            "eso/prd/bot/prd/config",
            "eso/prd/es/prd/creds",
            "eso/prd/trello/prd/trello",
            CATALOG,
        }

    def test_the_kubecoder_catalog_extracted_whole_by_data_from_alone_is_found(self):
        _, cluster = cluster_of()
        assert cluster.external_secrets(CATALOG) == [
            Ref("kubecoder-prd", "kubecoder-secret-catalog")
        ]
        assert cluster.external_secrets(LEAF) == [Ref("app-prd", "app-token")]


class TestConsumers:
    def test_auto_derives_every_workload_whose_pod_template_reads_the_secret(self):
        fake, cluster = cluster_of()
        assert [str(w) for w in cluster.consumers(LEAF)] == [
            "app-prd/deployment/app",  # env
            "app-prd/statefulset/app-db",  # a projected volume
        ]
        assert [str(w) for w in cluster.consumers("eso/prd/app/prd/oidc")] == [
            "app-prd/daemonset/app-agent",  # a secret volume
            "app-prd/deployment/app",  # envFrom
        ]
        assert [str(w) for w in cluster.consumers("eso/prd/bot/prd/config")] == [
            "bot-prd/deployment/bot"  # an init container's env
        ]

    def test_a_cron_job_and_a_bare_pod_are_never_targets_and_never_read(self):
        fake, cluster = cluster_of()
        assert [str(w) for w in cluster.consumers(CATALOG)] == [
            "kubecoder-prd/deployment/kubecoder-controller"
        ]
        assert not [p for _, p, _ in fake.requests if "pods" in p or "cronjobs" in p]

    def test_the_secret_is_the_target_name_and_lives_in_the_external_secret_s_namespace(self):
        fake = FakeCluster()
        fake.add(
            "externalsecrets",
            externalsecret("x-prd", "es", data=[("x/leaf", "k")], target="renamed"),
        )
        fake.add(
            "deployments", workload("Deployment", "x-prd", "reads-it", pod_spec(env=["renamed"]))
        )
        fake.add("deployments", workload("Deployment", "x-prd", "reads-es", pod_spec(env=["es"])))
        fake.add(
            "deployments", workload("Deployment", "y-prd", "elsewhere", pod_spec(env=["renamed"]))
        )
        _, cluster = cluster_of(fake)
        assert cluster.consumers("x/leaf") == [Workload("x-prd", "deployment", "reads-it")]

    def test_one_cluster_lists_each_kind_once(self):
        fake, cluster = cluster_of()
        cluster.consumers(LEAF)
        cluster.consumers(CATALOG)
        cluster.referenced()
        lists = [p for m, p, _ in fake.requests if m == "GET" and p.count("/") == 4]
        assert sorted(lists) == [
            "/apis/apps/v1/daemonsets",
            "/apis/apps/v1/deployments",
            "/apis/apps/v1/statefulsets",
            "/apis/external-secrets.io/v1/externalsecrets",
        ]


class TestSnapshot:
    """An offline plan's cluster: the List kubectl prints of the objects the live cluster lists."""

    @pytest.fixture(autouse=True)
    def path(self, tmp_path):
        self.file = tmp_path / "snapshot.json"

    def of(self, doc):
        self.file.write_text(doc if isinstance(doc, str) else json.dumps(doc))
        return Cluster.of_snapshot(self.file)

    def test_it_derives_what_the_live_cluster_derives_and_has_no_client(self):
        _, live = cluster_of()
        taken = self.of(snapshot())
        assert taken.referenced() == live.referenced()
        for leaf in sorted(live.referenced()):
            assert taken.external_secrets(leaf) == live.external_secrets(leaf)
            assert taken.consumers(leaf) == live.consumers(leaf)
        assert taken.snapshot and taken.kube is None
        assert not live.snapshot

    def test_kubectl_lists_the_kinds_it_holds(self):
        listed = [resource.partition(".")[0] for resource in SNAPSHOT.split(",")]
        assert listed == [f"{kind.lower()}s" for kind in SNAPSHOT_KINDS]

    def test_it_holds_no_secret(self):
        doc = snapshot()
        doc["items"].append({"apiVersion": "v1", "kind": "Secret", "metadata": {"name": "s"}})
        with pytest.raises(SnapshotError, match="it holds a Secret: a snapshot holds"):
            self.of(doc)

    @pytest.mark.parametrize(
        "doc, why",
        [("{", "not JSON"), ([], "not the List"), ({"kind": "List"}, "not the List")],
    )
    def test_a_file_that_is_not_kubectl_s_list(self, doc, why):
        with pytest.raises(SnapshotError, match=why):
            self.of(doc)


def deployment(generation=2, observed=2, replicas=2, updated=2, total=2, available=2):
    return {
        "metadata": {"generation": generation},
        "spec": {"replicas": replicas},
        "status": {
            "observedGeneration": observed,
            "replicas": total,
            "updatedReplicas": updated,
            "availableReplicas": available,
        },
    }


def statefulset(ready=3, updated=3, current="r2", update="r2", partition=None):
    spec = {"replicas": 3}
    if partition is not None:
        spec["updateStrategy"] = {"rollingUpdate": {"partition": partition}}
    return {
        "metadata": {"generation": 1},
        "spec": spec,
        "status": {
            "observedGeneration": 1,
            "readyReplicas": ready,
            "updatedReplicas": updated,
            "currentRevision": current,
            "updateRevision": update,
        },
    }


def daemonset(desired=3, updated=3, available=3):
    return {
        "metadata": {"generation": 1},
        "spec": {},
        "status": {
            "observedGeneration": 1,
            "desiredNumberScheduled": desired,
            "updatedNumberScheduled": updated,
            "numberAvailable": available,
        },
    }


@pytest.mark.parametrize(
    ("kind", "obj", "why"),
    [
        ("deployment", deployment(), None),
        ("deployment", deployment(observed=1), "waiting for its controller to see the change"),
        ("deployment", deployment(updated=1, total=3), "1/2 pods updated"),
        ("deployment", deployment(total=3), "1 old pod(s) still terminating"),
        ("deployment", deployment(available=1), "1/2 Ready"),
        ("deployment", deployment(replicas=0, updated=0, total=0, available=0), None),
        ("statefulset", statefulset(), None),
        ("statefulset", statefulset(ready=2), "2/3 Ready"),
        ("statefulset", statefulset(updated=1, current="r1"), "1/3 pods updated"),
        ("statefulset", statefulset(updated=1, current="r1", partition=2), None),
        ("statefulset", statefulset(updated=0, current="r1", partition=2), "0/1 pods updated"),
        ("daemonset", daemonset(), None),
        ("daemonset", daemonset(updated=2), "2/3 pods updated"),
        ("daemonset", daemonset(available=1), "1/3 Ready"),
    ],
)
def test_pending_judges_a_rollout_as_kubectl_rollout_status(kind, obj, why):
    assert pending(kind, obj) == why


class TestHealth:
    APP = Workload("app-prd", "deployment", "app")

    def test_rolled_out_and_its_application_healthy(self):
        _, cluster = cluster_of()
        assert cluster.health(self.APP) is None

    def test_its_application_not_healthy_or_missing(self):
        fake, cluster = cluster_of()
        fake.get("applications", "argocd-prd", "app-prd")["status"]["health"]["status"] = "Degraded"
        assert cluster.health(self.APP) == "its Argo Application app-prd is Degraded"
        del fake.objects["applications", "argocd-prd", "app-prd"]
        assert cluster.health(self.APP) == "its Argo Application app-prd does not exist"

    def test_a_workload_no_application_tracks_needs_none(self):
        fake, cluster = cluster_of()
        assert cluster.health(Workload("app-prd", "statefulset", "app-db")) is None
        assert not [p for _, p, _ in fake.requests if "applications" in p]

    def test_not_rolled_out_or_missing(self):
        fake, cluster = cluster_of()
        fake.get("deployments", "app-prd", "app")["status"]["availableReplicas"] = 1
        assert cluster.health(self.APP) == "1/2 Ready"
        assert cluster.health(Workload("app-prd", "deployment", "gone")) == (
            "app-prd/deployment/gone does not exist"
        )


def test_a_rollout_target_is_a_deployment_statefulset_or_daemonset():
    assert str(Workload.parse("a-prd/daemonset/x")) == "a-prd/daemonset/x"
    with pytest.raises(ValueError, match="is not <ns>/<deployment|statefulset|daemonset>/<name>"):
        Workload.parse("a-prd/pod/x")


def test_the_manifest_binds_the_service_account_to_cluster_admin_with_a_long_lived_token():
    manifest = Path(__file__).parent.parent / "k8s" / "cluster-identity.yaml"
    docs = {d["kind"]: d for d in yaml.safe_load_all(manifest.read_text())}
    assert set(docs) == {"ServiceAccount", "ClusterRoleBinding", "Secret"}
    sa = docs["ServiceAccount"]["metadata"]
    assert (sa["namespace"], sa["name"]) == ("kube-system", "secret-rotator")
    binding = docs["ClusterRoleBinding"]
    assert binding["roleRef"] == {
        "apiGroup": "rbac.authorization.k8s.io",
        "kind": "ClusterRole",
        "name": "cluster-admin",
    }
    assert binding["subjects"] == [
        {"kind": "ServiceAccount", "name": "secret-rotator", "namespace": "kube-system"}
    ]
    token = docs["Secret"]
    assert token["type"] == "kubernetes.io/service-account-token"
    assert token["metadata"]["namespace"] == "kube-system"
    assert token["metadata"]["annotations"] == {
        "kubernetes.io/service-account.name": "secret-rotator"
    }
