"""Building a plan (design §4.3): the leaf's annotations resolved into a Target, the kind's steps
built with the step factory, and kv.stamp appended by the core. A leaf has one plan per kind with
a plugin, one per key for a per-key kind (manual)."""

import datetime
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Protocol

from secret_rotator.audit import Audit, Leaf
from secret_rotator.contract import (
    NONE,
    Activator,
    copy_target,
    is_scheduled,
    parse_activate,
    parse_args,
)
from secret_rotator.kvsteps import DEFAULT_LENGTH, URLSAFE, KvCopy, KvStamp, KvWrite, RandomGenerate
from secret_rotator.model import Actor, Step
from secret_rotator.opsteps import OperatorConfirm, OperatorCredential, OperatorShow, Shape
from secret_rotator.schedule import schedule


class PlanError(Exception):
    pass


@dataclass(frozen=True, order=True)
class Copy:
    leaf: str
    key: str
    of: str  # the primary key it copies


@dataclass(frozen=True)
class Activation:
    """One leaf's rotation_activate: the primary's leaf, or a leaf a copy lands in."""

    leaf: str
    specs: tuple[Activator, ...]


@dataclass(frozen=True)
class Target:
    """What a kind plans from: one kind's keys on one leaf, with the annotations they resolve to."""

    leaf: str
    kind: str
    keys: tuple[str, ...]  # the keys the plan rotates, sorted
    args: Mapping  # rotation_args, when the kind is the leaf's rotation_mechanism; else empty
    copies: tuple[Copy, ...]  # every copy of those keys, sorted
    meta: Mapping[str, str]
    activations: tuple[Activation, ...]  # the primary's leaf first, then each copy's leaf

    @property
    def activates(self) -> bool:
        """Whether a leaf the plan writes names any activation."""
        return any(a.specs for a in self.activations)

    @property
    def confirms(self) -> tuple[str, ...]:
        """The texts of the manual: activators, each an operator.confirm of the plan."""
        return tuple(s.arg for a in self.activations for s in a.specs if s.name == "manual")


def tool_part(leaf: Target) -> str:
    """What the tool does with the new value, for a kind's description: `writes it to the leaf and
    its 2 copies and activates what reads it`."""
    n = len(leaf.copies)
    copies = "" if n == 0 else f" and its {n} cop{'y' if n == 1 else 'ies'}"
    activates = " and activates what reads it" if leaf.activates else ""
    return f"writes it to the leaf{copies}{activates}"


class StepFactory:
    """The patterns a plan is built from, named by what they do (design §4.3)."""

    def __init__(self, target: Target):
        self.target = target

    def generate(
        self, key: str, *, length: int = DEFAULT_LENGTH, charset: str = URLSAFE
    ) -> list[Step]:
        """A new random value for one of the plan's keys."""
        return [RandomGenerate(key, length=length, charset=charset)]

    def write(self) -> list[Step]:
        """kv.write of the plan's keys to the leaf, then a kv.copy per copy key."""
        t = self.target
        return [KvWrite(t.leaf, t.keys), *(KvCopy(c.leaf, c.key, c.of) for c in t.copies)]

    def credential(self, title: str, instruction: str, shape: Shape | None = None) -> list[Step]:
        """The operator mints the plan's keys elsewhere and enters them, one masked input each."""
        return [OperatorCredential(self.target.keys, title, instruction, shape)]

    def show(self, name: str, title: str, instruction: str) -> list[Step]:
        """The operator puts the value staged as `name` where only a human can."""
        return [OperatorShow(name, title, instruction)]

    def confirm(
        self, id: str, title: str, instruction: str = "", *, irreversible: str = ""
    ) -> list[Step]:
        """The operator does something and confirms it; irreversible: why it cannot be undone."""
        return [OperatorConfirm(id, title, instruction, irreversible=irreversible)]

    def activate(self) -> list[Step]:
        """The activation of the primary's leaf, then of each copy's leaf: every spec of its
        rotation_activate as its steps. A spec no step is built for refuses the plan."""
        steps: list[Step] = []
        for activation in self.target.activations:
            for n, spec in enumerate(activation.specs, 1):
                if spec.name != "manual":
                    whose = "" if activation.leaf == self.target.leaf else f"{activation.leaf}'s "
                    raise PlanError(
                        f"{self.target.leaf}: {whose}rotation_activate {spec}: no step is built "
                        f"for it yet"
                    )
                steps.append(
                    OperatorConfirm(
                        f"{activation.leaf}:{n}", spec.arg, f"for {activation.leaf}", activator=True
                    )
                )
        return steps


