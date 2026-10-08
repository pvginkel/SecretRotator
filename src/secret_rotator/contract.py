"""The annotation contract (AnsibleSpecs secret-rotation/design.md §5): one rotation_<key> entry
per data key, a JSON object of the key's fields; the kinds, activators, intervals and dates they
hold."""

import datetime
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass

MOUNT = "kv"

# The rotator's working leaves (design §3.3, §3.4, §4.3, §4.5): the staging leaves, the lock and
# the run state, never annotated or checked.
STAGING_PREFIX = "rotator/staging/"
LOCK_LEAF = "rotator/lock"
STATE_LEAF = "rotator/state"

# What annotate creates a marker leaf's one data key holding (catalog § rotator/): the credential
# itself is never in KV.
MARKER_VALUE = "marker leaf: the credential is not kept in KV"

# KV v2 custom_metadata limits (Vault's; not verified for OpenBao 2.5.4).
MAX_KEYS = 64
MAX_KEY_BYTES = 128
MAX_VALUE_BYTES = 512

# A data key's entry is the metadata key rotation_<key>, <key> verbatim.
ENTRY_PREFIX = "rotation_"
FIELDS = ("kind", "interval", "args", "activate", "expires_at", "notes")
DEFAULT_INTERVAL = "14d"


NONE = "none"
COPY = re.compile(r"copy:([^#\s]+)#(.+)")

# Which keys of a leaf a kind may rotate (design §3.1): ALL, any; ONE, the one a seed's leaf
# default of the kind falls to; or a set of key names.
ALL, ONE = "all", "one"


@dataclass(frozen=True)
class KindSpec:
    owns: str | frozenset[str]
    # Keys a seed's leaf default of the kind makes none without a kind of their own (annotate).
    implicit_none: frozenset[str] = frozenset()


# Every kind of design §6, both tables. Which of them are implemented is what the installed
# plugins say (registry); a kind without one is known: its keys are no finding, and are skipped.
KINDS: dict[str, KindSpec] = {
    "random": KindSpec(ALL),
    "approle": KindSpec(ONE),
    "keycloak-client": KindSpec(frozenset({"client_secret"}), frozenset({"client_id"})),
    "cnpg-role": KindSpec(frozenset({"password"})),
    "jenkins-token": KindSpec(ONE),
    "youtrack-token": KindSpec(ONE),
    "github-webhook-secret": KindSpec(ONE),
    "elastic-user": KindSpec(frozenset({"password"})),
    "home-assistant-token": KindSpec(ONE),
    "google-sa-key": KindSpec(ONE),
    "terraform": KindSpec(ONE),
    "mosquitto-user": KindSpec(ONE),
    "samba-user": KindSpec(ONE),
    "manual": KindSpec(ALL),
    "k8s-sa-token": KindSpec(ONE),
    "cephx": KindSpec(ONE),
    "rgw-admin": KindSpec(ONE),
    "grafana-admin": KindSpec(ONE),
    "pve-root-password": KindSpec(ONE),
    "kubecoder-client": KindSpec(ONE),
    "jenkins-job-token": KindSpec(ONE),
    "step-ca-password": KindSpec(ONE),
    "ssh-key": KindSpec(ONE),
}


class ContractError(ValueError):
    def __init__(self, *problems: str):
        super().__init__("; ".join(problems))
        self.problems = list(problems)


def is_working_leaf(path: str) -> bool:
    return path in (LOCK_LEAF, STATE_LEAF) or path.startswith(STAGING_PREFIX)


def kind_error(value: str) -> str | None:
    if value in KINDS or value == NONE or COPY.fullmatch(value):
        return None
    return f"unknown kind {value!r}: not a kind of design §6, none, or copy:<path>#<key>"


def copy_target(kind: str | None) -> tuple[str, str] | None:
    """The primary leaf and key a copy kind points at; None for any other kind."""
    match = COPY.fullmatch(kind or "")
    return None if match is None else (match[1], match[2])


def is_scheduled(kind: str | None) -> bool:
    """A key of this kind is rotated on its own schedule: neither none nor a copy (design §3.2)."""
    return kind is not None and kind != NONE and copy_target(kind) is None


