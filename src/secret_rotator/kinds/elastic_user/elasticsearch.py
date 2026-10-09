"""Elasticsearch's security API, as much of it as the elastic-user kind uses: GET
/_security/_authenticate, the user a basic-auth login is, which any user may call, and POST
/_security/user/<user>/_password, by which a user with manage_security sets a user's password.
Every request logs in by basic auth."""

import base64
import functools
import http.client
import json
import urllib.error
import urllib.request
from collections.abc import Callable

from secret_rotator.model import StepFailed

TIMEOUT = 30
REFUSED = 401  # the status of a login Elasticsearch refuses


class ElasticsearchError(StepFailed):
    """A refused request (status set) or a transport failure (status None). A StepFailed, so a
    step reports it by its one sentence, which carries Elasticsearch's own reason and never a
    password."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


def _said(raw: bytes) -> str:
    """What Elasticsearch's error answer says: its error's reason."""
    try:
        doc = json.loads(raw)
    except ValueError:
        return ""
    error = doc.get("error") if isinstance(doc, dict) else None
    said = error.get("reason") if isinstance(error, dict) else error
    return f": {said}" if isinstance(said, str) else ""


class Elasticsearch:
    """An Elasticsearch, by the base URL it is served at."""

    def __init__(self, base: str, opener: Callable | None = None):
        self.base = base.rstrip("/")
        self.open = opener or functools.partial(urllib.request.urlopen, timeout=TIMEOUT)

    def _call(
        self, method: str, path: str, login: str, password: str, body: object = None
    ) -> object:
        """The JSON answer; any status >= 400 is raised."""
        basic = base64.b64encode(f"{login}:{password}".encode()).decode()
        headers = {"Authorization": f"Basic {basic}", "Accept": "application/json"}
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(self.base + path, method=method, data=data, headers=headers)
        try:
            with self.open(req) as resp:
                status, raw = resp.status, resp.read()
        except urllib.error.HTTPError as e:
            raise ElasticsearchError(
                f"{method} {path}: HTTP {e.code}{_said(e.read())}", e.code
            ) from None
        except urllib.error.URLError as e:
            raise ElasticsearchError(f"{method} {path}: transport error: {e.reason}") from None
        except (OSError, http.client.HTTPException) as e:
            raise ElasticsearchError(f"{method} {path}: transport error: {e!r}") from None
        try:
            return json.loads(raw)
        except ValueError:
            raise ElasticsearchError(
                f"{method} {path}: HTTP {status}, not a JSON answer", status
            ) from None

    def authenticate(self, login: str, password: str) -> dict:
        """The user the login is: its username and roles."""
        return self._call("GET", "/_security/_authenticate", login, password)

    def takes(self, login: str, password: str) -> bool:
        """Whether Elasticsearch takes the login; any other failure than a refusal is raised."""
        try:
            self.authenticate(login, password)
        except ElasticsearchError as e:
            if e.status != REFUSED:
                raise
            return False
        return True

    def set_password(self, login: str, password: str, user: str, new: str) -> None:
        """Sets the user's password, logged in as a user who may: the user itself, or one with
        manage_security."""
        self._call("POST", f"/_security/user/{user}/_password", login, password, {"password": new})
