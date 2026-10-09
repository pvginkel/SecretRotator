"""What a k8s-sa-token key holds: one ServiceAccount token per cluster its args name, bare (one
cluster) or as a kubeconfig whose clusters, by name, are those, each with the token of its one
context's user; and the Secret a token names. A token Secret's token
(`kubernetes.io/service-account-token`) carries the legacy claims, which name its Secret; a bounded
token, from the TokenRequest API, names none and expires on its own."""

import base64
import binascii
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


def _config(value: str, what: str) -> dict | None:
    """The kubeconfig the value holds; None for a bare token. what: the key, in words."""
    if JWT.fullmatch(value.strip()):
        return None
    try:
        doc = yaml.safe_load(value)
    except yaml.YAMLError:
        doc = None
    if not (isinstance(doc, dict) and doc.get("kind") == "Config"):
        raise StepFailed(f"{what} holds neither a token nor a kubeconfig")
    return doc


def _named(doc: dict, section: str, what: str) -> dict[str, dict]:
    """The entries of a kubeconfig's clusters, users or contexts, each by its name."""
    field = section.removesuffix("s")
    entries = [e if isinstance(e, dict) else {} for e in doc.get(section) or []]
    names = [e.get("name") for e in entries]
    if not all(isinstance(n, str) for n in names) or len(set(names)) != len(names):
        raise StepFailed(f"{what} holds a kubeconfig whose {section} are not each named once")
    return {e["name"]: e.get(field) if isinstance(e.get(field), dict) else {} for e in entries}


def _token(doc: dict, cluster: str, what: str) -> str:
    """The token of the user of the kubeconfig's one context on the cluster."""
    users = [
        c.get("user") for c in _named(doc, "contexts", what).values() if c.get("cluster") == cluster
    ]
    if len(users) != 1:
        raise StepFailed(
            f"{what} holds a kubeconfig of {len(users)} contexts on cluster {cluster}, not one"
        )
    token = (_named(doc, "users", what).get(users[0]) or {}).get("token")
    if not (isinstance(token, str) and JWT.fullmatch(token)):
        raise StepFailed(f"the user of {what}'s context on cluster {cluster} has no token")
    return token


def tokens_of(value: str, clusters: tuple[str, ...], what: str) -> dict[str, str]:
    """The token the value holds for each of the clusters: the value itself for one cluster, or
    the token of each cluster of a kubeconfig, whose clusters must be those. what: the key, in
    words."""
    doc = _config(value, what)
    if doc is None:
        if len(clusters) != 1:
            raise StepFailed(
                f"{what} holds one bare token, not one per cluster of {', '.join(clusters)}"
            )
        return {clusters[0]: value.strip()}
    held = sorted(_named(doc, "clusters", what))
    if held != sorted(clusters):
        raise StepFailed(
            f"{what} holds a kubeconfig of cluster(s) {', '.join(held) or 'none'}, not "
            f"{', '.join(sorted(clusters))}"
        )
    return {cluster: _token(doc, cluster, what) for cluster in clusters}


def access(value: str, cluster: str, what: str) -> tuple[str, str, str]:
    """The apiserver a kubeconfig names for the cluster, its CA as PEM, and the cluster's token."""
    doc = _config(value, what)
    if doc is None:
        raise StepFailed(
            f"{what} holds a bare token, not a kubeconfig naming {cluster}'s apiserver"
        )
    entry = _named(doc, "clusters", what).get(cluster)
    if entry is None:
        raise StepFailed(f"{what} holds a kubeconfig without cluster {cluster}")
    server = entry.get("server")
    if not (isinstance(server, str) and server.startswith("https://")):
        raise StepFailed(f"{what}'s cluster {cluster} names no https server")
    try:
        ca = base64.b64decode(entry.get("certificate-authority-data") or "", validate=True)
        pem = ca.decode("ascii")
    except (binascii.Error, TypeError, UnicodeDecodeError):
        pem = ""
    if "-----BEGIN CERTIFICATE-----" not in pem:
        raise StepFailed(f"{what}'s cluster {cluster} has no certificate-authority-data")
    return server, pem, _token(doc, cluster, what)


def replaced(value: str, clusters: tuple[str, ...], cluster: str, new: str, what: str) -> str:
    """The value with the cluster's token replaced by new and nothing else changed: a kubeconfig
    keeps its servers, CAs, contexts and other tokens."""
    tokens = tokens_of(value, clusters, what)
    old = tokens[cluster]
    if old not in value:
        raise StepFailed(
            f"{what} does not hold its token on {cluster} verbatim, so it cannot be replaced"
        )
    result = value.replace(old, new)
    if tokens_of(result, clusters, what) != tokens | {cluster: new}:
        raise StepFailed(f"{what} with its token on {cluster} replaced does not hold the new one")
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
