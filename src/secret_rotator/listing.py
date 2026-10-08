"""What `secret-rotator ui` lists (design §7.3, §7.5, R86): every plan with an operator step, each
leaf's plans built as `run <path>` builds them, with where each stands and what its box shows. A
plan without an operator step is never listed, in flight or failed (R31); nor is a plan that cannot
be built, which the nightly run puts on the standing card and `plan <path>` explains."""

import datetime
from collections.abc import Mapping
from dataclasses import dataclass

from secret_rotator.audit import Audit, Leaf, audit, exempt
from secret_rotator.cluster import Cluster
from secret_rotator.executor import PlanMismatch, Stand, stand_of
from secret_rotator.plan import Kind, Plan, PlanError, Target, in_flight, of_leaf
from secret_rotator.schedule import schedule
from secret_rotator.staging import InFlight

# The kind whose keys the filter tells apart by their credential type, its args' `type` (R89).
MANUAL = "manual"


@dataclass(frozen=True)
class Rotation:
    """One listed plan."""

    plan: Plan
    stand: Stand  # FRESH while it is not in flight
    at: int  # the index of the step it is at
    # The first day it is due, the earliest of its keys' by the schedule (§3.2): date.min for a
    # key never stamped; None when none of its keys has a due date (R88).
    due_at: datetime.date | None
    # Its leaf's plan in flight: this plan, or another one, which this one waits on.
    flight: InFlight | None
    credential: str  # its key's credential in words, for the title (R85)
    type: str  # what the filter keeps together (R89)

    @property
    def waits(self) -> bool:
        """Another plan of its leaf is in flight: this one starts once that one is done or
        rolled back."""
        return self.stand is Stand.FRESH and self.flight is not None

    @property
    def estimate(self) -> int:
        """Seconds, of what remains of it: the whole plan until it starts."""
        return sum(step.estimate for step in self.plan.steps[self.at :])


def credential(kind: Kind, leaf: Target) -> str:
    words = getattr(kind, "credential", None)
    return kind.name if words is None else words(leaf)


def type_of(leaf: Target) -> str:
    if leaf.kind == MANUAL:
        return leaf.entries[leaf.keys[0]].args.get("type", MANUAL)
    return leaf.kind


def due_at(leaf: Target, stamps: Mapping[str, str]) -> datetime.date | None:
    """The earliest of the plan's keys' due dates; None when none has one."""
    dates = [s.due_at for s in schedule(leaf.leaf, leaf.entries, stamps) if s.due_at is not None]
    return min(dates, default=None)


def listed(
    store: Mapping[str, Leaf], kinds: Mapping[str, Kind], cluster: Cluster | None = None
) -> list[Rotation]:
    """Every plan with an operator step, in §7.5's order: the plans in flight first, then by when
    they fall due, the earliest first, then those without a due date. store: read with its runs
    (live_store); cluster: what the plans are built against, as `run <path>` builds them."""
    referenced = None if cluster is None else cluster.referenced()
    unexempted = audit(store, referenced, kinds)
    found = []
    for path in sorted(store):
        found += _of_leaf(store, path, unexempted, referenced, kinds, cluster)
    never = datetime.date.max
    return sorted(
        found,
        key=lambda r: (
            r.stand is Stand.FRESH,
            r.due_at is None,
            r.due_at or never,
            r.plan.target.leaf,
            r.plan.target.keys,
        ),
    )


def _of_leaf(
    store: Mapping[str, Leaf],
    path: str,
    result: Audit,
    referenced: set[str] | None,
    kinds: Mapping[str, Kind],
    cluster: Cluster | None,
) -> list[Rotation]:
    """The leaf's plans with an operator step. Only its plan in flight is built under the orphan
    exemption (design §3.3); result is the audit without it."""
    leaf = store[path]
    flight = leaf.flight
    plans, _ = of_leaf(path, store, result, kinds, cluster)
    stood = [
        (p.plan, 0, Stand.FRESH)
        for p in plans
        if p.plan is not None and (flight is None or (p.kind, p.keys) != (flight.kind, flight.keys))
    ]
    if flight is not None:
        exempted = audit(store, exempt(store, path, referenced), kinds)
        if taken_up := _in_flight(leaf, exempted, kinds, cluster):
            stood.append(taken_up)
    return [
        Rotation(
            plan,
            stand,
            at,
            due_at(plan.target, leaf.state.stamps),
            flight,
            credential(kinds[plan.target.kind], plan.target),
            type_of(plan.target),
        )
        for plan, at, stand in stood
        if plan.needs_operator
    ]


def _in_flight(
    leaf: Leaf, result: Audit, kinds: Mapping[str, Kind], cluster: Cluster | None
) -> tuple[Plan, int, Stand] | None:
    """The leaf's plan in flight, built again as `run <path>` builds it, with the step it is at and
    its Stand; None when it cannot be built again or lacks the step it is at, which `run <path>`
    says and the standing card names."""
    try:
        plan = in_flight(kinds, leaf.path, leaf.flight, result, cluster)
        return plan, *stand_of(plan, leaf.flight, leaf.state.status)
    except (PlanError, PlanMismatch):
        return None
