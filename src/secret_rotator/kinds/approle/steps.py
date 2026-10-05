"""The approle kind's own steps (design §4.2: custom steps are the plugin's): approle.mint,
approle.login, approle.k8s_secret and approle.destroy_old_accessor, through OpenBao's AppRole API
with the rotator's token (its policy, design §8). No detail or error they report carries a
secret_id; an accessor names a secret_id without being one."""

import base64
import datetime
from collections.abc import Callable

from secret_rotator.cluster import Cluster
from secret_rotator.contract import EXPIRES_AT
from secret_rotator.model import Context, Step, StepFailed
from secret_rotator.openbao import OpenBao, OpenBaoError

# The plan's staging names beside its new secret_id's.
OLD = "approle:old-accessor"  # the accessor the consumer held before; "" when it held none
NEW = "approle:new-accessor"
EXPIRES_FROM = "approle:expires-from"  # the leaf's rotator_expires_at before the mint; "" for none
# The new secret_id where no kv.write takes it: on a marker leaf, which holds the marker text.
SECRET = "approle:secret-id"
# OpenBao's expiration_time of a secret_id that never expires (OpenBao 2.5.4).
NO_EXPIRY = "0001-01-01T00:00:00Z"
# The data key of a k8s_secret delivery's Secret that ESO's ClusterSecretStore openbao-prd reads.
SECRET_KEY = "secret_id"

# The secret_id a consumer holds where the step can read it; "" or None when it holds none.
Held = Callable[[Context], str | None]


class SecretIds:
    """One AppRole's secret_ids. On OpenBao 2.5.4 an unknown secret_id looks up as 204, an
    unknown accessor as 404, and destroying an unknown accessor is a 500."""

    def __init__(self, bao: OpenBao, role: str):
        self.bao = bao
        self.role = role
        self.path = f"auth/approle/role/{role}"

    def _missing(self) -> StepFailed:
        return StepFailed(f"OpenBao has no AppRole {self.role}")

    def role_id(self) -> str:
        status, doc = self.bao.call("GET", f"{self.path}/role-id")
        if status == 404:
            raise self._missing()
        return doc["data"]["role_id"]

    def mint(self, days: int) -> tuple[str, str]:
        """A new secret_id with a ttl of that many days, and its accessor. Above the approle
        mount's max_lease_ttl OpenBao caps the ttl without an error."""
        status, doc = self.bao.call("POST", f"{self.path}/secret-id", {"ttl": f"{days * 24}h"})
        if status == 404:
            raise self._missing()
        return doc["data"]["secret_id"], doc["data"]["secret_id_accessor"]

    def accessors(self) -> list[str]:
        status, doc = self.bao.call("LIST", f"{self.path}/secret-id")
        return [] if status == 404 else doc["data"]["keys"]

    def accessor_of(self, secret_id: str) -> str | None:
        """The accessor of a live secret_id of the role; None when it has no such one."""
        body = {"secret_id": secret_id}
        status, doc = self.bao.call("POST", f"{self.path}/secret-id/lookup", body)
        return None if status in (204, 404) else doc["data"]["secret_id_accessor"]

    def lookup(self, accessor: str) -> dict | None:
        """The accessor's secret_id, described; None when the role has no such accessor."""
        body = {"secret_id_accessor": accessor}
        status, doc = self.bao.call("POST", f"{self.path}/secret-id-accessor/lookup", body)
        return None if status == 404 else doc["data"]

    def destroy(self, accessor: str) -> None:
        body = {"secret_id_accessor": accessor}
        self.bao.call("POST", f"{self.path}/secret-id-accessor/destroy", body)


def held_in_kv(leaf: str, key: str) -> Held:
    """The secret_id the leaf holds as that key: the kv delivery's consumer."""

    def held(ctx: Context) -> str | None:
        version = ctx.bao.read(leaf)
        return None if version is None else version.data.get(key)

    return held


