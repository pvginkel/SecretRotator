"""The committed switches (design §8): switches.yaml ships inside the package, so a run reads the
copy its install brought."""

from dataclasses import dataclass
from importlib import resources

import yaml

from secret_rotator.contract import KINDS

SWITCHES = resources.files("secret_rotator") / "switches.yaml"


class SwitchesError(Exception):
    pass


@dataclass(frozen=True)
class Switches:
    dry_run: bool
    paused: bool  # the kill switch: a run stops before it does anything
    kinds_enabled: frozenset[str]
    max_rotations_per_run: int
    card_tag: str
    telegram_chat_id: int | None


def parse(text: str) -> Switches:
    """SwitchesError lists every problem."""
    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise SwitchesError(f"not valid YAML: {e}") from None
    if not isinstance(doc, dict):
        raise SwitchesError("not a mapping of switches")
    fields = Switches.__dataclass_fields__
    problems = [f"{k}: not a switch" for k in doc if k not in fields]
    problems += [f"{k}: missing" for k in fields if k not in doc]

    def check(name: str, ok: bool, what: str) -> None:
        if name in doc and not ok:
            problems.append(f"{name}: {what}")

    def is_int(value) -> bool:
        return isinstance(value, int) and not isinstance(value, bool)

    check("dry_run", isinstance(doc.get("dry_run"), bool), "not true or false")
    check("paused", isinstance(doc.get("paused"), bool), "not true or false")
    kinds = doc.get("kinds_enabled")
    if not isinstance(kinds, list) or not all(isinstance(k, str) for k in kinds):
        check("kinds_enabled", False, "not a list of kind names")
    else:
        for kind in kinds:
            if kind not in KINDS or not KINDS[kind].implemented:
                problems.append(f"kinds_enabled: {kind!r} is not an implemented kind")
    count = doc.get("max_rotations_per_run")
    check("max_rotations_per_run", is_int(count) and count >= 1, "not a whole number from 1")
    tag = doc.get("card_tag")
    check("card_tag", isinstance(tag, str) and tag.strip() != "", "not a tag name")
    chat = doc.get("telegram_chat_id")
    check("telegram_chat_id", chat is None or is_int(chat), "not a chat id or null")
    if problems:
        raise SwitchesError("; ".join(problems))
    return Switches(
        dry_run=doc["dry_run"],
        paused=doc["paused"],
        kinds_enabled=frozenset(doc["kinds_enabled"]),
        max_rotations_per_run=doc["max_rotations_per_run"],
        card_tag=doc["card_tag"],
        telegram_chat_id=doc["telegram_chat_id"],
    )


def load() -> Switches:
    return parse(SWITCHES.read_text())
