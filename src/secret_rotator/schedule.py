"""When each scheduled key falls due (design §3.2): per key, from its own stamp in the run state
(§3.4) and its interval, capped by the leaf's expiries."""

import datetime
from collections.abc import Mapping
from dataclasses import dataclass

from secret_rotator.contract import EXPIRES_AT, is_scheduled, parse_date, parse_interval

DEFAULT_INTERVAL = "14d"
EXPIRY_LEAD = datetime.timedelta(days=7)
EXPIRIES = ("rotation_expires_at", EXPIRES_AT)


def interval_of(meta: Mapping[str, str], key: str) -> int | None:
    """The key's interval in days, None for never: interval_<key>, else the leaf's."""
    return parse_interval(
        meta.get(f"interval_{key}", meta.get("rotation_interval", DEFAULT_INTERVAL))
    )


@dataclass(frozen=True)
class KeySchedule:
    leaf: str
    key: str
    kind: str
    interval: int | None  # days; None: never
    rotated_at: datetime.date | None
    cap: datetime.date | None  # the earliest expiry less the lead
    # The first day the key is due: date.min for a key never stamped, None for a never key
    # without an expiry.
    due_at: datetime.date | None

    def due(self, today: datetime.date) -> bool:
        return self.due_at is not None and self.due_at <= today


def schedule(
    path: str,
    meta: Mapping[str, str],
    kinds: Mapping[str, str | None],
    stamps: Mapping[str, str],
) -> list[KeySchedule]:
    """Every scheduled key of a leaf. Its annotations must hold the contract (the audit's);
    stamps: the leaf's rotation stamps, data key -> ISO date."""
    expiries = [parse_date(meta[k]) for k in EXPIRIES if k in meta]
    cap = min(expiries) - EXPIRY_LEAD if expiries else None
    out = []
    for key, kind in sorted(kinds.items()):
        if not is_scheduled(kind):
            continue
        interval = interval_of(meta, key)
        stamp = stamps.get(key)
        rotated_at = None if stamp is None else parse_date(stamp)
        if interval is None:
            scheduled = None
        elif rotated_at is None:
            scheduled = datetime.date.min
        else:
            scheduled = rotated_at + datetime.timedelta(days=interval)
        candidates = [d for d in (scheduled, cap) if d is not None]
        due_at = min(candidates) if candidates else None
        out.append(KeySchedule(path, key, kind, interval, rotated_at, cap, due_at))
    return out
