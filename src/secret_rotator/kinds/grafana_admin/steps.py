"""The grafana-admin kind's own steps (design §4.2: custom steps are the plugin's): grafana.login
and grafana.set_admin_password, through Grafana's API by basic auth as the user the leaf's
admin-user names. No detail or error they report carries a password."""

from secret_rotator.kinds.grafana_admin.grafana import REFUSED, Grafana, GrafanaError
from secret_rotator.model import Context, Step, StepFailed, value_name

USER = "admin-user"  # the leaf's key that holds the admin's login: the chart's admin.userKey
# The staging name of the password Grafana held before the plan.
HELD = "grafana-admin:held"


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


def taken(grafana: Grafana, user: str, tries: list[tuple[str, str]]) -> tuple[str, dict]:
    """The first of the (what, password) tries Grafana takes for the user, by its what, and the
    user Grafana answers; a StepFailed when it refuses every one."""
    refused: list[str] = []
    for what, password in tries:
        try:
            return what, grafana.user(user, password)
        except GrafanaError as e:
            if e.status != REFUSED:
                raise
            refused.append(what)
            error = e
    raise StepFailed(f"Grafana refuses user {user} with {' or '.join(refused)}: {error}")


class Login(Step):
    """Logs in to Grafana as the leaf's admin-user with the password the leaf's key holds, which
    must make that user a Grafana server admin, and stages that password as the one Grafana held
    before the plan: the set logs in with it and its undo sets it back. A leaf whose password
    Grafana refuses stops the plan before it writes. A re-run with a password staged keeps it."""

    type = "grafana.login"
    silent = True

    def __init__(self, grafana: Grafana, leaf: str, key: str):
        super().__init__(
            "grafana.login", "log in to Grafana with the admin password the leaf holds"
        )
        self.grafana = grafana
        self.leaf = leaf
        self.key = key  # the data key that holds the password

    def run(self, ctx: Context) -> str:
        user, password = held(ctx, self.leaf, USER), held(ctx, self.leaf, self.key)
        _, found = taken(self.grafana, user, [(f"the password {self.leaf} holds", password)])
        if not found.get("isGrafanaAdmin"):
            raise StepFailed(f"Grafana user {user} is no Grafana server admin")
        if ctx.staged(HELD) is None:
            ctx.stage(HELD, password)
        return f"Grafana user {user}, a server admin"


class SetAdminPassword(Step):
    """Sets the Grafana password of the leaf's admin-user to the new one, logged in with the one
    Grafana held before the plan, and logs in with the new one. A re-run on a Grafana that takes
    the new one already sets nothing.

    Its undo sets the held one back, logged in with the new one, and logs in with it. On a Grafana
    that takes the held one it sets nothing, so it leaves a set that did not land as it is."""

    type = "grafana.set_admin_password"
    mutates = True

    def __init__(self, grafana: Grafana, leaf: str, key: str):
        super().__init__(
            "grafana.set_admin_password", "set Grafana's admin password to the new one"
        )
        self.grafana = grafana
        self.leaf = leaf
        self.key = key  # the data key whose new password it sets

    def _passwords(self, ctx: Context) -> tuple[tuple[str, str], tuple[str, str]]:
        """(what, password) of the password Grafana held and of the new one."""
        old = staged(ctx, HELD, "password Grafana held before the plan")
        new = staged(ctx, value_name(self.key), "new password")
        return ("the password it held", old), ("the new password", new)

    def _set(self, user: str, have: tuple[str, str], want: tuple[str, str]) -> bool:
        """Sets the user's password from have to want; False when Grafana takes want already."""
        what, found = taken(self.grafana, user, [have, want])
        if what == want[0]:
            return False
        self.grafana.set_password(user, have[1], found["id"], want[1])
        taken(self.grafana, user, [want])
        return True

    def run(self, ctx: Context) -> str:
        old, new = self._passwords(ctx)
        if not self._set(held(ctx, self.leaf, USER), old, new):
            return "Grafana takes the new password already"
        return "set; Grafana takes a login with it"

    def undo(self, ctx: Context) -> str:
        old, new = self._passwords(ctx)
        if not self._set(held(ctx, self.leaf, USER), new, old):
            return "Grafana takes the password it held: nothing to set back"
        return "set back; Grafana takes a login with the password it held"
