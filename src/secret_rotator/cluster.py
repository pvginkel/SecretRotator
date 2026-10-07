"""What the rotator reads of the prd cluster (design §4.3): the ExternalSecrets that reference a
leaf, the workloads whose pod templates read their Secrets, and whether a workload is rolled out
with its Argo Application Healthy.

One match serves auto, the sync before every rollout and the orphan check (design R70): an
ExternalSecret references a leaf when a data[].remoteRef.key or a dataFrom[].extract.key names it.
The KubeCoder catalogs are extracted whole, by dataFrom[].extract alone."""

import json
from dataclasses import dataclass, field

from secret_rotator.contract import WORKLOAD
from secret_rotator.kube import Kube

ESO = "/apis/external-secrets.io/v1"
APPS = "/apis/apps/v1"
# Every Argo CD Application of the prd cluster lives in Argo CD's own namespace.
APPLICATIONS = "/apis/argoproj.io/v1alpha1/namespaces/argocd-prd/applications"
# Argo CD tracks what an Application deployed by this annotation, <app>:<group>/<Kind>:<ns>/<name>.
TRACKING = "argocd.argoproj.io/tracking-id"
RESOURCES = {"deployment": "deployments", "statefulset": "statefulsets", "daemonset": "daemonsets"}


@dataclass(frozen=True, order=True)
class Ref:
    """An ExternalSecret."""

    namespace: str
    name: str

    def __str__(self) -> str:
        return f"{self.namespace}/{self.name}"

    @property
    def path(self) -> str:
        return f"{ESO}/namespaces/{self.namespace}/externalsecrets/{self.name}"


@dataclass(frozen=True, order=True)
class Workload:
    """A rollout target: a Deployment, StatefulSet or DaemonSet; a bare pod never is one."""

    namespace: str
    kind: str  # a key of RESOURCES
    name: str

    def __str__(self) -> str:
        """The target as rotation_activate names it."""
        return f"{self.namespace}/{self.kind}/{self.name}"

    @classmethod
    def parse(cls, text: str) -> "Workload":
        if not WORKLOAD.fullmatch(text):
            raise ValueError(f"{text!r} is not <ns>/<deployment|statefulset|daemonset>/<name>")
        namespace, kind, name = text.split("/")
        return cls(namespace, kind, name)

    @property
    def path(self) -> str:
        return f"{APPS}/namespaces/{self.namespace}/{RESOURCES[self.kind]}/{self.name}"


@dataclass
class Derived:
    """What a plan derived from the cluster, by leaf (design §4.5): the ExternalSecrets that
    reference the leaf, and the workloads that read their Secrets where a rollout's targets are
    derived. A plan in flight keeps it in its record and is rebuilt from it, not from the
    cluster."""

    externalsecrets: dict[str, list[Ref]] = field(default_factory=dict)
    workloads: dict[str, list[Workload]] = field(default_factory=dict)

    def referenced(self) -> set[str]:
        """The leaves an ExternalSecret referenced when the plan derived them."""
        return {leaf for leaf, found in self.externalsecrets.items() if found}

    def dump(self) -> str:
        return json.dumps(
            {
                "externalsecrets": {
                    leaf: [str(es) for es in found] for leaf, found in self.externalsecrets.items()
                },
                "workloads": {
                    leaf: [str(w) for w in found] for leaf, found in self.workloads.items()
                },
            }
        )

    @classmethod
    def load(cls, text: str) -> "Derived":
        doc = json.loads(text)
        return cls(
            {
                leaf: [Ref(*es.split("/")) for es in found]
                for leaf, found in doc["externalsecrets"].items()
            },
            {leaf: [Workload.parse(w) for w in found] for leaf, found in doc["workloads"].items()},
        )


def leaves_of(es: dict) -> set[str]:
    """Every leaf the ExternalSecret references."""
    spec = es.get("spec") or {}
    data = [(d.get("remoteRef") or {}).get("key") for d in spec.get("data") or []]
    extracted = [(d.get("extract") or {}).get("key") for d in spec.get("dataFrom") or []]
    return {key for key in data + extracted if key}


def _ref(es: dict) -> Ref:
    return Ref(es["metadata"]["namespace"], es["metadata"]["name"])


def _target_secret(es: dict) -> str:
    return ((es.get("spec") or {}).get("target") or {}).get("name") or es["metadata"]["name"]


def secrets_read(pod: dict) -> set[str]:
    """The Secrets a pod spec reads: env, envFrom, secret volumes and projected volumes, of its
    containers and init containers."""
    names = set()
    for c in [*pod.get("containers", []), *pod.get("initContainers", [])]:
        for env in c.get("env") or []:
            names.add(((env.get("valueFrom") or {}).get("secretKeyRef") or {}).get("name"))
        for source in c.get("envFrom") or []:
            names.add((source.get("secretRef") or {}).get("name"))
    for volume in pod.get("volumes") or []:
        names.add((volume.get("secret") or {}).get("secretName"))
        for source in (volume.get("projected") or {}).get("sources") or []:
            names.add((source.get("secret") or {}).get("name"))
    return names - {None}