def may_rotate(kind: str, key: str) -> bool:
    """Whether a key of that name may be of the kind (design §3.1): a kind that names the keys it
    rotates those; any other kind, none and a copy any key."""
    owns = KINDS[kind].owns if kind in KINDS else ALL
    return owns in (ALL, ONE) or key in owns


def entry_name(key: str) -> str:
    """The metadata key of a data key's entry."""
    return f"{ENTRY_PREFIX}{key}"


def entries_of(meta: Mapping[str, str]) -> dict[str, str]:
    """A leaf's entries by data key, each its JSON text as the metadata holds it."""
    return {k[len(ENTRY_PREFIX) :]: v for k, v in meta.items() if k.startswith(ENTRY_PREFIX)}


def load_entry(text: str) -> dict:
    """An entry's fields; ContractError when its text is no JSON object."""
    try:
        fields = json.loads(text)
    except ValueError:
        raise ContractError("not JSON") from None
    if not isinstance(fields, dict):
        raise ContractError("not a JSON object")
    return fields


def dump_entry(fields: Mapping) -> str:
    """An entry's JSON text, compact, its fields in the contract's order."""
    rank = {name: n for n, name in enumerate(FIELDS)}
    ordered = sorted(fields.items(), key=lambda item: (rank.get(item[0], len(FIELDS)), item[0]))
    return json.dumps(dict(ordered), separators=(",", ":"), ensure_ascii=False)


def kind_in(text: str | None) -> str | None:
    """The kind an entry's text names, when it is JSON naming a kind of the contract."""
    try:
        kind = load_entry(text).get("kind") if text is not None else None
    except ContractError:
        return None
    return kind if isinstance(kind, str) and kind_error(kind) is None else None


def takes(kind: str) -> tuple[str, ...]:
    """The fields an entry of the kind takes (design §5): a none key its kind and notes, a copy
    its activate too, a scheduled key every field."""
    if kind == NONE:
        return ("kind", "notes")
    if copy_target(kind):
        return ("kind", "activate", "notes")
    return FIELDS


INTERVAL = re.compile(r"([1-9][0-9]*)d|never")
ISO_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")


def parse_interval(value: str) -> int | None:
    """Days; None for never."""
    match = INTERVAL.fullmatch(value)
    if match is None:
        raise ContractError(f"{value!r} is not <n>d or never")
    return None if value == "never" else int(match[1])


def parse_date(value: str) -> datetime.date:
    try:
        if ISO_DATE.fullmatch(value):
            return datetime.date.fromisoformat(value)
    except ValueError:
        pass
    raise ContractError(f"{value!r} is not an ISO date (YYYY-MM-DD)")


@dataclass(frozen=True)
class Activator:
    """One spec of an entry's activate (design §4.3). A k8s-rollout without targets derives them."""

    name: str
    arg: str | None = None
    targets: tuple[str, ...] = ()

    def __str__(self) -> str:
        """The spec as an activate writes it."""
        rest = self.arg if self.arg is not None else ",".join(self.targets)
        return f"{self.name}:{rest}" if rest else self.name


WORKLOAD = re.compile(r"[a-z0-9-]+/(deployment|statefulset|daemonset)/[a-z0-9.-]+")
# The argument each activator takes; None: it takes none.
ACTIVATORS: dict[str, re.Pattern | None] = {
    "eso": None,
    "k8s-rollout": WORKLOAD,  # optional
    "jenkins-credential": re.compile(r"\S+"),
    "jenkins-job": re.compile(r"[^?\s]+(\?[^=&\s]+=[^&\s]*(&[^=&\s]+=[^&\s]*)*)?"),
    "github-webhook": re.compile(r"\S+/[0-9]+"),
    "argocd-sync": re.compile(r"\S+"),
    "manual": re.compile(r".*\S.*"),
}


