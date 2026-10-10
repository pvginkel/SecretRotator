"""The prd apiserver as an opener for secret_rotator.kube.Kube: ExternalSecrets, the three workload
kinds, Argo CD Applications, Secrets, and objects the rotator must never ask for (a CronJob, bare
pods).
Its ESO syncs a force-synced ExternalSecret, its controllers roll a restarted workload out and its
Argo CD starts a requested operation, each once the fake clock, which the client's sleep advances,
has passed their lag. Argo CD syncs the revision an operation names, else the head of the branch
the Application tracks, and auto-syncs a push to that branch. A merge patch that names a
resourceVersion is refused with a 409 once the object's is another.

The compliant cluster references every eso/prd/ leaf of fixtures.COMPLIANT but
eso/prd/yt/prd/webhook, whose copy in jenkins/youtrack is its consumer, and holds the KubeCoder
pattern: an ExternalSecret that extracts its leaf whole by dataFrom alone, a controller Deployment
and bare environment pods that read its Secret. Its Pushgateway is reached through the apiserver's
service proxy.

It takes the rotator's static token, and the token of every ServiceAccount token Secret it holds as
that ServiceAccount, which a SelfSubjectReview answers; its token controller fills a created token
Secret's token once its lag has passed, a token with the legacy claims that name the Secret.
Another apiserver, as the dev cluster's, is one at another address; an apiserver that is off makes
no connection, as urlopen says of a host that is down."""

import base64
import copy
import datetime
import io
import itertools
import json
import secrets
import urllib.error
import urllib.parse

from fake_pushgateway import PUSHGATEWAY, FakePushgateway

from secret_rotator.kube import ADDR, Kube

TOKEN = "SECRET-token-of-the-secret-rotator-sa"
ROTATOR = "system:serviceaccount:kube-system:secret-rotator"
SA_TOKEN = "kubernetes.io/service-account-token"
SA_NAME = "kubernetes.io/service-account.name"
REVIEWS = "/apis/authentication.k8s.io/v1/selfsubjectreviews"
VERSION = "/version"
ESO = "/apis/external-secrets.io/v1"
APPS = "/apis/apps/v1"
ARGO = "/apis/argoproj.io/v1alpha1/namespaces/argocd-prd/applications"
KINDS = {"deployments": "Deployment", "statefulsets": "StatefulSet", "daemonsets": "DaemonSet"}
HEAD = "5a1f3c9e0b7d4e2a8c6f1b3d5e7a9c0b2d4f6a8e"  # every Application's branch head until a push
EPOCH = datetime.datetime(2026, 10, 5, 4, 30, tzinfo=datetime.UTC)  # the fake clock's 0
RETRY = {"limit": 3, "backoff": {"duration": "30s", "factor": 2}}


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


def secret(ns, name, **data):
    """A Secret holding each value base64-encoded under its key."""
    return {
        "metadata": {"namespace": ns, "name": name},
        "type": "Opaque",
        "data": {k: base64.b64encode(v.encode()).decode() for k, v in data.items()},
    }


def b64url(doc):
    return base64.urlsafe_b64encode(json.dumps(doc).encode()).rstrip(b"=").decode()


def legacy_token(ns, account, name):
    """A token as the token controller fills a token Secret's: a JWT with the legacy claims."""
    claims = {
        "iss": "kubernetes/serviceaccount",
        "kubernetes.io/serviceaccount/namespace": ns,
        "kubernetes.io/serviceaccount/secret.name": name,
        "kubernetes.io/serviceaccount/service-account.name": account,
        "sub": f"system:serviceaccount:{ns}:{account}",
    }
    signature = secrets.token_urlsafe(32).rstrip("=")
    return f"{b64url({'alg': 'RS256', 'kid': 'k'})}.{b64url(claims)}.{signature}"


