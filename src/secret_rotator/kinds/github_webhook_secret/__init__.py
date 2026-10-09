"""The github-webhook-secret kind (design §6): a new HMAC secret of a GitHub repository hook. Its
plan generates the secret and writes it to the leaf and its copies, syncs every ExternalSecret
that reads them, whatever the leaf's activate, and activates. The hook's github.webhook steps,
which its entries' github-webhook: activators build, come last, after every other activation step:
GitHub signs with the new secret only once the hook's receivers hold it, then a ping proves it and
what failed meanwhile is redelivered."""

from collections.abc import Mapping

from secret_rotator.githubsteps import GitHubWebhook
from secret_rotator.model import Step
from secret_rotator.plan import PlanContext, PlanError, Target, tool_part


class GitHubWebhookSecret:
    name = "github-webhook-secret"
    per_key = False

    def args_problems(self, args: Mapping) -> list[str]:
        return [f"{k}: github-webhook-secret takes no args" for k in args]

    def ask(self, leaf: Target) -> str:
        return "; ".join(leaf.confirms)

    def credential(self, leaf: Target) -> str:
        return "GitHub webhook secret"

    def description(self, leaf: Target) -> str:
        hooks = sorted(
            {s.arg for a in leaf.activations for s in a.specs if s.name == "github-webhook"}
        )
        confirm = " You confirm what only you can do." if leaf.confirms else ""
        return (
            f"The tool generates a new secret and {tool_part(leaf)}. Once every ExternalSecret "
            f"that reads the leaf has synced, it sets the secret on GitHub hook "
            f"{', '.join(hooks)}, proves it with a ping and redelivers each delivery that failed "
            f"meanwhile.{confirm}"
        )

    def plan(self, leaf: Target, ctx: PlanContext) -> list[Step]:
        if len(leaf.keys) != 1:
            raise PlanError(
                f"{leaf.leaf}: a github-webhook-secret plan rotates one key, not {len(leaf.keys)}"
            )
        readers = [leaf.leaf, *dict.fromkeys(c.leaf for c in leaf.copies)]
        steps = [
            *ctx.steps.generate(leaf.keys[0]),
            *ctx.steps.write(),
            *ctx.steps.eso_sync_and_rollout([], readers),
        ]
        planned = {step.id for step in steps}
        activation = [step for step in ctx.steps.activate() if step.id not in planned]
        hooks = [step for step in activation if isinstance(step, GitHubWebhook)]
        if not hooks:
            raise PlanError(
                f"{leaf.leaf}: no entry the plan writes names the GitHub hook "
                f"(github-webhook:<owner>/<repo>/<hook-id>) whose secret it is"
            )
        return [*steps, *(step for step in activation if step not in hooks), *hooks]
