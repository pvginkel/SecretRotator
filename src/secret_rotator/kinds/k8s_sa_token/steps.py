"""The k8s-sa-token kind's own steps (design §4.2: custom steps are the plugin's): k8s.sa_token,
k8s.sa_token.prove and k8s.sa_token.delete, one each per cluster a key's tokens are on, each step id
suffixed with its cluster, reached as reach.py says. The rotator's own token is a key of the kind:
the delete switches the client it calls prd with to the new token before it deletes the old one,
so the rest of the run calls prd with the new token, and the mint's undo switches it back before
it deletes the new one. No detail or error they report carries a token."""

import base64
import dataclasses
import json
from dataclasses import dataclass
from typing import Protocol

from secret_rotator.kinds.k8s_sa_token.reach import held
from secret_rotator.kinds.k8s_sa_token.tokens import claims, replaced, successor, tokens_of
from secret_rotator.kube import Kube, KubeError
from secret_rotator.model import Context, Step, StepFailed, not_landed, value_name, wait
from secret_rotator.openbao import OpenBao

TYPE = "kubernetes.io/service-account-token"
ACCOUNT = "kubernetes.io/service-account.name"
REVIEWS = "/apis/authentication.k8s.io/v1/selfsubjectreviews"
TOKEN_BOUND, TOKEN_POLL = 60, 1  # seconds: the token controller fills a new Secret's token


class Reach(Protocol):
    """A cluster, by its name, and the client a step calls it with (reach.py)."""

    name: str

    def kube(self, bao: OpenBao) -> Kube: ...

    def unanswered(self, bao: OpenBao) -> str | None: ...


def record_name(cluster: str) -> str:
    """The staging name of what the mint on the cluster verified and created."""
    return f"k8s.sa_token:{cluster}:secrets"


def secrets_path(namespace: str, name: str = "") -> str:
    return f"/api/v1/namespaces/{namespace}/secrets" + (f"/{name}" if name else "")


def token_in(secret: dict) -> str:
    """The token a token Secret holds; empty until the token controller fills it."""
    data = (secret.get("data") or {}).get("token")
    return base64.b64decode(data).decode() if data else ""


def of_account(secret: dict, account: str) -> bool:
    annotations = secret["metadata"].get("annotations") or {}
    return secret.get("type") == TYPE and annotations.get(ACCOUNT) == account


@dataclass(frozen=True)
class Record:
    """The token Secret of the token the leaf held, verified by the mint, and its successor."""

    namespace: str
    account: str  # the ServiceAccount
    old: str  # the Secret of the token the leaf held
    old_uid: str
    new: str  # the successor Secret

    def dump(self) -> str:
        return json.dumps(dataclasses.asdict(self))

    @classmethod
    def load(cls, text: str) -> "Record":
        return cls(**json.loads(text))

    def path(self, name: str) -> str:
        return secrets_path(self.namespace, name)

    def manifest(self) -> dict:
        """The successor: a token Secret of the ServiceAccount, which the token controller fills."""
        return {
            "apiVersion": "v1",
            "kind": "Secret",
            "metadata": {
                "name": self.new,
                "namespace": self.namespace,
                "annotations": {ACCOUNT: self.account},
            },
            "type": TYPE,
        }


def verified(kube: Kube, cluster: str, token: str, what: str) -> Record:
    """The record of a mint replacing the token, which must be the one the cluster's token Secret
    named in it holds for its ServiceAccount. what: the token, in words."""
    namespace, account, name = claims(token, what)
    secret = kube.get(secrets_path(namespace, name))
    where = f"{cluster}'s Secret {namespace}/{name}"
    if secret is None:
        raise StepFailed(f"{cluster} holds no Secret {namespace}/{name}, which {what} names")
    if not of_account(secret, account):
        raise StepFailed(f"{where} is not a token Secret of ServiceAccount {account}")
    if token_in(secret) != token:
        raise StepFailed(f"{where} does not hold {what}")
    return Record(namespace, account, name, secret["metadata"]["uid"], successor(account))


