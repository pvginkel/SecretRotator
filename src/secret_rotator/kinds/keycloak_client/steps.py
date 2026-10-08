"""The keycloak-client kind's own steps (design §4.2: custom steps are the plugin's):
keycloak.regenerate, through Keycloak's admin API as the realm's counterpart client, and
keycloak.openbao_oidc_config, through OpenBao's API with the rotator's token (its policy, design
§8). No detail or error they report carries a client secret."""

from secret_rotator.kinds.keycloak_client.keycloak import Keycloak, KeycloakError
from secret_rotator.model import Context, Step, StepFailed, not_landed, value_name

CLIENT_ID, CLIENT_SECRET = "client_id", "client_secret"
# The field of OpenBao's OIDC config that holds the client secret, which a read never gives back.
OIDC_SECRET = "oidc_client_secret"


class Regenerate(Step):
    """Has Keycloak regenerate a client's secret, stages the new one, and verifies it by re-reading
    the client's secret. The client is the args' client, else the leaf's own client_id; when the
    leaf holds a client_id the args' client must be it. The step logs in as the realm's counterpart
    client with the secret its leaf holds; on the plan of that leaf itself, once a new secret is
    staged, with that one, since the regenerate ended the one the leaf holds. A re-run with a new
    secret staged regenerates nothing: it re-reads that one.

    It has no undo: Keycloak ends the secret the client held when it makes the new one (design
    §4.5). On a run that finds nothing staged, every failure before the regenerate, and the
    regenerate refused (a 4xx answer), report that the step did not land."""

    type = "keycloak.regenerate"
    mutates = True

    def __init__(
        self, keycloak: Keycloak, leaf: str, key: str, client: str | None, counterpart: str
    ):
        self.who = f"client {client}" if client else "the leaf's client"
        super().__init__(
            "keycloak.regenerate",
            f"regenerate the secret of {self.who} in Keycloak realm {keycloak.realm}",
        )
        self.keycloak = keycloak
        self.leaf = leaf
        self.key = key  # the data key the new secret is staged for
        self.client = client
        self.counterpart = counterpart
        self.no_undo = f"Keycloak ended the secret {self.who} held when it made the new one"

    def _client_id(self, ctx: Context) -> str:
        version = ctx.bao.read(self.leaf)
        held = None if version is None else version.data.get(CLIENT_ID)
        if self.client is None:
            if not held:
                raise StepFailed(f"{self.leaf} holds no client_id, and its args name no client")
            return held
        if held and held != self.client:
            raise StepFailed(
                f"{self.leaf} holds client_id {held}, not {self.client}, the client its args name"
            )
        return self.client

    def _login(self, ctx: Context) -> None:
        version = ctx.bao.read(self.counterpart)
        if version is None:
            raise StepFailed(
                f"{self.counterpart} cannot be read: no such leaf, or its current version is "
                f"deleted"
            )
        if missing := [key for key in (CLIENT_ID, CLIENT_SECRET) if not version.data.get(key)]:
            raise StepFailed(f"{self.counterpart} has no {' or '.join(missing)}")
        client_id, secret = version.data[CLIENT_ID], version.data[CLIENT_SECRET]
        if self.leaf == self.counterpart:
            secret = ctx.staged(value_name(self.key)) or secret
        try:
            self.keycloak.login(client_id, secret)
        except KeycloakError as e:
            if e.status is None:
                raise
            raise StepFailed(
                f"Keycloak realm {self.keycloak.realm} refuses the login of {self.counterpart}'s "
                f"client {client_id}: {e}"
            ) from None

    def _uuid(self, client_id: str) -> str:
        uuid = self.keycloak.client_uuid(client_id)
        if uuid is None:
            raise StepFailed(f"Keycloak realm {self.keycloak.realm} has no client {client_id}")
        return uuid

    def run(self, ctx: Context) -> str:
        name = value_name(self.key)
        fresh = ctx.staged(name) is None
        try:
            client_id = self._client_id(ctx)
            self._login(ctx)
            uuid = self._uuid(client_id)
        except Exception as e:
            if fresh:
                raise not_landed(e) from e
            raise
        if fresh:
            try:
                secret = self.keycloak.regenerate(uuid)
            except KeycloakError as e:
                if e.status is not None and e.status < 500:
                    raise not_landed(e) from e
                raise
            ctx.stage(name, secret)
        if self.keycloak.secret(uuid) != ctx.staged(name):
            raise StepFailed(
                f"the re-read of client {client_id}'s secret is not the one Keycloak regenerated"
            )
        return f"client {client_id}: regenerated; the re-read holds the new secret"


class OpenBaoOidcConfig(Step):
    """Puts the new client secret into OpenBao's OIDC auth config and keeps every other field. A
    write replaces the whole config and a read never gives the secret back (Ansible
    roles/openbao/tasks/oidc.yml), so it writes each field it read back with the new secret, and
    verifies by a re-read that every one of them holds what it read. It has no undo: the secret the
    config held is the one the regenerate before it ended."""

    type = "keycloak.openbao_oidc_config"
    mutates = True

    def __init__(self, path: str, key: str):
        super().__init__(
            "keycloak.openbao_oidc_config", f"put the new client secret into OpenBao's {path}"
        )
        self.path = path
        self.key = key  # the data key whose new secret it writes
        self.no_undo = f"the client secret OpenBao's {path} held is the one Keycloak ended"

    def _read(self, ctx: Context) -> dict:
        status, doc = ctx.bao.call("GET", self.path)
        if status == 404:
            raise StepFailed(f"OpenBao has no {self.path}: its OIDC login is not set up")
        return doc["data"]

    def run(self, ctx: Context) -> str:
        secret = ctx.staged(value_name(self.key))
        if secret is None:
            raise StepFailed("no new client secret is staged")
        before = {k: v for k, v in self._read(ctx).items() if k != OIDC_SECRET}
        ctx.bao.call("POST", self.path, before | {OIDC_SECRET: secret})
        after = self._read(ctx)
        if changed := sorted(k for k, v in before.items() if after.get(k) != v):
            raise StepFailed(f"the re-read of {self.path} changed {', '.join(changed)}")
        return f"{OIDC_SECRET} written; its {len(before)} other fields kept"
