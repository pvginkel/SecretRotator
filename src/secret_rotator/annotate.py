"""The seed (catalog.md transcribed) and its apply: each leaf's compact form, a leaf default and
per-key overrides, expanded into one rotation_<key> entry per data key; a leaf's custom metadata
made exactly those entries by metadata patch, never put, and an automatic leaf's max_versions set;
and the marker leaves it declares (design §3.4, §5, catalog § rotator/)."""

import json
import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path

import yaml

from secret_rotator.audit import Leaf
from secret_rotator.contract import (
    ALL,
    FIELDS,
    KINDS,
    MARKER_VALUE,
    MAX_KEY_BYTES,
    MAX_KEYS,
    MAX_VALUE_BYTES,
    MOUNT,
    NONE,
    ONE,
    ContractError,
    copy_target,
    dump_entry,
    entry_name,
    is_working_leaf,
    kind_error,
    load_entry,
    takes,
)
from secret_rotator.openbao import Metadata, OpenBao, OpenBaoError

DEFAULT_SEED = resources.files("secret_rotator") / "seed.yaml"
DEFAULT_KEYS = resources.files("secret_rotator") / "store-keys.json"
# The expires_at a rotation or `stamp` may add to a scheduled key's entry (design §3.2), for its
# size: an ISO date's ten characters.
SIZED_EXPIRY = "9999-12-31"

# A seed leaf's names beside the entry's fields: `keys:`, each key's own fields over the leaf
# default's; `marker: <key>`, a marker leaf (R11), created with that one data key when the store
# lacks it, only under rotator/, the one prefix the rotator's policy may create in.
KEYS = "keys"
MARKER = "marker"
MARKER_PREFIX = "rotator/"
DATA_KEY = re.compile(r"[^/\s]+")

# The KV versions an automatic leaf keeps (design §3.4).
MAX_VERSIONS = 20

LEAF_PATH = re.compile(r"[^/\s]+(/[^/\s]+)*")


class SeedError(Exception):
    pass


@dataclass(frozen=True)
class SeedLeaf:
    default: dict  # the leaf default's fields
    keys: dict[str, dict] = field(default_factory=dict)  # each named key's own fields


@dataclass
class Seed:
    leaves: dict[str, SeedLeaf]
    markers: dict[str, str]  # marker leaf -> its data key


class _UniqueKeyLoader(yaml.SafeLoader):
    pass


def _unique_mapping(loader: yaml.SafeLoader, node: yaml.MappingNode) -> dict:
    seen = set()
    for key_node, _ in node.value:
        key = loader.construct_object(key_node)
        if key in seen:
            raise SeedError(f"line {key_node.start_mark.line + 1}: {key} appears twice")
        seen.add(key)
    return loader.construct_mapping(node, deep=True)


_UniqueKeyLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _unique_mapping)


def size(text: str) -> int:
    return len(text.encode())


def _field_problems(fields: object) -> list[str]:
    """What is wrong with a seed's fields as the seed holds them: the contract's names, args a
    mapping and the rest strings, a kind of the contract. The entries' audit checks the rest."""
    if not isinstance(fields, dict) or not fields:
        return ["not a mapping of the entry's fields"]
    problems = []
    for name, value in fields.items():
        if name not in FIELDS:
            problems.append(f"{name}: not a field of the entry")
        elif name == "args" and not isinstance(value, dict):
            problems.append(f"{name}: not a mapping")
        elif name != "args" and not isinstance(value, str):
            problems.append(f"{name}: not a string")
        elif name == "kind" and (error := kind_error(value)):
            problems.append(f"{name}: {error}")
    return problems


def _entry(leaf: SeedLeaf, kind: str, own: Mapping) -> dict:
    """A key's entry: the kind, the default's other fields its kind takes, its own over them."""
    taken = takes(kind)
    shared = {name: v for name, v in leaf.default.items() if name != "kind" and name in taken}
    return {"kind": kind, **shared, **{name: v for name, v in own.items() if name != "kind"}}


