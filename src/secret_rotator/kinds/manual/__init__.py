"""The manual kind (design §6): a vendor-minted credential. The operator mints it and enters it,
then the write, the copies and the activation; with its operator step it never runs nightly. One
plan per key. On a marker leaf of the bootstrap tier the operator rotates the credential at its
source and confirms it, and the plan rewrites the marker key: the credential never enters
OpenBao.

A key's one arg, `type`, names its credential type: one document in types/ each, `<type>.md`, the
type's standard instructions for minting it under a YAML front matter of what the credential is,
the shape a pasted value has, and whether it expires. The instructions are the same for every key
of the type; the key's own notes show below them. For a type that expires the operator gives the
new credential's expiry with it, which the stamp writes as the key's expires_at."""

import re
from collections.abc import Mapping
from dataclasses import dataclass
from importlib import resources

import yaml

from secret_rotator.model import Step
from secret_rotator.opsteps import Shape
from secret_rotator.plan import PlanContext, Target, tool_part

# The manual kind's marker leaves (catalog § rotator/).
MARKERS = "rotator/bootstrap/"
DOCUMENT = re.compile(r"---\n(.*?)\n---\n(.*)", re.DOTALL)


@dataclass(frozen=True)
class CredentialType:
    """A credential type's document."""

    credential: str  # what it is, after `a new`: `GitHub personal access token`
    instructions: str
    shape: Shape
    expires: bool  # whether the credential carries an expiry


def load_type(text: str) -> CredentialType:
    front, body = DOCUMENT.fullmatch(text).groups()
    fields = yaml.safe_load(front)
    pattern = re.compile(fields["shape"]["pattern"], re.DOTALL)
    return CredentialType(
        fields["credential"],
        body.strip(),
        Shape(fields["shape"]["words"], lambda value: pattern.fullmatch(value) is not None),
        fields["expires"],
    )


TYPES = {
    doc.name.removesuffix(".md"): load_type(doc.read_text())
    for doc in (resources.files(__package__) / "types").iterdir()
    if doc.name.endswith(".md")
}


def _marker(leaf: Target) -> bool:
    return leaf.leaf.startswith(MARKERS)


def _entry(leaf: Target):
    """The entry of the plan's one key."""
    return leaf.entries[leaf.keys[0]]


def _type(leaf: Target) -> CredentialType | None:
    return TYPES.get(_entry(leaf).args.get("type"))


def _what(leaf: Target) -> str:
    known = _type(leaf)
    return known.credential if known else ", ".join(leaf.keys)


class Manual:
    name = "manual"
    per_key = True

    def args_problems(self, args: Mapping) -> list[str]:
        problems = [f"{k}: not manual's; its one arg is type" for k in args if k != "type"]
        if "type" in args and not (isinstance(args["type"], str) and args["type"] in TYPES):
            problems.append(f"type: {args['type']!r} is not a credential type manual documents")
        return problems

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
        notes = _entry(leaf).notes
        if _marker(leaf):
            return [
                *ctx.steps.marker(),
                *ctx.steps.confirm(
                    "source",
                    f"Rotate {_what(leaf)} at its source",
                    notes,
                    irreversible=f"{_what(leaf)} was rotated at its source",
                ),
                *ctx.steps.write(),
                *ctx.steps.activate(),
            ]
        known = _type(leaf)
        instruction = (
            known.instructions
            if known
            else f"Mint a new {', '.join(leaf.keys)} for {leaf.leaf} where it is issued."
        )
        return [
            *ctx.steps.credential(
                f"Mint a new {_what(leaf)} and enter it",
                instruction + (f"\n\nNotes: {notes}" if notes else ""),
                known.shape if known else None,
                expires=bool(known and known.expires),
            ),
            *ctx.steps.write(),
            *ctx.steps.activate(),
        ]
