"""The external kind (design §6, R81): a key rotated outside the tool, whose value the tool never
changes. One plan per key: the operator does the work the key's notes name, outside the tool, and
confirms it; then the stamp restarts the key's interval. The plan writes nothing and activates
nothing: a copy of the key is its procedure's to rewrite, and a marker leaf stays as it is."""

from collections.abc import Mapping

from secret_rotator.model import Step
from secret_rotator.plan import PlanContext, Target

# What to do when an external key falls due.
RUNBOOK = "Ansible docs/runbooks/external-key-due.md"


class External:
    name = "external"
    per_key = True

    def args_problems(self, args: Mapping) -> list[str]:
        return [f"{k}: external takes no args" for k in args]

    def ask(self, leaf: Target) -> str:
        return "rotate it outside the tool"

    def description(self, leaf: Target) -> str:
        return (
            f"You rotate {leaf.keys[0]} outside the tool, as its notes say, and confirm it. The "
            f"tool stamps the key and changes nothing else ({RUNBOOK})."
        )

    def plan(self, leaf: Target, ctx: PlanContext) -> list[Step]:
        (key,) = leaf.keys
        notes = leaf.entries[key].notes
        instruction = "\n\n".join(text for text in (notes, f"Runbook: {RUNBOOK}") if text)
        return ctx.steps.confirm("done", f"Rotate {key} outside the tool", instruction)
