"""The compliance check of design §3.3: every leaf held to the annotation contract (§5).

A finding blocks only what it touches: a key-level finding blocks that data key, a leaf-level one
blocks the leaf and every primary key copied into it, since that primary's plan would write and
activate the blocked leaf."""

import datetime
from dataclasses import dataclass, field

from secret_rotator.contract import (
    ContractError,
    copy_target,
    is_scheduled,
    kind_error,
    parse_activate,
    parse_args,
    parse_date,
    parse_interval,
    resolve,
)
from secret_rotator.openbao import OpenBao
from secret_rotator.schedule import STAMP_PREFIX, KeySchedule, interval_of, schedule


@dataclass
class Leaf:
    path: str
    keys: set[str] | None  # None: the current version's keys cannot be read
    meta: dict[str, str]


@dataclass(frozen=True)
class Finding:
    leaf: str
    key: str  # the metadata key or data key at fault
    message: str
    blocks: str | None = None  # the data key it blocks; None: the whole leaf

    def __str__(self) -> str:
        return f"{self.leaf}: {self.key}: {self.message}"


@dataclass
class Audit:
    findings: list[Finding]
    kinds: dict[str, dict[str, str | None]]  # per leaf whose keys could be read
    blocked_leaves: set[str] = field(default_factory=set)
    blocked_keys: set[tuple[str, str]] = field(default_factory=set)
    never: list[tuple[str, str]] = field(default_factory=list)  # scheduled keys at never

    def blocked(self, leaf: str, key: str) -> bool:
        return leaf in self.blocked_leaves or (leaf, key) in self.blocked_keys


def check_leaf(leaf: Leaf, store: dict[str, Leaf], kinds_of: dict) -> list[Finding]:
    m = leaf.meta
    findings = []

    def find(key: str, message: str, blocks: str | None = None) -> None:
        findings.append(Finding(leaf.path, key, message, blocks))

    kind = m.get("rotation_mechanism")
    has_notes = bool(m.get("notes", "").strip())
    kinds = kinds_of.get(leaf.path)
    if leaf.keys is None:
        find("(data)", "its current version is deleted or destroyed: its keys cannot be read")

    if kind is None:
        find("rotation_mechanism", "missing")
    elif err := kind_error(kind):
        find("rotation_mechanism", err)
    if "rotation_activate" not in m:
        find("rotation_activate", "missing")
    if (
        "rotation_interval" not in m
        and kinds is not None
        and not all(k is not None and not is_scheduled(k) for k in kinds.values())
    ):
        find("rotation_interval", "missing (a key of the leaf is neither a copy nor none)")

    for meta_key, value in sorted(m.items()):
        if meta_key.startswith("key_"):
            name = meta_key[len("key_") :]
            if err := kind_error(value):
                find(meta_key, err, name)
            if leaf.keys is not None and name not in leaf.keys:
                find(meta_key, f"stale override: the leaf has no key {name!r}", name)
        elif meta_key.startswith("interval_"):
            name = meta_key[len("interval_") :]
            try:
                if parse_interval(value) is None and not has_notes:
                    find(meta_key, "never without the leaf's notes", name)
            except ContractError as e:
                find(meta_key, str(e), name)
            if leaf.keys is not None and name not in leaf.keys:
                find(meta_key, f"the leaf has no key {name!r}", name)
            elif kinds is not None and (k := kinds.get(name)) is not None and not is_scheduled(k):
                find(meta_key, f"key {name!r} is {k}, which takes no interval", name)
        elif meta_key.startswith(STAMP_PREFIX):
            name = meta_key[len(STAMP_PREFIX) :]
            try:
                parse_date(value)
            except ContractError as e:
                find(meta_key, str(e), name)

    if kinds is not None and kind is not None and kind_error(kind) is None:
        for key, k in kinds.items():
            if k is None and not (f"key_{key}" in m and kind_error(m[f"key_{key}"])):
                find(
                    key,
                    f"no kind resolves it: {kind} does not own it and no key_{key} names one",
                    key,
                )
    for key, k in (kinds or {}).items():
        target = copy_target(k)
        if target is None:
            continue
        primary, primary_key = target
        source = f"key_{key}" if f"key_{key}" in m else "rotation_mechanism"
        if primary not in store:
            find(source, f"copy of {primary}#{primary_key}: no leaf {primary}", key)
        elif store[primary].keys is not None and primary_key not in store[primary].keys:
            find(
                source,
                f"copy of {primary}#{primary_key}: {primary} has no key {primary_key!r}",
                key,
            )
        elif copy_target(kinds_of.get(primary, {}).get(primary_key)):
            find(source, f"copy of {primary}#{primary_key}, which is itself a copy", key)

    if "rotation_interval" in m:
        try:
            if parse_interval(m["rotation_interval"]) is None and not has_notes:
                find("rotation_interval", "never without notes")
        except ContractError as e:
            find("rotation_interval", str(e))
    if "rotation_activate" in m:
        try:
            parse_activate(m["rotation_activate"])
        except ContractError as e:
            for problem in e.problems:
                find("rotation_activate", problem)
    if "rotation_args" in m:
        try:
            parse_args(m["rotation_args"])
        except ContractError as e:
            find("rotation_args", str(e))
    for meta_key in ("rotation_expires_at", "rotator_expires_at"):
        if meta_key in m:
            try:
                parse_date(m[meta_key])
            except ContractError as e:
                find(meta_key, str(e))
    return findings


