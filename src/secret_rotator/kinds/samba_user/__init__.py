"""The samba-user kind (design §6): a new password for one Samba account, which the Samba servers
take at start. Its one arg, `account`, says whose the account is. A person's (`personal`, the
default) is typed where the shares are mounted, so the person chooses it and enters it (050 F1). An
app's (`app`) the tool generates, so its plan runs nightly. Then the write and the activation,
which syncs every ExternalSecret that reads the leaf and restarts the servers before the clients;
a person's ends with the manual: activator that has them set it where they mount the shares.

The default is a person's because an entry annotated before the arg existed names none, and no
plan may generate a person's password."""

from collections.abc import Mapping

from secret_rotator.model import Step
from secret_rotator.plan import PlanContext, PlanError, Target, tool_part

PERSONAL = "personal"
APP = "app"
ACCOUNTS = (PERSONAL, APP)


def account(leaf: Target) -> str:
    return leaf.entries[leaf.keys[0]].args.get("account", PERSONAL)


class SambaUser:
    name = "samba-user"
    per_key = False

    def args_problems(self, args: Mapping) -> list[str]:
        problems = [
            f"{k}: not samba-user's; its one arg is account" for k in args if k != "account"
        ]
        if "account" in args and args["account"] not in ACCOUNTS:
            problems.append(f"account: {args['account']!r} is not one of {', '.join(ACCOUNTS)}")
        return problems

    def ask(self, leaf: Target) -> str:
        typed = ["type the new password"] if account(leaf) == PERSONAL else []
        return "; ".join([*typed, *leaf.confirms])

    def credential(self, leaf: Target) -> str:
        return "Samba password"

    def description(self, leaf: Target) -> str:
        confirm = " You confirm what only you can do." if leaf.confirms else ""
        if account(leaf) == APP:
            return f"The tool generates a new Samba password and {tool_part(leaf)}.{confirm}"
        return (
            f"You choose a new password for the Samba account {leaf.keys[0]} and type it. The tool "
            f"{tool_part(leaf)}.{confirm}"
        )

    def plan(self, leaf: Target, ctx: PlanContext) -> list[Step]:
        if len(leaf.keys) != 1:
            raise PlanError(f"{leaf.leaf}: a samba-user plan rotates one key, not {len(leaf.keys)}")
        key = leaf.keys[0]
        if account(leaf) == APP:
            new = ctx.steps.generate(key)
        else:
            new = ctx.steps.credential(
                f"Choose a new password for the Samba account {key} and enter it",
                f"The new password of the Samba account {key}, which you type where you mount "
                f"its shares: choose one you can type.",
            )
        return [*new, *ctx.steps.write(), *ctx.steps.activate()]
