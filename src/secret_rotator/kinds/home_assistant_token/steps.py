"""The home-assistant-token kind's own steps (design §4.2: custom steps are the plugin's):
home_assistant_token.mint, home_assistant_token.prove and home_assistant_token.delete, over Home
Assistant's websocket API, each connection logged in with a token of the leaf's: the one it held
before the plan, or the one the plan minted. No detail or error they report carries a token's
value; an id names a token without being one."""

import datetime

from secret_rotator.kinds.home_assistant_token.homeassistant import (
    INVALID_ID,
    LOGIN_REFUSED,
    LONG_LIVED,
    HomeAssistantError,
    Opener,
    Session,
    Token,
    connect,
)
from secret_rotator.model import Context, Step, StepFailed, expiry_name, not_landed, value_name

# The staging names of the token the leaf held, by id, and of the minted one, by name and id.
OLD = "home-assistant-token:old"
NAME = "home-assistant-token:name"
NEW = "home-assistant-token:new"


def held(ctx: Context, leaf: str, key: str) -> str:
    version = ctx.bao.read(leaf)
    value = None if version is None else version.data.get(key)
    if not value:
        raise StepFailed(f"{leaf}#{key} holds no token")
    return value


def session(token: str, what: str, opener: Opener | None) -> Session:
    """A connection logged in with the token, described as what."""
    try:
        return connect(token, opener)
    except HomeAssistantError as e:
        if e.code != LOGIN_REFUSED:
            raise
        raise StepFailed(f"Home Assistant refuses {what}: {e}") from None


def named(tokens: list[Token], name: str) -> list[Token]:
    """The user's long-lived access tokens by that name: one at most."""
    return [t for t in tokens if t.type == LONG_LIVED and t.name == name]


def refused(e: Exception) -> bool:
    return isinstance(e, HomeAssistantError) and e.refused


class Mint(Step):
    """Mints the successor of the token the leaf holds with that token, for the same user: a
    long-lived access token named after the leaf, the key and the time of the step's first run,
    since Home Assistant refuses a name one of the user's long-lived tokens has, that expires in
    the given days. It stages the id of
    the token the leaf holds, the one the connection logs in with; the new token's name, then its
    expiry date, which kv.stamp writes as the key's expires_at, and its value; and last its id,
    found by its name, which verifies it.

    A re-run with a token minted mints none. One without, after a mint whose answer was lost,
    deletes the token that mint left under the staged name, which no one holds, and mints again.
    Its undo deletes the token named so, logged in with the one the leaf holds, which a rollback
    has put back by then. On a run that finds nothing minted, every failure before the mint, and
    the mint refused, report that the step did not land."""

    type = "home_assistant_token.mint"
    mutates = True

    def __init__(self, opener: Opener | None, leaf: str, key: str, days: int):
        super().__init__(
            "home_assistant_token.mint",
            f"mint a new Home Assistant long-lived access token that expires in {days} days",
        )
        self.opener = opener
        self.leaf = leaf
        self.key = key
        self.days = days

    def _connect(self, ctx: Context) -> Session:
        what = f"the token {self.leaf}#{self.key} holds"
        return session(held(ctx, self.leaf, self.key), what, self.opener)

    def run(self, ctx: Context) -> str:
        fresh = ctx.staged(value_name(self.key)) is None
        sending = False
        try:
            with self._connect(ctx) as ha:
                tokens = ha.tokens()
                if fresh:
                    if ctx.staged(OLD) is None:
                        (current,) = (t for t in tokens if t.current)
                        ctx.stage(OLD, current.id)
                    name = ctx.staged(NAME)
                    if name is None:
                        name = f"{self.leaf}#{self.key} {ctx.now:%Y-%m-%dT%H:%M:%SZ}"
                        ctx.stage(NAME, name)
                    for lost in named(tokens, name):
                        ha.delete(lost.id)
                    expires = ctx.now.date() + datetime.timedelta(days=self.days)
                    ctx.stage(expiry_name(self.key), expires.isoformat())
                    sending = True
                    ctx.stage(value_name(self.key), ha.mint(name, self.days))
                    tokens = ha.tokens()
        except Exception as e:
            if fresh and (not sending or refused(e)):
                raise not_landed(e) from e
            raise
        name = ctx.staged(NAME)
        minted = named(tokens, name)
        if not minted:
            raise StepFailed(f"Home Assistant lists no long-lived access token named {name}")
        ctx.stage(NEW, minted[0].id)
        return f"{name}, which expires {ctx.staged(expiry_name(self.key))}"

    def undo(self, ctx: Context) -> str:
        name = ctx.staged(NAME)
        if name is None:
            return "nothing was minted"
        with self._connect(ctx) as ha:
            minted = named(ha.tokens(), name)
            if not minted:
                return f"Home Assistant lists no token named {name}: nothing to delete"
            try:
                ha.delete(minted[0].id)
            except HomeAssistantError as e:
                if e.code != INVALID_ID:
                    raise
        return f"deleted {name}"


class Prove(Step):
    """Logs in to Home Assistant with the token the leaf holds, which must be the one the plan
    minted, and lists the user's tokens on that connection: Home Assistant must take the token as
    the one the mint found."""

    type = "home_assistant_token.prove"
    silent = True

    def __init__(self, opener: Opener | None, leaf: str, key: str):
        super().__init__("home_assistant_token.prove", "log in with the new token")
        self.opener = opener
        self.leaf = leaf
        self.key = key

    def run(self, ctx: Context) -> str:
        minted, new = ctx.staged(value_name(self.key)), ctx.staged(NEW)
        if minted is None or new is None:
            raise StepFailed("no new token is staged")
        if held(ctx, self.leaf, self.key) != minted:
            raise StepFailed(f"{self.leaf}#{self.key} does not hold the token the plan minted")
        with session(minted, "the new token", self.opener) as ha:
            (current,) = (t for t in ha.tokens() if t.current)
        if current.id != new:
            raise StepFailed(f"Home Assistant takes the new token as token {current.id}, not {new}")
        return f"logged in as {current.name}"


class Delete(Step):
    """Deletes the token the leaf held before the plan, by the id the mint staged, and no other,
    logged in with the token the plan minted. It verifies by listing the user's tokens again. The
    plan puts it after the proof of the new token.

    It has no undo. A failure before its delete, and that delete refused (an error but
    INVALID_ID, which says the token is gone), report that the step did not land."""

    type = "home_assistant_token.delete"
    mutates = True

    def __init__(self, opener: Opener | None, key: str):
        super().__init__("home_assistant_token.delete", "delete the token the leaf held")
        self.opener = opener
        self.key = key
        self.no_undo = "a deleted Home Assistant token cannot be restored"

    def run(self, ctx: Context) -> str:
        sending = False
        try:
            old, minted = ctx.staged(OLD), ctx.staged(value_name(self.key))
            if old is None or minted is None:
                raise StepFailed("no new token is staged")
            with session(minted, "the new token", self.opener) as ha:
                if old in {t.id for t in ha.tokens()}:
                    sending = True
                    try:
                        ha.delete(old)
                    except HomeAssistantError as e:
                        if e.code != INVALID_ID:
                            raise
                left = {t.id for t in ha.tokens()}
        except Exception as e:
            if not sending or refused(e):
                raise not_landed(e) from e
            raise
        if old in left:
            raise StepFailed(f"Home Assistant still lists token {old} after its delete")
        if not sending:
            return f"token {old} was deleted already"
        return f"deleted token {old}"
