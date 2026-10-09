"""The home-assistant-token kind (design §6): a Home Assistant long-lived access token, which
mints its own successor over Home Assistant's websocket API, so the kind has no counterpart
credential. Its plan mints the successor with the token the leaf holds, and writes it to the leaf
and its copies. It syncs every ExternalSecret that reads them, whatever the leaf's activate, then
activates, logs in with the new token, and last deletes the token the leaf held, and no other."""

from collections.abc import Mapping

from secret_rotator.kinds.home_assistant_token.homeassistant import Opener
from secret_rotator.kinds.home_assistant_token.steps import Delete, Mint, Prove
from secret_rotator.model import Step
from secret_rotator.plan import PlanContext, PlanError, Target, tool_part

# A successor expires after four times its key's interval, and never under 90 days, as an
# AppRole secret_id does (design R74): a stop of the rotator for weeks expires no consumer's token.
EXPIRY_FACTOR, MIN_EXPIRY_DAYS = 4, 90


def lifespan(leaf: Target) -> int | None:
    """The days the successor is minted to last; None for a key that rotates never."""
    interval = leaf.entries[leaf.keys[0]].interval
    return None if interval is None else max(EXPIRY_FACTOR * interval, MIN_EXPIRY_DAYS)


class HomeAssistantToken:
    name = "home-assistant-token"
    per_key = False

    def __init__(self, opener: Opener | None = None):
        self.opener = opener  # what opens Home Assistant's websocket; None: the real one

    def args_problems(self, args: Mapping) -> list[str]:
        return [f"{k}: home-assistant-token takes no args" for k in args]

    def ask(self, leaf: Target) -> str:
        return "; ".join(leaf.confirms)

    def credential(self, leaf: Target) -> str:
        return "Home Assistant long-lived access token"

    def description(self, leaf: Target) -> str:
        confirm = " You confirm what only you can do." if leaf.confirms else ""
        return (
            f"The tool mints a new Home Assistant long-lived access token that expires in "
            f"{lifespan(leaf)} days with the one the leaf holds, and {tool_part(leaf)}. Once "
            f"every ExternalSecret that reads the leaf has synced, it logs in with the new token "
            f"and deletes the one the leaf held.{confirm}"
        )

    def plan(self, leaf: Target, ctx: PlanContext) -> list[Step]:
        if len(leaf.keys) != 1:
            raise PlanError(
                f"{leaf.leaf}: a home-assistant-token plan rotates one key, not {len(leaf.keys)}"
            )
        key, days = leaf.keys[0], lifespan(leaf)
        if days is None:
            raise PlanError(
                f"{leaf.leaf}: {key} rotates never, and a Home Assistant token is minted to "
                f"expire in {EXPIRY_FACTOR} times its interval"
            )
        readers = [leaf.leaf, *dict.fromkeys(c.leaf for c in leaf.copies)]
        steps = [
            Mint(self.opener, leaf.leaf, key, days),
            *ctx.steps.write(),
            *ctx.steps.eso_sync_and_rollout([], readers),
        ]
        planned = {step.id for step in steps}
        return [
            *steps,
            *(step for step in ctx.steps.activate() if step.id not in planned),
            Prove(self.opener, leaf.leaf, key),
            Delete(self.opener, key),
        ]
