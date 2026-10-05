"""The standing card (design §3.3, R16): one open ANS card with the switches' tag carries what is
open — findings, failed rotations and activations, plans stopped or skipped, manual rotations due.
A run rewrites its description to the current list and comments with what changed; a run that
changes nothing touches nothing; with nothing open and no card open, none is made. The operator
closes the card once it is actioned, and the next finding opens a new one.

The description's `- ` lines are the list a run compares its own with: an item is one line."""

import datetime
from dataclasses import dataclass

from secret_rotator.youtrack import Card, YouTrack

SUMMARY = "Secret rotation: findings and failed rotations"
DRY_RUN = " (dry run)"
ITEM = "- "
EXPLAIN = (
    "Every nightly run of the secret rotator rewrites this list and comments with what changed. "
    "Close the card once it is actioned; the next finding opens a new one."
)


@dataclass(frozen=True)
class Section:
    title: str
    items: tuple[str, ...]


def one_line(text: str) -> str:
    return " ".join(text.split())


def render(sections: list[Section], today: datetime.date, dry_run: bool) -> str:
    lines = [f"Open as of {today}{DRY_RUN if dry_run else ''}.", "", EXPLAIN]
    for section in sections:
        if section.items:
            lines += ["", f"### {section.title}", *(ITEM + item for item in section.items)]
    if not any(section.items for section in sections):
        lines += ["", "Nothing is open."]
    return "\n".join(lines)


def items_of(description: str) -> list[str]:
    return [line[len(ITEM) :] for line in description.splitlines() if line.startswith(ITEM)]


def is_dry_run(description: str) -> bool:
    return description.split("\n", 1)[0].endswith(f"{DRY_RUN}.")


def comment(new: list[str], gone: list[str], today: datetime.date, dry_run: bool) -> str:
    lines = [f"{today}{DRY_RUN if dry_run else ''}: {len(new)} new, {len(gone)} resolved."]
    if new:
        lines += ["", "New:", *(ITEM + item for item in new)]
    if gone:
        lines += ["", "Resolved:", *(ITEM + item for item in gone)]
    return "\n".join(lines)


def post(
    youtrack: YouTrack,
    tag: str,
    card: Card | None,
    sections: list[Section],
    *,
    today: datetime.date,
    dry_run: bool,
) -> tuple[Card | None, str]:
    """Puts the list on the open card, or a new one; the card that carries it (None when none
    does) and what was done, for the log."""
    sections = [Section(s.title, tuple(dict.fromkeys(map(one_line, s.items)))) for s in sections]
    items = [item for section in sections for item in section.items]
    description = render(sections, today, dry_run)
    if card is None:
        if not items:
            return None, "no card: nothing is open"
        created = youtrack.create_card(tag, SUMMARY, description)
        return created, f"{created.readable}: created with {len(items)} item(s)"
    before = items_of(card.description)
    if set(before) == set(items) and is_dry_run(card.description) == dry_run:
        return card, f"{card.readable}: nothing changed"
    new = [item for item in items if item not in before]
    gone = [item for item in before if item not in items]
    youtrack.describe(card, description)
    youtrack.comment(card, comment(new, gone, today, dry_run))
    return card, f"{card.readable}: {len(new)} new, {len(gone)} resolved"
