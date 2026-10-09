"""The youtrack-token kind's own steps (design §4.2: custom steps are the plugin's):
youtrack_token.mint, youtrack_token.prove and youtrack_token.revoke, through Hub's REST API as the
kind's counterpart, a permanent token with Hub's scope on an admin account, which each step reads
from its leaf when it runs. On the plan of that leaf itself the steps after the kv.write therefore
use the new token, and a rollback's undo of the mint, after the kv.write undo, the old one. No
detail or error they report carries a token's value."""

from collections.abc import Callable

from secret_rotator.kinds.youtrack_token import hub
from secret_rotator.model import Context, Step, StepFailed, not_landed, value_name
from secret_rotator.youtrack import YouTrack, YouTrackError

COUNTERPART = ("rotator/youtrack-token/credentials", "token")
# The staging names of whom the token the leaf held belongs to, its id, and the minted one's.
OWNER = "youtrack-token:owner"
LOGIN = "youtrack-token:login"
OLD = "youtrack-token:old"
NEW = "youtrack-token:new"


def refused(e: Exception) -> bool:
    """A request Hub answered with a refusal (4xx), which changed nothing."""
    return isinstance(e, YouTrackError) and e.status is not None and e.status < 500


def admin(ctx: Context, opener: Callable | None) -> YouTrack:
    """Hub's client as the counterpart, with the token its leaf holds now."""
    leaf, key = COUNTERPART
    version = ctx.bao.read(leaf)
    if version is None:
        raise StepFailed(f"{leaf} cannot be read: no such leaf, or its current version is deleted")
    if not version.data.get(key):
        raise StepFailed(f"{leaf} has no {key}")
    return hub.client(version.data[key], opener)


def whose(token: str, what: str, opener: Callable | None) -> hub.Owner:
    try:
        return hub.owner(token, opener)
    except YouTrackError as e:
        if e.status not in (401, 403):
            raise
        raise StepFailed(f"neither YouTrack nor Hub takes {what}: {e}") from None


def held(ctx: Context, leaf: str, key: str) -> str:
    version = ctx.bao.read(leaf)
    value = None if version is None else version.data.get(key)
    if not value:
        raise StepFailed(f"{leaf}#{key} holds no token")
    return value


class Mint(Step):
    """Finds the token the leaf holds, then mints a new permanent token for its owner, named after
    the leaf and key, with the found token's scope, and stages the found token's id, its owner,
    and the new token's id and value. The owner is whom the token belongs to, asked with the token
    itself, so the leaf carries no user name. The token is found among the owner's by the name its
    value carries: the login it carries must be the owner's, and exactly one of the owner's tokens
    must have that name; else which one the leaf holds cannot be told, and the step mints nothing.
    It verifies by finding the new token's id under its name among the owner's tokens. A re-run
    with a token minted mints none: it looks that one up again.

    Its undo revokes the token it minted. On a run that finds nothing minted, every failure before
    the mint, and the mint refused (a 4xx answer), report that the step did not land. A mint whose
    answer is lost leaves a token no one holds, named after the leaf, which the operator revokes
    on Hub: until then, the plan cannot tell which token the leaf holds once that one is named
    after the leaf too."""

    type = "youtrack_token.mint"
    mutates = True

    def __init__(self, opener: Callable | None, leaf: str, key: str, name: str):
        super().__init__("youtrack_token.mint", f"mint a new YouTrack permanent token named {name}")
        self.opener = opener
        self.leaf = leaf
        self.key = key  # the data key the new token is staged for
        self.name = name

    def _found(self, ctx: Context, admin: YouTrack) -> tuple[hub.Owner, hub.Token]:
        """The owner of the token the leaf holds, and that token among the owner's."""
        where = f"{self.leaf}#{self.key}"
        value = held(ctx, self.leaf, self.key)
        owner = whose(value, f"the token {where} holds", self.opener)
        carried = hub.carried(value)
        unknown = f"which of {owner.login}'s tokens {where} holds cannot be told"
        if carried is None:
            raise StepFailed(
                f"the token {where} holds is not of the form perm:<login>.<name>.<secret>: "
                f"{unknown}"
            )
        login, name = carried
        if login != owner.login:
            raise StepFailed(
                f"the token {where} holds carries login {login}, but is {owner.login}'s: {unknown}"
            )
        named = [token for token in hub.tokens(admin, owner.id) if token.name == name]
        if len(named) != 1:
            raise StepFailed(f"{len(named)} of {owner.login}'s tokens are named {name}: {unknown}")
        return owner, named[0]

    def run(self, ctx: Context) -> str:
        fresh = ctx.staged(NEW) is None
        try:
            client = admin(ctx, self.opener)
            if fresh:
                owner, old = self._found(ctx, client)
        except Exception as e:
            if fresh:
                raise not_landed(e) from e
            raise
        if fresh:
            ctx.stage(OWNER, owner.id)
            ctx.stage(LOGIN, owner.login)
            ctx.stage(OLD, old.id)
            try:
                new, value = hub.mint(client, owner.id, self.name, old.scope)
            except YouTrackError as e:
                if refused(e):
                    raise not_landed(e) from e
                raise
            ctx.stage(NEW, new)
            if value is not None:
                ctx.stage(value_name(self.key), value)
        new, login = ctx.staged(NEW), ctx.staged(LOGIN)
        if ctx.staged(value_name(self.key)) is None:
            raise StepFailed(f"Hub's answer to the mint of token {new} carries no token")
        listed = hub.tokens(client, ctx.staged(OWNER))
        if new not in {token.id for token in listed if token.name == self.name}:
            raise StepFailed(f"Hub lists no token {new} named {self.name} of {login}")
        return f"token {new} of {login}, with the scope of token {ctx.staged(OLD)}"

    def undo(self, ctx: Context) -> str:
        new = ctx.staged(NEW)
        if new is None:
            return "nothing was minted"
        try:
            hub.revoke(admin(ctx, self.opener), ctx.staged(OWNER), new)
        except YouTrackError as e:
            if e.status != 404:
                raise
            return f"token {new} was revoked already"
        return f"token {new} revoked"


