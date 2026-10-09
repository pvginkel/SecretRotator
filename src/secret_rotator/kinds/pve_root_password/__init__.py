"""The pve-root-password kind (design §6, §4.6): a new PVE root@pam password, which is the Linux
root password of each node. The tool generates it and sets it as root's on every node, one
ssh.set_password each, before it writes it to the leaf and its copies and activates; last, the
operator stores it in RoboForm (052 D3). A rollback sets the password the leaf held before back
on every node."""

from collections.abc import Mapping

from secret_rotator.model import Step, value_name
from secret_rotator.plan import PlanContext, PlanError, Target, tool_part

# The PVE cluster's nodes: Ansible ansible/inventories/prd/hosts.yml's proxmox group.
NODES = ("pve", "pve1", "pve2")
USER = "root"
STORE = "Store it in RoboForm (PVE root@pam)"


def nodes() -> str:
    return f"{', '.join(NODES[:-1])} and {NODES[-1]}"


class PveRootPassword:
    name = "pve-root-password"
    per_key = False

    def args_problems(self, args: Mapping) -> list[str]:
        return [f"{k}: pve-root-password takes no args" for k in args]

    def ask(self, leaf: Target) -> str:
        return "; ".join(["store the new password in RoboForm", *leaf.confirms])

    def credential(self, leaf: Target) -> str:
        return "PVE root@pam password"

    def description(self, leaf: Target) -> str:
        confirm = " You confirm what only you can do." if leaf.confirms else ""
        return (
            f"The tool generates a new root@pam password, sets it as root's on the PVE nodes "
            f"{nodes()} over SSH and {tool_part(leaf)}. You store it in RoboForm.{confirm}"
        )

    def plan(self, leaf: Target, ctx: PlanContext) -> list[Step]:
        if len(leaf.keys) != 1:
            raise PlanError(
                f"{leaf.leaf}: a pve-root-password plan rotates one key, not {len(leaf.keys)}"
            )
        key = leaf.keys[0]
        return [
            *ctx.steps.generate(key),
            *ctx.steps.set_password(key, USER, NODES),
            *ctx.steps.write(),
            *ctx.steps.activate(),
            *ctx.steps.show(
                value_name(key),
                STORE,
                f"The new root@pam password of the PVE nodes {nodes()}: replace the one RoboForm "
                f"holds for PVE root@pam with it.",
            ),
        ]
