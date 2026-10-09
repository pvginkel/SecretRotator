"""The kubecoder-client kind's own steps (design §4.2: custom steps are the plugin's):
kubecoder_client.mint and kubecoder_client.prove, through KubeCoder's controller API with a named
client's credential as bearer: the mint with the one the leaf holds, the proof with the new one. No
detail or error they report carries a credential."""

from secret_rotator.kinds.kubecoder_client.kubecoder import REFUSED, KubeCoder, KubeCoderError
from secret_rotator.model import Context, Step, StepFailed, not_landed, value_name

MINTED = "minted"  # the source GET /clients gives a client that POST /clients mints for


def held(ctx: Context, leaf: str, key: str) -> str:
    """What the leaf's key holds now."""
    version = ctx.bao.read(leaf)
    value = None if version is None else version.data.get(key)
    if not value:
        raise StepFailed(f"{leaf}#{key} holds no credential")
    return value


def listed(kubecoder: KubeCoder, bearer: str, what: str, client: str) -> None:
    """Fails unless the controller takes the bearer, what in words, and lists the client as
    minted."""
    try:
        sources = kubecoder.clients(bearer)
    except KubeCoderError as e:
        if e.status != REFUSED:
            raise
        raise StepFailed(f"KubeCoder's controller refuses {what}") from None
    source = sources.get(client)
    if source is None:
        raise StepFailed(f"KubeCoder's controller has no client {client}")
    if source != MINTED:
        raise StepFailed(
            f"KubeCoder's client {client} is {source}, not {MINTED}: POST /clients mints no "
            f"credential for it"
        )


class Mint(Step):
    """Mints a new credential for the client through POST /clients, with the credential the leaf
    holds as bearer, and stages it. Before it mints, the controller must take the leaf's
    credential and list the client as minted. It verifies that the controller takes the new
    credential and refuses the leaf's, which the mint ended if it was the client's: the
    credential carries no client name. A re-run with a credential staged mints none: it verifies
    that one.

    It has no undo: the controller ends the credential the client held when it mints the new one.
    On a run that finds nothing staged, every failure before the mint, and the mint refused (a 4xx
    answer), report that the step did not land. A mint whose answer is lost has ended the leaf's
    credential and left the client with one no one holds; a re-run then fails on the leaf's."""

    type = "kubecoder_client.mint"
    mutates = True

    def __init__(self, kubecoder: KubeCoder, leaf: str, key: str, client: str):
        super().__init__(
            "kubecoder_client.mint", f"mint a new credential for KubeCoder client {client}"
        )
        self.kubecoder = kubecoder
        self.leaf = leaf
        self.key = key  # the data key the new credential is staged for
        self.client = client
        self.no_undo = (
            f"KubeCoder's controller ended the credential client {client} held when it minted the "
            f"new one"
        )

    def run(self, ctx: Context) -> str:
        name = value_name(self.key)
        fresh = ctx.staged(name) is None
        what = f"the credential {self.leaf}#{self.key} holds"
        try:
            old = held(ctx, self.leaf, self.key)
            if fresh:
                listed(self.kubecoder, old, what, self.client)
        except Exception as e:
            if fresh:
                raise not_landed(e) from e
            raise
        if fresh:
            try:
                credential = self.kubecoder.mint(old, self.client)
            except KubeCoderError as e:
                if e.status is not None and e.status < 500:
                    raise not_landed(e) from e
                raise
            ctx.stage(name, credential)
        listed(self.kubecoder, ctx.staged(name), "the new credential", self.client)
        if self.kubecoder.takes(old):
            raise StepFailed(
                f"KubeCoder's controller still takes {what}: it is not client {self.client}'s, "
                f"whose credential the mint ended"
            )
        return f"client {self.client}: minted; the controller refuses the credential it held"


class Prove(Step):
    """Proves the credential the leaf holds once its consumers read it: it must be the one the
    plan minted, and the controller must take it and list the client as minted."""

    type = "kubecoder_client.prove"
    silent = True

    def __init__(self, kubecoder: KubeCoder, leaf: str, key: str, client: str):
        super().__init__("kubecoder_client.prove", "prove the new credential")
        self.kubecoder = kubecoder
        self.leaf = leaf
        self.key = key
        self.client = client

    def run(self, ctx: Context) -> str:
        minted = ctx.staged(value_name(self.key))
        if minted is None:
            raise StepFailed("no new credential is staged")
        if held(ctx, self.leaf, self.key) != minted:
            raise StepFailed(f"{self.leaf}#{self.key} does not hold the credential the plan minted")
        listed(self.kubecoder, minted, "the new credential", self.client)
        return f"taken; client {self.client} is minted"