def token_secret(ns, name, account, token=None, *, uid=None):
    """A ServiceAccount token Secret the token controller has filled: with a legacy token naming it
    unless token is given."""
    token = token or legacy_token(ns, account, name)
    return {
        "metadata": {
            "namespace": ns,
            "name": name,
            "uid": uid or f"uid-{name}",
            "annotations": {SA_NAME: account},
        },
        "type": SA_TOKEN,
        "data": {
            "token": base64.b64encode(token.encode()).decode(),
            "namespace": base64.b64encode(ns.encode()).decode(),
        },
    }


def token_in(secret):
    data = (secret.get("data") or {}).get("token")
    return base64.b64decode(data).decode() if data else ""


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


def application(name, *, health="Healthy"):
    """An Argo CD Application as the releases chart makes one, auto-syncing main, Synced at HEAD
    by an auto-sync that succeeded a day before the fake clock's 0."""
    return {
        "metadata": {"namespace": "argocd-prd", "name": name, "resourceVersion": "1"},
        "spec": {
            "source": {"repoURL": f"https://github.com/pvginkel/{name}.git", "path": "chart"}
            | {"targetRevision": "main"},
            "syncPolicy": {"automated": {"prune": True, "selfHeal": False}, "retry": RETRY},
        },
        "status": {
            "sync": {"status": "Synced", "revision": HEAD},
            "health": {"status": health},
            "operationState": {
                "operation": {
                    "initiatedBy": {"automated": True},
                    "sync": {"revision": HEAD, "prune": True},
                    "retry": RETRY,
                },
                "phase": "Succeeded",
                "message": "successfully synced (all tasks run)",
                "startedAt": "2026-10-04T04:30:00Z",
                "finishedAt": "2026-10-04T04:31:22Z",
                "syncResult": {"revision": HEAD},
            },
        },
    }


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
        ("applications", application("app-prd")),
        ("applications", application("bot-prd")),
        ("applications", application("kubecoder-prd")),
    ]


def snapshot(objects=None):
    """The List `kubectl get <cluster.SNAPSHOT> -A -o json` prints of the cluster's objects (the
    compliant cluster's by default): its ExternalSecrets and workloads, each with its kind."""
    kinds = {"externalsecrets": ("external-secrets.io/v1", "ExternalSecret")}
    kinds |= {resource: ("apps/v1", kind) for resource, kind in KINDS.items()}
    return {
        "apiVersion": "v1",
        "kind": "List",
        "items": [
            {"apiVersion": kinds[resource][0], "kind": kinds[resource][1], **copy.deepcopy(o)}
            for resource, o in (compliant_objects() if objects is None else objects)
            if resource in kinds
        ],
    }