def condition(obj: dict, kind: str) -> dict:
    """The object's status condition of that type; empty when it has none."""
    conditions = (obj.get("status") or {}).get("conditions") or []
    return next((c for c in conditions if c.get("type") == kind), {})


def pending(kind: str, obj: dict) -> str | None:
    """What a workload's rollout still waits for, as `kubectl rollout status` judges it; None when
    it is complete with every pod Ready."""
    spec, status = obj.get("spec") or {}, obj.get("status") or {}
    if status.get("observedGeneration", 0) < obj["metadata"].get("generation", 0):
        return "waiting for its controller to see the change"
    if kind == "daemonset":
        want = status.get("desiredNumberScheduled", 0)
        updated = status.get("updatedNumberScheduled", 0)
        ready = status.get("numberAvailable", 0)
        if updated < want:
            return f"{updated}/{want} pods updated"
        return f"{ready}/{want} Ready" if ready < want else None
    want = spec.get("replicas", 1)
    updated = status.get("updatedReplicas", 0)
    if kind == "deployment":
        ready = status.get("availableReplicas", 0)
        if updated < want:
            return f"{updated}/{want} pods updated"
        if (old := status.get("replicas", 0) - updated) > 0:
            return f"{old} old pod(s) still terminating"
        return f"{ready}/{want} Ready" if ready < updated else None
    ready = status.get("readyReplicas", 0)
    if ready < want:
        return f"{ready}/{want} Ready"
    partition = ((spec.get("updateStrategy") or {}).get("rollingUpdate") or {}).get("partition")
    if partition:
        return f"{updated}/{want - partition} pods updated" if updated < want - partition else None
    if status.get("updateRevision") != status.get("currentRevision"):
        return f"{updated}/{want} pods updated"
    return None


def owning_app(obj: dict) -> str | None:
    """The Argo CD Application that deployed the object; None when none did."""
    tracking = (obj["metadata"].get("annotations") or {}).get(TRACKING)
    return tracking.partition(":")[0] if tracking else None


class Cluster:
    """The cluster through one client. The ExternalSecrets and workloads are listed once, on first
    use, so every plan built from one Cluster derives from the same reading."""

    def __init__(self, kube: Kube):
        self.kube = kube
        self._externalsecrets: list[dict] | None = None
        self._workloads: list[tuple[Workload, dict]] | None = None

    @property
    def externalsecrets(self) -> list[dict]:
        if self._externalsecrets is None:
            self._externalsecrets = self.kube.items(f"{ESO}/externalsecrets")
        return self._externalsecrets

    @property
    def workloads(self) -> list[tuple[Workload, dict]]:
        """Each workload with its pod template's spec."""
        if self._workloads is None:
            self._workloads = [
                (
                    Workload(o["metadata"]["namespace"], kind, o["metadata"]["name"]),
                    o["spec"]["template"].get("spec") or {},
                )
                for kind, resource in RESOURCES.items()
                for o in self.kube.items(f"{APPS}/{resource}")
            ]
        return self._workloads

    def referenced(self) -> set[str]:
        """Every leaf an ExternalSecret references."""
        return {leaf for es in self.externalsecrets for leaf in leaves_of(es)}

    def external_secrets(self, leaf: str) -> list[Ref]:
        return sorted({_ref(es) for es in self.externalsecrets if leaf in leaves_of(es)})

    def consumers(self, leaf: str) -> list[Workload]:
        """auto's rollout targets: every workload whose pod template reads the Secret of an
        ExternalSecret that references the leaf. A CronJob or a Job needs none, a bare pod is
        never one: neither is a workload."""
        secrets = {
            (es["metadata"]["namespace"], _target_secret(es))
            for es in self.externalsecrets
            if leaf in leaves_of(es)
        }
        return sorted(
            {
                workload
                for workload, pod in self.workloads
                if any((workload.namespace, name) in secrets for name in secrets_read(pod))
            }
        )

    def health(self, workload: Workload) -> str | None:
        """Why the workload is not rolled out with every pod Ready and its Argo Application
        Healthy, read live; None when it is."""
        obj = self.kube.get(workload.path)
        if obj is None:
            return f"{workload} does not exist"
        if why := pending(workload.kind, obj):
            return why
        app = owning_app(obj)
        if app is None:
            return None
        application = self.kube.get(f"{APPLICATIONS}/{app}")
        if application is None:
            return f"its Argo Application {app} does not exist"
        health = ((application.get("status") or {}).get("health") or {}).get("status")
        return None if health == "Healthy" else f"its Argo Application {app} is {health}"