def copies_in(meta: dict[str, str]) -> set[tuple[str, str]]:
    """Every primary key the leaf's annotations copy."""
    values = [v for k, v in meta.items() if k == "rotation_mechanism" or k.startswith("key_")]
    return {target for v in values if (target := copy_target(v))}


def audit(store: dict[str, Leaf]) -> Audit:
    kinds = {
        path: resolve(leaf.meta, leaf.keys) for path, leaf in store.items() if leaf.keys is not None
    }
    findings = [f for path in sorted(store) for f in check_leaf(store[path], store, kinds)]
    result = Audit(findings, kinds)
    result.blocked_leaves = {f.leaf for f in findings if f.blocks is None}
    result.blocked_keys = {(f.leaf, f.blocks) for f in findings if f.blocks is not None}
    for path in result.blocked_leaves:
        result.blocked_keys |= copies_in(store[path].meta)
    for path in sorted(kinds):
        for key, kind in kinds[path].items():
            unblocked = is_scheduled(kind) and not result.blocked(path, key)
            if unblocked and interval_of(store[path].meta, key) is None:
                result.never.append((path, key))
    return result


def due_keys(store: dict[str, Leaf], result: Audit, today: datetime.date) -> list[KeySchedule]:
    """The keys due today that the audit leaves unblocked, oldest first."""
    due = []
    for path, kinds in result.kinds.items():
        if path in result.blocked_leaves:
            continue
        unblocked = {key: kind for key, kind in kinds.items() if not result.blocked(path, key)}
        due += [s for s in schedule(path, store[path].meta, unblocked) if s.due(today)]
    return sorted(due, key=lambda s: (s.due_at, s.leaf, s.key))


def live_store(bao: OpenBao) -> dict[str, Leaf]:
    """Every leaf of the mount with its metadata and key names; no value is read."""
    return {path: Leaf(path, bao.subkeys(path), bao.metadata(path) or {}) for path in bao.leaves()}


def report(result: Audit, store: dict[str, Leaf], out) -> int:
    for f in result.findings:
        out(str(f))
    for path, key in result.never:
        out(f"never: {path}#{key}")
    keys = sum(1 for leaf, key in result.blocked_keys if leaf not in result.blocked_leaves)
    out(
        f"{len(result.findings)} finding(s) on {len({f.leaf for f in result.findings})} of "
        f"{len(store)} leaf(s); blocked: {len(result.blocked_leaves)} leaf(s), {keys} key(s); "
        f"{len(result.never)} key(s) never rotate"
    )
    return 1 if result.findings else 0
