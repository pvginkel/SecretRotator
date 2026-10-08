"""The generic steps every rotation through KV uses (design §4.2): random.generate, kv.write,
kv.copy and kv.stamp; and the marker text a kind with marker leaves rewrites. No detail or error
they report carries a value."""

import secrets
import string
from collections.abc import Mapping

from secret_rotator.contract import MARKER_VALUE, dump_entry, entry_name, load_entry
from secret_rotator.model import Context, Step, StepFailed, expiry_name, value_name
from secret_rotator.openbao import Version
from secret_rotator.state import LeafState

URLSAFE = string.ascii_letters + string.digits + "-_"
# 43 URL-safe characters carry more than 32 random bytes: above Kibana's 32-character
# encryptionKey minimum (slice 044 close-out A8).
DEFAULT_LENGTH = 43


class RandomGenerate(Step):
    """A new value for one data key, staged before anything uses it; a re-run keeps it."""

    type = "random.generate"
    silent = True

    def __init__(self, key: str, *, length: int = DEFAULT_LENGTH, charset: str = URLSAFE):
        if length < 1:
            raise ValueError(f"length {length}: not a whole number from 1")
        if len(charset) < 2 or len(set(charset)) != len(charset):
            raise ValueError("charset: not two or more distinct characters")
        super().__init__(f"random.generate:{key}", f"generate a new {key}")
        self.key = key
        self.length = length
        self.charset = charset

    def run(self, ctx: Context) -> str:
        name = value_name(self.key)
        if ctx.staged(name) is None:
            ctx.stage(name, "".join(secrets.choice(self.charset) for _ in range(self.length)))
        return f"{self.length} characters"


class Marker(Step):
    """Stages a marker key's new text for the kv.write: the marker text and when it rotated, so
    each rotation is a KV version. A key that does not hold the marker text is a credential the
    write would overwrite: the step fails there. Its type is its kind's (approle.marker), a step
    of that kind's plans."""

    silent = True

    def __init__(self, kind: str, leaf: str, key: str):
        self.type = f"{kind}.marker"
        super().__init__(f"{self.type}:{key}", f"the new marker text of {key}")
        self.leaf = leaf
        self.key = key

    def run(self, ctx: Context) -> str:
        name = value_name(self.key)
        if ctx.staged(name) is None:
            current = ctx.bao.read(self.leaf)
            held = None if current is None else current.data.get(self.key)
            if held is None or not held.startswith(MARKER_VALUE):
                raise StepFailed(
                    f"{self.leaf}#{self.key} does not hold the marker text: it is no marker leaf"
                )
            ctx.stage(name, f"{MARKER_VALUE}; rotated {ctx.now.isoformat(timespec='seconds')}")
        return "staged"


def _current(ctx: Context, leaf: str) -> Version:
    version = ctx.bao.read(leaf)
    if version is None:
        raise StepFailed(f"{leaf} cannot be read: no such leaf, or its current version is deleted")
    return version


def _holds(version: Version, values: Mapping[str, str | None]) -> bool:
    return all(version.data.get(key) == value for key, value in values.items())


