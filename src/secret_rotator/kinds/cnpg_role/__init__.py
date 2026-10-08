"""The cnpg-role kind (design §6): a new password for a role CloudNativePG manages from a
passwordSecret. Its plan generates the password and writes it to the leaf. On a prd leaf it then
syncs every ExternalSecret that reads the leaf, whatever the leaf's activate, those of the CNPG
Cluster's namespace last, so every other reader's Secret holds the new password before CNPG can
apply it. It has CNPG apply it, logs in as the role with it, and only then activates. An eso/dev/
leaf's role is on the dev cluster, which the rotator does not reach: its plan is the KV write."""

import re
from collections.abc import Mapping

from secret_rotator.kinds.cnpg_role import postgres
from secret_rotator.kinds.cnpg_role.steps import Login, PostgresLogin, Reconcile
from secret_rotator.model import Step
from secret_rotator.plan import PlanContext, PlanError, Target, tool_part

ARGS = ("role",)
ROLE = re.compile(r"[a-z_][a-z0-9_]*")
DEV = "eso/dev/"
# The CNPG Cluster whose managed roles the prd leaves are, and the address its read-write primary
# is reached at: the postgres-tf-rw LoadBalancer Terraform uses (PostgresPasDeploy
# config/prd/values.yaml), not a port-forward.
NAMESPACE, CLUSTER = "postgres-pas-prd", "postgres"
HOST, PORT = "postgres-pas.home", 5432


def args_of(leaf: Target) -> Mapping:
    """The args of the plan's one key, password."""
    return leaf.entries[leaf.keys[0]].args


class CnpgRole:
    name = "cnpg-role"
    per_key = False

    def __init__(self, login: Login | None = None):
        self.login = login or postgres.login  # logs in as a role; None: to the real Postgres

    def args_problems(self, args: Mapping) -> list[str]:
        problems = [f"{k}: not one of cnpg-role's {', '.join(ARGS)}" for k in args if k not in ARGS]
        role = args.get("role")
        if "role" in args and not (isinstance(role, str) and ROLE.fullmatch(role)):
            problems.append("role: not a Postgres role name")
        return problems

    def ask(self, leaf: Target) -> str:
        return "; ".join(leaf.confirms)

    def description(self, leaf: Target) -> str:
        role = args_of(leaf).get("role")
        confirm = " You confirm what only you can do." if leaf.confirms else ""
        if role is None:
            return (
                f"The tool generates a new password and {tool_part(leaf)}: the role is on the dev "
                f"cluster, which the tool does not reach.{confirm}"
            )
        first = "Before it activates, every" if leaf.activates else "Every"
        return (
            f"The tool generates a new password for role {role} and {tool_part(leaf)}. {first} "
            f"ExternalSecret that reads the leaf syncs, CNPG applies the password to the role, "
            f"and the tool logs in as {role} with it.{confirm}"
        )

    def plan(self, leaf: Target, ctx: PlanContext) -> list[Step]:
        role, key = args_of(leaf).get("role"), leaf.keys[0]
        dev = leaf.leaf.startswith(DEV)
        if role is None and not dev:
            raise PlanError(
                f"{leaf.leaf}: its args name no role, and only an {DEV} leaf rotates as a KV "
                f"write alone"
            )
        if role is not None and dev:
            raise PlanError(
                f"{leaf.leaf}: its args name role {role}, on the dev cluster, which the rotator "
                f"does not reach"
            )
        written = [*ctx.steps.generate(key), *ctx.steps.write()]
        if dev:
            return [*written, *ctx.steps.activate()]
        readers = [leaf.leaf, *dict.fromkeys(c.leaf for c in leaf.copies)]
        syncs = ctx.steps.eso_sync_and_rollout([], readers)
        if not any(sync.es.namespace == NAMESPACE for sync in syncs):
            raise PlanError(
                f"{leaf.leaf}: no ExternalSecret in {NAMESPACE} references it, so CNPG Cluster "
                f"{CLUSTER} does not get the new password"
            )
        syncs.sort(key=lambda sync: sync.es.namespace == NAMESPACE)
        steps = [
            *written,
            *syncs,
            Reconcile(ctx.steps.cluster, NAMESPACE, CLUSTER, role),
            PostgresLogin(self.login, HOST, PORT, role, key),
        ]
        planned = {step.id for step in steps}
        return [*steps, *(step for step in ctx.steps.activate() if step.id not in planned)]