def parse_activate(value: str) -> list[Activator]:
    """The specs of an activate; ContractError lists every problem.

    auto is eso,k8s-rollout with derived targets; none is no spec. A k8s-rollout:<target> may be
    followed by further <ns>/<kind>/<name> targets."""
    if value == "auto":
        return [Activator("eso"), Activator("k8s-rollout")]
    if value == NONE:
        return []
    specs: list[Activator] = []
    errors = []
    for item in value.split(","):
        last = specs[-1] if specs else None
        if last and last.name == "k8s-rollout" and last.targets and WORKLOAD.fullmatch(item):
            specs[-1] = Activator("k8s-rollout", targets=(*last.targets, item))
            continue
        name, sep, arg = item.partition(":")
        pattern = ACTIVATORS.get(name)
        if item in ("auto", NONE):
            errors.append(f"{item} stands alone, not in a list")
        elif name not in ACTIVATORS:
            errors.append(f"unknown activator {item!r}")
        elif pattern is None:
            if sep:
                errors.append(f"{name} takes no argument")
            else:
                specs.append(Activator(name))
        elif name == "k8s-rollout" and not sep:
            specs.append(Activator(name))
        elif not pattern.fullmatch(arg):
            errors.append(f"{item!r} is not {name}'s form")
        elif name == "k8s-rollout":
            specs.append(Activator(name, targets=(arg,)))
        else:
            specs.append(Activator(name, arg))
    if errors:
        raise ContractError(*errors)
    return specs


def _interval_problems(value: object) -> list[str]:
    if not isinstance(value, str):
        return ["not a string"]
    try:
        parse_interval(value)
    except ContractError as e:
        return e.problems
    return []


def _activate_problems(value: object) -> list[str]:
    if not isinstance(value, str):
        return ["not a string"]
    try:
        parse_activate(value)
    except ContractError as e:
        return e.problems
    return []


def _date_problems(value: object) -> list[str]:
    if not isinstance(value, str):
        return ["not a string"]
    try:
        parse_date(value)
    except ContractError as e:
        return e.problems
    return []


# Each field's check but kind's: its problems, none when it holds.
CHECKS = {
    "interval": _interval_problems,
    "args": lambda value: [] if isinstance(value, dict) else ["not a JSON object"],
    "activate": _activate_problems,
    "expires_at": _date_problems,
    "notes": lambda value: [] if isinstance(value, str) else ["not a string"],
}


def entry_problems(fields: Mapping) -> list[str]:
    """What is wrong with an entry on its own, each problem led by its field (design §3.3, §5):
    a field of the contract its kind takes, kind and a scheduled key's or a copy's activate given,
    each well formed, and never with notes. Whether its kind may rotate the key, and a copy's
    primary, are the leaf's and the store's."""
    problems = [f"{name}: not a field of the entry" for name in sorted(set(fields) - set(FIELDS))]
    kind = fields.get("kind")
    if not isinstance(kind, str):
        return [*problems, "kind: missing" if kind is None else "kind: not a string"]
    if error := kind_error(kind):
        return [*problems, f"kind: {error}"]
    taken = takes(kind)
    what = "a none key" if kind == NONE else "a copy"
    for name in FIELDS:
        if name not in fields:
            if name == "activate" and name in taken:
                problems.append("activate: missing")
        elif name not in taken:
            problems.append(f"{name}: {what} takes none")
        elif name in CHECKS:
            problems += [f"{name}: {problem}" for problem in CHECKS[name](fields[name])]
    notes = fields.get("notes")
    if fields.get("interval") == "never" and not (isinstance(notes, str) and notes.strip()):
        problems.append("interval: never without notes")
    return problems


@dataclass(frozen=True)
class Entry:
    """A key's entry, read once entry_problems finds nothing wrong with it."""

    kind: str
    interval: int | None  # days; None: never
    args: Mapping
    activate: tuple[Activator, ...]
    expires_at: datetime.date | None
    notes: str

    @classmethod
    def load(cls, fields: Mapping) -> "Entry":
        return cls(
            fields["kind"],
            parse_interval(fields.get("interval", DEFAULT_INTERVAL)),
            dict(fields.get("args", {})),
            tuple(parse_activate(fields["activate"])) if "activate" in fields else (),
            parse_date(fields["expires_at"]) if "expires_at" in fields else None,
            fields.get("notes", ""),
        )
