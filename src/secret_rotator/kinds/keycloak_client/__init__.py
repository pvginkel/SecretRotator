"""The keycloak-client kind (design §6): a new secret for a Keycloak client. Its plan has Keycloak
regenerate the secret, which ends the one the client held, writes the new one to the leaf and its
copies, puts it into OpenBao's OIDC config where the args' target names it, and activates what
reads it. The consumer is down from the regenerate until it is activated (design §9, rollout
windows). The kind reaches a realm as its counterpart client, a service-account client with
manage-clients whose leaf is rotator/keycloak-client/<realm>, itself rotated by this kind."""

import re
from collections.abc import Callable, Mapping

from secret_rotator.kinds.keycloak_client.keycloak import Keycloak
from secret_rotator.kinds.keycloak_client.steps import OpenBaoOidcConfig, Regenerate
from secret_rotator.model import Step
from secret_rotator.plan import PlanContext, Target, tool_part

ARGS = ("realm", "client", "target")
# The base URL each realm is served at: the one place the kind reaches Keycloak from, with no
# proxy and no fallback. keycloak-dev.home answers HTTPS with another app; ANS-9 moves it to HTTPS.
REALMS = {"homelab": "https://auth.ginbov.nl", "homelab-dev": "http://keycloak-dev.home"}
# What an args' target may name: OpenBao's own OIDC login (catalog § rotator/).
TARGETS = ("auth/oidc/config",)
CLIENT = re.compile(r"\S+")


def counterpart(realm: str) -> str:
    """The leaf of the realm's counterpart client: its client_id and client_secret."""
    return f"rotator/keycloak-client/{realm}"


def args_of(leaf: Target) -> Mapping:
    """The args of the plan's one key, client_secret."""
    return leaf.entries[leaf.keys[0]].args


def _who(args: Mapping) -> str:
    if "client" in args:
        return f"client {args['client']}"
    return "the client the leaf's client_id names"


class KeycloakClient:
    name = "keycloak-client"
    per_key = False

    def __init__(self, opener: Callable | None = None):
        self.opener = opener  # Keycloak's HTTP opener; None: the real one

    def args_problems(self, args: Mapping) -> list[str]:
        problems = [
            f"{k}: not one of keycloak-client's {', '.join(ARGS)}" for k in args if k not in ARGS
        ]
        realm = args.get("realm")
        if not (isinstance(realm, str) and realm in REALMS):
            problems.append(f"realm: not {' or '.join(REALMS)}")
        client = args.get("client")
        if "client" in args and not (isinstance(client, str) and CLIENT.fullmatch(client)):
            problems.append("client: not a client id")
        if "target" in args and args["target"] not in TARGETS:
            problems.append(f"target: not {', '.join(TARGETS)}")
        return problems

    def ask(self, leaf: Target) -> str:
        return "; ".join(leaf.confirms)

    def description(self, leaf: Target) -> str:
        args = args_of(leaf)
        target = f" and puts it into OpenBao's {args['target']}" if "target" in args else ""
        confirm = " You confirm what only you can do." if leaf.confirms else ""
        return (
            f"Keycloak regenerates the secret of {_who(args)} in realm {args['realm']}, which "
            f"ends the old one. The tool {tool_part(leaf)}{target}.{confirm}"
        )

    def plan(self, leaf: Target, ctx: PlanContext) -> list[Step]:
        args = args_of(leaf)
        realm, key = args["realm"], leaf.keys[0]
        keycloak = Keycloak(REALMS[realm], realm, self.opener)
        target = [OpenBaoOidcConfig(args["target"], key)] if "target" in args else []
        return [
            Regenerate(keycloak, leaf.leaf, key, args.get("client"), counterpart(realm)),
            *ctx.steps.write(),
            *target,
            *ctx.steps.activate(),
        ]