def _sizes(path: str, leaf: SeedLeaf) -> list[str]:
    """What the leaf expands to that the metadata cannot hold: a named key's entry name over 128
    bytes, an entry over 512, the default's on a key without fields of its own or a named key's;
    an entry whose kind takes an expires_at counted with one, which a rotation may add later."""
    default = leaf.default.get("kind")
    problems = [
        f"{path}: {KEYS}: {key}: {entry_name(key)} is longer than {MAX_KEY_BYTES} bytes"
        for key in leaf.keys
        if size(entry_name(key)) > MAX_KEY_BYTES
    ]
    entries = {} if default is None else {"the leaf default's entry": _entry(leaf, default, {})}
    for key, own in leaf.keys.items():
        if (kind := own.get("kind", default)) is not None:
            entries[f"{KEYS}: {key}: its entry"] = _entry(leaf, kind, own)
    for what, fields in entries.items():
        expiry = "expires_at" in takes(fields["kind"]) and "expires_at" not in fields
        sized = {**fields, "expires_at": SIZED_EXPIRY} if expiry else fields
        if (n := size(dump_entry(sized))) > MAX_VALUE_BYTES:
            counted = " with an expires_at" if expiry else ""
            problems.append(f"{path}: {what}: {n} bytes{counted}, more than {MAX_VALUE_BYTES}")
    return problems


def load_seed(path: Path) -> Seed:
    """SeedError lists every problem."""
    try:
        doc = yaml.load(path.read_text(), Loader=_UniqueKeyLoader)
    except yaml.YAMLError as e:
        raise SeedError(f"{path}: not valid YAML: {e}") from None
    if not isinstance(doc, dict):
        raise SeedError(f"{path}: not a mapping of leaf paths")
    problems = []
    seed = Seed({}, {})
    for leaf, entry in doc.items():
        if not isinstance(leaf, str) or not LEAF_PATH.fullmatch(leaf):
            problems.append(f"{leaf!r}: not a leaf path under the {MOUNT} mount")
            continue
        if not isinstance(entry, dict) or not entry.keys() - {MARKER}:
            problems.append(f"{leaf}: not a mapping of the entry's fields")
            continue
        default = dict(entry)
        if MARKER in default:
            marker = default.pop(MARKER)
            if not leaf.startswith(MARKER_PREFIX) or is_working_leaf(leaf):
                problems.append(f"{leaf}: marker: a marker leaf lives under {MARKER_PREFIX}")
            elif not isinstance(marker, str) or not DATA_KEY.fullmatch(marker):
                problems.append(f"{leaf}: marker: not a data key name")
            else:
                seed.markers[leaf] = marker
        keys = default.pop(KEYS, {})
        if not isinstance(keys, dict):
            problems.append(f"{leaf}: {KEYS}: not a mapping of data keys")
            keys = {}
        found = [f"{leaf}: {problem}" for problem in _field_problems(default)] if default else []
        for key, own in keys.items():
            if not isinstance(key, str) or not DATA_KEY.fullmatch(key):
                found.append(f"{leaf}: {KEYS}: {key!r}: not a data key name")
            else:
                found += [f"{leaf}: {KEYS}: {key}: {p}" for p in _field_problems(own)]
        if found:
            problems += found
            continue
        seed.leaves[leaf] = SeedLeaf(default, keys)
        problems += _sizes(leaf, seed.leaves[leaf])
    if problems:
        raise SeedError("\n".join(problems))
    return seed


def expand(leaf: SeedLeaf, keys: Iterable[str]) -> dict[str, dict]:
    """Each data key's entry (design §5): the kind its own fields name, else the default's where
    that kind may rotate the key — a kind that rotates one key of a leaf only the single key
    without a kind of its own, while none names it (slice 045's A7) — and none where the default's
    kind leaves the key so; then the default's other fields its kind takes, its own over them. A
    key no kind falls to has no entry."""
    default = leaf.default.get("kind")
    named = {key: own["kind"] for key, own in leaf.keys.items() if "kind" in own}
    kinds: dict[str, str] = {}
    unnamed = []
    for key in sorted(keys):
        if key in named:
            kinds[key] = named[key]
        elif default is None:
            continue
        elif default == NONE or copy_target(default) or KINDS[default].owns == ALL:
            kinds[key] = default
        elif KINDS[default].owns == ONE:
            unnamed.append(key)
        elif key in KINDS[default].owns:
            kinds[key] = default
        elif key in KINDS[default].implicit_none:
            kinds[key] = NONE
    if len(unnamed) == 1 and default not in named.values():
        kinds[unnamed[0]] = default
    return {key: _entry(leaf, kind, leaf.keys.get(key, {})) for key, kind in kinds.items()}


