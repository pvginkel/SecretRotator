"""The k8s-sa-token kind (design §6): a new long-lived token for the ServiceAccount of each token a
key holds, one per cluster its args name, bare or in a kubeconfig, minted on that cluster as a new
`kubernetes.io/service-account-token` Secret: on prd with the rotator's own identity, on dev with
the dev write token the KubeCoder catalog holds (ruling D1); so the kind has no counterpart. One
plan per key: each of the KubeCoder catalog's kubeconfigs is a credential of its own. Its plan
mints the tokens, writes the key with only the tokens changed to the leaf and its copies, syncs
every ExternalSecret that reads them whatever the leaf's activate, activates, proves that each
cluster takes its new token, and last deletes the Secrets of the tokens it replaced. A plan on dev
starts srvk8sdev first when it is off, and shuts it down again after (vmsteps). The rotator's own
token, iac/rotator-k8s-token, is a key of the kind: its plan switches the running rotator to the
new token before that delete."""

from collections.abc import Callable, Mapping

from secret_rotator.kinds.k8s_sa_token.reach import CLUSTERS, DEV, Dev, Prd, connect
from secret_rotator.kinds.k8s_sa_token.steps import Delete, Mint, Prove
from secret_rotator.kube import Kube
from secret_rotator.model import Step
from secret_rotator.plan import PlanContext, PlanError, Target, tool_part

ARGS = ("clusters",)


def clusters_of(leaf: Target) -> tuple[str, ...]:
    """The clusters the tokens of the plan's one key are on, in the order the plan takes them."""
    named = leaf.entries[leaf.keys[0]].args["clusters"]
    return tuple(c for c in CLUSTERS if c in named)


class K8sSaToken:
    name = "k8s-sa-token"
    per_key = True

    def __init__(self, connect: Callable[[str, str, str], Kube] = connect):
        self.connect = connect  # dev's client from its token, apiserver and CA

    def args_problems(self, args: Mapping) -> list[str]:
        problems = [
            f"{k}: not one of k8s-sa-token's {', '.join(ARGS)}" for k in args if k not in ARGS
        ]
        clusters = args.get("clusters")
        if clusters is None:
            problems.append(
                f"clusters: missing; the clusters whose tokens the key holds, of "
                f"{' and '.join(CLUSTERS)}"
            )
        elif not (
            isinstance(clusters, list)
            and clusters
            and all(isinstance(c, str) and c in CLUSTERS for c in clusters)
            and len(set(clusters)) == len(clusters)
        ):
            problems.append(
                f"clusters: not a list of distinct clusters of {' and '.join(CLUSTERS)}"
            )
        return problems

    def ask(self, leaf: Target) -> str:
        return "; ".join(leaf.confirms)

    def credential(self, leaf: Target) -> str:
        return "Kubernetes ServiceAccount token"

    def description(self, leaf: Target) -> str:
        clusters = clusters_of(leaf)
        confirm = " You confirm what only you can do." if leaf.confirms else ""
        dev = (
            f" It reaches {DEV} with the {DEV} write token the catalog holds."
            if DEV in clusters
            else ""
        )
        if len(clusters) == 1:
            (cluster,) = clusters
            return (
                f"The tool mints a new token on {cluster} for the ServiceAccount of the token the "
                f"key holds and {tool_part(leaf)}; in a kubeconfig only the token changes. Once "
                f"every ExternalSecret that reads the leaf has synced, it proves {cluster} takes "
                f"the new token and deletes the Secret of the token it replaced.{dev}{confirm}"
            )
        return (
            f"The tool mints a new token on {' and on '.join(clusters)} for the ServiceAccount of "
            f"each token the key holds and {tool_part(leaf)}; in the kubeconfig only the tokens "
            f"change. Once every ExternalSecret that reads the leaf has synced, it proves each "
            f"cluster takes its new token and deletes the Secrets of the tokens it replaced."
            f"{dev}{confirm}"
        )

    def plan(self, leaf: Target, ctx: PlanContext) -> list[Step]:
        if len(leaf.keys) != 1:
            raise PlanError(
                f"{leaf.leaf}: a k8s-sa-token plan rotates one key, not {len(leaf.keys)}"
            )
        key, clusters = leaf.keys[0], clusters_of(leaf)
        readers = [leaf.leaf, *dict.fromkeys(c.leaf for c in leaf.copies)]
        synced = ctx.steps.eso_sync_and_rollout([], readers)
        reaches = [Dev(self.connect) if c == DEV else Prd(ctx.steps.cluster) for c in clusters]
        steps = [
            *(Mint(r, leaf.leaf, key, clusters) for r in reaches),
            *ctx.steps.write(),
            *synced,
        ]
        planned = {step.id for step in steps}
        return [
            *steps,
            *(step for step in ctx.steps.activate() if step.id not in planned),
            *(Prove(r, leaf.leaf, key, clusters) for r in reaches),
            *(Delete(r, leaf.leaf, key, clusters) for r in reaches),
        ]
