"""Keycloak's REST API, as much of it as the keycloak-client kind uses: a realm's token endpoint,
where a service-account client logs in by the client_credentials grant, and the admin API's
clients and their secrets under that login's token."""

import functools
import http.client
import json
import ssl
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable

from secret_rotator.model import StepFailed

TIMEOUT = 30


class KeycloakError(StepFailed):
    """A refused request (status set) or a transport failure (status None). A StepFailed, so a
    step reports it by its one sentence, which carries Keycloak's own error and never a secret."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


def _said(raw: bytes) -> str:
    """What Keycloak's error answer says: its error_description, errorMessage or error."""
    try:
        doc = json.loads(raw)
    except ValueError:
        return ""
    if not isinstance(doc, dict):
        return ""
    said = doc.get("error_description") or doc.get("errorMessage") or doc.get("error")
    return f": {said}" if isinstance(said, str) else ""


class Keycloak:
    """One realm of a Keycloak, by the base URL it is served at."""

    def __init__(self, base: str, realm: str, opener: Callable | None = None):
        self.base = base.rstrip("/")
        self.realm = realm
        self.open = opener or functools.partial(
            urllib.request.urlopen, context=ssl.create_default_context(), timeout=TIMEOUT
        )
        self.token: str | None = None

    def _call(self, method: str, path: str, body: bytes | None, headers: dict[str, str]) -> object:
        """The JSON answer, None when it has no body; any status >= 400 is raised."""
        req = urllib.request.Request(self.base + path, method=method, data=body, headers=headers)
        try:
            with self.open(req) as resp:
                status, raw = resp.status, resp.read()
        except urllib.error.HTTPError as e:
            raise KeycloakError(
                f"{method} {path}: HTTP {e.code}{_said(e.read())}", e.code
            ) from None
        except urllib.error.URLError as e:
            raise KeycloakError(f"{method} {path}: transport error: {e.reason}") from None
        except (OSError, http.client.HTTPException) as e:
            raise KeycloakError(f"{method} {path}: transport error: {e!r}") from None
        try:
            return json.loads(raw) if raw else None
        except ValueError:
            raise KeycloakError(
                f"{method} {path}: HTTP {status}, not a JSON answer", status
            ) from None

    def login(self, client_id: str, secret: str) -> None:
        """Logs in as the client by its secret: client_credentials, a service account's grant."""
        form = {"grant_type": "client_credentials", "client_id": client_id, "client_secret": secret}
        doc = self._call(
            "POST",
            f"/realms/{urllib.parse.quote(self.realm, safe='')}/protocol/openid-connect/token",
            urllib.parse.urlencode(form).encode(),
            {"Content-Type": "application/x-www-form-urlencoded"},
        )
        self.token = doc["access_token"]

    def _admin(self, method: str, path: str, body: bytes | None = None) -> object:
        realm = urllib.parse.quote(self.realm, safe="")
        auth = {"Authorization": f"Bearer {self.token}"}
        return self._call(method, f"/admin/realms/{realm}{path}", body, auth)

    def client_uuid(self, client_id: str) -> str | None:
        """The id of the realm's client by its client id; None when the realm has no such one.
        The admin API matches clientId exactly."""
        found = self._admin("GET", f"/clients?{urllib.parse.urlencode({'clientId': client_id})}")
        return found[0]["id"] if found else None

    def regenerate(self, uuid: str) -> str:
        """A new secret for the client, which ends the one it held."""
        return self._admin("POST", f"/clients/{uuid}/client-secret", b"")["value"]

    def secret(self, uuid: str) -> str:
        """The secret the client holds now."""
        return self._admin("GET", f"/clients/{uuid}/client-secret")["value"]
