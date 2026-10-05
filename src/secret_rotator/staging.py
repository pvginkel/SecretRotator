"""A plan's staging leaf (design §4.5, R19): what its steps produce and what their undos need,
written before anything uses it, so an exit or a crash loses nothing; destroyed with every version
when the plan finishes or is rolled back. The rotator's policy grants delete here only."""

from secret_rotator.contract import STAGING_PREFIX
from secret_rotator.openbao import OpenBao


def staging_leaf(kind: str, leaf: str) -> str:
    return f"{STAGING_PREFIX}{kind}/{leaf}"


class Staging:
    def __init__(self, bao: OpenBao, kind: str, leaf: str):
        self.bao = bao
        self.path = staging_leaf(kind, leaf)
        self.data: dict[str, str] = {}
        self.exists = False

    def load(self) -> None:
        version = self.bao.read(self.path)
        self.data = {} if version is None else dict(version.data)
        self.exists = version is not None

    def get(self, name: str) -> str | None:
        return self.data.get(name)

    def put(self, name: str, value: str) -> None:
        data = self.data | {name: value}
        self.bao.write(self.path, data)
        self.data = data
        self.exists = True

    def destroy(self) -> None:
        if self.exists:
            self.bao.destroy(self.path)
        self.data = {}
        self.exists = False
