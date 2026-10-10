"""The clusters a k8s-sa-token key's tokens are on, and how a step reaches each (ruling D1): prd
through the rotator's running client, under its own identity; dev with the dev write token the
KubeCoder catalog holds, at the apiserver and CA its kubeconfig names, the rotator having no
identity of its own there. dev is off by default: a step on it is on its VM (Step.vm), which the
plan starts when it is off (vmsteps)."""

import functools
import ssl
import urllib.request
from collections.abc import Callable

from secret_rotator.cluster import Cluster
from secret_rotator.kinds.k8s_sa_token.tokens import access
from secret_rotator.kube import TIMEOUT, Kube, KubeError, Unanswered
from secret_rotator.model import StepFailed
from secret_rotator.openbao import OpenBao
from secret_rotator.vmsteps import DEV_VM

PRD, DEV = "prd", "dev"
CLUSTERS = (DEV, PRD)  # in the order a plan mints, proves and deletes on them
# kubecoder-rw on dev, bound to edit cluster-wide: its kubeconfig names dev's apiserver by the
# address no cluster read yields (KubeCoder docs/operations/cluster-identity-remint.md).
DEV_WRITE = ("eso/prd/kubecoder/prd/catalog", "kubeconfig-dev-write")
# A request any client may make, which an apiserver that is up answers.
VERSION = "/version"


def held(bao: OpenBao, leaf: str, key: str) -> str:
    """What the leaf's key holds now."""
    version = bao.read(leaf)
    value = None if version is None else version.data.get(key)
    if not value:
        raise StepFailed(f"{leaf}#{key} holds nothing")
    return value


def connect(token: str, server: str, ca: str) -> Kube:
    """A client of the apiserver at server, whose certificate the CA (PEM) signs."""
    context = ssl.create_default_context(cadata=ca)
    opener = functools.partial(urllib.request.urlopen, context=context, timeout=TIMEOUT)
    return Kube(token, server, opener)


class Prd:
    """prd, through the rotator's running client: a switch of its token lasts the rest of the
    run."""

    name = PRD
    vm = None

    def __init__(self, cluster: Cluster):
        self.cluster = cluster

    def kube(self, bao: OpenBao) -> Kube:
        return self.cluster.kube

    def unanswered(self, bao: OpenBao) -> str | None:
        return None


class Dev:
    """dev, through a client of what the catalog's dev write kubeconfig holds when the step runs:
    the plan of that key reaches dev with the old token until its kv.write and with the new one
    after it, the old one again once a rollback has undone the write."""

    name = DEV
    vm = DEV_VM

    def __init__(self, connect: Callable[[str, str, str], Kube]):
        self.connect = connect  # a client from its token, apiserver and CA

    def kube(self, bao: OpenBao) -> Kube:
        leaf, key = DEV_WRITE
        server, ca, token = access(held(bao, leaf, key), DEV, f"{leaf}#{key}")
        return self.connect(token, server, ca)

    def unanswered(self, bao: OpenBao) -> str | None:
        """Why dev does not answer: no connection to its apiserver. A refusal, of the token or
        of TLS, is an answer."""
        kube = self.kube(bao)
        try:
            kube.call("GET", VERSION)
        except Unanswered as e:
            return f"{DEV} does not answer at {kube.addr}: {e}"
        except KubeError:
            return None
        return None
