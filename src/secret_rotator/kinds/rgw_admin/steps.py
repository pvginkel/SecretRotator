"""The rgw-admin kind's own steps (design §4.2: custom steps are the plugin's): rgw_admin.mint,
rgw_admin.prove and rgw_admin.delete, through RGW's admin API, each request signed with a key of
the leaf's: the one it held before the plan, or the one the plan added. A step on a Site on a VM
that may be off names that VM (Step.vm). No detail or error they report carries a secret key; an
access key id names a key without being one."""

import json

from secret_rotator.kinds.rgw_admin.rgw import CACHE_BOUND, CACHE_POLL, Gateway, Key, RgwError
from secret_rotator.model import Context, Step, StepFailed, not_landed, value_name, wait
from secret_rotator.openbao import OpenBao

ACCESS, SECRET = "access_key_id", "secret_access_key"  # the leaf's keys, rotated together
# The staging names of the access key the leaf held, and of the access keys the user had before
# the mint, a JSON list.
OLD = "rgw-admin:old"
BEFORE = "rgw-admin:before"


def held(ctx: Context, leaf: str) -> Key:
    """The S3 key the leaf holds now."""
    version = ctx.bao.read(leaf)
    data = {} if version is None else version.data
    if missing := [k for k in (ACCESS, SECRET) if not data.get(k)]:
        raise StepFailed(f"{leaf} holds no {' and no '.join(missing)}")
    return Key(data[ACCESS], data[SECRET])


def added(ctx: Context) -> Key:
    """The S3 key the mint added, as staged."""
    access, secret = ctx.staged(value_name(ACCESS)), ctx.staged(value_name(SECRET))
    if access is None or secret is None:
        raise StepFailed("no new key is staged")
    return Key(access, secret)


def refused(e: Exception) -> bool:
    return isinstance(e, RgwError) and e.refused


class RgwStep(Step):
    """A step on a Site's RGW, on the Site's VM when it may be off."""

    def __init__(self, id: str, title: str, gateway: Gateway, leaf: str):
        super().__init__(id, title)
        self.gateway = gateway
        self.leaf = leaf
        self.uid = gateway.site.uid
        self.vm = gateway.site.vm

    def unanswered(self, bao: OpenBao) -> str | None:
        return self.gateway.unanswered()


class Mint(RgwStep):
    """Adds an S3 key RGW generates to the Site's admin user, signed with the key the leaf holds,
    which must be one of that user's. It stages the leaf's access key and the access keys the user
    has, before the add; then the new key's secret, and last its access key, the one key the user
    has after the add that it did not before, which it verifies by listing the user's keys.

    A re-run with a key added adds none. One without, after an add whose answer was lost, removes
    the keys the user has that it did not before, which no one holds, and adds one again. Its undo
    removes those keys, signed with the key the leaf holds, which a rollback has put back by then.
    On a run that finds no key added, every failure before the add, and the add refused (a 4xx
    answer), report that the step did not land."""

    type = "rgw_admin.mint"
    mutates = True

    def __init__(self, gateway: Gateway, leaf: str):
        site = gateway.site
        super().__init__(
            "rgw_admin.mint",
            f"add a new S3 key to the RGW admin user {site.uid} on {site.cluster}",
            gateway,
            leaf,
        )

    def run(self, ctx: Context) -> str:
        fresh = ctx.staged(value_name(ACCESS)) is None
        sending = False
        try:
            key = held(ctx, self.leaf)
            admin, keys = self.gateway.user(key)
            if fresh:
                if key.access not in keys:
                    raise StepFailed(
                        f"{self.uid} on {self.gateway.site.cluster} has no key {key.access}, "
                        f"which {self.leaf} holds"
                    )
                if ctx.staged(OLD) is None:
                    ctx.stage(OLD, key.access)
                if ctx.staged(BEFORE) is None:
                    ctx.stage(BEFORE, json.dumps(sorted(keys)))
                before = set(json.loads(ctx.staged(BEFORE)))
                for lost in sorted(set(keys) - before):
                    admin.remove_key(self.uid, lost)
                sending = True
                new = [k for k in admin.add_key(self.uid) if k.access not in before]
                if len(new) != 1:
                    raise StepFailed(f"RGW answered {len(new)} new keys of {self.uid}, not one")
                if not new[0].secret:
                    raise StepFailed(f"RGW answered the new key {new[0].access} without its secret")
                ctx.stage(value_name(SECRET), new[0].secret)
                ctx.stage(value_name(ACCESS), new[0].access)
                keys = admin.keys(self.uid)
        except Exception as e:
            if fresh and (not sending or refused(e)):
                raise not_landed(e) from e
            raise
        access = ctx.staged(value_name(ACCESS))
        if access not in keys:
            raise StepFailed(f"RGW lists no key {access} of {self.uid}")
        return f"added key {access} to {self.uid}"

    def undo(self, ctx: Context) -> str:
        before = ctx.staged(BEFORE)
        if before is None:
            return "nothing was added"
        key = held(ctx, self.leaf)
        admin, keys = self.gateway.user(key)
        new = sorted(set(keys) - set(json.loads(before)))
        if key.access in new:
            raise StepFailed(f"{self.leaf} holds the key the plan added, {key.access}")
        if not new:
            return f"{self.uid} has no key the plan added: nothing to remove"
        for access in new:
            admin.remove_key(self.uid, access)
        return f"removed key {', '.join(new)}"


