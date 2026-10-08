"""Design §3.4's metrics: the run state and the nightly run's health as Prometheus series, PUT to
the Pushgateway through the Kubernetes API's service proxy with the secret-rotator
ServiceAccount's token, so the push needs no route of its own. A PUT replaces its whole group
(`instance`): `state`, the per-key and per-leaf series over the whole store, from every process
that may change it; `audit`, the nightly run's findings; `nightly`, the run health, from the
nightly run alone. A group not pushed is one line of the process's output and changes nothing
else."""

import datetime
from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING

from secret_rotator.audit import Audit, Leaf, audit, live_store
from secret_rotator.kube import Kube, KubeError
from secret_rotator.openbao import OpenBao, OpenBaoError
from secret_rotator.schedule import schedule
from secret_rotator.switches import Switches

if TYPE_CHECKING:
    from secret_rotator.plan import Kind

GROUPS = (
    "/api/v1/namespaces/prometheus-prd/services/prometheus-prd-prometheus-pushgateway:9091/proxy"
    "/metrics/job/secret-rotator/instance/"
)
EXPOSITION = "text/plain; version=0.0.4"
STATE, AUDIT, NIGHTLY = "state", "audit", "nightly"
STATUSES = ("ok", "failed", "failed-activation", "manual-due", "skipped")
EPOCH = datetime.date(1970, 1, 1)

HELP = {
    "secret_rotation_key_info": "A scheduled key whose entry the audit accepts: its interval in "
    "days or never, and whether it was ever stamped.",
    "secret_rotation_last_rotated_timestamp": "The key's rotation stamp, Unix seconds at 00:00 "
    "UTC; absent for a key never stamped.",
    "secret_rotation_due_timestamp": "The day the key falls due, Unix seconds at 00:00 UTC; absent "
    "for a key never stamped that has an interval and for a never key without an expiry.",
    "secret_rotation_status": "1 for the leaf's status in the run state, 0 for the others.",
    "secret_rotation_finding": "A compliance finding of the nightly run's audit.",
    "secret_rotator_run_timestamp": "When the nightly run ended, Unix seconds.",
    "secret_rotator_run_success": "0 when the nightly run itself broke and exited non-zero.",
    "secret_rotator_run_duration_seconds": "How long the nightly run took.",
    "secret_rotator_run_rotations": "The plans the nightly run rotated; in a dry run, the plans "
    "it would have run.",
    "secret_rotator_run_deferred": "The due plans past max_rotations_per_run, left to the next "
    "nights.",
    "secret_rotator_dry_run": "1 while the switches put the rotator in dry run.",
    "secret_rotator_paused": "1 while the switches pause the nightly run.",
}


class Series:
    """One group's series in the text exposition format: each metric's samples together after its
    HELP and TYPE lines. A sample added again with the same labels replaces the first, since the
    Pushgateway refuses a group holding one twice."""

    def __init__(self) -> None:
        self.samples: dict[str, dict[tuple[tuple[str, str], ...], int | float]] = {}

    def add(self, name: str, value: int | float, **labels: str) -> None:
        self.samples.setdefault(name, {})[tuple(labels.items())] = value

    def text(self) -> str:
        lines = []
        for name, samples in self.samples.items():
            lines += [f"# HELP {name} {HELP[name]}", f"# TYPE {name} gauge"]
            for labels, value in samples.items():
                pairs = ",".join(f'{label}="{escaped(text)}"' for label, text in labels)
                lines.append(f"{name}{{{pairs}}} {value}" if labels else f"{name} {value}")
        return "".join(f"{line}\n" for line in lines)


def escaped(text: str) -> str:
    return text.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def day(date: datetime.date) -> int:
    """Unix seconds at 00:00 UTC of the date."""
    return (date - EPOCH).days * 86400


def of_store(store: Mapping[str, Leaf], result: Audit) -> Series:
    """The `state` group: the per-key series of every scheduled key whose entry the audit accepts,
    from the per-key schedule, and each leaf's status as the state holds it."""
    series = Series()
    for path, entries in sorted(result.entries.items()):
        for s in schedule(path, entries, store[path].state.stamps):
            key = {"leaf": path, "key": s.key, "kind": s.kind}
            series.add(
                "secret_rotation_key_info",
                1,
                **key,
                interval="never" if s.interval is None else str(s.interval),
                stamped="false" if s.rotated_at is None else "true",
            )
            if s.rotated_at is not None:
                series.add("secret_rotation_last_rotated_timestamp", day(s.rotated_at), **key)
            # date.min: a key never stamped that has an interval, due at once and on no day.
            if s.due_at not in (None, datetime.date.min):
                series.add("secret_rotation_due_timestamp", day(s.due_at), **key)
    for path, leaf in sorted(store.items()):
        if (held := leaf.state.status) is not None:
            for status in STATUSES:
                series.add("secret_rotation_status", int(status == held), leaf=path, status=status)
    return series


def state(bao: OpenBao, kinds: "Mapping[str, Kind]") -> Series:
    """The `state` group over the whole store as it reads now. The audit's orphan check is left
    out: an orphan blocks its leaf and removes no entry, so the series are the same without it."""
    store = live_store(bao, runs=True)
    return of_store(store, audit(store, plugins=kinds))


def findings(result: Audit) -> Series:
    """The `audit` group: one series per finding, with the key it names and its text."""
    series = Series()
    for f in result.findings:
        series.add("secret_rotation_finding", 1, leaf=f.leaf, key=f.key, message=f.message)
    return series


def run_health(
    *,
    ended: datetime.datetime,
    duration: float,
    success: bool,
    rotations: int,
    deferred: int,
    switches: Switches,
) -> Series:
    """The `nightly` group."""
    series = Series()
    series.add("secret_rotator_run_timestamp", round(ended.timestamp()))
    series.add("secret_rotator_run_success", int(success))
    series.add("secret_rotator_run_duration_seconds", round(duration, 3))
    series.add("secret_rotator_run_rotations", rotations)
    series.add("secret_rotator_run_deferred", deferred)
    series.add("secret_rotator_dry_run", int(switches.dry_run))
    series.add("secret_rotator_paused", int(switches.paused))
    return series


def push(
    kube: Kube, groups: Mapping[str, Callable[[], Series]], out: Callable[[str], None]
) -> list[str]:
    """PUTs each group in order, its series made as it is pushed; a group not pushed is one line
    of out, and the next is still pushed. The groups pushed."""
    pushed = []
    for group, series in groups.items():
        try:
            kube.put_text(GROUPS + group, series().text(), EXPOSITION)
        except (KubeError, OpenBaoError) as e:
            out(f"metrics: the {group} group is not pushed: {e}")
            continue
        pushed.append(group)
    return pushed


def push_state(
    bao: OpenBao, kinds: "Mapping[str, Kind]", kube: Kube, out: Callable[[str], None]
) -> None:
    """The `state` group, at the end of an operator's process that may have changed the state."""
    push(kube, {STATE: lambda: state(bao, kinds)}, out)
