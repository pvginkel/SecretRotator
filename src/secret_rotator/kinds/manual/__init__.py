"""The manual kind (design §6): a vendor-minted credential. The operator mints it and enters it,
then the write, the copies and the activation; with its operator step it never runs nightly. One
plan per key. On a marker leaf of the bootstrap tier the operator rotates the credential at its
source and confirms it, and the plan rewrites the marker key: the credential never enters
OpenBao."""

from collections.abc import Mapping

from secret_rotator.contract import MARKER_VALUE
from secret_rotator.model import Context, Step, StepFailed, value_name
from secret_rotator.opsteps import starts_with
from secret_rotator.plan import PlanContext, Target, tool_part

# The manual kind's marker leaves (catalog § rotator/).
MARKERS = "rotator/bootstrap/"
# rotation_args, each optional and describing every manual key of the leaf: the credential in
# words (`GitHub PAT`), where to mint it and with which scopes, and the prefix its values have.
ARGS = ("what", "mint", "prefix")


class Marker(Step):
    """Stages the marker key's new text for the kv.write: the marker text and when it rotated, so
    each rotation is a KV version. A key that does not hold the marker text is a credential the
    write would overwrite: the step fails there."""

    type = "manual.marker"
    silent = True

    def __init__(self, leaf: str, key: str):
        super().__init__(f"manual.marker:{key}", f"the new marker text of {key}")
        self.leaf = leaf
        self.key = key

    def run(self, ctx: Context) -> str:
        name = value_name(self.key)
        if ctx.staged(name) is None:
            current = ctx.bao.read(self.leaf)
            held = None if current is None else current.data.get(self.key)
            if held is None or not held.startswith(MARKER_VALUE):
                raise StepFailed(
                    f"{self.leaf}#{self.key} does not hold the marker text: it is no marker leaf"
                )
            ctx.stage(name, f"{MARKER_VALUE}; rotated {ctx.now.isoformat(timespec='seconds')}")
        return "staged"


def _marker(leaf: Target) -> bool:
    return leaf.leaf.startswith(MARKERS)


def _what(leaf: Target) -> str:
    return leaf.args.get("what") or ", ".join(leaf.keys)


class Manual:
    name = "manual"
    per_key = True

    def args_problems(self, args: Mapping) -> list[str]:
        problems = [f"{k}: not one of manual's {', '.join(ARGS)}" for k in args if k not in ARGS]
        return problems + [
            f"{k}: not a text"
            for k in ARGS
            if k in args and (not isinstance(args[k], str) or not args[k].strip())
        ]

    def ask(self, leaf: Target) -> str:
        return "rotate it at its source" if _marker(leaf) else f"paste a new {_what(leaf)}"

    def description(self, leaf: Target) -> str:
        if _marker(leaf):
            return (
                f"You rotate {_what(leaf)} at its source and confirm it. The tool records the "
                f"rotation on this marker leaf; the credential never enters OpenBao."
            )
        return f"You mint a new {_what(leaf)} and enter it. The tool {tool_part(leaf)}."

    def plan(self, leaf: Target, ctx: PlanContext) -> list[Step]:
        notes = leaf.meta.get("notes", "")
        if _marker(leaf):
            return [
                *(Marker(leaf.leaf, key) for key in leaf.keys),
                *ctx.steps.confirm(
                    "source",
                    f"Rotate {_what(leaf)} at its source",
                    notes,
                    irreversible=f"{_what(leaf)} was rotated at its source",
                ),
                *ctx.steps.write(),
                *ctx.steps.activate(),
            ]
        instruction = leaf.args.get("mint") or (
            f"Mint a new {', '.join(leaf.keys)} for {leaf.leaf} where it is issued."
        )
        prefix = leaf.args.get("prefix")
        return [
            *ctx.steps.credential(
                f"Mint a new {_what(leaf)} and enter it",
                instruction + (f"\nNotes: {notes}" if notes else ""),
                starts_with(prefix) if prefix else None,
            ),
            *ctx.steps.write(),
            *ctx.steps.activate(),
        ]
