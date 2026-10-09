"""The k8s-sa-token kind (design §6): a new long-lived token for the ServiceAccount whose token a
key holds, bare or as the one user of a kubeconfig, minted on prd as a new
`kubernetes.io/service-account-token` Secret with the rotator's own identity; so the kind has no
counterpart. One plan per key: each of the KubeCoder catalog's kubeconfigs is a credential of its
own. Its plan mints the token, writes the key with only the token changed to the leaf and its
copies, syncs every ExternalSecret that reads them whatever the leaf's activate, activates, proves
that prd takes the new token as its ServiceAccount, and last deletes the Secret of the token it
replaced. The rotator's own token, iac/rotator-k8s-token, is a key of the kind: its plan switches
the running rotator to the new token before that delete."""

from collections.abc import Mapping

from secret_rotator.kinds.k8s_sa_token.steps import PRD, Delete, Mint, Prove
from secret_rotator.model import Step
from secret_rotator.plan import PlanContext, PlanError, Target, tool_part


class K8sSaToken:
    name = "k8s-sa-token"
    per_key = True

    def args_problems(self, args: Mapping) -> list[str]:
        return [f"{k}: k8s-sa-token takes no args" for k in args]

    def ask(self, leaf: Target) -> str:
        return "; ".join(leaf.confirms)

    def credential(self, leaf: Target) -> str:
        return "Kubernetes ServiceAccount token"

    def description(self, leaf: Target) -> str:
        confirm = " You confirm what only you can do." if leaf.confirms else ""
        return (
            f"The tool mints a new token on {PRD} for the ServiceAccount of the token the key "
            f"holds and {tool_part(leaf)}; in a kubeconfig only the token changes. Once every "
            f"ExternalSecret that reads the leaf has synced, it proves {PRD} takes the new token "
            f"and deletes the Secret of the token it replaced.{confirm}"
        )

    def plan(self, leaf: Target, ctx: PlanContext) -> list[Step]:
        if len(leaf.keys) != 1:
            raise PlanError(
                f"{leaf.leaf}: a k8s-sa-token plan rotates one key, not {len(leaf.keys)}"
            )
        key = leaf.keys[0]
        readers = [leaf.leaf, *dict.fromkeys(c.leaf for c in leaf.copies)]
        synced = ctx.steps.eso_sync_and_rollout([], readers)
        cluster = ctx.steps.cluster
        steps = [Mint(cluster, leaf.leaf, key), *ctx.steps.write(), *synced]
        planned = {step.id for step in steps}
        return [
            *steps,
            *(step for step in ctx.steps.activate() if step.id not in planned),
            Prove(cluster, leaf.leaf, key),
            Delete(cluster, leaf.leaf, key),
        ]