class FakeCluster:
    def __init__(self, objects=None, *, eso_lag=3, rollout_lag=12, addr=ADDR):
        self.addr = addr
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
        self.pushgateway = FakePushgateway()
        self.static = {TOKEN}  # bearers it takes as the rotator without a token Secret
        self.token_lag = 2
        self.token_controller = True  # whether it fills a created token Secret's token
        self.uids = itertools.count(1)
        self.refused = {}  # (method, path) -> the status it answers instead
        self.bearers = []  # each request's bearer, in the order of requests
        self.off = False  # no request connects, and none is recorded
        self.heads = {}  # Application -> the head of the branch it tracks, once pushed to
        self.argo_lag = 2  # from a requested operation to its start
        self.webhook_lag = 4  # from a push to the auto-sync it requests
        self.sync_took = 80  # the PreSync hook's terraform init and apply, then the sync
        self.sync_failing = {}  # Application -> the message its syncs fail with

    def kube(self, token=TOKEN):
        return Kube(token, self.addr, opener=self, sleep=self.sleep, clock=self.clock)

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
        assert f"{url.scheme}://{url.netloc}" == self.addr, url
        if self.off:
            raise urllib.error.URLError(OSError(113, "No route to host"))
        method, path = req.get_method(), url.path
        proxied = path.startswith(PUSHGATEWAY)
        decode = bytes.decode if proxied else json.loads
        body = None if req.data is None else decode(req.data)
        self.requests.append((method, path, body))
        bearer = (req.get_header("Authorization") or "").removeprefix("Bearer ")
        self.bearers.append(bearer)
        if (method, path) in self.broken:
            raise self.broken[method, path]
        user = self.whose(bearer)
        if user is None:
            return self.answer(401, {"kind": "Status", "message": "Unauthorized"})
        if (method, path) in self.refused:
            return self.answer(self.refused[method, path], {"kind": "Status", "message": "no"})
        if proxied:
            assert method == "PUT", method
            assert req.get_header("Content-type") == "text/plain; version=0.0.4"
            status, raw = self.pushgateway.put(path, body)
            if status >= 400:
                raise urllib.error.HTTPError(ADDR, status, "err", {}, io.BytesIO(raw))
            return FakeResponse(status, raw)
        if path == VERSION:
            assert method == "GET", method
            return self.answer(200, {"major": "1", "minor": "35", "gitVersion": "v1.35.6"})
        if path == REVIEWS:
            assert method == "POST" and body["kind"] == "SelfSubjectReview", (method, body)
            return self.answer(201, body | {"status": {"userInfo": {"username": user}}})
        parts = path.strip("/").split("/")
        if parts[0] == "api":  # the core group: /api/<version>/...
            parts.insert(1, "")
        # /apis/<group>/<version>/<resource>, or .../namespaces/<ns>/<resource>[/<name>]
        if len(parts) == 4 and method == "GET":
            items = [o for (r, _, _), o in sorted(self.objects.items()) if r == parts[3]]
            return self.answer(200, {"items": items})
        if len(parts) == 6 and method == "POST":
            assert parts[3] == "namespaces" and body["metadata"]["namespace"] == parts[4], path
            return self.create(parts[5], body)
        assert len(parts) == 7 and parts[3] == "namespaces", path
        key = (parts[5], parts[4], parts[6])
        obj = self.objects.get(key)
        if obj is None:
            return self.answer(
                404, {"kind": "Status", "message": f"{parts[5]} {parts[6]} not found"}
            )
        if method == "GET":
            return self.answer(200, obj)
        if method == "DELETE":
            want = ((body or {}).get("preconditions") or {}).get("uid")
            if want is not None and want != obj["metadata"].get("uid"):
                message = f"Precondition failed: UID in precondition: {want}"
                return self.answer(409, {"kind": "Status", "message": message})
            del self.objects[key]
            return self.answer(200, {"kind": "Status", "status": "Success"})
        assert (
            method == "PATCH" and req.get_header("Content-type") == "application/merge-patch+json"
        )
        version = (body.get("metadata") or {}).get("resourceVersion")
        if version is not None and version != obj["metadata"].get("resourceVersion"):
            message = "the object has been modified"
            return self.answer(409, {"kind": "Status", "message": message})
        before = copy.deepcopy(obj)
        merge(obj, body)
        self.touch(obj)
        if parts[5] == "externalsecrets" and obj["metadata"]["annotations"] != before[
            "metadata"
        ].get("annotations"):
            self.later(self.eso_lag, lambda: self.eso_sync(obj))
        if parts[5] in KINDS and obj["spec"]["template"] != before["spec"]["template"]:
            obj["metadata"]["generation"] += 1
            self.later(self.rollout_lag, lambda: self.roll_out(parts[5], obj))
        if parts[5] == "applications" and obj.get("operation") and not before.get("operation"):
            self.later(self.argo_lag, lambda: self.start_sync(obj))
        return self.answer(200, obj)

    def later(self, lag, event):
        self.events.append((self.now + lag, event))

    def whose(self, bearer):
        """The user it takes a bearer for: the rotator for a static one, else the ServiceAccount
        of the token Secret that holds it; None when it refuses it."""
        if bearer in self.static:
            return ROTATOR
        for (resource, ns, _), obj in self.objects.items():
            if resource == "secrets" and obj.get("type") == SA_TOKEN and token_in(obj) == bearer:
                return f"system:serviceaccount:{ns}:{obj['metadata']['annotations'][SA_NAME]}"
        return None

    def create(self, resource, obj):
        meta = obj["metadata"]
        key = (resource, meta["namespace"], meta["name"])
        if key in self.objects:
            return self.answer(409, {"kind": "Status", "message": f"{meta['name']} already exists"})
        obj = copy.deepcopy(obj)
        obj["metadata"]["uid"] = f"uid-{next(self.uids)}"
        self.objects[key] = obj
        if resource == "secrets" and obj.get("type") == SA_TOKEN and self.token_controller:
            self.later(self.token_lag, lambda: self.fill(key, obj))
        return self.answer(201, obj)

    def fill(self, key, secret):
        """The token controller fills the token Secret it was created as, if it still exists."""
        if self.objects.get(key) is not secret:
            return
        ns, name = key[1], key[2]
        account = secret["metadata"]["annotations"][SA_NAME]
        secret["data"] = token_secret(ns, name, account)["data"]

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
    def touch(obj):
        """A write of an object that has a resourceVersion gives it a new one."""
        if "resourceVersion" in obj["metadata"]:
            obj["metadata"]["resourceVersion"] = str(int(obj["metadata"]["resourceVersion"]) + 1)

    def iso(self):
        return (EPOCH + datetime.timedelta(seconds=self.now)).strftime("%Y-%m-%dT%H:%M:%SZ")

    def head(self, app):
        return self.heads.get(app, HEAD)

    def push(self, app, sha, *, webhook=True):
        """A commit on the branch the Application tracks, which leaves it OutOfSync; webhook:
        GitHub's push webhook reaches Argo CD, which auto-syncs the commit."""
        self.heads[app] = sha
        obj = self.get("applications", "argocd-prd", app)
        obj["status"]["sync"]["status"] = "OutOfSync"
        self.touch(obj)
        if webhook and "automated" in obj["spec"]["syncPolicy"]:
            self.later(self.webhook_lag, lambda: self.auto_sync(obj, sha))

    def auto_sync(self, obj, sha):
        """Auto-sync requests a sync of the pushed commit once no operation runs."""
        if obj.get("operation") is not None:
            self.later(5, lambda: self.auto_sync(obj, sha))
            return
        obj["operation"] = {
            "initiatedBy": {"automated": True},
            "sync": {"revision": sha, "prune": True},
            "retry": RETRY,
        }
        self.touch(obj)
        self.later(self.argo_lag, lambda: self.start_sync(obj))

    def start_sync(self, obj):
        """Argo CD starts the requested operation: of the revision it names, else of the head."""
        operation = obj["operation"]
        revision = operation["sync"].get("revision") or self.head(obj["metadata"]["name"])
        obj["status"]["operationState"] = {
            "operation": copy.deepcopy(operation),
            "phase": "Running",
            "message": "waiting for completion of hook batch/Job/terraform-presync",
            "startedAt": self.iso(),
            "syncResult": {"revision": revision},
        }
        self.touch(obj)
        self.later(self.sync_took, lambda: self.finish_sync(obj))

    def finish_sync(self, obj):
        name = obj["metadata"]["name"]
        state = obj["status"]["operationState"]
        failing = self.sync_failing.get(name)
        state["phase"] = "Failed" if failing else "Succeeded"
        state["message"] = failing or "successfully synced (all tasks run)"
        state["finishedAt"] = self.iso()
        del obj["operation"]
        if not failing:
            revision = state["syncResult"]["revision"]
            synced = "Synced" if revision == self.head(name) else "OutOfSync"
            obj["status"]["sync"] = {"status": synced, "revision": revision}
        self.touch(obj)

    @staticmethod
    def answer(status, doc):
        body = json.dumps(doc).encode()
        if status >= 400:
            raise urllib.error.HTTPError(ADDR, status, "err", {}, io.BytesIO(body))
        return FakeResponse(status, body)

    def patches(self):
        return [(path, body) for method, path, body in self.requests if method == "PATCH"]

    def pushes(self):
        """The groups pushed, in order."""
        return [
            path.rsplit("/", 1)[1]
            for method, path, _ in self.requests
            if method == "PUT" and path.startswith(PUSHGATEWAY)
        ]