def automatic(entries: Mapping[str, dict]) -> bool:
    """A leaf with a key whose kind is neither manual nor none, a copy included (design §3.4)."""
    return any(fields["kind"] not in ("manual", NONE) for fields in entries.values())


def changes(entries: Mapping[str, dict], current: Mapping[str, str]) -> dict[str, str | None]:
    """The patch that makes the custom metadata exactly the entries: each entry the seed adds or
    changes, then None for every other key the metadata holds. An existing entry keeps its
    expires_at, or its absence: the seed's is written only with a new entry (design §3.2)."""
    out: dict[str, str | None] = {}
    for key, fields in sorted(entries.items()):
        name = entry_name(key)
        if name not in current:
            out[name] = dump_entry(fields)
            continue
        try:
            live = load_entry(current[name])
        except ContractError:
            live = {}
        want = {n: v for n, v in fields.items() if n != "expires_at"}
        if "expires_at" in live:
            want["expires_at"] = live["expires_at"]
        if want != live:
            out[name] = dump_entry(want)
    names = {entry_name(key) for key in entries}
    out.update(dict.fromkeys(sorted(current.keys() - names)))
    return out


@dataclass
class Write:
    """One leaf's: its custom metadata patch, and an automatic leaf's max_versions when it is not
    MAX_VERSIONS."""

    patch: dict[str, str | None]  # a None value removes its key
    current: dict[str, str]
    max_versions: int | None = None  # the leaf's, which the write sets to MAX_VERSIONS


@dataclass
class Plan:
    writes: dict[str, Write] = field(default_factory=dict)
    creates: dict[str, str] = field(default_factory=dict)  # marker leaf -> its data key
    unchanged: list[str] = field(default_factory=list)
    absent: list[str] = field(default_factory=list)
    unreadable: list[str] = field(default_factory=list)  # leaves whose keys cannot be read
    unnamed: list[str] = field(default_factory=list)  # leaf#key no kind of the seed falls to
    unheld: list[str] = field(default_factory=list)  # leaf#key the seed names, the leaf lacks
    uncovered: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def make_plan(bao: OpenBao, seed: Seed) -> Plan:
    """What the apply writes; it reads metadata and key names only."""
    plan = Plan()
    live = bao.leaves()
    plan.uncovered = [leaf for leaf in live if leaf not in seed.leaves]
    live_set = set(live)
    for leaf in sorted(seed.leaves):
        held = bao.leaf_metadata(leaf) if leaf in live_set else None
        if held is None and leaf in seed.markers:
            plan.creates[leaf] = seed.markers[leaf]
            held, keys = Metadata({}, 0), {seed.markers[leaf]}
        elif held is None:
            plan.absent.append(leaf)
            continue
        elif (keys := bao.subkeys(leaf)) is None:
            plan.unreadable.append(leaf)
            continue
        entries = expand(seed.leaves[leaf], keys)
        plan.unnamed += [f"{leaf}#{key}" for key in sorted(keys - entries.keys())]
        plan.unheld += [f"{leaf}#{key}" for key in sorted(seed.leaves[leaf].keys.keys() - keys)]
        patch = changes(entries, held.custom)
        versions = held.max_versions
        if not automatic(entries) or versions == MAX_VERSIONS:
            versions = None
        if not patch and versions is None:
            plan.unchanged.append(leaf)
            continue
        plan.writes[leaf] = Write(patch, held.custom, versions)
        for name, value in patch.items():
            if value is not None and size(name) > MAX_KEY_BYTES:
                plan.errors.append(f"{leaf}: {name}: longer than {MAX_KEY_BYTES} bytes")
        if len(entries) > MAX_KEYS:
            plan.errors.append(f"{leaf}: more than {MAX_KEYS} metadata keys once patched")
    return plan


