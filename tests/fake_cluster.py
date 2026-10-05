"""The prd apiserver as an opener for secret_rotator.kube.Kube: ExternalSecrets, the three workload
kinds, Argo CD Applications, and objects the rotator must never ask for (a CronJob, bare pods).
Its ESO syncs a force-synced ExternalSecret and its controllers roll a restarted workload out, each
once the fake clock, which the client's sleep advances, has passed their lag.

The compliant cluster references every eso/prd/ leaf of fixtures.COMPLIANT but
eso/prd/yt/prd/webhook, whose copy in jenkins/youtrack is its consumer, and holds the KubeCoder
pattern: an ExternalSecret that extracts its leaf whole by dataFrom alone, a controller Deployment
and bare environment pods that read its Secret."""

import copy
import io
import json
import urllib.error
import urllib.parse

from secret_rotator.kube import ADDR, Kube

TOKEN = "SECRET-token-of-the-secret-rotator-sa"
ESO = "/apis/external-secrets.io/v1"
APPS = "/apis/apps/v1"
ARGO = "/apis/argoproj.io/v1alpha1/namespaces/argocd-prd/applications"
KINDS = {"deployments": "Deployment", "statefulsets": "StatefulSet", "daemonsets": "DaemonSet"}


class FakeResponse(io.BytesIO):
    def __init__(self, status, body):
        super().__init__(body)
        self.status = status


def merge(target, patch):
    """RFC 7386: a JSON merge patch applied to target in place."""
    for key, value in patch.items():
        if value is None:
            target.pop(key, None)
        elif isinstance(value, dict) and isinstance(target.get(key), dict):
            merge(target[key], value)
        else:
            target[key] = copy.deepcopy(value)
    return target


def externalsecret(ns, name, *, data=(), extract=(), target=None):
    """data: (leaf, property) pairs by remoteRef; extract: leaves extracted whole by dataFrom."""
    spec = {"secretStoreRef": {"kind": "ClusterSecretStore", "name": "openbao-prd"}}
    if data:
        spec["data"] = [
            {"secretKey": prop, "remoteRef": {"key": leaf, "property": prop}} for leaf, prop in data
        ]
    if extract:
        spec["dataFrom"] = [{"extract": {"key": leaf}} for leaf in extract]
    if target:
        spec["target"] = {"name": target}
    return {
        "metadata": {"namespace": ns, "name": name, "generation": 1, "annotations": {}},
        "spec": spec,
        "status": {
            "syncedResourceVersion": "1-0",
            "conditions": [{"type": "Ready", "status": "True", "message": "secret synced"}],
        },
    }


def pod_spec(*, env=(), env_from=(), volume=(), projected=(), init_env=()):
    """A pod spec reading each named Secret by one of the ways a pod can."""
    container = {"name": "main", "env": [], "envFrom": []}
    container["env"] = [
        {"name": f"S_{n}", "valueFrom": {"secretKeyRef": {"name": s, "key": "k"}}}
        for n, s in enumerate(env)
    ] + [{"name": "PLAIN", "value": "x"}]
    container["envFrom"] = [{"secretRef": {"name": s}} for s in env_from] + [
        {"configMapRef": {"name": "cm"}}
    ]
    volumes = [{"name": f"v{n}", "secret": {"secretName": s}} for n, s in enumerate(volume)]
    volumes += [
        {
            "name": f"p{n}",
            "projected": {"sources": [{"configMap": {"name": "cm"}}, {"secret": {"name": s}}]},
        }
        for n, s in enumerate(projected)
    ]
    volumes.append({"name": "data", "emptyDir": {}})
    spec = {"containers": [container], "volumes": volumes}
    if init_env:
        spec["initContainers"] = [
            {
                "name": "init",
                "env": [{"name": "I", "valueFrom": {"secretKeyRef": {"name": s, "key": "k"}}}],
            }
            for s in init_env
        ]
    return spec


def workload(kind, ns, name, spec, *, app=None, replicas=1):
    """A Deployment, StatefulSet or DaemonSet, rolled out with every pod Ready."""
    annotations = {}
    if app:
        annotations["argocd.argoproj.io/tracking-id"] = f"{app}:apps/{kind}:{ns}/{name}"
    obj = {
        "metadata": {"namespace": ns, "name": name, "generation": 1, "annotations": annotations},
        "spec": {"template": {"metadata": {"annotations": {}}, "spec": spec}},
    }
    if kind != "DaemonSet":
        obj["spec"]["replicas"] = replicas
    settle(kind, obj)
    return obj


