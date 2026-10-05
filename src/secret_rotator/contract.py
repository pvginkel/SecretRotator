"""The annotation contract (AnsibleSpecs secret-rotation/design.md §5): kinds, owned keys,
activators, intervals and dates."""

import datetime
import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

MOUNT = "kv"

# The rotator's working leaves (design §3.3, §4.3, §4.5): run state, never annotated or checked.
STAGING_PREFIX = "rotator/staging/"
LOCK_LEAF = "rotator/lock"

# KV v2 custom_metadata limits (Vault's; not verified for OpenBao 2.5.4).
MAX_KEYS = 64
MAX_KEY_BYTES = 128
MAX_VALUE_BYTES = 512

NONE = "none"
COPY = re.compile(r"copy:([^#\s]+)#(.+)")

# What a kind owns: ALL, every key of the leaf; ONE, the leaf's single key no key_<name> names
# (design §3.1, slice 045's A7); or a set of key names.
ALL, ONE = "all", "one"


@dataclass(frozen=True)
class KindSpec:
    owns: str | frozenset[str]
    implicit_none: frozenset[str] = frozenset()  # keys that resolve to none without an override
    implemented: bool = False


# Every kind of design §6, both tables. A kind not implemented yet is known: its keys are no
# finding, and they are skipped.
KINDS: dict[str, KindSpec] = {
    "random": KindSpec(ALL, implemented=True),
    "approle": KindSpec(ONE, implemented=True),
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
    "manual": KindSpec(ALL, implemented=True),
    "k8s-sa-token": KindSpec(ONE),
    "cephx": KindSpec(ONE),
    "rgw-admin": KindSpec(ONE),
    "grafana-admin": KindSpec(ONE),
    "jenkins-admin-password": KindSpec(ONE),
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
    return path == LOCK_LEAF or path.startswith(STAGING_PREFIX)


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


def is_implemented(kind: str) -> bool:
    return kind == NONE or copy_target(kind) is not None or KINDS[kind].implemented


def resolve(meta: Mapping[str, str], keys: Iterable[str]) -> dict[str, str | None]:
    """Each data key's kind (design §3.1); None for a key no kind resolves."""
    kind = meta.get("rotation_mechanism")
    overrides = {k[len("key_") :]: v for k, v in meta.items() if k.startswith("key_")}
    kinds: dict[str, str | None] = {}
    leftover = []
    for key in sorted(keys):
        if key in overrides:
            kinds[key] = overrides[key] if kind_error(overrides[key]) is None else None
        elif kind is None or kind_error(kind):
            kinds[key] = None
        elif kind == NONE or copy_target(kind) or KINDS[kind].owns == ALL:
            kinds[key] = kind
        elif KINDS[kind].owns == ONE:
            leftover.append(key)
        elif key in KINDS[kind].owns:
            kinds[key] = kind
        elif key in KINDS[kind].implicit_none:
            kinds[key] = NONE
        else:
            kinds[key] = None
    # A one-key kind owns the single unnamed key only while no override names the kind (A7).
    claimed = kind in overrides.values()
    for key in leftover:
        kinds[key] = kind if len(leftover) == 1 and not claimed else None
    return kinds


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


def parse_args(value: str) -> dict:
    try:
        args = json.loads(value)
    except ValueError:
        raise ContractError("not JSON") from None
    if not isinstance(args, dict):
        raise ContractError("not a JSON object")
    if len(value.encode()) > MAX_VALUE_BYTES:
        raise ContractError(f"larger than {MAX_VALUE_BYTES} bytes")
    return args


@dataclass(frozen=True)
class Activator:
    """One spec of rotation_activate (design §4.3). A k8s-rollout without targets derives them."""

    name: str
    arg: str | None = None
    targets: tuple[str, ...] = ()


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
    """The specs of a rotation_activate value; ContractError lists every problem.

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
