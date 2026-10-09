"""The elastic-user kind's own steps (design §4.2: custom steps are the plugin's): elastic.login and
elastic.set_password, through Elasticsearch's security API by basic auth. A password is set logged
in as the superuser elastic, with the password the kind's counterpart, elastic's own leaf, holds;
on the plan of that leaf, whose set changes elastic's password, with the one Elasticsearch held
before the set. No detail or error they report carries a password."""

from secret_rotator.kinds.elastic_user.elasticsearch import (
    REFUSED,
    Elasticsearch,
    ElasticsearchError,
)
from secret_rotator.model import Context, Step, StepFailed, value_name

SUPERUSER = "elastic"
COUNTERPART = "eso/prd/elasticsearch/prd/elastic"  # the superuser's leaf (044 D3)
PASSWORD = "password"  # the counterpart's key that holds elastic's password
USERNAME = "username"  # the key of a consumer's leaf that names its user, where it has one
# The staging name of the password Elasticsearch held for the user before the plan.
HELD = "elastic-user:held"


def held(ctx: Context, leaf: str, key: str) -> str:
    """What the leaf's key holds now."""
    version = ctx.bao.read(leaf)
    value = None if version is None else version.data.get(key)
    if not value:
        raise StepFailed(f"{leaf} holds no {key}")
    return value


def staged(ctx: Context, name: str, what: str) -> str:
    value = ctx.staged(name)
    if value is None:
        raise StepFailed(f"no {what} is staged")
    return value


class Login(Step):
    """Logs in to Elasticsearch as the user with the password the leaf holds and, on any leaf but
    the counterpart, as elastic with the password the counterpart holds. It stages the leaf's
    password as the one Elasticsearch held before the plan: the set's undo sets it back, and the
    superuser's set logs in with it. A leaf whose username names another user, and a login
    Elasticsearch refuses, stop the plan before it writes. A re-run with a password staged keeps
    it."""

    type = "elastic.login"
    silent = True

    def __init__(self, es: Elasticsearch, leaf: str, key: str, user: str):
        counterpart = "" if leaf == COUNTERPART else ", and as elastic with the one its leaf holds"
        super().__init__(
            "elastic.login",
            f"log in to Elasticsearch as {user} with the password the leaf holds{counterpart}",
        )
        self.es = es
        self.leaf = leaf
        self.key = key  # the data key that holds the password
        self.user = user

    def run(self, ctx: Context) -> str:
        version = ctx.bao.read(self.leaf)
        named = None if version is None else version.data.get(USERNAME)
        if named and named != self.user:
            raise StepFailed(
                f"{self.leaf} holds username {named}, not {self.user}, the user its args name"
            )
        password = held(ctx, self.leaf, self.key)
        logins = [(self.user, password, f"the password {self.leaf} holds")]
        if self.leaf != COUNTERPART:
            counterpart = held(ctx, COUNTERPART, PASSWORD)
            logins.append((SUPERUSER, counterpart, f"the password {COUNTERPART} holds"))
        for user, value, what in logins:
            if not self.es.takes(user, value):
                raise StepFailed(f"Elasticsearch refuses user {user} with {what}")
        if ctx.staged(HELD) is None:
            ctx.stage(HELD, password)
        return f"Elasticsearch takes {' and '.join(user for user, _, _ in logins)}"


class SetPassword(Step):
    """Sets the user's password in Elasticsearch to the new one, logged in as elastic, and logs in
    as the user with it. A re-run on an Elasticsearch that takes the new one and not the held one
    sets nothing.

    Its undo sets the password Elasticsearch held back, and logs in with it. On an Elasticsearch
    that takes the held one it sets nothing, so it leaves a set that did not land as it is.

    elastic logs in with the password the counterpart holds; on the superuser's own plan with the
    one the set changes from: the held one forward, the new one in the undo."""

    type = "elastic.set_password"
    mutates = True

    def __init__(self, es: Elasticsearch, leaf: str, key: str, user: str):
        super().__init__(
            "elastic.set_password", f"set the password of Elasticsearch user {user} to the new one"
        )
        self.es = es
        self.leaf = leaf
        self.key = key  # the data key whose new password it sets
        self.user = user

    def _passwords(self, ctx: Context) -> tuple[tuple[str, str], tuple[str, str]]:
        """(what, password) of the password Elasticsearch held and of the new one."""
        old = staged(ctx, HELD, "password Elasticsearch held before the plan")
        new = staged(ctx, value_name(self.key), "new password")
        return ("the password it held", old), ("the new password", new)

    def _set(self, ctx: Context, have: tuple[str, str], want: tuple[str, str]) -> bool:
        """Sets the user's password from have's to want's; False when Elasticsearch takes want's
        already."""
        if not self.es.takes(self.user, have[1]) and self.es.takes(self.user, want[1]):
            return False
        if self.leaf == COUNTERPART:
            what, password = have
        else:
            what, password = f"the password {COUNTERPART} holds", held(ctx, COUNTERPART, PASSWORD)
        try:
            self.es.set_password(SUPERUSER, password, self.user, want[1])
        except ElasticsearchError as e:
            if e.status != REFUSED:
                raise
            raise StepFailed(f"Elasticsearch refuses user {SUPERUSER} with {what}") from None
        if not self.es.takes(self.user, want[1]):
            raise StepFailed(f"Elasticsearch refuses user {self.user} with {want[0]}")
        return True

    def run(self, ctx: Context) -> str:
        old, new = self._passwords(ctx)
        if not self._set(ctx, old, new):
            return "Elasticsearch takes the new password already"
        return "set; Elasticsearch takes a login with it"

    def undo(self, ctx: Context) -> str:
        old, new = self._passwords(ctx)
        if not self._set(ctx, new, old):
            return "Elasticsearch takes the password it held: nothing to set back"
        return "set back; Elasticsearch takes a login with the password it held"