def filled(kube: Kube, ctx: Context, record: Record) -> str:
    """The successor's token, once the token controller has filled it."""
    where = f"Secret {record.namespace}/{record.new}"
    found = {}

    def why_not() -> str | None:
        secret = kube.get(record.path(record.new))
        if secret is None:
            raise StepFailed(f"{where} does not exist")
        if not of_account(secret, record.account):
            raise StepFailed(f"{where} is not a token Secret of ServiceAccount {record.account}")
        found["token"] = token_in(secret)
        return None if found["token"] else "waiting for the token controller to fill its token"

    wait(kube, ctx, TOKEN_BOUND, TOKEN_POLL, why_not, f"{where} got no token")
    return found["token"]


def delete(kube: Kube, namespace: str, name: str, uid: str) -> None:
    """Deletes the Secret if it is still the one of that uid; done once the client's cluster holds
    it no more."""
    kube.call("DELETE", secrets_path(namespace, name), {"preconditions": {"uid": uid}})
    if kube.get(secrets_path(namespace, name)) is not None:
        raise StepFailed(f"Secret {namespace}/{name} still exists after its delete")


def reviewed(kube: Kube) -> str | None:
    """Whom the apiserver takes the client's token for; None when it refuses the token."""
    review = {"apiVersion": "authentication.k8s.io/v1", "kind": "SelfSubjectReview"}
    try:
        _, doc = kube.call("POST", REVIEWS, review)
    except KubeError as e:
        if e.status != 401:
            raise
        return None
    return doc["status"]["userInfo"]["username"]


def refused(e: Exception) -> bool:
    """A request the apiserver answered with a refusal (4xx), which changed nothing."""
    return isinstance(e, KubeError) and e.status is not None and e.status < 500


class OnCluster(Step):
    """A step on one cluster for one key of a leaf, whose tokens are on the clusters named."""

    def __init__(
        self, id: str, title: str, reach: Reach, leaf: str, key: str, clusters: tuple[str, ...]
    ):
        super().__init__(f"{id}:{reach.name}", f"{title} on {reach.name}")
        self.reach = reach
        self.leaf = leaf
        self.key = key  # the data key the new value is staged for
        self.clusters = clusters
        self.record = record_name(reach.name)

    @property
    def what(self) -> str:
        return f"{self.leaf}#{self.key}"

    def unanswered(self, bao: OpenBao) -> str | None:
        return self.reach.unanswered(bao)

    def recorded(self, ctx: Context) -> Record:
        staged = ctx.staged(self.record)
        if staged is None:
            raise StepFailed("no new token is staged")
        return Record.load(staged)


class Mint(OnCluster):
    """Mints a successor of the token the leaf holds on the cluster, for its ServiceAccount, and
    stages the leaf's value with that token replaced and nothing else: a kubeconfig keeps its
    servers, CAs, contexts and the token of its other cluster, which the plan's mint on that one
    replaces in the value staged. The token must name its Secret, and the cluster must hold that
    Secret for the ServiceAccount with that very token. The successor is a new token Secret of the
    ServiceAccount, its name staged before it is created, done once the token controller has
    filled its token. A re-run creates no other: it waits for that one.

    Its undo deletes the successor, after switching the client back to the old token if it calls
    the cluster with the new one. Every failure before the create is sent, and the create refused
    (a 4xx answer), report that the step did not land."""

    type = "k8s.sa_token"
    mutates = True

    def __init__(self, reach: Reach, leaf: str, key: str, clusters: tuple[str, ...]):
        super().__init__(
            "k8s.sa_token", "mint a new ServiceAccount token", reach, leaf, key, clusters
        )

    def run(self, ctx: Context) -> str:
        cluster, what = self.reach.name, self.what
        sent = False
        try:
            kube = self.reach.kube(ctx.bao)
            value = held(ctx.bao, self.leaf, self.key)
            old = tokens_of(value, self.clusters, what)[cluster]
            if ctx.staged(self.record) is None:
                token = f"the token {what} holds on {cluster}"
                ctx.stage(self.record, verified(kube, cluster, old, token).dump())
            record = self.recorded(ctx)
            if kube.get(record.path(record.new)) is None:
                sent = True
                kube.call("POST", secrets_path(record.namespace), record.manifest())
        except Exception as e:
            if not sent or refused(e):
                raise not_landed(e) from e
            raise
        new = filled(kube, ctx, record)
        staged = ctx.staged(value_name(self.key)) or value
        if tokens_of(staged, self.clusters, what)[cluster] != new:
            ctx.stage(value_name(self.key), replaced(staged, self.clusters, cluster, new, what))
        return f"ServiceAccount {record.account}: Secret {record.namespace}/{record.new} minted"

    def undo(self, ctx: Context) -> str:
        staged = ctx.staged(self.record)
        if staged is None:
            return "nothing was minted"
        record = Record.load(staged)
        cluster = self.reach.name
        kube = self.reach.kube(ctx.bao)
        where = f"Secret {record.namespace}/{record.new}"
        secret = kube.get(record.path(record.new))
        if secret is None:
            return f"{where} does not exist"
        switched = ""
        if kube.token == token_in(secret):
            old = kube.get(record.path(record.old))
            if old is None:
                raise StepFailed(
                    f"the rotator calls {cluster} with the new token, and Secret "
                    f"{record.namespace}/{record.old} of the old one is gone"
                )
            kube.token = token_in(old)
            switched = f"; the rotator calls {cluster} with the old token again"
        delete(kube, record.namespace, record.new, secret["metadata"]["uid"])
        return f"{where} deleted{switched}"


