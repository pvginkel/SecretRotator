"""The compliance check of design §3.3: every leaf held to the annotation contract (§5), one
rotation_<key> entry per data key.

A finding blocks only what it touches: a finding on a key's entry blocks that key and every
primary it copies, since that primary's plan would write and activate it; a leaf-level one blocks
the leaf and every primary key copied into it; a stale entry blocks nothing."""

import datetime
from collections import defaultdict
from dataclasses import dataclass, field

from secret_rotator.contract import (
    ContractError,
    Entry,
    copy_target,
    entries_of,
    entry_name,
    entry_problems,
    is_scheduled,
    kind_in,
    load_entry,
    may_rotate,
)
from secret_rotator.openbao import OpenBao
from secret_rotator.schedule import KeySchedule, schedule
from secret_rotator.staging import InFlight, flights
from secret_rotator.state import LeafState
from secret_rotator.state import read as read_state

# The leaves the prd cluster's ESO reads: the orphan check's, since the rotator reads no other
# cluster. eso/dev/ is the dev cluster's.
ESO_PRD = "eso/prd/"

# Finding.blocks of a finding that blocks nothing: a stale entry.
NOTHING = ""


@dataclass
class Leaf:
    path: str
    keys: set[str] | None  # None: the current version's keys cannot be read
    meta: dict[str, str]
    state: LeafState = field(default_factory=LeafState)  # its run state
    flight: InFlight | None = None  # its plan in flight


@dataclass(frozen=True)
class Finding:
    leaf: str
    key: str  # the metadata key or data key at fault
    message: str
    blocks: str | None = None  # the data key it blocks; None: the whole leaf; NOTHING: nothing

    def __str__(self) -> str:
        return f"{self.leaf}: {self.key}: {self.message}"


@dataclass
class Audit:
    findings: list[Finding]
    # Per leaf whose keys could be read, each key's kind as its entry names it; None: none does.
    kinds: dict[str, dict[str, str | None]]
    # Per leaf whose keys could be read, the entry of each key no finding is on.
    entries: dict[str, dict[str, Entry]] = field(default_factory=dict)
    blocked_leaves: set[str] = field(default_factory=set)
    blocked_keys: set[tuple[str, str]] = field(default_factory=set)
    never: list[tuple[str, str]] = field(default_factory=list)  # scheduled keys at never

    def blocked(self, leaf: str, key: str) -> bool:
        return leaf in self.blocked_leaves or (leaf, key) in self.blocked_keys


def check_leaf(leaf: Leaf, store: dict[str, Leaf], kinds_of: dict) -> list[Finding]:
    findings = []

    def find(key: str, message: str, blocks: str | None = None) -> None:
        findings.append(Finding(leaf.path, key, message, blocks))

    if leaf.keys is None:
        find("(data)", "its current version is deleted or destroyed: its keys cannot be read")
    texts = entries_of(leaf.meta)
    held = leaf.keys if leaf.keys is not None else set(texts)
    for key in sorted(held | set(texts)):
        name = entry_name(key)
        if key not in texts:
            find(name, "missing", key)
            continue
        if key not in held:
            find(name, f"stale: the leaf has no key {key!r}", NOTHING)
            continue
        try:
            fields = load_entry(texts[key])
        except ContractError as e:
            find(name, str(e), key)
            continue
        for problem in entry_problems(fields):
            find(name, problem, key)
        kind = kind_in(texts[key])
        if kind is None:
            continue
        if not may_rotate(kind, key):
            find(name, f"kind: {kind} does not rotate {key}", key)
        target = copy_target(kind)
        if target is None:
            continue
        primary, primary_key = target
        if primary not in store:
            find(name, f"copy of {primary}#{primary_key}: no leaf {primary}", key)
        elif store[primary].keys is not None and primary_key not in store[primary].keys:
            find(
                name,
                f"copy of {primary}#{primary_key}: {primary} has no key {primary_key!r}",
                key,
            )
        elif copy_target(kinds_of.get(primary, {}).get(primary_key)):
            find(name, f"copy of {primary}#{primary_key}, which is itself a copy", key)
    return findings


