"""A plan's staging leaf (design §3.4, §4.5): the record of a plan in flight, which exists exactly
while the plan is in flight. It holds the plan's keys, the step it is at and what it derived from
the cluster, what its steps produce and what their undos need, each written before anything uses it,
so an exit or a crash loses nothing; destroyed with every version once the plan is stamped or rolled
back. Its path, rotator/staging/<kind>/<leaf>, names the plan's kind and leaf. The rotator's policy
grants delete here only."""

import json
from dataclasses import dataclass, field

from secret_rotator.cluster import Derived
from secret_rotator.contract import STAGING_PREFIX
from secret_rotator.openbao import OpenBao

# The record's names among the staged ones: the plan's keys, a JSON array, the id of the step the
# plan is at, what it derived from the cluster (Derived.dump), and that step's id once it failed
# reporting it did not land (model.StepFailed.landed), which the next record drops.
KEYS = "keys"
STEP = "step"
DERIVED = "derived"
NOT_LANDED = "not-landed"


def staging_leaf(kind: str, leaf: str) -> str:
    return f"{STAGING_PREFIX}{kind}/{leaf}"


@dataclass(frozen=True)
class InFlight:
    """A plan in flight: its kind and keys, the step it is at, written before that step runs, so
    every step before it finished, and what it derived from the cluster."""

    kind: str
    keys: tuple[str, ...]
    step: str
    derived: Derived = field(default_factory=Derived)


def flights(bao: OpenBao) -> dict[str, InFlight]:
    """Every plan in flight, by its leaf: one per staging leaf. A leaf has one at a time."""
    found = {}
    for path in bao.leaves(STAGING_PREFIX):
        kind, leaf = path.removeprefix(STAGING_PREFIX).split("/", 1)
        data = bao.read(path).data
        keys = tuple(json.loads(data[KEYS]))
        found[leaf] = InFlight(kind, keys, data[STEP], Derived.load(data[DERIVED]))
    return found


class Staging:
    def __init__(self, bao: OpenBao, kind: str, leaf: str):
        self.bao = bao
        self.path = staging_leaf(kind, leaf)
        self.data: dict[str, str] = {}
        self.exists = False  # whether the plan is in flight

    def load(self) -> None:
        version = self.bao.read(self.path)
        self.data = {} if version is None else dict(version.data)
        self.exists = version is not None

    def get(self, name: str) -> str | None:
        return self.data.get(name)

    def record(self, keys: tuple[str, ...], step: str, derived: Derived) -> None:
        """The plan is in flight at this step: written before it runs, so it has not failed."""
        kept = {name: value for name, value in self.data.items() if name != NOT_LANDED}
        self._write(kept | {KEYS: json.dumps(list(keys)), STEP: step, DERIVED: derived.dump()})

    def put(self, name: str, value: str) -> None:
        self._write(self.data | {name: value})

    def _write(self, data: dict[str, str]) -> None:
        self.bao.write(self.path, data)
        self.data = data
        self.exists = True

    def destroy(self) -> None:
        if self.exists:
            self.bao.destroy(self.path)
        self.data = {}
        self.exists = False