class Prove(OnCluster):
    """Proves the token the leaf holds on the cluster once its consumers read it: the leaf must
    hold the value the plan staged, and the cluster must take its token there as the
    ServiceAccount's."""

    type = "k8s.sa_token.prove"
    silent = True

    def __init__(self, reach: Reach, leaf: str, key: str, clusters: tuple[str, ...]):
        super().__init__("k8s.sa_token.prove", "prove the new token", reach, leaf, key, clusters)

    def run(self, ctx: Context) -> str:
        cluster, what = self.reach.name, self.what
        record = self.recorded(ctx)
        minted = ctx.staged(value_name(self.key))
        if minted is None:
            raise StepFailed("no new token is staged")
        if held(ctx.bao, self.leaf, self.key) != minted:
            raise StepFailed(f"{what} does not hold the value the plan staged")
        token = tokens_of(minted, self.clusters, what)[cluster]
        user = reviewed(self.reach.kube(ctx.bao).bearing(token))
        want = f"system:serviceaccount:{record.namespace}:{record.account}"
        if user is None:
            raise StepFailed(f"{cluster} refuses the new token")
        if user != want:
            raise StepFailed(f"{cluster} takes the new token as {user}, not {want}")
        return f"{cluster} takes it as {want}"


class Delete(OnCluster):
    """Deletes the Secret of the token the leaf held on the cluster, which ends that token, if it
    is still the one the mint verified. If the client calls the cluster with that token, it is
    switched to the new one first. A re-run that finds the Secret gone is done.

    It has no undo. Every failure before the delete, and the delete refused (a 4xx answer), report
    that the step did not land: the mint's undo switches the client back."""

    type = "k8s.sa_token.delete"
    mutates = True

    def __init__(self, reach: Reach, leaf: str, key: str, clusters: tuple[str, ...]):
        super().__init__(
            "k8s.sa_token.delete", "delete the old ServiceAccount token", reach, leaf, key, clusters
        )
        self.no_undo = "the old token ended with its Secret, and a deleted Secret is not restored"

    def run(self, ctx: Context) -> str:
        cluster = self.reach.name
        sent = False
        try:
            kube = self.reach.kube(ctx.bao)
            record = self.recorded(ctx)
            where = f"Secret {record.namespace}/{record.old}"
            old = kube.get(record.path(record.old))
            if old is None:
                return f"{where} is gone already"
            if old["metadata"].get("uid") != record.old_uid:
                raise StepFailed(f"{where} is not the one the plan verified: it was replaced")
            switched = ""
            if kube.token == token_in(old):
                new = kube.get(record.path(record.new))
                if new is None:
                    raise StepFailed(f"Secret {record.namespace}/{record.new} does not exist")
                kube.token = token_in(new)
                switched = f"; the rotator calls {cluster} with the new token"
            sent = True
            kube.call("DELETE", record.path(record.old), {"preconditions": {"uid": record.old_uid}})
        except Exception as e:
            if not sent or refused(e):
                raise not_landed(e) from e
            raise
        if kube.get(record.path(record.old)) is not None:
            raise StepFailed(f"{where} still exists after its delete")
        return f"{where} deleted{switched}"
