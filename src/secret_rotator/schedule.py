"""When each scheduled key falls due (design §3.2): per key, from its own stamp in the run state
(§3.4) and its entry's interval, brought forward by its entry's expires_at."""

import datetime
from collections.abc import Mapping
from dataclasses import dataclass

from secret_rotator.contract import Entry, is_scheduled, parse_date

EXPIRY_LEAD = datetime.timedelta(days=7)


@dataclass(frozen=True)
class KeySchedule:
    leaf: str
    key: str
    kind: str
    interval: int | None  # days; None: never
    rotated_at: datetime.date | None
    cap: datetime.date | None  # its expiry less the lead
    # The first day the key is due: date.min for a key never stamped, None for a never key
    # without an expiry.
    due_at: datetime.date | None

    def due(self, today: datetime.date) -> bool:
        return self.due_at is not None and self.due_at <= today


def schedule(
    path: str, entries: Mapping[str, Entry], stamps: Mapping[str, str]
) -> list[KeySchedule]:
    """Every scheduled key among a leaf's entries, which hold the contract (the audit's); stamps:
    the leaf's rotation stamps, data key -> ISO date."""
    out = []
    for key, entry in sorted(entries.items()):
        if not is_scheduled(entry.kind):
            continue
        cap = None if entry.expires_at is None else entry.expires_at - EXPIRY_LEAD
        stamp = stamps.get(key)
        rotated_at = None if stamp is None else parse_date(stamp)
        if entry.interval is None:
            scheduled = None
        elif rotated_at is None:
            scheduled = datetime.date.min
        else:
            scheduled = rotated_at + datetime.timedelta(days=entry.interval)
        candidates = [d for d in (scheduled, cap) if d is not None]
        due_at = min(candidates) if candidates else None
        out.append(KeySchedule(path, key, entry.kind, entry.interval, rotated_at, cap, due_at))
    return out