class Prove(Step):
    """Asks whom the token the leaf holds belongs to, with that token: the leaf must hold the token
    the plan minted, and YouTrack or Hub must take it as the owner of the token it replaces."""

    type = "youtrack_token.prove"
    silent = True

    def __init__(self, opener: Callable | None, leaf: str, key: str):
        super().__init__("youtrack_token.prove", "prove the new token")
        self.opener = opener
        self.leaf = leaf
        self.key = key

    def run(self, ctx: Context) -> str:
        minted = ctx.staged(value_name(self.key))
        if minted is None:
            raise StepFailed("no new token is staged")
        if held(ctx, self.leaf, self.key) != minted:
            raise StepFailed(f"{self.leaf}#{self.key} does not hold the token the plan minted")
        owner = whose(minted, "the new token", self.opener)
        login = ctx.staged(LOGIN)
        if owner.id != ctx.staged(OWNER):
            raise StepFailed(f"the new token is {owner.login}'s, not {login}'s")
        return f"taken as {login}"


class Revoke(Step):
    """Revokes the token the leaf held before the plan, by the id the mint found it under, and no
    other. The owner's tokens must list the one the plan minted; it verifies by listing them again.
    The plan puts it after the proof of the new token.

    It has no undo. A failure before its revoke, and that revoke refused (a 4xx answer but 404,
    which says the token is gone), report that the step did not land."""

    type = "youtrack_token.revoke"
    mutates = True

    def __init__(self, opener: Callable | None):
        super().__init__("youtrack_token.revoke", "revoke the token the leaf held")
        self.opener = opener
        self.no_undo = "a revoked YouTrack permanent token cannot be restored"

    def run(self, ctx: Context) -> str:
        sending = False
        try:
            old, new, owner = ctx.staged(OLD), ctx.staged(NEW), ctx.staged(OWNER)
            if old is None or new is None or owner is None:
                raise StepFailed("no new token is staged")
            login = ctx.staged(LOGIN)
            client = admin(ctx, self.opener)
            found = {token.id for token in hub.tokens(client, owner)}
            if new not in found:
                raise StepFailed(
                    f"Hub lists no token {new} of {login}, the one the plan minted: token {old} "
                    f"stays"
                )
            if old in found:
                sending = True
                try:
                    hub.revoke(client, owner, old)
                except YouTrackError as e:
                    if e.status != 404:
                        raise
        except Exception as e:
            if not sending or refused(e):
                raise not_landed(e) from e
            raise
        if old in {token.id for token in hub.tokens(client, owner)}:
            raise StepFailed(f"Hub still lists token {old} of {login} after its revoke")
        if not sending:
            return f"token {old} of {login} was revoked already"
        return f"revoked token {old} of {login}"
