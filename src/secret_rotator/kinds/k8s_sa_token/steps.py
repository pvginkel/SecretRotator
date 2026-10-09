"""The k8s-sa-token kind's own steps (design §4.2: custom steps are the plugin's): k8s.sa_token,
k8s.sa_token.prove and k8s.sa_token.delete, on prd through the rotator's running client, under its
own identity. The rotator's own token is a key of the kind: the delete switches that client to the
new token before it deletes the old one, so the rest of the run calls prd with the new token, and
the mint's undo switches it back before it deletes the new one. No detail or error they report
carries a token."""

import base64
import dataclasses
import json
from dataclasses import dataclass

from secret_rotator.cluster import Cluster
from secret_rotator.kinds.k8s_sa_token.tokens import claims, replaced, successor, token_of
from secret_rotator.kube import Kube, KubeError
from secret_rotator.model import Context, Step, StepFailed, not_landed, value_name, wait

PRD = "prd"
TYPE = "kubernetes.io/service-account-token"
ACCOUNT = "kubernetes.io/service-account.name"
REVIEWS = "/apis/authentication.k8s.io/v1/selfsubjectreviews"
TOKEN_BOUND, TOKEN_POLL = 60, 1  # seconds: the token controller fills a new Secret's token
# The staging name of what the mint verified and created.
RECORD = f"k8s.sa_token:{PRD}:secrets"


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


def held(ctx: Context, leaf: str, key: str) -> str:
    """What the leaf's key holds now."""
    version = ctx.bao.read(leaf)
    value = None if version is None else version.data.get(key)
    if not value:
        raise StepFailed(f"{leaf}#{key} holds nothing")
    return value


def verified(kube: Kube, token: str, what: str) -> Record:
    """The record of a mint replacing the token, which must be the one prd's token Secret named in
    it holds for its ServiceAccount. what: the token, in words."""
    namespace, account, name = claims(token, what)
    secret = kube.get(secrets_path(namespace, name))
    where = f"{PRD}'s Secret {namespace}/{name}"
    if secret is None:
        raise StepFailed(f"{PRD} holds no Secret {namespace}/{name}, which {what} names")
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
    """Deletes the Secret if it is still the one of that uid; done once prd holds it no more."""
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


def recorded(ctx: Context) -> Record:
    staged = ctx.staged(RECORD)
    if staged is None:
        raise StepFailed("no new token is staged")
    return Record.load(staged)


class Mint(Step):
    """Mints a successor of the token the leaf holds, for its ServiceAccount, on prd, and stages
    the leaf's value with that token replaced and nothing else: a kubeconfig keeps its server, CA
    and contexts. The token must name its Secret, and prd must hold that Secret for the
    ServiceAccount with that very token. The successor is a new token Secret of the
    ServiceAccount, its name staged before it is created, done once the token controller has
    filled its token. A re-run creates no other: it waits for that one.

    Its undo deletes the successor, after switching the rotator's client back to the old token if
    it calls prd with the new one. Every failure before the create is sent, and the create refused
    (a 4xx answer), report that the step did not land."""

    type = "k8s.sa_token"
    mutates = True

    def __init__(self, cluster: Cluster, leaf: str, key: str):
        super().__init__(f"k8s.sa_token:{PRD}", f"mint a new ServiceAccount token on {PRD}")
        self.cluster = cluster
        self.leaf = leaf
        self.key = key  # the data key the new value is staged for

    def run(self, ctx: Context) -> str:
        kube = self.cluster.kube
        what = f"{self.leaf}#{self.key}"
        sent = False
        try:
            value = held(ctx, self.leaf, self.key)
            old = token_of(value, what)
            if ctx.staged(RECORD) is None:
                ctx.stage(RECORD, verified(kube, old, f"the token {what} holds").dump())
            record = recorded(ctx)
            if kube.get(record.path(record.new)) is None:
                sent = True
                kube.call("POST", secrets_path(record.namespace), record.manifest())
        except Exception as e:
            if not sent or refused(e):
                raise not_landed(e) from e
            raise
        new = filled(kube, ctx, record)
        if ctx.staged(value_name(self.key)) is None:
            ctx.stage(value_name(self.key), replaced(value, old, new, what))
        return f"ServiceAccount {record.account}: Secret {record.namespace}/{record.new} minted"

    def undo(self, ctx: Context) -> str:
        staged = ctx.staged(RECORD)
        if staged is None:
            return "nothing was minted"
        record = Record.load(staged)
        kube = self.cluster.kube
        where = f"Secret {record.namespace}/{record.new}"
        secret = kube.get(record.path(record.new))
        if secret is None:
            return f"{where} does not exist"
        switched = ""
        if kube.token == token_in(secret):
            old = kube.get(record.path(record.old))
            if old is None:
                raise StepFailed(
                    f"the rotator calls {PRD} with the new token, and Secret "
                    f"{record.namespace}/{record.old} of the old one is gone"
                )
            kube.token = token_in(old)
            switched = f"; the rotator calls {PRD} with the old token again"
        delete(kube, record.namespace, record.new, secret["metadata"]["uid"])
        return f"{where} deleted{switched}"


class Prove(Step):
    """Proves the token the leaf holds once its consumers read it: the leaf must hold the value
    the plan staged, and prd must take its token as the ServiceAccount's."""

    type = "k8s.sa_token.prove"
    silent = True

    def __init__(self, cluster: Cluster, leaf: str, key: str):
        super().__init__(f"k8s.sa_token.prove:{PRD}", f"prove the new token on {PRD}")
        self.cluster = cluster
        self.leaf = leaf
        self.key = key

    def run(self, ctx: Context) -> str:
        record = recorded(ctx)
        minted = ctx.staged(value_name(self.key))
        what = f"{self.leaf}#{self.key}"
        if minted is None:
            raise StepFailed("no new token is staged")
        if held(ctx, self.leaf, self.key) != minted:
            raise StepFailed(f"{what} does not hold the value the plan staged")
        user = reviewed(self.cluster.kube.bearing(token_of(minted, what)))
        want = f"system:serviceaccount:{record.namespace}:{record.account}"
        if user is None:
            raise StepFailed(f"{PRD} refuses the new token")
        if user != want:
            raise StepFailed(f"{PRD} takes the new token as {user}, not {want}")
        return f"{PRD} takes it as {want}"


class Delete(Step):
    """Deletes the Secret of the token the leaf held, which ends that token, if it is still the
    one the mint verified. If the rotator's client calls prd with that token, it is switched to the
    new one first. A re-run that finds the Secret gone is done.

    It has no undo. Every failure before the delete, and the delete refused (a 4xx answer), report
    that the step did not land: the mint's undo switches the client back."""

    type = "k8s.sa_token.delete"
    mutates = True

    def __init__(self, cluster: Cluster, leaf: str, key: str):
        super().__init__(
            f"k8s.sa_token.delete:{PRD}", f"delete the old ServiceAccount token on {PRD}"
        )
        self.cluster = cluster
        self.leaf = leaf
        self.key = key
        self.no_undo = "the old token ended with its Secret, and a deleted Secret is not restored"

    def run(self, ctx: Context) -> str:
        kube = self.cluster.kube
        sent = False
        try:
            record = recorded(ctx)
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
                switched = f"; the rotator calls {PRD} with the new token"
            sent = True
            kube.call("DELETE", record.path(record.old), {"preconditions": {"uid": record.old_uid}})
        except Exception as e:
            if not sent or refused(e):
                raise not_landed(e) from e
            raise
        if kube.get(record.path(record.old)) is not None:
            raise StepFailed(f"{where} still exists after its delete")
        return f"{where} deleted{switched}"
