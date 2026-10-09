"""The kubecoder-client kind (design §6): a new credential for a KubeCoder named client, minted
through the controller's client routes with the credential the leaf holds as bearer, since any
named client may mint; so the kind has no counterpart. Its plan mints the credential for the client
its args name, which ends the one the leaf holds, writes it to the leaf and its copies, activates
what reads it, and proves it. The consumer's calls to the controller fail from the mint until it is
activated (design §9, rollout windows)."""

import re
from collections.abc import Callable, Mapping

from secret_rotator.kinds.kubecoder_client.kubecoder import KubeCoder
from secret_rotator.kinds.kubecoder_client.steps import Mint, Prove
from secret_rotator.model import Step
from secret_rotator.plan import PlanContext, PlanError, Target, tool_part

# The address KubeCoder's controller is served at off the cluster: KubeCoderDeploy
# config/prd/values.yaml's serverName, with a homelab-ca certificate; http:// answers a redirect.
BASE = "https://kubecoder.home"
ARGS = ("client",)
# A client name as the controller stores it, normalised (KubeCoder docs/api/controller-api.md,
# POST /clients): a name in any other spelling would not be the one GET /clients lists.
CLIENT = re.compile(r"[a-z0-9][a-z0-9._-]{0,39}")


def client_of(leaf: Target) -> str:
    """The client of the plan's one key."""
    return leaf.entries[leaf.keys[0]].args["client"]


class KubeCoderClient:
    name = "kubecoder-client"
    per_key = False

    def __init__(self, opener: Callable | None = None):
        self.opener = opener  # the controller's HTTP opener; None: the real one

    def args_problems(self, args: Mapping) -> list[str]:
        problems = [
            f"{k}: not one of kubecoder-client's {', '.join(ARGS)}" for k in args if k not in ARGS
        ]
        client = args.get("client")
        if client is None:
            problems.append("client: missing; the KubeCoder client whose credential it is")
        elif not (isinstance(client, str) and CLIENT.fullmatch(client)):
            problems.append("client: not a KubeCoder client name as the controller stores it")
        return problems

    def ask(self, leaf: Target) -> str:
        return "; ".join(leaf.confirms)

    def credential(self, leaf: Target) -> str:
        return "KubeCoder client credential"

    def description(self, leaf: Target) -> str:
        confirm = " You confirm what only you can do." if leaf.confirms else ""
        return (
            f"KubeCoder's controller mints a new credential for client {client_of(leaf)}, asked "
            f"with the one the leaf holds, which ends that one. The tool {tool_part(leaf)} and "
            f"proves the new one.{confirm}"
        )

    def plan(self, leaf: Target, ctx: PlanContext) -> list[Step]:
        if len(leaf.keys) != 1:
            raise PlanError(
                f"{leaf.leaf}: a kubecoder-client plan rotates one key, not {len(leaf.keys)}"
            )
        key, client = leaf.keys[0], client_of(leaf)
        kubecoder = KubeCoder(BASE, self.opener)
        return [
            Mint(kubecoder, leaf.leaf, key, client),
            *ctx.steps.write(),
            *ctx.steps.activate(),
            Prove(kubecoder, leaf.leaf, key, client),
        ]
