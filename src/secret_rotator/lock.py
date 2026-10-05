"""One plan runs at a time, anywhere (design R24, §4.3): the nightly job and an operator's
`run <path>` run in separate iac containers, so the lock is the leaf kv/rotator/lock. A KV v2
check-and-set write takes it and a write that clears its holder releases it; it is never deleted.
A lock whose holder died is broken through break_held, never by editing OpenBao."""

import datetime
import os
import socket
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass

from secret_rotator.contract import LOCK_LEAF
from secret_rotator.openbao import OpenBao, OpenBaoError, Version


def utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC)


def holder_name(command: str) -> str:
    """Who takes the lock: the command, and the container and process it runs in."""
    return f"{command} on {socket.gethostname()}, pid {os.getpid()}"


@dataclass(frozen=True)
class Holder:
    who: str
    since: str
    plan: str
    version: int  # the lock leaf's version that names this holder

    def __str__(self) -> str:
        return f"{self.who} holds it since {self.since} for the {self.plan}"


def _holder(version: Version | None) -> Holder | None:
    if version is None or not version.data.get("holder"):
        return None
    d = version.data
    return Holder(d["holder"], d.get("since", ""), d.get("plan", ""), version.number)


class LockError(Exception):
    pass


class LockHeld(LockError):
    def __init__(self, holder: Holder):
        super().__init__(f"another plan runs: {holder}")
        self.holder = holder


class Lock:
    def __init__(self, bao: OpenBao, who: str, clock: Callable[[], datetime.datetime] = utcnow):
        self.bao = bao
        self.who = who
        self.clock = clock
        self.version: int | None = None  # while held: the version that names this holder

    def holder(self) -> Holder | None:
        return _holder(self.bao.read(LOCK_LEAF))

    def take(self, plan: str) -> None:
        version = self.bao.read(LOCK_LEAF)
        if (holder := _holder(version)) is not None:
            raise LockHeld(holder)
        data = {"holder": self.who, "since": self.clock().isoformat(timespec="seconds")}
        try:
            self.version = self.bao.write(
                LOCK_LEAF, data | {"plan": plan}, cas=0 if version is None else version.number
            )
        except OpenBaoError as e:
            # A 400 is the check-and-set refused: another holder took it in between.
            if e.status != 400 or (holder := self.holder()) is None:
                raise
            raise LockHeld(holder) from None

    def release(self) -> None:
        try:
            self.bao.write(LOCK_LEAF, {}, cas=self.version)
        except OpenBaoError as e:
            if e.status != 400:
                raise
            raise LockError(
                f"the lock was broken while this plan held it; now: {self.holder() or 'free'}"
            ) from None
        finally:
            self.version = None

    def break_held(self, seen: Holder) -> None:
        """Clears the lock of the holder seen, a holder that died; refused when another holder
        has taken it since."""
        try:
            self.bao.write(LOCK_LEAF, {}, cas=seen.version)
        except OpenBaoError as e:
            if e.status != 400:
                raise
            if (holder := self.holder()) is not None:
                raise LockHeld(holder) from None

    @contextmanager
    def held(self, plan: str) -> Iterator[None]:
        self.take(plan)
        try:
            yield
        finally:
            self.release()