@dataclass(frozen=True)
class PlanContext:
    steps: StepFactory


class Kind(Protocol):
    """A kind's plugin (design §6, plugin contract), registered under its name."""

    name: str  # the rotation_mechanism or key_<name> value
    per_key: bool  # one plan per key of the kind on a leaf; else one plan of all of them

    def args_problems(self, args: Mapping) -> list[str]:
        """What is wrong with the leaf's rotation_args for this kind; empty when nothing is."""

    def ask(self, leaf: Target) -> str:
        """What the plan asks of the operator, in a few words; empty when it asks nothing."""

    def description(self, leaf: Target) -> str:
        """One or two sentences on the operator's part and the tool's."""

    def plan(self, leaf: Target, ctx: PlanContext) -> list[Step]:
        """The whole plan but kv.stamp, built with ctx.steps; the same steps with the same ids
        for the same Target."""


@dataclass(frozen=True)
class Plan:
    target: Target
    steps: tuple[Step, ...]
    ask: str = ""
    description: str = ""

    @property
    def name(self) -> str:
        return f"{self.target.kind} plan of {self.target.leaf}"

    @property
    def needs_operator(self) -> bool:
        return any(step.actor is Actor.OPERATOR for step in self.steps)

    def index(self, step_id: str) -> int | None:
        return next((i for i, step in enumerate(self.steps) if step.id == step_id), None)


def target(
    leaf: str, kind: str, keys: list[str], store: Mapping[str, Leaf], audit: Audit
) -> Target:
    """The Target of rotating these keys of the leaf, which must resolve to the kind and be
    unblocked by the audit of the store."""
    kinds = audit.kinds.get(leaf)
    if kinds is None:
        raise PlanError(f"{leaf}: no such leaf, or its keys cannot be read")
    if not is_scheduled(kind):
        raise PlanError(f"{leaf}: {kind} is not a kind the rotator rotates")
    problems = [] if keys else ["no key to rotate"]
    for key in sorted(keys):
        if kinds.get(key) != kind:
            problems.append(f"{key} is not a {kind} key")
        elif not set(key).isdisjoint(",/"):
            problems.append(f"{key}: a key name with ',' or '/' cannot be named in rotator_step")
        elif audit.blocked(leaf, key):
            problems.append(f"{key} is blocked by a finding")
    if problems:
        raise PlanError(f"{leaf}: {'; '.join(problems)}")
    meta = store[leaf].meta
    own = kind == meta.get("rotation_mechanism") and "rotation_args" in meta
    copies = sorted(
        Copy(path, key, of[1])
        for path, leaf_kinds in audit.kinds.items()
        for key, k in leaf_kinds.items()
        if (of := copy_target(k)) and of[0] == leaf and of[1] in keys
    )
    # Unblocked, so every one of these leaves holds a rotation_activate that parses: a leaf a
    # copy lands in blocks the primary key when it has a leaf-level finding.
    leaves = [leaf, *sorted({c.leaf for c in copies} - {leaf})]
    activations = tuple(
        Activation(path, tuple(parse_activate(store[path].meta["rotation_activate"])))
        for path in leaves
    )
    return Target(
        leaf,
        kind,
        tuple(sorted(keys)),
        parse_args(meta["rotation_args"]) if own else {},
        tuple(copies),
        dict(meta),
        activations,
    )