def expiry_date(described: dict) -> datetime.date:
    """The UTC date a described secret_id expires. OpenBao reports the time in its host's zone."""
    text = described["expiration_time"]
    if text == NO_EXPIRY:
        raise StepFailed("the new secret_id never expires: OpenBao did not take its ttl")
    return datetime.datetime.fromisoformat(text).astimezone(datetime.UTC).date()


class Mint(Step):
    """Mints a new secret_id with a ttl and records the expiry it reports as the leaf's
    rotator_expires_at. First it stages the accessor the consumer holds: that of the secret_id
    `held` reads, where the delivery can read it, else the role's only one. A role with more than
    one whose consumer cannot be read fails here, before anything is minted.

    Its undo destroys the new secret_id and puts the previous rotator_expires_at back. A mint whose
    answer is lost leaves a secret_id no one holds, which expires with its ttl."""

    type = "approle.mint"
    mutates = True

    def __init__(
        self, role: str, leaf: str, secret: str, days: int, consumer: str, held: Held | None
    ):
        super().__init__("approle.mint", f"mint a new {role} secret_id that expires in {days} days")
        self.role = role
        self.leaf = leaf
        self.secret = secret  # the staging name of the new secret_id
        self.days = days
        self.consumer = consumer
        self.held = held

    def _old(self, ctx: Context, ids: SecretIds) -> str:
        if self.held is not None:
            value = self.held(ctx)
            return (ids.accessor_of(value) if value else None) or ""
        accessors = ids.accessors()
        if len(accessors) > 1:
            raise StepFailed(
                f"AppRole {self.role} has {len(accessors)} secret_ids, and which one "
                f"{self.consumer} holds cannot be read: destroy the ones no consumer holds first"
            )
        return accessors[0] if accessors else ""

    def run(self, ctx: Context) -> str:
        ids = SecretIds(ctx.bao, self.role)
        if ctx.staged(self.secret) is None:
            if ctx.staged(OLD) is None:
                ctx.stage(OLD, self._old(ctx, ids))
            if ctx.staged(EXPIRES_FROM) is None:
                meta = ctx.bao.metadata(self.leaf)
                if meta is None:
                    raise StepFailed(f"no leaf {self.leaf}")
                ctx.stage(EXPIRES_FROM, meta.get(EXPIRES_AT, ""))
            secret_id, accessor = ids.mint(self.days)
            ctx.stage(self.secret, secret_id)
            ctx.stage(NEW, accessor)
        accessor = ctx.staged(NEW)
        if accessor is None:
            accessor = ids.accessor_of(ctx.staged(self.secret))
            if accessor is None:
                raise StepFailed(f"the new secret_id is not one of AppRole {self.role}'s")
            ctx.stage(NEW, accessor)
        described = ids.lookup(accessor)
        if described is None:
            raise StepFailed(f"the new secret_id of AppRole {self.role} is gone")
        expires = expiry_date(described).isoformat()
        ctx.bao.patch_metadata(self.leaf, {EXPIRES_AT: expires})
        return f"expires {expires}"

    def undo(self, ctx: Context) -> str:
        ids = SecretIds(ctx.bao, self.role)
        done = []
        accessor = ctx.staged(NEW)
        if accessor is None and (secret_id := ctx.staged(self.secret)) is not None:
            accessor = ids.accessor_of(secret_id)
        if accessor is not None and ids.lookup(accessor) is not None:
            ids.destroy(accessor)
            done.append("the new secret_id destroyed")
        previous = ctx.staged(EXPIRES_FROM)
        if previous is not None:
            ctx.bao.patch_metadata(self.leaf, {EXPIRES_AT: previous or None})
            done.append(f"{EXPIRES_AT} back to {previous or 'none'}")
        return "; ".join(done) or "nothing was minted"


