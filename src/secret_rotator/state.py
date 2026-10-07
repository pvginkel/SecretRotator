"""The rotator's run state (design §3.4): one data leaf, kv/rotator/state, never a secret leaf's
metadata. Each of its data keys is a secret leaf's path, its value that leaf's state as a JSON
object. Every write is a KV v2 check-and-set, started again from a fresh read when another write
came first, so the nightly run and an operator's `run <path>` never lose each other's writes. A
write drops the state of every leaf the store no longer holds; the state leaf itself is never
deleted, since the rotator's policy grants no delete there."""

import dataclasses
import json
from collections.abc import Callable, Collection
from dataclasses import dataclass, field

from secret_rotator.contract import STATE_LEAF
from secret_rotator.openbao import OpenBao, OpenBaoError


@dataclass
class LeafState:
    """One secret leaf's run state. Status is one per leaf, as its last plan left it."""

    stamps: dict[str, str] = field(default_factory=dict)  # data key -> the ISO date it rotated
    status: str | None = None  # ok, failed, failed-activation or manual-due
    last_run: str | None = None  # an ISO timestamp, UTC
    last_error: str | None = None
    # What the last plan's activation read from the cluster: the ExternalSecrets it synced and the
    # workloads it derived.
    consumers: tuple[str, ...] = ()
    # The nightly run's backoff: how many nights in a row a plan of the leaf failed, and the
    # standing card the leaf waits on once that reached three, not retried until it is closed.
    failed_nights: int = 0
    held_by: str | None = None

    def dump(self) -> str:
        """The JSON object stored for the leaf: its fields that are set."""
        doc = {name: value for name, value in dataclasses.asdict(self).items() if value}
        return json.dumps(doc, sort_keys=True, separators=(",", ":"))

    @classmethod
    def load(cls, text: str) -> "LeafState":
        doc = json.loads(text)
        return cls(**doc | {"consumers": tuple(doc.get("consumers", ()))})


def read(bao: OpenBao) -> tuple[int, dict[str, LeafState]]:
    """The state leaf's current version number (0: no state leaf yet) and every leaf's state."""
    version = bao.read(STATE_LEAF)
    if version is None:
        return 0, {}
    return version.number, {path: LeafState.load(text) for path, text in version.data.items()}


class State:
    """The run state as a process reads and writes it. leaves: the secret leaves the store holds,
    as the process read them; a write keeps the state of these and of the leaf it writes."""

    def __init__(self, bao: OpenBao, leaves: Collection[str]):
        self.bao = bao
        self.leaves = leaves

    def of(self, leaf: str) -> LeafState:
        return read(self.bao)[1].get(leaf, LeafState())

    def update(self, leaf: str, change: Callable[[LeafState], None]) -> LeafState:
        """Applies change to the leaf's state as just read and writes it by check-and-set, from
        a fresh read whenever another write came first; returns the leaf's state as written."""
        refused: OpenBaoError | None = None
        tried: int | None = None
        while True:
            number, states = read(self.bao)
            if number == tried:
                # Refused, and nothing was written since: no other write came first.
                raise refused
            mine = states.setdefault(leaf, LeafState())
            change(mine)
            data = {
                path: state.dump()
                for path, state in sorted(states.items())
                if (path == leaf or path in self.leaves) and state != LeafState()
            }
            try:
                self.bao.write(STATE_LEAF, data, cas=number)
            except OpenBaoError as e:
                # A 400 is the check-and-set refused: another write came first.
                if e.status != 400:
                    raise
                tried, refused = number, e
                continue
            return mine