def build(kind: Kind, leaf: Target) -> Plan:
    if problems := kind.args_problems(leaf.args):
        raise PlanError(f"{leaf.leaf}: rotation_args: {'; '.join(problems)}")
    steps = [*kind.plan(leaf, PlanContext(StepFactory(leaf))), KvStamp(leaf.leaf, leaf.keys)]
    ids = [step.id for step in steps]
    if dupes := sorted({i for i in ids if ids.count(i) > 1}):
        raise PlanError(f"{leaf.leaf}: the {kind.name} plan repeats step id(s) {', '.join(dupes)}")
    return Plan(leaf, tuple(steps), kind.ask(leaf), kind.description(leaf))


def make(
    kinds: Mapping[str, Kind],
    leaf: str,
    kind: str,
    keys: list[str],
    store: Mapping[str, Leaf],
    audit: Audit,
) -> Plan:
    """The plan of rotating these keys of the leaf, built by the kind's plugin."""
    if kind not in kinds:
        raise PlanError(f"{leaf}: {kind} is not a kind this install has a plugin for")
    return build(kinds[kind], target(leaf, kind, keys, store, audit))


def split(kind: Kind, keys: Iterable[str]) -> list[tuple[str, ...]]:
    """The key sets of a kind's plans on one leaf: one per key for a per-key kind, else one."""
    ordered = tuple(sorted(keys))
    return [(key,) for key in ordered] if kind.per_key else [ordered]


@dataclass(frozen=True)
class LeafPlan:
    kind: str
    keys: tuple[str, ...]
    due_at: datetime.date | None  # the earliest of its keys' (schedule.KeySchedule); None: never
    plan: Plan | None  # None: it cannot be built, for error
    error: str = ""


def _blocked_by(audit: Audit, leaf: str, key: str) -> str:
    found = [
        f"{f.key}: {f.message}"
        for f in audit.findings
        if f.leaf == leaf and f.blocks in (None, key)
    ]
    return "; ".join(found) or "a leaf it is copied into has a finding"


def of_leaf(
    leaf: str, store: Mapping[str, Leaf], audit: Audit, kinds: Mapping[str, Kind]
) -> tuple[list[LeafPlan], dict[str, str]]:
    """The leaf's plans by hand, the soonest due first: each covers every key of its kind on the
    leaf, per key for a per-key kind; and each other key with why it has none."""
    leaf_kinds = audit.kinds.get(leaf, {})
    groups: dict[str, list[str]] = {}
    unplanned = {}
    for key, kind in sorted(leaf_kinds.items()):
        if audit.blocked(leaf, key):
            unplanned[key] = f"blocked: {_blocked_by(audit, leaf, key)}"
        elif kind == NONE:
            unplanned[key] = "none: not a secret, never rotated"
        elif of := copy_target(kind):
            unplanned[key] = f"a copy of {of[0]}#{of[1]}, written by its primary's plan"
        elif kind not in kinds:
            unplanned[key] = f"{kind}: a kind this install has no plugin for yet"
        else:
            groups.setdefault(kind, []).append(key)
    if not groups:
        return [], unplanned
    planned = {key: kind for kind, keys in groups.items() for key in keys}
    due = {s.key: s.due_at for s in schedule(leaf, store[leaf].meta, planned)}
    plans = []
    for kind, keys in groups.items():
        for subset in split(kinds[kind], keys):
            dates = [due[key] for key in subset if due[key] is not None]
            try:
                built, error = make(kinds, leaf, kind, list(subset), store, audit), ""
            except PlanError as e:
                built, error = None, str(e)
            plans.append(LeafPlan(kind, subset, min(dates, default=None), built, error))
    never = datetime.date.max
    plans.sort(key=lambda p: (p.due_at is None, p.due_at or never, p.kind, p.keys))
    return plans, unplanned