class Login(Step):
    """Logs in as the role with the new secret_id, from a client of its own, so the rotator keeps
    its token. The proving login's token is left to expire: the rotator's policy has no token
    paths."""

    type = "approle.login"
    silent = True

    def __init__(self, role: str, secret: str):
        super().__init__("approle.login", f"log in as {role} with the new secret_id")
        self.role = role
        self.secret = secret

    def run(self, ctx: Context) -> str:
        secret_id = ctx.staged(self.secret)
        if secret_id is None:
            raise StepFailed("no new secret_id is staged")
        role_id = SecretIds(ctx.bao, self.role).role_id()
        proof = OpenBao(ctx.bao.addr, opener=ctx.bao.open)
        try:
            proof.login_approle(role_id, secret_id)
        except OpenBaoError as e:
            if e.status is None:
                raise
            raise StepFailed(
                f"the login as {self.role} with the new secret_id is refused: {e}"
            ) from None
        return f"logged in as {self.role}"


class K8sSecret(Step):
    """The k8s_secret delivery: writes the new secret_id into the secret_id key of an existing
    Secret with the rotator's cluster identity and verifies it by read-back. The value the key held
    is staged before it writes; its undo writes that back."""

    type = "approle.k8s_secret"
    mutates = True

    def __init__(self, cluster: Cluster, namespace: str, name: str, secret: str):
        super().__init__(
            f"approle.k8s_secret:{namespace}/{name}",
            f"write the new secret_id into Secret {namespace}/{name}",
        )
        self.cluster = cluster
        self.ref = f"{namespace}/{name}"
        self.path = f"/api/v1/namespaces/{namespace}/secrets/{name}"
        self.secret = secret

    def _encoded(self) -> str | None:
        obj = self.cluster.kube.get(self.path)
        if obj is None:
            raise StepFailed(f"Secret {self.ref} does not exist")
        return (obj.get("data") or {}).get(SECRET_KEY)

    def held(self, ctx: Context) -> str:
        """The secret_id the Secret holds now: the mint's consumer."""
        encoded = self._encoded()
        return "" if encoded is None else base64.b64decode(encoded).decode()

    def _write(self, encoded: str | None) -> None:
        self.cluster.kube.merge_patch(self.path, {"data": {SECRET_KEY: encoded}})
        if self._encoded() != encoded:
            raise StepFailed(f"the read-back of Secret {self.ref} does not hold what was written")

    def run(self, ctx: Context) -> str:
        secret_id = ctx.staged(self.secret)
        if secret_id is None:
            raise StepFailed("no new secret_id is staged")
        want = base64.b64encode(secret_id.encode()).decode()
        current = self._encoded()
        memo = f"{self.id}:from"
        if ctx.staged(memo) is None:
            ctx.stage(memo, current or "")
        if current != want:
            self._write(want)
        return f"{SECRET_KEY} written"

    def undo(self, ctx: Context) -> str:
        memo = ctx.staged(f"{self.id}:from")
        if memo is None:
            return "nothing was written"
        if self._encoded() != (memo or None):
            self._write(memo or None)
        return f"the previous {SECRET_KEY} back"


class DestroyOldAccessor(Step):
    """Destroys the accessor the mint staged as the consumer's before this rotation; nothing when
    it held none. The plan puts it after the login with the new secret_id."""

    type = "approle.destroy_old_accessor"
    mutates = True

    def __init__(self, role: str, consumer: str):
        super().__init__(
            "approle.destroy_old_accessor", f"destroy the secret_id {consumer} held before"
        )
        self.role = role
        self.no_undo = f"the secret_id {consumer} held before is destroyed"

    def run(self, ctx: Context) -> str:
        old = ctx.staged(OLD)
        if not old:
            return "nothing to destroy: it held no live secret_id"
        ids = SecretIds(ctx.bao, self.role)
        if ids.lookup(old) is None:
            return f"accessor {old} is gone already"
        ids.destroy(old)
        return f"accessor {old} destroyed"