def settle(kind, obj, ready=None):
    """The status of a workload whose rollout is complete; ready: fewer pods Ready."""
    generation = obj["metadata"]["generation"]
    want = obj["spec"].get("replicas", 3)
    ok = want if ready is None else ready
    if kind == "Deployment":
        status = {"replicas": want, "updatedReplicas": want, "availableReplicas": ok}
    elif kind == "StatefulSet":
        revision = f"rev-{generation}"
        status = {
            "replicas": want,
            "updatedReplicas": want,
            "readyReplicas": ok,
            "currentRevision": revision,
            "updateRevision": revision,
        }
    else:
        status = {
            "desiredNumberScheduled": want,
            "updatedNumberScheduled": want,
            "numberAvailable": ok,
        }
    obj["status"] = {"observedGeneration": generation, **status}


def compliant_objects():
    """(resource, object) of the compliant cluster."""
    return [
        (
            "externalsecrets",
            externalsecret("app-prd", "app-token", data=[("eso/prd/app/prd/token", "token")]),
        ),
        (
            "externalsecrets",
            externalsecret(
                "app-prd",
                "app-oidc",
                data=[
                    ("eso/prd/app/prd/oidc", "client_id"),
                    ("eso/prd/app/prd/oidc", "client_secret"),
                ],
            ),
        ),
        (
            "externalsecrets",
            externalsecret("bot-prd", "bot", data=[("eso/prd/bot/prd/config", "jenkins-token")]),
        ),
        (
            "externalsecrets",
            externalsecret("es-prd", "es-creds", data=[("eso/prd/es/prd/creds", "password")]),
        ),
        (
            "externalsecrets",
            externalsecret("trello-prd", "trello", data=[("eso/prd/trello/prd/trello", "token")]),
        ),
        (
            "externalsecrets",
            externalsecret(
                "kubecoder-prd",
                "kubecoder-secret-catalog",
                extract=["eso/prd/kc/prd/catalog"],
                target="kubecoder-secret-catalog",
            ),
        ),
        # A generator-only ExternalSecret references no leaf.
        (
            "externalsecrets",
            {
                "metadata": {"namespace": "app-prd", "name": "generated", "generation": 1},
                "spec": {
                    "dataFrom": [{"sourceRef": {"generatorRef": {"kind": "Password", "name": "p"}}}]
                },
                "status": {},
            },
        ),
        (
            "deployments",
            workload(
                "Deployment",
                "app-prd",
                "app",
                pod_spec(env=["app-token"], env_from=["app-oidc"]),
                app="app-prd",
                replicas=2,
            ),
        ),
        (
            "statefulsets",
            workload("StatefulSet", "app-prd", "app-db", pod_spec(projected=["app-token"])),
        ),
        (
            "daemonsets",
            workload("DaemonSet", "app-prd", "app-agent", pod_spec(volume=["app-oidc"])),
        ),
        ("deployments", workload("Deployment", "app-prd", "unrelated", pod_spec(env=["other"]))),
        (
            "deployments",
            workload("Deployment", "bot-prd", "bot", pod_spec(init_env=["bot"]), app="bot-prd"),
        ),
        ("deployments", workload("Deployment", "es-prd", "a", pod_spec(env=["es-creds"]))),
        ("statefulsets", workload("StatefulSet", "es-prd", "b", pod_spec(volume=["es-creds"]))),
        (
            "deployments",
            workload("Deployment", "trello-prd", "trello-mcp", pod_spec(env_from=["trello"])),
        ),
        (
            "deployments",
            workload(
                "Deployment",
                "kubecoder-prd",
                "kubecoder-controller",
                pod_spec(volume=["kubecoder-secret-catalog"]),
                app="kubecoder-prd",
            ),
        ),
        (
            "cronjobs",
            {
                "metadata": {"namespace": "app-prd", "name": "backup"},
                "spec": {
                    "jobTemplate": {"spec": {"template": {"spec": pod_spec(env=["app-token"])}}}
                },
            },
        ),
        (
            "pods",
            {
                "metadata": {"namespace": "app-prd", "name": "debug"},
                "spec": pod_spec(env_from=["app-token"]),
            },
        ),
        (
            "pods",
            {
                "metadata": {"namespace": "kubecoder-prd", "name": "env-1"},
                "spec": pod_spec(env=["kubecoder-secret-catalog"]),
            },
        ),
        (
            "applications",
            {
                "metadata": {"namespace": "argocd-prd", "name": "app-prd"},
                "status": {"health": {"status": "Healthy"}},
            },
        ),
        (
            "applications",
            {
                "metadata": {"namespace": "argocd-prd", "name": "bot-prd"},
                "status": {"health": {"status": "Healthy"}},
            },
        ),
        (
            "applications",
            {
                "metadata": {"namespace": "argocd-prd", "name": "kubecoder-prd"},
                "status": {"health": {"status": "Healthy"}},
            },
        ),
    ]


