"""Grafana's HTTP API, as much of it as the grafana-admin kind uses: GET /api/user, the user a
basic-auth login is, and PUT /api/admin/users/:id/password, by which a Grafana server admin sets a
user's password. Every request logs in by basic auth."""

import base64
import functools
import http.client
import json
import ssl
import urllib.error
import urllib.request
from collections.abc import Callable

from secret_rotator.model import StepFailed

TIMEOUT = 30
REFUSED = 401  # the status of a login Grafana refuses


class GrafanaError(StepFailed):
    """A refused request (status set) or a transport failure (status None). A StepFailed, so a
    step reports it by its one sentence, which carries Grafana's own message and never a
    password."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


def _said(raw: bytes) -> str:
    """What Grafana's error answer says: its message."""
    try:
        doc = json.loads(raw)
    except ValueError:
        return ""
    said = doc.get("message") if isinstance(doc, dict) else None
    return f": {said}" if isinstance(said, str) else ""


class Grafana:
    """A Grafana, by the base URL it is served at."""

    def __init__(self, base: str, opener: Callable | None = None):
        self.base = base.rstrip("/")
        self.open = opener or functools.partial(
            urllib.request.urlopen, context=ssl.create_default_context(), timeout=TIMEOUT
        )

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
            raise GrafanaError(f"{method} {path}: HTTP {e.code}{_said(e.read())}", e.code) from None
        except urllib.error.URLError as e:
            raise GrafanaError(f"{method} {path}: transport error: {e.reason}") from None
        except (OSError, http.client.HTTPException) as e:
            raise GrafanaError(f"{method} {path}: transport error: {e!r}") from None
        try:
            return json.loads(raw)
        except ValueError:
            raise GrafanaError(
                f"{method} {path}: HTTP {status}, not a JSON answer", status
            ) from None

    def user(self, login: str, password: str) -> dict:
        """The user the login is: its id, login and isGrafanaAdmin."""
        return self._call("GET", "/api/user", login, password)

    def set_password(self, login: str, password: str, user_id: int, new: str) -> None:
        """Sets the password of the user by its id, logged in as a Grafana server admin."""
        self._call(
            "PUT", f"/api/admin/users/{user_id}/password", login, password, {"password": new}
        )
