"""The random kind (design §6): the tool generates the new value — 32 random bytes, URL-safe (43
characters), unless rotation_args sets its length and charset — then the write, the copies and the
activation. It needs the operator only for a manual: activator."""

from collections.abc import Mapping

from secret_rotator.kvsteps import DEFAULT_LENGTH, URLSAFE
from secret_rotator.model import Step
from secret_rotator.plan import PlanContext, Target, tool_part

ARGS = ("length", "charset")


class Random:
    name = "random"
    per_key = False

    def args_problems(self, args: Mapping) -> list[str]:
        problems = [f"{k}: not one of random's {', '.join(ARGS)}" for k in args if k not in ARGS]
        length = args.get("length", DEFAULT_LENGTH)
        if not isinstance(length, int) or isinstance(length, bool) or length < 1:
            problems.append("length: not a whole number from 1")
        charset = args.get("charset", URLSAFE)
        if not isinstance(charset, str) or len(charset) < 2 or len(set(charset)) != len(charset):
            problems.append("charset: not two or more distinct characters")
        return problems

    def ask(self, leaf: Target) -> str:
        return "; ".join(leaf.confirms)

    def description(self, leaf: Target) -> str:
        length = leaf.args.get("length", DEFAULT_LENGTH)
        text = (
            f"The tool generates a new {length}-character {', '.join(leaf.keys)} and "
            f"{tool_part(leaf)}."
        )
        return text + (" You confirm what only you can do." if leaf.confirms else "")

    def plan(self, leaf: Target, ctx: PlanContext) -> list[Step]:
        length = leaf.args.get("length", DEFAULT_LENGTH)
        charset = leaf.args.get("charset", URLSAFE)
        generate = [
            step
            for key in leaf.keys
            for step in ctx.steps.generate(key, length=length, charset=charset)
        ]
        return [*generate, *ctx.steps.write(), *ctx.steps.activate()]
