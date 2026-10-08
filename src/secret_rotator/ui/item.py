"""One box of the list: a listed rotation and the state its box shows (design §7.3)."""

import datetime
from dataclasses import dataclass
from enum import Enum

from secret_rotator.executor import Stand
from secret_rotator.listing import Rotation
from secret_rotator.ui.collate import Screen, collate


class Phase(Enum):
    DUE = "due"  # not in flight, due or not
    IN_FLIGHT = "in-flight"
    FAILED = "failed"
    ROLLBACK_FAILED = "rollback-failed"


PHASE_OF = {
    Stand.FRESH: Phase.DUE,
    Stand.IN_FLIGHT: Phase.IN_FLIGHT,
    Stand.FAILED: Phase.FAILED,
    Stand.ROLLING_BACK: Phase.ROLLBACK_FAILED,
}


@dataclass(eq=False)
class Item:
    rotation: Rotation
    phase: Phase
    screens: list[Screen]

    @classmethod
    def of(cls, rotation: Rotation) -> "Item":
        return cls(rotation, PHASE_OF[rotation.stand], collate(rotation.plan.steps))

    @property
    def id(self) -> str:
        """Its leaf with its keys, `leaf#key`: unique, as a key belongs to one plan of its leaf."""
        target = self.rotation.plan.target
        return f"{target.leaf}#{','.join(target.keys)}"

    def waits_on_you(self, today: datetime.date) -> bool:
        """What the status bar counts (R90): in flight, failed, or due."""
        due_at = self.rotation.due_at
        return self.phase is not Phase.DUE or (due_at is not None and due_at <= today)