class FakeCluster:
    def __init__(self, objects=None, *, eso_lag=3, rollout_lag=12):
        # (resource, namespace, name) -> object
        self.objects = {
            (resource, o["metadata"]["namespace"], o["metadata"]["name"]): copy.deepcopy(o)
            for resource, o in (compliant_objects() if objects is None else objects)
        }
        self.requests = []  # (method, path, body)
        self.now = 0.0
        self.eso_lag = eso_lag
        self.rollout_lag = rollout_lag
        self.events = []  # (due time, callable)
        self.eso_failing = set()  # "<ns>/<name>": its syncs fail at the provider
        self.stuck = set()  # "<ns>/<name>": its rollouts never get a pod Ready
        self.syncs = 0
        self.broken = {}  # (method, path) -> the OSError it raises

    def kube(self, token=TOKEN):
        return Kube(token, opener=self, sleep=self.sleep, clock=self.clock)

    def clock(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds
        due = [e for e in self.events if e[0] <= self.now]
        self.events = [e for e in self.events if e[0] > self.now]
        for _, event in due:
            event()

    def add(self, resource, obj):
        self.objects[resource, obj["metadata"]["namespace"], obj["metadata"]["name"]] = obj

    def get(self, resource, ns, name):
        return self.objects[resource, ns, name]

    def __call__(self, req):
        url = urllib.parse.urlsplit(req.full_url)
        assert f"{url.scheme}://{url.netloc}" == ADDR, url
        method, path = req.get_method(), url.path
        body = json.loads(req.data) if req.data else None
        self.requests.append((method, path, body))
        if (method, path) in self.broken:
            raise self.broken[method, path]
        if req.get_header("Authorization") != f"Bearer {TOKEN}":
            return self.answer(401, {"kind": "Status", "message": "Unauthorized"})
        parts = path.strip("/").split("/")
        # /apis/<group>/<version>/<resource>, or .../namespaces/<ns>/<resource>/<name>
        if len(parts) == 4 and method == "GET":
            items = [o for (r, _, _), o in sorted(self.objects.items()) if r == parts[3]]
            return self.answer(200, {"items": items})
        assert len(parts) == 7 and parts[3] == "namespaces", path
        key = (parts[5], parts[4], parts[6])
        obj = self.objects.get(key)
        if obj is None:
            return self.answer(
                404, {"kind": "Status", "message": f"{parts[5]} {parts[6]} not found"}
            )
        if method == "GET":
            return self.answer(200, obj)
        assert (
            method == "PATCH" and req.get_header("Content-type") == "application/merge-patch+json"
        )
        before = copy.deepcopy(obj)
        merge(obj, body)
        if parts[5] == "externalsecrets" and obj["metadata"]["annotations"] != before[
            "metadata"
        ].get("annotations"):
            self.later(self.eso_lag, lambda: self.eso_sync(obj))
        if parts[5] in KINDS and obj["spec"]["template"] != before["spec"]["template"]:
            obj["metadata"]["generation"] += 1
            self.later(self.rollout_lag, lambda: self.roll_out(parts[5], obj))
        return self.answer(200, obj)

    def later(self, lag, event):
        self.events.append((self.now + lag, event))

    def eso_sync(self, es):
        ref = f"{es['metadata']['namespace']}/{es['metadata']['name']}"
        if ref in self.eso_failing:
            message = "could not get secret data from provider"
            es["status"]["conditions"] = [{"type": "Ready", "status": "False", "message": message}]
            return
        self.syncs += 1
        es["status"]["syncedResourceVersion"] = f"{es['metadata']['generation']}-{self.syncs}"
        es["status"]["conditions"] = [
            {"type": "Ready", "status": "True", "message": "secret synced"}
        ]

    def roll_out(self, resource, obj):
        ref = f"{obj['metadata']['namespace']}/{obj['metadata']['name']}"
        want = obj["spec"].get("replicas", 3)
        settle(KINDS[resource], obj, ready=want - 1 if ref in self.stuck else None)

    @staticmethod
    def answer(status, doc):
        body = json.dumps(doc).encode()
        if status >= 400:
            raise urllib.error.HTTPError(ADDR, status, "err", {}, io.BytesIO(body))
        return FakeResponse(status, body)

    def patches(self):
        return [(path, body) for method, path, body in self.requests if method == "PATCH"]
