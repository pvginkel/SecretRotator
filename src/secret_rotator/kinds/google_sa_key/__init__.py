"""The google-sa-key kind (design §6): a Google service account key file, whose account creates
its own new keys, so the kind has no counterpart credential; the operator grants each account the
right to manage its own keys. Its plan creates the new key logged in with the key the leaf holds,
and writes it to the leaf and its copies. It syncs every ExternalSecret that reads them, whatever
the leaf's activate, then activates, logs in with the new key, and last deletes the key the leaf
held, and no other."""

from collections.abc import Mapping

from secret_rotator.kinds.google_sa_key.google import Google
from secret_rotator.kinds.google_sa_key.steps import Delete, Mint, Prove
from secret_rotator.model import Step
from secret_rotator.plan import PlanContext, PlanError, Target, tool_part


class GoogleSaKey:
    name = "google-sa-key"
    per_key = False

    def __init__(self, google: Google | None = None):
        self.google = google or Google()

    def args_problems(self, args: Mapping) -> list[str]:
        return [f"{k}: google-sa-key takes no args" for k in args]

    def ask(self, leaf: Target) -> str:
        return "; ".join(leaf.confirms)

    def credential(self, leaf: Target) -> str:
        return "Google service account key"

    def description(self, leaf: Target) -> str:
        confirm = " You confirm what only you can do." if leaf.confirms else ""
        return (
            f"The tool creates a new key of the service account with the key the leaf holds, and "
            f"{tool_part(leaf)}. Once every ExternalSecret that reads the leaf has synced, it logs "
            f"in with the new key and deletes the one the leaf held.{confirm}"
        )

    def plan(self, leaf: Target, ctx: PlanContext) -> list[Step]:
        if len(leaf.keys) != 1:
            raise PlanError(
                f"{leaf.leaf}: a google-sa-key plan rotates one key, not {len(leaf.keys)}"
            )
        key = leaf.keys[0]
        readers = [leaf.leaf, *dict.fromkeys(c.leaf for c in leaf.copies)]
        steps = [
            Mint(self.google, leaf.leaf, key),
            *ctx.steps.write(),
            *ctx.steps.eso_sync_and_rollout([], readers),
        ]
        planned = {step.id for step in steps}
        return [
            *steps,
            *(step for step in ctx.steps.activate() if step.id not in planned),
            Prove(self.google, leaf.leaf, key),
            Delete(self.google, key),
        ]