class Prove(RgwStep):
    """Lists the Site's admin user's keys on each RGW instance that answers, signed with the key
    the leaf holds, which must be the one the plan added: each must take it as one of the user's.
    An instance that refuses it or does not list it yet is asked again for a moment."""

    type = "rgw_admin.prove"
    silent = True

    def __init__(self, gateway: Gateway, leaf: str):
        super().__init__("rgw_admin.prove", "prove the new key against RGW", gateway, leaf)

    def run(self, ctx: Context) -> str:
        new = added(ctx)
        if held(ctx, self.leaf) != new:
            raise StepFailed(f"{self.leaf} does not hold the key the plan added")
        taken = self.gateway.prove(ctx, new)
        return f"{len(taken)} RGW instance(s) take key {new.access} of {self.uid}"


class Delete(RgwStep):
    """Removes the key the leaf held before the plan, by the access key the mint staged, and no
    other, signed with the key the plan added, which the leaf must hold. It verifies by listing the
    user's keys until that one is gone. The plan puts it after the proof of the new key.

    It has no undo. A failure before its remove, and that remove refused (a 4xx answer), report
    that the step did not land."""

    type = "rgw_admin.delete"
    mutates = True

    def __init__(self, gateway: Gateway, leaf: str):
        super().__init__("rgw_admin.delete", "remove the key the leaf held", gateway, leaf)
        self.no_undo = "a removed RGW key cannot be restored"

    def run(self, ctx: Context) -> str:
        sending = False
        try:
            old, new = ctx.staged(OLD), added(ctx)
            if old is None:
                raise StepFailed("no new key is staged")
            if held(ctx, self.leaf) != new:
                raise StepFailed(f"{self.leaf} does not hold the key the plan added")
            admin, keys = self.gateway.user(new, ctx)
            if old in keys:
                sending = True
                admin.remove_key(self.uid, old)

                def listed() -> str | None:
                    still = old in admin.keys(self.uid)
                    return f"{admin.endpoint} still lists key {old}" if still else None

                wait(self.gateway, ctx, CACHE_BOUND, CACHE_POLL, listed, f"key {old} is not gone")
        except Exception as e:
            if not sending or refused(e):
                raise not_landed(e) from e
            raise
        if not sending:
            return f"key {old} was removed already"
        return f"removed key {old} of {self.uid}"
