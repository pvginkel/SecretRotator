"""The jenkins-token kind (design §6): a new API token of the Jenkins account rotator/jenkins logs
in as, which owns every token the kind rotates (045 D4). Its plan mints the token, named
<leaf>#<key>, and writes it to the leaf and its copies. It syncs every ExternalSecret that reads
them, whatever the leaf's activate, then activates, logs in with the token the leaf holds, and
last revokes the tokens the consumers held before: the others of its name, and any named the
args' legacy, the name of the token the leaf held before the rotator first rotated it."""

from collections.abc import Mapping

from secret_rotator.kinds.jenkins_token.steps import Login, Mint, Revoke, replaced
from secret_rotator.model import Step
from secret_rotator.plan import PlanContext, PlanError, Target, tool_part

ARGS = ("legacy",)


def token_name(leaf: str, key: str) -> str:
    """The name the kind gives the token it mints for a leaf's key, on Jenkins' security page."""
    return f"{leaf}#{key}"


def legacy_of(leaf: Target) -> str | None:
    """The args' legacy token name of the plan's one key."""
    return leaf.entries[leaf.keys[0]].args.get("legacy")


class JenkinsToken:
    name = "jenkins-token"
    per_key = False

    def args_problems(self, args: Mapping) -> list[str]:
        problems = [
            f"{k}: not one of jenkins-token's {', '.join(ARGS)}" for k in args if k not in ARGS
        ]
        legacy = args.get("legacy")
        if "legacy" in args and not (isinstance(legacy, str) and legacy.strip()):
            problems.append("legacy: not a token name")
        return problems

    def ask(self, leaf: Target) -> str:
        return "; ".join(leaf.confirms)

    def description(self, leaf: Target) -> str:
        name, legacy = token_name(leaf.leaf, leaf.keys[0]), legacy_of(leaf)
        confirm = " You confirm what only you can do." if leaf.confirms else ""
        return (
            f"The tool mints a new Jenkins API token named {name} and {tool_part(leaf)}. Once "
            f"every ExternalSecret that reads the leaf has synced, it logs in with the new token "
            f"and revokes {replaced(name, legacy)}.{confirm}"
        )

    def plan(self, leaf: Target, ctx: PlanContext) -> list[Step]:
        if len(leaf.keys) != 1:
            raise PlanError(
                f"{leaf.leaf}: a jenkins-token plan rotates one key, not {len(leaf.keys)}"
            )
        key = leaf.keys[0]
        name, jenkins = token_name(leaf.leaf, key), ctx.steps.jenkins
        readers = [leaf.leaf, *dict.fromkeys(c.leaf for c in leaf.copies)]
        steps = [
            Mint(jenkins, name, key),
            *ctx.steps.write(),
            *ctx.steps.eso_sync_and_rollout([], readers),
        ]
        planned = {step.id for step in steps}
        return [
            *steps,
            *(step for step in ctx.steps.activate() if step.id not in planned),
            Login(jenkins, leaf.leaf, key),
            Revoke(jenkins, name, legacy_of(leaf)),
        ]