class KvPatch(Step):
    """Patches data keys of one leaf to staged values by KV v2 patch, so the leaf's other keys are
    never rewritten, and verifies them by read-back. The version it started from is staged before
    it writes; its undo patches the same keys back to their values in that version."""

    mutates = True

    def __init__(self, id: str, title: str, leaf: str, values: Mapping[str, str]):
        super().__init__(id, title)
        self.leaf = leaf
        self.values = dict(values)  # data key -> the staging name of its new value

    def _patch(self, ctx: Context, want: Mapping[str, str | None], current: Version) -> int:
        version = ctx.bao.patch(self.leaf, dict(want), cas=current.number)
        if not _holds(_current(ctx, self.leaf), want):
            raise StepFailed(f"the read-back of {self.leaf} does not hold what was written")
        return version

    def run(self, ctx: Context) -> str:
        want = {key: ctx.staged(name) for key, name in self.values.items()}
        if missing := sorted(key for key, value in want.items() if value is None):
            raise StepFailed(f"no new value is staged for {', '.join(missing)}")
        current = _current(ctx, self.leaf)
        memo = f"{self.id}:from"
        if ctx.staged(memo) is None:
            ctx.stage(memo, str(current.number))
        start = ctx.staged(memo)
        if _holds(current, want):
            return f"v{start} → v{current.number}"
        return f"v{start} → v{self._patch(ctx, want, current)}"

    def undo(self, ctx: Context) -> str:
        memo = ctx.staged(f"{self.id}:from")
        if memo is None:
            return "nothing was written"
        start = int(memo)
        old = ctx.bao.read(self.leaf, version=start)
        if old is None:
            raise StepFailed(f"version {start} of {self.leaf} cannot be read: it is deleted")
        want = {key: old.data.get(key) for key in self.values}
        current = _current(ctx, self.leaf)
        if _holds(current, want):
            return f"holds v{start}'s values"
        return f"v{start}'s values back as v{self._patch(ctx, want, current)}"


class KvWrite(KvPatch):
    """The new values of the plan's keys, written to the primary leaf."""

    type = "kv.write"

    def __init__(self, leaf: str, keys: tuple[str, ...]):
        super().__init__("kv.write", f"write {leaf}", leaf, {k: value_name(k) for k in keys})


class KvCopy(KvPatch):
    """The new value of a primary key, written to one copy key."""

    type = "kv.copy"

    def __init__(self, leaf: str, key: str, of: str):
        super().__init__(
            f"kv.copy:{leaf}#{key}", f"copy to {leaf}#{key}", leaf, {key: value_name(of)}
        )


class KvStamp(Step):
    """Records the rotation once it took effect (design §3.2, §3.4): first each rotated key's
    expires_at in its entry, the expiry its plan staged for it, cleared where it staged none or
    an empty one; then, in one check-and-set write of the run state, the rotated keys' stamps, the
    leaf's status ok and last run, the nightly run's backoff cleared, and its consumers: what the
    plan's activation read from the cluster, none when it read nothing. The core appends it to
    every plan (design R20)."""

    type = "kv.stamp"
    silent = True

    def __init__(self, leaf: str, keys: tuple[str, ...], consumers: tuple[str, ...] = ()):
        super().__init__("kv.stamp", f"stamp {', '.join(keys)}")
        self.leaf = leaf
        self.keys = keys
        self.consumers = consumers

    def run(self, ctx: Context) -> str:
        meta = ctx.bao.metadata(self.leaf)
        if meta is None:
            raise StepFailed(
                f"no leaf {self.leaf}: it is gone from the store, so nothing is stamped"
            )
        expiries = {k: ctx.staged(expiry_name(k)) or None for k in self.keys}
        patch, cleared = {}, []
        for key, expires in expiries.items():
            name = entry_name(key)
            if name not in meta:
                if expires is None:
                    continue
                raise StepFailed(f"{self.leaf} has no entry {name}: its expiry cannot be written")
            fields = load_entry(meta[name])
            if fields.get("expires_at") == expires:
                continue
            if expires is None:
                cleared.append(key)
                fields.pop("expires_at")
            else:
                fields["expires_at"] = expires
            patch[name] = dump_entry(fields)
        if patch:
            ctx.bao.patch_metadata(self.leaf, patch)
        today = ctx.now.date().isoformat()

        def stamp(state: LeafState) -> None:
            state.stamps |= dict.fromkeys(self.keys, today)
            state.status = "ok"
            state.last_run = ctx.now.isoformat(timespec="seconds")
            state.consumers = self.consumers
            state.failed_nights = 0
            state.held_by = None

        ctx.state.update(self.leaf, stamp)
        expire = "".join(f"; {k} expires {d}" for k, d in expiries.items() if d is not None)
        clear = "".join(f"; {key} expiry cleared" for key in cleared)
        return f"{', '.join(self.keys)} rotated {today}{expire}{clear}"