def orphans(
    store: dict[str, Leaf], kinds_of: dict, referenced: set[str]
) -> dict[str, list[Finding]]:
    """The orphan check (design §3.3): an eso/prd/ leaf no ExternalSecret references, nor any
    leaf a key of it is copied into. Copies count as consumers (045's RV2): one in an eso/prd/
    leaf an ExternalSecret references, or in a leaf outside eso/prd/, which the check does not
    reach. An orphan blocks the leaf: its activation has nothing to activate."""
    copied_into = defaultdict(set)
    for path, kinds in kinds_of.items():
        for kind in kinds.values():
            if target := copy_target(kind):
                copied_into[target[0]].add(path)

    def consumed(path: str) -> bool:
        return path in referenced or not path.startswith(ESO_PRD)

    return {
        path: [
            Finding(
                path,
                "(consumers)",
                "an orphan: no ExternalSecret references it or a leaf it is copied into",
            )
        ]
        for path in store
        if path.startswith(ESO_PRD)
        and path not in referenced
        and not any(consumed(c) for c in copied_into[path])
    }


def copies_in(meta: dict[str, str]) -> set[tuple[str, str]]:
    """Every primary key the leaf's entries copy."""
    kinds = (kind_in(text) for text in entries_of(meta).values())
    return {target for kind in kinds if (target := copy_target(kind))}


def audit(store: dict[str, Leaf], referenced: set[str] | None = None) -> Audit:
    """The compliance check of the store; with referenced, the leaves the prd cluster's
    ExternalSecrets reference, the orphan check too."""
    kinds = {
        path: {key: kind_in(entries_of(leaf.meta).get(key)) for key in leaf.keys}
        for path, leaf in store.items()
        if leaf.keys is not None
    }
    orphaned = {} if referenced is None else orphans(store, kinds, referenced)
    findings = [
        f
        for path in sorted(store)
        for f in [*check_leaf(store[path], store, kinds), *orphaned.get(path, [])]
    ]
    result = Audit(findings, kinds)
    result.blocked_leaves = {f.leaf for f in findings if f.blocks is None}
    flagged = {(f.leaf, f.blocks) for f in findings if f.blocks}
    result.blocked_keys = set(flagged)
    for path in result.blocked_leaves:
        result.blocked_keys |= copies_in(store[path].meta)
    for path, key in flagged:
        if target := copy_target(kinds.get(path, {}).get(key)):
            result.blocked_keys.add(target)
    for path, leaf_kinds in kinds.items():
        texts = entries_of(store[path].meta)
        result.entries[path] = {
            key: Entry.load(load_entry(texts[key]))
            for key in sorted(leaf_kinds)
            if key in texts and (path, key) not in flagged
        }
        for key, entry in result.entries[path].items():
            unblocked = is_scheduled(entry.kind) and not result.blocked(path, key)
            if unblocked and entry.interval is None:
                result.never.append((path, key))
    return result


def due_keys(store: dict[str, Leaf], result: Audit, today: datetime.date) -> list[KeySchedule]:
    """The keys due today that the audit leaves unblocked, oldest first."""
    due = []
    for path, entries in result.entries.items():
        if path in result.blocked_leaves:
            continue
        unblocked = {key: e for key, e in entries.items() if not result.blocked(path, key)}
        due += [s for s in schedule(path, unblocked, store[path].state.stamps) if s.due(today)]
    return sorted(due, key=lambda s: (s.due_at, s.leaf, s.key))


def live_store(bao: OpenBao, *, runs: bool = False) -> dict[str, Leaf]:
    """Every leaf of the mount with its metadata and key names; no value is read. With runs, each
    leaf's run state and plan in flight too, read from the rotator's working leaves: the state
    leaf, and the staging leaves with the values the plans in flight staged."""
    states = read_state(bao)[1] if runs else {}
    flying = flights(bao) if runs else {}
    return {
        path: Leaf(
            path,
            bao.subkeys(path),
            bao.metadata(path) or {},
            states.get(path, LeafState()),
            flying.get(path),
        )
        for path in bao.leaves()
    }


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