def report(out: Callable[[str], None], plan: Plan, apply: bool) -> None:
    for leaf, write in plan.writes.items():
        out(leaf)
        if leaf in plan.creates:
            out(f"  create  marker leaf, data key {plan.creates[leaf]}")
        for key, value in write.patch.items():
            have = write.current.get(key)
            if value is None:
                out(f"  remove  {key}={have}")
            elif have is None:
                out(f"  add     {key}={value}")
            else:
                out(f"  change  {key}={value}  (was {have})")
        if write.max_versions is not None:
            out(f"  set     max_versions={MAX_VERSIONS}  (was {write.max_versions})")
    for leaf in plan.absent:
        out(f"absent from the store, skipped: {leaf}")
    for leaf in plan.unreadable:
        out(f"its keys cannot be read (current version deleted or destroyed), skipped: {leaf}")
    for key in plan.unnamed:
        out(f"no kind in the seed, no entry: {key}")
    for key in plan.unheld:
        out(f"named in the seed, not held by the leaf: {key}")
    for leaf in plan.uncovered:
        out(f"not in the seed: {leaf}")
    verb = "patching" if apply else "would patch (dry run; --apply writes)"
    out(
        f"{verb} {len(plan.writes)} leaf(s), {len(plan.creates)} of them new marker leaves; "
        f"{len(plan.unchanged)} unchanged, {len(plan.absent)} absent from the store, "
        f"{len(plan.uncovered)} live leaf(s) not in the seed"
    )


def run_apply(bao: OpenBao, seed: Seed, apply: bool, out: Callable[[str], None]) -> int:
    plan = make_plan(bao, seed)
    report(out, plan, apply)
    if plan.errors:
        for error in plan.errors:
            out(f"cannot write: {error}")
        out("nothing written")
        return 1
    if not apply:
        return 0
    for done, (leaf, write) in enumerate(plan.writes.items()):
        try:
            if leaf in plan.creates:
                bao.create(leaf, {plan.creates[leaf]: MARKER_VALUE})
            bao.patch_metadata(
                leaf, write.patch, None if write.max_versions is None else MAX_VERSIONS
            )
        except OpenBaoError as e:
            if e.status == 403:
                out(
                    f"stopped at {leaf}: OpenBao refused the write ({e}). The token's policy "
                    f"lacks the capability: the openbao role's rotator policy grants patch on the "
                    f"{MOUNT} mount and create under {MARKER_PREFIX}. Patched {done} of "
                    f"{len(plan.writes)} leaf(s); run the apply again after the converge."
                )
            else:
                out(f"stopped at {leaf}: {e}. Patched {done} of {len(plan.writes)} leaf(s).")
            return 1
        out(f"{'created and patched' if leaf in plan.creates else 'patched'} {leaf}")
    return 0


def offline_store(keys_file: Path, seed: Seed, out: Callable[[str], None]) -> dict[str, Leaf]:
    """The store as keys_file names it (leaf path -> data key names), annotated by the seed."""
    try:
        doc = json.loads(keys_file.read_text())
    except ValueError:
        raise SeedError(f"{keys_file}: not JSON") from None
    if not isinstance(doc, dict) or not all(
        isinstance(v, list) and all(isinstance(k, str) for k in v) for v in doc.values()
    ):
        raise SeedError(f"{keys_file}: not a JSON object of leaf path -> key names")
    for leaf in sorted(set(seed.leaves) - set(doc)):
        out(f"seed leaf not in the key file: {leaf}")
    store = {}
    for leaf, keys in doc.items():
        held = seed.leaves.get(leaf)
        entries = {} if held is None else expand(held, keys)
        for key in sorted(held.keys.keys() - set(keys)) if held else ():
            out(f"seed key not in the key file: {leaf}#{key}")
        meta = {entry_name(key): dump_entry(fields) for key, fields in entries.items()}
        store[leaf] = Leaf(leaf, set(keys), meta)
    return store
