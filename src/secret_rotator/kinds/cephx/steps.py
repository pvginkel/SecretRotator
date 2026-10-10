"""The cephx kind's own steps (design §4.2: custom steps are the plugin's): cephx.mint and
cephx.prove through Ceph's CLI in one of the cluster's Ceph VMs (ceph.py), and the eso.sync of an
ExternalSecret on dev the plan names (Site.readers). A step on a Site on a VM that may be off names
that VM (Step.vm). No detail or error they report carries a key; an entity names a key without
being one."""

from pathlib import Path

from secret_rotator.cluster import Cluster, Ref
from secret_rotator.k8ssteps import EsoSync
from secret_rotator.kinds.cephx.ceph import Ceph, keyring, new_key, where
from secret_rotator.kinds.k8s_sa_token.reach import Dev
from secret_rotator.model import Context, Step, StepFailed, not_landed, value_name
from secret_rotator.openbao import OpenBao

USER, KEY = "user_id", "user_key"  # the leaf's keys, rotated together


def held(ctx: Context, leaf: str) -> tuple[str, str]:
    """The entity's name and the key the leaf holds now."""
    version = ctx.bao.read(leaf)
    data = {} if version is None else version.data
    if missing := [k for k in (USER, KEY) if not data.get(k)]:
        raise StepFailed(f"{leaf} holds no {' and no '.join(missing)}")
    return data[USER], data[KEY]


class CephStep(Step):
    """A step on a Site's Ceph, on the Site's VM when it may be off."""

    def __init__(self, id: str, title: str, ceph: Ceph, leaf: str):
        super().__init__(id, title)
        self.ceph = ceph
        self.leaf = leaf
        self.site = ceph.site
        self.vm = ceph.site.vm

    def unanswered(self, bao: OpenBao) -> str | None:
        return self.ceph.unanswered()


class Mint(CephStep):
    """Gives the entity of the Site's pair the leaf does not hold, the idle one, a new key and the
    caps of the one it holds, the active one, by `ceph auth import` of a keyring on standard input,
    which creates the idle entity where Ceph has none and never touches the active one: a mounted
    client keeps the key it mounted with (slice 061 ruling D1). First every monitor must list no
    client session of the idle entity: if one does, it fails naming the hosts the sessions come
    from, by the inventory's host_vars. The active entity must hold the key the leaf holds. It
    stages the new key, a cephx AES key it makes, then the idle entity's name, and verifies by
    reading the idle entity back.

    A re-run imports the key staged again. Its undo leaves the idle entity as it is: once the
    rollback has put the leaf back, no reader holds its key, and the next rotation re-keys it after
    the same check. Every failure before the import reports that the step did not land."""

    type = "cephx.mint"
    mutates = True

    def __init__(self, ceph: Ceph, leaf: str, inventory: Path):
        super().__init__(
            "cephx.mint",
            f"give the idle Ceph client on {ceph.site.cluster} a new key",
            ceph,
            leaf,
        )
        self.inventory = inventory

    def run(self, ctx: Context) -> str:
        cluster = self.site.cluster
        sending = False
        try:
            user, key = held(ctx, self.leaf)
            idle = self.site.other(user, self.leaf)
            staged = ctx.staged(value_name(USER))
            if staged not in (None, idle):
                raise StepFailed(f"the plan re-keys client.{staged}, which {self.leaf} holds now")
            if found := self.ceph.clients(f"client.{idle}"):
                raise StepFailed(
                    f"the monitors list Ceph clients of client.{idle}, which the plan would "
                    f"re-key, on {', '.join(where(found, self.inventory))}: restart the "
                    f"Ceph-backed pods there, or let the next update round that reboots them move "
                    f"them"
                )
            active = self.ceph.entity(f"client.{user}")
            if active.key != key:
                raise StepFailed(
                    f"client.{user} on {cluster} does not hold the key {self.leaf} holds"
                )
            if quoted := sorted(s for s, c in active.caps.items() if '"' in c or "\n" in c):
                raise StepFailed(
                    f"client.{user}'s {', '.join(quoted)} caps hold a quote or a line break, which "
                    f"a keyring cannot carry"
                )
            new = ctx.staged(value_name(KEY))
            if new is None:
                new = new_key(ctx.now)
                ctx.stage(value_name(KEY), new)
            if staged is None:
                ctx.stage(value_name(USER), idle)
            sending = True
            self.ceph.run(
                ("auth", "import", "-i", "-"), keyring(f"client.{idle}", new, active.caps)
            )
        except Exception as e:
            if not sending:
                raise not_landed(e) from e
            raise
        imported = self.ceph.entity(f"client.{idle}")
        if imported.key != new:
            raise StepFailed(f"client.{idle} on {cluster} does not hold the new key")
        if imported.caps != active.caps:
            raise StepFailed(f"client.{idle} on {cluster} does not hold client.{user}'s caps")
        return f"client.{idle} re-keyed with client.{user}'s caps"

    def undo(self, ctx: Context) -> str:
        idle = ctx.staged(value_name(USER))
        if idle is None:
            return "nothing was re-keyed"
        return f"client.{idle} keeps its new key, which no reader holds once the leaf is put back"


class Prove(CephStep):
    """Authenticates to the Site's monitors as the entity the leaf holds, with the key it holds,
    which must be the ones the plan staged."""

    type = "cephx.prove"
    silent = True

    def __init__(self, ceph: Ceph, leaf: str):
        super().__init__("cephx.prove", "prove the new key against Ceph", ceph, leaf)

    def run(self, ctx: Context) -> str:
        staged = (ctx.staged(value_name(USER)), ctx.staged(value_name(KEY)))
        if None in staged:
            raise StepFailed("no new key is staged")
        if held(ctx, self.leaf) != staged:
            raise StepFailed(f"{self.leaf} does not hold the entity and key the plan staged")
        user, key = staged
        self.ceph.authenticates(f"client.{user}", key)
        return f"{self.site.cluster}'s monitors take client.{user}'s new key"


class DevSync(Step):
    """An eso.sync of an ExternalSecret on dev, through the dev write token the KubeCoder catalog
    holds (k8s-sa-token's Dev reach), on srvk8sdev (Step.vm). A rollback re-runs it, as any
    eso.sync, once the leaf is put back."""

    type = EsoSync.type
    mutates = True
    activator = True

    def __init__(self, reach: Dev, es: Ref):
        super().__init__(f"eso.sync:{reach.name}:{es}", f"sync ExternalSecret {es} on {reach.name}")
        self.reach = reach
        self.es = es
        self.vm = reach.vm

    def unanswered(self, bao: OpenBao) -> str | None:
        return self.reach.unanswered(bao)

    def run(self, ctx: Context) -> str:
        return EsoSync(Cluster(self.reach.kube(ctx.bao)), self.es).run(ctx)
