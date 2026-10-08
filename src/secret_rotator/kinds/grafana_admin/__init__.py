"""The grafana-admin kind (design §6): a new password for Grafana's local admin. Grafana takes its
admin Secret only when it creates its database (GrafanaDeploy config/prd/values.yaml), so the kind
sets the password through Grafana's API. Its plan logs in with the password the leaf holds, which
stops it before it writes when Grafana refuses that one. It generates the new password, writes it
to the leaf and its copies, syncs every ExternalSecret that reads them, whatever the leaf's
activate, and activates. Only then does it set the password in Grafana, logged in with the one
Grafana held, so no Secret holds a password Grafana no longer takes (ruling F3)."""

from collections.abc import Callable, Mapping

from secret_rotator.kinds.grafana_admin.grafana import Grafana
from secret_rotator.kinds.grafana_admin.steps import Login, SetAdminPassword
from secret_rotator.model import Step
from secret_rotator.plan import PlanContext, PlanError, Target, tool_part

# The URL Grafana is served at: GrafanaDeploy config/prd/values.yaml's root_url.
BASE = "http://grafana.home"


class GrafanaAdmin:
    name = "grafana-admin"
    per_key = False

    def __init__(self, opener: Callable | None = None):
        self.opener = opener  # Grafana's HTTP opener; None: the real one

    def args_problems(self, args: Mapping) -> list[str]:
        return [f"{k}: grafana-admin takes no args" for k in args]

    def ask(self, leaf: Target) -> str:
        return "; ".join(leaf.confirms)

    def description(self, leaf: Target) -> str:
        confirm = " You confirm what only you can do." if leaf.confirms else ""
        return (
            f"The tool generates a new password for Grafana's admin and {tool_part(leaf)}. Once "
            f"every ExternalSecret that reads the leaf has synced, it sets the password in "
            f"Grafana, logged in with the one Grafana held, and logs in with the new one.{confirm}"
        )

    def plan(self, leaf: Target, ctx: PlanContext) -> list[Step]:
        if len(leaf.keys) != 1:
            raise PlanError(
                f"{leaf.leaf}: a grafana-admin plan rotates one key, not {len(leaf.keys)}"
            )
        key, grafana = leaf.keys[0], Grafana(BASE, self.opener)
        readers = [leaf.leaf, *dict.fromkeys(c.leaf for c in leaf.copies)]
        steps = [
            Login(grafana, leaf.leaf, key),
            *ctx.steps.generate(key),
            *ctx.steps.write(),
            *ctx.steps.eso_sync_and_rollout([], readers),
        ]
        planned = {step.id for step in steps}
        return [
            *steps,
            *(step for step in ctx.steps.activate() if step.id not in planned),
            SetAdminPassword(grafana, leaf.leaf, key),
        ]
