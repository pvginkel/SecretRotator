"""OpenBao's HTTP API, as much of it as the rotator uses: srviac's iac image has no bao CLI."""

import functools
import http.client
import json
import ssl
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable

from secret_rotator.contract import MOUNT, is_working_leaf

# The listener port through the leader-tracking VIP (design §8). The 443 front door passes clients
# on as 127.0.0.1, which the rotator AppRole's secret_id_bound_cidrs refuses.
ADDR = "https://secrets.home:8200"
TIMEOUT = 30


class OpenBaoError(Exception):
    """A refused request (status set) or a transport failure (status None)."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class OpenBao:
    def __init__(self, addr: str = ADDR, token: str | None = None, opener: Callable | None = None):
        self.addr = addr.rstrip("/")
        self.token = token
        self.open = opener or functools.partial(
            urllib.request.urlopen, context=ssl.create_default_context(), timeout=TIMEOUT
        )

    def call(
        self,
        method: str,
        path: str,
        body: dict | None = None,
        query: dict[str, str] | None = None,
        content_type: str = "application/json",
    ) -> tuple[int, dict | None]:
        """The status and the JSON answer; a 404 is returned, any other status >= 400 raised."""
        url = f"{self.addr}/v1/{urllib.parse.quote(path)}"
        if query:
            url += "?" + urllib.parse.urlencode(query)
        headers = {"Content-Type": content_type}
        if self.token is not None:
            headers["X-Vault-Token"] = self.token
        req = urllib.request.Request(
            url,
            method=method,
            data=None if body is None else json.dumps(body).encode(),
            headers=headers,
        )
        try:
            with self.open(req) as resp:
                status, raw = resp.status, resp.read()
        except urllib.error.HTTPError as e:
            status, raw = e.code, e.read()
        except urllib.error.URLError as e:
            raise OpenBaoError(f"{method} {path}: transport error: {e.reason}") from None
        except (OSError, http.client.HTTPException) as e:
            raise OpenBaoError(f"{method} {path}: transport error: {e!r}") from None
        try:
            doc = json.loads(raw) if raw else None
        except ValueError:
            message = f"{method} {path}: HTTP {status}, not a JSON answer"
            raise OpenBaoError(message, status) from None
        if status >= 400 and status != 404:
            errors = "; ".join(" ".join(e.split()) for e in (doc or {}).get("errors", []))
            raise OpenBaoError(
                f"{method} {path}: HTTP {status}" + (f": {errors}" if errors else ""), status
            )
        return status, doc

    def login_approle(self, role_id: str, secret_id: str) -> None:
        _, doc = self.call(
            "POST", "auth/approle/login", {"role_id": role_id, "secret_id": secret_id}
        )
        self.token = doc["auth"]["client_token"]

    def leaves(self, prefix: str = "") -> list[str]:
        """Every leaf under the prefix, but the rotator's working leaves."""
        status, doc = self.call("LIST", f"{MOUNT}/metadata/{prefix}")
        found = []
        for name in [] if status == 404 else doc["data"]["keys"]:
            if is_working_leaf(prefix + name):
                continue
            if name.endswith("/"):
                found += self.leaves(prefix + name)
            else:
                found.append(prefix + name)
        return sorted(found)

    def metadata(self, leaf: str) -> dict[str, str] | None:
        """The leaf's custom_metadata; None when the store has no such leaf."""
        status, doc = self.call("GET", f"{MOUNT}/metadata/{leaf}")
        if status == 404:
            return None
        return doc["data"].get("custom_metadata") or {}

    def subkeys(self, leaf: str) -> set[str] | None:
        """The data key names of the current version, without their values; None when that
        version is deleted or destroyed, or the leaf does not exist."""
        status, doc = self.call("GET", f"{MOUNT}/subkeys/{leaf}", query={"depth": "1"})
        subkeys = None if status == 404 else doc["data"]["subkeys"]
        return None if subkeys is None else set(subkeys)

    def patch_metadata(self, leaf: str, custom: dict[str, str]) -> None:
        """A merge patch of custom_metadata: keys it does not name are kept (never a put)."""
        self.call(
            "PATCH",
            f"{MOUNT}/metadata/{leaf}",
            {"custom_metadata": custom},
            content_type="application/merge-patch+json",
        )

    def create(self, leaf: str, data: dict[str, str]) -> None:
        """Writes the first version of a leaf; refused when the leaf already exists."""
        self.call("POST", f"{MOUNT}/data/{leaf}", {"options": {"cas": 0}, "data": data})
