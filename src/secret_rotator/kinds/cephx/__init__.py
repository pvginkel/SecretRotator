"""The cephx kind (design §6): the Ceph entity and key a cluster's CSI drivers and the Argo CD
Terraform hook read. prd runs reef 18.2.0, which has no `auth rotate`, and a mounted kernel client
needs its key again at each monitor reconnect, so no rotation re-keys the entity the leaf holds
(slice 061 ruling D1): two entities with the same caps take turns (Site.pair). A plan gives the one
the leaf does not hold a new key and the caps of the one it holds, if no client uses it, writes its
name and key to the leaf, syncs every ExternalSecret that reads the leaf whatever the leaf's
activate, on dev also dev's own CSI readers, and proves the new key. It ends no key and restarts
nothing: CSI reads its Secret at each operation, the hook at each Job run. Ceph is reached through
the PVE nodes and the Ceph VMs' guest agent (ruling D2); a plan on dev starts srvk8sdev first when
it is off, and shuts it down again after (vmsteps)."""

from collections.abc import Callable, Mapping
from pathlib import Path

from secret_rotator.kinds.cephx.ceph import DEV, HOST_VARS, PRD, Ceph, Site
from secret_rotator.kinds.cephx.steps import KEY, USER, DevSync, Mint, Prove
from secret_rotator.kinds.k8s_sa_token.reach import Dev, connect
from secret_rotator.kube import Kube
from secret_rotator.model import Step
from secret_rotator.plan import PlanContext, PlanError, Target, tool_part

SITES: dict[str, Site] = {
    "shared/prd/ceph-csi": PRD,
    "shared/dev/ceph-csi": DEV,
}


class Cephx:
    name = "cephx"
    per_key = False

    def __init__(
        self,
        connect: Callable[[str, str, str], Kube] = connect,
        *,
        inventory: Path = HOST_VARS,
    ):
        self.connect = connect  # dev's client from its token, apiserver and CA
        self.inventory = inventory  # the host_vars a blocked rotation names hosts by

    def args_problems(self, args: Mapping) -> list[str]:
        return [f"{k}: cephx takes no args" for k in args]

    def ask(self, leaf: Target) -> str:
        return "; ".join(leaf.confirms)

    def credential(self, leaf: Target) -> str:
        return "Ceph client key"

    def description(self, leaf: Target) -> str:
        site = SITES.get(leaf.leaf)
        cluster = f" on {site.cluster}" if site else ""
        pair = f" between client.{' and client.'.join(site.pair)}" if site else ""
        readers = (
            f" It syncs {site.cluster}'s own readers with the {site.cluster} write token the "
            f"catalog holds."
            if site and site.readers
            else ""
        )
        vm = (
            f" It starts {site.vm} first when it is off, and shuts it down again after."
            if site and site.vm
            else ""
        )
        confirm = " You confirm what only you can do." if leaf.confirms else ""
        return (
            f"The leaf's Ceph client takes turns{pair}: the tool gives the one{cluster} the leaf "
            f"does not hold a new key and the caps of the one it holds, once no client uses it, "
            f"through the Ceph VMs' guest agent, and {tool_part(leaf)}. Once every ExternalSecret "
            f"that reads the leaf has synced, it proves the new key; it ends no key.{readers}{vm}"
            f"{confirm}"
        )

    def plan(self, leaf: Target, ctx: PlanContext) -> list[Step]:
        if leaf.keys != (USER, KEY):
            raise PlanError(
                f"{leaf.leaf}: a cephx plan rotates {USER} and {KEY} together, not "
                f"{', '.join(leaf.keys)}"
            )
        site = SITES.get(leaf.leaf)
        if site is None:
            raise PlanError(
                f"{leaf.leaf}: not the leaf of a Ceph cluster cephx reaches: {', '.join(SITES)}"
            )
        ceph = Ceph(site, ctx.steps.pve)
        readers = [leaf.leaf, *dict.fromkeys(c.leaf for c in leaf.copies)]
        steps = [
            Mint(ceph, leaf.leaf, self.inventory),
            *ctx.steps.write(),
            *ctx.steps.eso_sync_and_rollout([], readers),
            *(DevSync(Dev(self.connect), es) for es in site.readers),
        ]
        planned = {step.id for step in steps}
        return [
            *steps,
            *(step for step in ctx.steps.activate() if step.id not in planned),
            Prove(ceph, leaf.leaf),
        ]
