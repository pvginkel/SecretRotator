"""The terraform kind (design §6, §3.7): a credential Terraform mints in an app's Argo CD PreSync
hook, rotated by a new value of the keeper the hook's Terraform passes to the resource that mints
it. Its leaf is a marker leaf (catalog § rotator/): no credential passes through the rotator.

Its plan commits the keeper to config/<stage>/rotation.tfvars of the deploy repo, waits for the
Application's sync of a revision that contains the commit, proves that the Secret Terraform writes
changed, rewrites the marker, restarts every workload whose pod template reads that Secret, and
activates. Where none does, the sync judges the Application Healthy. With no operator step it runs
at night. Nothing undoes the commit, since the value it replaced mints a third credential: a plan
that fails past it stops for `secret-rotator run <leaf>`."""

import re
from collections.abc import Mapping

from secret_rotator.kinds.terraform.steps import CommitKeeper, Keeper, Made, ProveRemint
from secret_rotator.model import Step
from secret_rotator.plan import PlanContext, PlanError, Target

ARGS = ("repo", "path", "app", "keeper", "secret")
NAME = r"[a-z0-9]([-a-z0-9]*[a-z0-9])?"
FORMS = {
    "repo": (re.compile(r"[A-Za-z0-9-]+/[A-Za-z0-9_.-]+"), "<owner>/<repo>"),
    "app": (re.compile(NAME), "an Argo CD Application's name"),
    "keeper": (re.compile(r"[a-z0-9]([a-z0-9_-]*[a-z0-9])?"), "a keeper's name"),
    "secret": (re.compile(rf"{NAME}/[a-z0-9]([-.a-z0-9]*[a-z0-9])?"), "<namespace>/<name>"),
}
PATH = re.compile(r"[A-Za-z0-9_.-]+(/[A-Za-z0-9_.-]+)*")


def keeper_of(leaf: Target) -> Keeper:
    """The args of the plan's one key."""
    return Keeper.of(leaf.entries[leaf.keys[0]].args)


class Terraform:
    name = "terraform"
    per_key = False

    def __init__(self):
        # The commits this process made, by SHA, with the fingerprint of the Secret before each:
        # the proof's memory, held nowhere but in the process (ruling F1).
        self.made: dict[str, Made] = {}

    def args_problems(self, args: Mapping) -> list[str]:
        problems = [f"{k}: not one of terraform's {', '.join(ARGS)}" for k in args if k not in ARGS]
        for name, (pattern, form) in FORMS.items():
            value = args.get(name)
            if not isinstance(value, str) or not pattern.fullmatch(value):
                problems.append(f"{name}: not {form}")
        path = args.get("path", "")
        if path != "" and not (isinstance(path, str) and PATH.fullmatch(path)):
            problems.append("path: not a directory of the repo, nor empty for its root")
        return problems

    def ask(self, leaf: Target) -> str:
        return "; ".join(leaf.confirms)

    def credential(self, leaf: Target) -> str:
        return "Terraform-minted credential"

    def description(self, leaf: Target) -> str:
        keeper = keeper_of(leaf)
        where = f" ({keeper.path}/)" if keeper.path else ""
        activates = " and activates what reads it" if leaf.activates else ""
        confirm = " You confirm what only you can do." if leaf.confirms else ""
        return (
            f"The tool commits a new {keeper.name} keeper to {keeper.repo}{where}, waits for Argo "
            f"CD to sync {keeper.app}, whose PreSync hook's Terraform re-mints the credential into "
            f"Secret {keeper.secret}, and proves that the Secret changed. It records the rotation "
            f"on this marker leaf, restarts every workload that reads the Secret{activates}."
            f"{confirm}"
        )

    def plan(self, leaf: Target, ctx: PlanContext) -> list[Step]:
        if len(leaf.keys) != 1:
            raise PlanError(f"{leaf.leaf}: a terraform plan rotates one key, not {len(leaf.keys)}")
        keeper = keeper_of(leaf)
        cluster = ctx.steps.cluster
        if cluster is None:
            raise PlanError(
                f"{leaf.leaf}: its plan reads Argo Application {keeper.app} and Secret "
                f"{keeper.secret} on the cluster, which an offline plan without a snapshot does "
                f"not reach"
            )
        commit = CommitKeeper(ctx.steps.github, cluster, self.made, keeper)
        restarts = ctx.steps.rollout_readers(keeper.secret)
        (sync,) = ctx.steps.argocd_sync(keeper.app, commit, healthy=not restarts)
        return [
            *ctx.steps.marker(),
            commit,
            sync,
            ProveRemint(commit, sync),
            *ctx.steps.write(),
            *restarts,
            *ctx.steps.activate(),
        ]
