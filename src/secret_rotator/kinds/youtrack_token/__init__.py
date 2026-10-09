"""The youtrack-token kind (design §6): a new YouTrack permanent token for the owner of the one the
leaf holds, minted through Hub's REST API by the kind's counterpart, a permanent token with Hub's
scope on an admin account in rotator/youtrack-token/credentials, itself rotated by this kind. Its
plan mints the token, named <leaf>#<key>, with the scope of the token it replaces, and writes it to
the leaf and its copies. It syncs every ExternalSecret that reads them, whatever the leaf's
activate, then activates, proves the new token, and last revokes the token the leaf held, and no
other."""

from collections.abc import Callable, Mapping

from secret_rotator.kinds.youtrack_token.steps import Mint, Prove, Revoke
from secret_rotator.model import Step
from secret_rotator.plan import PlanContext, PlanError, Target, tool_part


def token_name(leaf: str, key: str) -> str:
    """The name the kind gives the token it mints for a leaf's key, on Hub's token list."""
    return f"{leaf}#{key}"


class YouTrackToken:
    name = "youtrack-token"
    per_key = False

    def __init__(self, opener: Callable | None = None):
        self.opener = opener  # YouTrack's and Hub's HTTP opener; None: the real one

    def args_problems(self, args: Mapping) -> list[str]:
        return [f"{k}: youtrack-token takes no args" for k in args]

    def ask(self, leaf: Target) -> str:
        return "; ".join(leaf.confirms)

    def credential(self, leaf: Target) -> str:
        return "YouTrack permanent token"

    def description(self, leaf: Target) -> str:
        name = token_name(leaf.leaf, leaf.keys[0])
        confirm = " You confirm what only you can do." if leaf.confirms else ""
        return (
            f"The tool mints a new YouTrack permanent token named {name} for the owner of the "
            f"token the leaf holds, with that token's scope, and {tool_part(leaf)}. Once every "
            f"ExternalSecret that reads the leaf has synced, it proves the new token and revokes "
            f"the one the leaf held.{confirm}"
        )

    def plan(self, leaf: Target, ctx: PlanContext) -> list[Step]:
        if len(leaf.keys) != 1:
            raise PlanError(
                f"{leaf.leaf}: a youtrack-token plan rotates one key, not {len(leaf.keys)}"
            )
        key = leaf.keys[0]
        readers = [leaf.leaf, *dict.fromkeys(c.leaf for c in leaf.copies)]
        steps = [
            Mint(self.opener, leaf.leaf, key, token_name(leaf.leaf, key)),
            *ctx.steps.write(),
            *ctx.steps.eso_sync_and_rollout([], readers),
        ]
        planned = {step.id for step in steps}
        return [
            *steps,
            *(step for step in ctx.steps.activate() if step.id not in planned),
            Prove(self.opener, leaf.leaf, key),
            Revoke(self.opener),
        ]
