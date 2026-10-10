"""The rgw-admin kind (design §6): the S3 key of a cluster's RGW admin user, which adds its own
successor through RGW's admin API, on the storage backplane, with no shell (slice 061 ruling D2),
so the kind has no counterpart credential. RGW names an S3 key by its access key, so a new key is a
new pair: the plan rotates the leaf's access_key_id and secret_access_key together (ruling Q1). It
adds the key with the one the leaf holds and writes it to the leaf and its copies. It syncs every
ExternalSecret that reads them, whatever the leaf's activate, then activates, proves the new key
against RGW, and last removes the key the leaf held, and no other. The leaf names its cluster; a
plan on dev starts srvk8sdev first when it is off, and shuts it down again after (vmsteps)."""

import datetime
import time
from collections.abc import Callable, Mapping

from secret_rotator.kinds.rgw_admin.rgw import DEV, PRD, Gateway, Site, utcnow
from secret_rotator.kinds.rgw_admin.steps import ACCESS, SECRET, Delete, Mint, Prove
from secret_rotator.model import Step
from secret_rotator.plan import PlanContext, PlanError, Target, tool_part

SITES: dict[str, Site] = {
    "shared/prd/ceph-rgw/s3": PRD,
    "shared/dev/ceph-rgw/s3": DEV,
}


class RgwAdmin:
    name = "rgw-admin"
    per_key = False

    def __init__(
        self,
        opener: Callable | None = None,
        *,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        now: Callable[[], datetime.datetime] = utcnow,
    ):
        self.opener = opener  # what opens RGW's requests; None: the real one
        self.sleep = sleep
        self.clock = clock
        self.now = now

    def args_problems(self, args: Mapping) -> list[str]:
        return [f"{k}: rgw-admin takes no args" for k in args]

    def ask(self, leaf: Target) -> str:
        return "; ".join(leaf.confirms)

    def credential(self, leaf: Target) -> str:
        return "Ceph RGW admin S3 key"

    def description(self, leaf: Target) -> str:
        site = SITES.get(leaf.leaf)
        cluster = f" on {site.cluster}" if site else ""
        vm = (
            f" It starts {site.vm} first when it is off, and shuts it down again after."
            if site and site.vm
            else ""
        )
        confirm = " You confirm what only you can do." if leaf.confirms else ""
        return (
            f"The tool adds a new S3 key to the RGW admin user{cluster} with the one the leaf "
            f"holds, through RGW's admin API, and {tool_part(leaf)}. Once every ExternalSecret "
            f"that reads the leaf has synced, it proves the new key and removes the one the leaf "
            f"held.{vm}{confirm}"
        )

    def plan(self, leaf: Target, ctx: PlanContext) -> list[Step]:
        if leaf.keys != (ACCESS, SECRET):
            raise PlanError(
                f"{leaf.leaf}: an rgw-admin plan rotates {ACCESS} and {SECRET} together, not "
                f"{', '.join(leaf.keys)}"
            )
        site = SITES.get(leaf.leaf)
        if site is None:
            raise PlanError(
                f"{leaf.leaf}: not the leaf of an RGW admin user rgw-admin reaches: "
                f"{', '.join(SITES)}"
            )
        gateway = Gateway(site, self.opener, sleep=self.sleep, clock=self.clock, now=self.now)
        readers = [leaf.leaf, *dict.fromkeys(c.leaf for c in leaf.copies)]
        steps = [
            Mint(gateway, leaf.leaf),
            *ctx.steps.write(),
            *ctx.steps.eso_sync_and_rollout([], readers),
        ]
        planned = {step.id for step in steps}
        return [
            *steps,
            *(step for step in ctx.steps.activate() if step.id not in planned),
            Prove(gateway, leaf.leaf),
            Delete(gateway, leaf.leaf),
        ]
