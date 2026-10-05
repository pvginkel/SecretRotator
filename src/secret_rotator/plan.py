"""Building a plan (design §4.3): the leaf's annotations resolved into a Target, the kind's steps
built with the step factory, and kv.stamp appended by the core."""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

from secret_rotator.audit import Audit, Leaf
from secret_rotator.contract import copy_target, is_implemented, is_scheduled, parse_args
from secret_rotator.kvsteps import DEFAULT_LENGTH, URLSAFE, KvCopy, KvStamp, KvWrite, RandomGenerate
from secret_rotator.model import Actor, Step


class PlanError(Exception):
    pass


@dataclass(frozen=True, order=True)
class Copy:
    leaf: str
    key: str
    of: str  # the primary key it copies


@dataclass(frozen=True)
class Target:
    """What a kind plans from: one kind's keys on one leaf, with the annotations they resolve to."""

    leaf: str
    kind: str
    keys: tuple[str, ...]  # the keys the plan rotates, sorted
    args: Mapping  # rotation_args, when the kind is the leaf's rotation_mechanism; else empty
    copies: tuple[Copy, ...]  # every copy of those keys, sorted
    meta: Mapping[str, str]


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


@dataclass(frozen=True)
class PlanContext:
    steps: StepFactory


class Kind(Protocol):
    name: str  # the rotation_mechanism or key_<name> value

    def plan(self, leaf: Target, ctx: PlanContext) -> list[Step]:
        """The whole plan but kv.stamp, built with ctx.steps; the same steps with the same ids
        for the same Target."""


@dataclass(frozen=True)
class Plan:
    target: Target
    steps: tuple[Step, ...]

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
    if not is_scheduled(kind) or not is_implemented(kind):
        raise PlanError(f"{leaf}: {kind} is not a kind the rotator rotates")
    problems = [] if keys else ["no key to rotate"]
    for key in sorted(keys):
        if kinds.get(key) != kind:
            problems.append(f"{key} is not a {kind} key")
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
    return Target(
        leaf,
        kind,
        tuple(sorted(keys)),
        parse_args(meta["rotation_args"]) if own else {},
        tuple(copies),
        dict(meta),
    )


def build(kind: Kind, leaf: Target) -> Plan:
    steps = [*kind.plan(leaf, PlanContext(StepFactory(leaf))), KvStamp(leaf.leaf, leaf.keys)]
    ids = [step.id for step in steps]
    if dupes := sorted({i for i in ids if ids.count(i) > 1}):
        raise PlanError(f"{leaf.leaf}: the {kind.name} plan repeats step id(s) {', '.join(dupes)}")
    return Plan(leaf, tuple(steps))
