"""What a k8s-sa-token key holds: one ServiceAccount token, bare or as the one user of a kubeconfig,
and the Secret that token names. A token Secret's token (`kubernetes.io/service-account-token`)
carries the legacy claims, which name its Secret; a bounded token, from the TokenRequest API, names
none and expires on its own."""

import base64
import json
import re
import secrets

import yaml

from secret_rotator.model import StepFailed

# A JWT: three base64url segments.
JWT = re.compile(r"[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+")
NAMESPACE = "kubernetes.io/serviceaccount/namespace"
SERVICE_ACCOUNT = "kubernetes.io/serviceaccount/service-account.name"
SECRET = "kubernetes.io/serviceaccount/secret.name"
# The characters and length of the suffix generateName gives a name (Kubernetes' utilrand.String).
SUFFIX = "bcdfghjklmnpqrstvwxz2456789"
SUFFIX_LENGTH = 5


def token_of(value: str, what: str) -> str:
    """The token the value holds: the value itself, or the token of a kubeconfig's one user. what:
    the key, in words."""
    if JWT.fullmatch(value.strip()):
        return value.strip()
    try:
        doc = yaml.safe_load(value)
    except yaml.YAMLError:
        doc = None
    if not (isinstance(doc, dict) and doc.get("kind") == "Config"):
        raise StepFailed(f"{what} holds neither a token nor a kubeconfig")
    users = doc.get("users") or []
    if len(users) != 1:
        raise StepFailed(f"{what} holds a kubeconfig of {len(users)} users, not one")
    token = ((users[0] or {}).get("user") or {}).get("token")
    if not (isinstance(token, str) and JWT.fullmatch(token)):
        raise StepFailed(f"the user of the kubeconfig {what} holds has no token")
    return token


def replaced(value: str, old: str, new: str, what: str) -> str:
    """The value with its token old replaced by new and nothing else changed: a kubeconfig keeps its
    server, CA and contexts."""
    if old not in value:
        raise StepFailed(f"{what} does not hold its token verbatim, so it cannot be replaced")
    result = value.replace(old, new)
    if token_of(result, what) != new:
        raise StepFailed(f"{what} with its token replaced does not hold the new one")
    return result


def claims(token: str, what: str) -> tuple[str, str, str]:
    """The namespace, ServiceAccount and Secret a token Secret's token names, unverified. what: the
    token, in words."""
    payload = token.split(".")[1]
    try:
        doc = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except ValueError:
        doc = None
    named = (
        [doc.get(c) for c in (NAMESPACE, SERVICE_ACCOUNT, SECRET)] if isinstance(doc, dict) else []
    )
    if not (named and all(isinstance(n, str) and n for n in named)):
        raise StepFailed(
            f"{what} names no token Secret: it is not a ServiceAccount token Secret's token"
        )
    namespace, account, secret = named
    return namespace, account, secret


def successor(account: str) -> str:
    """A name for a new token Secret of the ServiceAccount: `<account>-token-` and the suffix
    generateName would give it, as k8s/cluster-identity.yaml names the rotator's."""
    suffix = "".join(secrets.choice(SUFFIX) for _ in range(SUFFIX_LENGTH))
    return f"{account}-token-{suffix}"
