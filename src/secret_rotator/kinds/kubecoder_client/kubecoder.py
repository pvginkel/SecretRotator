"""KubeCoder's controller API, as much of it as the kubecoder-client kind uses (KubeCoder
docs/api/controller-api.md, Clients): GET /clients, the named clients and whether each is static or
minted, and POST /clients, which mints a client's credential and ends the one the client held. Any
named client may call either, with its credential as bearer."""

import functools
import http.client
import json
import ssl
import urllib.error
import urllib.request
from collections.abc import Callable

from secret_rotator.model import StepFailed

TIMEOUT = 30
REFUSED = 401  # the status of a bearer the controller does not take


class KubeCoderError(StepFailed):
    """A refused request (status set) or a transport failure (status None). A StepFailed, so a
    step reports it by its one sentence, which carries the controller's own title and never a
    credential."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


def _said(raw: bytes) -> str:
    """What the controller's problem+json answer says: its title, which alone tells its two 409s
    on the client routes apart."""
    try:
        doc = json.loads(raw)
    except ValueError:
        return ""
    said = doc.get("title") if isinstance(doc, dict) else None
    return f": {said}" if isinstance(said, str) else ""


class KubeCoder:
    """KubeCoder's controller, by the base URL it is served at."""

    def __init__(self, base: str, opener: Callable | None = None):
        self.base = base.rstrip("/")
        self.open = opener or functools.partial(
            urllib.request.urlopen, context=ssl.create_default_context(), timeout=TIMEOUT
        )

    def _call(self, method: str, path: str, bearer: str, body: object = None) -> object:
        """The JSON answer; any status >= 400 is raised."""
        headers = {"Authorization": f"Bearer {bearer}", "Accept": "application/json"}
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(self.base + path, method=method, data=data, headers=headers)
        try:
            with self.open(req) as resp:
                status, raw = resp.status, resp.read()
        except urllib.error.HTTPError as e:
            raise KubeCoderError(
                f"{method} {path}: HTTP {e.code}{_said(e.read())}", e.code
            ) from None
        except urllib.error.URLError as e:
            raise KubeCoderError(f"{method} {path}: transport error: {e.reason}") from None
        except (OSError, http.client.HTTPException) as e:
            raise KubeCoderError(f"{method} {path}: transport error: {e!r}") from None
        try:
            return json.loads(raw)
        except ValueError:
            raise KubeCoderError(
                f"{method} {path}: HTTP {status}, not a JSON answer", status
            ) from None

    def clients(self, bearer: str) -> dict[str, str]:
        """Each named client's source, static or minted, by its name."""
        doc = self._call("GET", "/clients", bearer)
        return {item["name"]: item["source"] for item in doc["items"]}

    def takes(self, bearer: str) -> bool:
        """Whether the controller takes the credential; any other failure than a refusal is
        raised."""
        try:
            self.clients(bearer)
        except KubeCoderError as e:
            if e.status != REFUSED:
                raise
            return False
        return True

    def mint(self, bearer: str, client: str) -> str:
        """A new credential for the client, which ends the one the client held."""
        return self._call("POST", "/clients", bearer, {"name": client})["credential"]
