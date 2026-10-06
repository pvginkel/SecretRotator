"""The seed (catalog.md transcribed) and its apply: metadata patch, never put, and the marker leaves
it declares (design §5, catalog § rotator/)."""

import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path

import yaml

from secret_rotator.audit import Leaf
from secret_rotator.contract import (
    MARKER_VALUE,
    MAX_KEY_BYTES,
    MAX_KEYS,
    MAX_VALUE_BYTES,
    MOUNT,
    is_working_leaf,
)
from secret_rotator.openbao import OpenBao, OpenBaoError

DEFAULT_SEED = resources.files("secret_rotator") / "seed.yaml"
DEFAULT_KEYS = resources.files("secret_rotator") / "store-keys.json"

# The operator-owned keys of the contract: the only metadata a seed may hold.
SEED_KEYS = {
    "rotation_mechanism",
    "rotation_interval",
    "rotation_activate",
    "rotation_args",
    "rotation_expires_at",
    "notes",
}
SEED_PREFIXES = ("key_", "interval_")

# A seed entry's `marker: <key>` declares a marker leaf (R11): created with that one data key
# when the store lacks it. Only under rotator/, the one prefix the rotator's policy may create in.
MARKER = "marker"
MARKER_PREFIX = "rotator/"
DATA_KEY = re.compile(r"[^/\s]+")

# Where the seed adds notes to a leaf that already has other notes, the earlier text follows.
NOTES_JOIN = " | earlier: "

LEAF_PATH = re.compile(r"[^/\s]+(/[^/\s]+)*")


class SeedError(Exception):
    pass


@dataclass
class Seed:
    annotations: dict[str, dict[str, str]]  # leaf -> the metadata keys to write
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
    return loader.construct_mapping(node)


_UniqueKeyLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _unique_mapping)


def size(text: str) -> int:
    return len(text.encode())


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
            problems.append(f"{leaf}: not a mapping of metadata keys")
            continue
        meta = dict(entry)
        if MARKER in meta:
            marker = meta.pop(MARKER)
            if not leaf.startswith(MARKER_PREFIX) or is_working_leaf(leaf):
                problems.append(f"{leaf}: marker: a marker leaf lives under {MARKER_PREFIX}")
            elif not isinstance(marker, str) or not DATA_KEY.fullmatch(marker):
                problems.append(f"{leaf}: marker: not a data key name")
            else:
                seed.markers[leaf] = marker
        if len(meta) > MAX_KEYS:
            problems.append(f"{leaf}: {len(meta)} metadata keys, more than {MAX_KEYS}")
        for key, value in meta.items():
            if not isinstance(key, str) or not (key in SEED_KEYS or key.startswith(SEED_PREFIXES)):
                problems.append(f"{leaf}: {key}: not an operator key of the contract")
            elif size(key) > MAX_KEY_BYTES:
                problems.append(f"{leaf}: {key}: longer than {MAX_KEY_BYTES} bytes")
            elif not isinstance(value, str):
                problems.append(f"{leaf}: {key}: not a string")
            elif size(value) > MAX_VALUE_BYTES:
                problems.append(f"{leaf}: {key}: longer than {MAX_VALUE_BYTES} bytes")
        seed.annotations[leaf] = meta
    if problems:
        raise SeedError("\n".join(problems))
    return seed


def changes(seed: dict[str, str], current: dict[str, str]) -> dict[str, str]:
    """The metadata keys the seed adds or changes; earlier notes are kept after the seed's."""
    out = {}
    for key, value in seed.items():
        have = current.get(key)
        if key == "notes" and have and value != have:
            if have.startswith(value + NOTES_JOIN):
                continue
            value = f"{value}{NOTES_JOIN}{have}"
        if have != value:
            out[key] = value
    return out


@dataclass
class Plan:
    patches: dict[str, dict[str, str]] = field(default_factory=dict)
    current: dict[str, dict[str, str]] = field(default_factory=dict)
    creates: dict[str, str] = field(default_factory=dict)  # marker leaf -> its data key
    unchanged: list[str] = field(default_factory=list)
    absent: list[str] = field(default_factory=list)
    uncovered: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def make_plan(bao: OpenBao, seed: Seed) -> Plan:
    """What the apply writes; it reads metadata only."""
    plan = Plan()
    live = bao.leaves()
    plan.uncovered = [leaf for leaf in live if leaf not in seed.annotations]
    live_set = set(live)
    for leaf in sorted(seed.annotations):
        current = bao.metadata(leaf) if leaf in live_set else None
        if current is None and leaf in seed.markers:
            plan.creates[leaf] = seed.markers[leaf]
            current = {}
        elif current is None:
            plan.absent.append(leaf)
            continue
        patch = changes(seed.annotations[leaf], current)
        if not patch:
            plan.unchanged.append(leaf)
            continue
        plan.patches[leaf], plan.current[leaf] = patch, current
        if size(patch.get("notes", "")) > MAX_VALUE_BYTES:
            plan.errors.append(
                f"{leaf}: notes: with the earlier notes kept, longer than {MAX_VALUE_BYTES} bytes"
            )
        if len(current.keys() | patch.keys()) > MAX_KEYS:
            plan.errors.append(f"{leaf}: more than {MAX_KEYS} metadata keys once patched")
    return plan


def report(out: Callable[[str], None], plan: Plan, apply: bool) -> None:
    for leaf, patch in plan.patches.items():
        out(leaf)
        if leaf in plan.creates:
            out(f"  create  marker leaf, data key {plan.creates[leaf]}")
        for key, value in patch.items():
            have = plan.current[leaf].get(key)
            out(
                f"  add     {key}={value}"
                if have is None
                else f"  change  {key}={value}  (was {have})"
            )
    for leaf in plan.absent:
        out(f"absent from the store, skipped: {leaf}")
    for leaf in plan.uncovered:
        out(f"not in the seed: {leaf}")
    verb = "patching" if apply else "would patch (dry run; --apply writes)"
    out(
        f"{verb} {len(plan.patches)} leaf(s), {len(plan.creates)} of them new marker leaves; "
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
    for done, (leaf, patch) in enumerate(plan.patches.items()):
        try:
            if leaf in plan.creates:
                bao.create(leaf, {plan.creates[leaf]: MARKER_VALUE})
            bao.patch_metadata(leaf, patch)
        except OpenBaoError as e:
            if e.status == 403:
                out(
                    f"stopped at {leaf}: OpenBao refused the write ({e}). The token's policy "
                    f"lacks the capability: the openbao role's rotator policy grants patch on the "
                    f"{MOUNT} mount and create under {MARKER_PREFIX}. Patched {done} of "
                    f"{len(plan.patches)} leaf(s); run the apply again after the converge."
                )
            else:
                out(f"stopped at {leaf}: {e}. Patched {done} of {len(plan.patches)} leaf(s).")
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
    for leaf in sorted(set(seed.annotations) - set(doc)):
        out(f"seed leaf not in the key file: {leaf}")
    return {
        leaf: Leaf(leaf, set(keys), dict(seed.annotations.get(leaf, {})))
        for leaf, keys in doc.items()
    }
