"""OpenBao's HTTP API, as much of it as the rotator uses: srviac's iac image has no bao CLI."""

import functools
import http.client
import json
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass

from secret_rotator.contract import MOUNT, is_working_leaf

# The listener port through the leader-tracking VIP (design §8). The 443 front door passes clients
# on as 127.0.0.1, which the rotator AppRole's secret_id_bound_cidrs refuses.
ADDR = "https://secrets.home:8200"
TIMEOUT = 30
# A client logged in by AppRole logs in again this many seconds before its token's lease ends: the
# rotator's token lives 1 h from login and cannot renew itself (its policy has no token paths).
RELOGIN_MARGIN = 300


class OpenBaoError(Exception):
    """A refused request (status set) or a transport failure (status None)."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class Version:
    """One version of a leaf's data."""

    number: int
    data: dict[str, str]


class OpenBao:
    def __init__(
        self,
        addr: str = ADDR,
        token: str | None = None,
        opener: Callable | None = None,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.addr = addr.rstrip("/")
        self.token = token
        self.open = opener or functools.partial(
            urllib.request.urlopen, context=ssl.create_default_context(), timeout=TIMEOUT
        )
        self.clock = clock
        # The leaf whose role_id and secret_id the client logs in with again when its token's
        # lease ends; None: it never logs in again.
        self.credential_leaf: str | None = None
        # Those of the last login, or of the leaf as this client last wrote it: a token that has
        # ended cannot read the leaf.
        self._credentials: tuple[str, str] | None = None
        self.expires: float | None = None  # when the token's lease ends, by clock
        self._relogging = False

    def call(
        self,
        method: str,
        path: str,
        body: dict | None = None,
        query: dict[str, str] | None = None,
        content_type: str = "application/json",
    ) -> tuple[int, dict | None]:
        """The status and the JSON answer; a 404 is returned, any other status >= 400 raised."""
        self._fresh()
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

    def _fresh(self) -> None:
        """Logs in again when the token's lease is about to end, or has ended while nothing was
        asked: with the leaf's credentials while the token can still read them, else with those in
        hand. The requests that read them do not log in again themselves."""
        if (
            self.credential_leaf is None
            or self.expires is None
            or self._relogging
            or self.clock() < self.expires - RELOGIN_MARGIN
        ):
            return
        self._relogging = True
        try:
            ended = self.clock() >= self.expires
            self.login_approle(*(self._credentials if ended else self._stored_credentials()))
        finally:
            self._relogging = False

    def _stored_credentials(self) -> tuple[str, str]:
        leaf = self.credential_leaf
        return self.value(leaf, "role_id"), self.value(leaf, "secret_id")

    def _wrote(self, leaf: str) -> None:
        """A rotation of the client's own AppRole writes the new secret_id to the credential leaf
        before it destroys the old one."""
        if leaf == self.credential_leaf:
            self._credentials = self._stored_credentials()

    def login_approle(self, role_id: str, secret_id: str) -> None:
        _, doc = self.call(
            "POST", "auth/approle/login", {"role_id": role_id, "secret_id": secret_id}
        )
        self.token = doc["auth"]["client_token"]
        self._credentials = role_id, secret_id
        lease = doc["auth"].get("lease_duration") or 0
        self.expires = self.clock() + lease if lease else None

    def leaves(self, prefix: str = "") -> list[str]:
        """Every leaf under the prefix. The rotator's working leaves are left out unless the
        prefix is among them: leaves("rotator/staging/") lists the staging leaves."""
        status, doc = self.call("LIST", f"{MOUNT}/metadata/{prefix}")
        found = []
        for name in [] if status == 404 else doc["data"]["keys"]:
            if is_working_leaf(prefix + name) and not is_working_leaf(prefix):
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

    def patch_metadata(self, leaf: str, custom: dict[str, str | None]) -> None:
        """A merge patch of custom_metadata: keys it does not name are kept (never a put), a None
        value removes its key. Refused when the store has no such leaf."""
        path = f"{MOUNT}/metadata/{leaf}"
        status, _ = self.call(
            "PATCH", path, {"custom_metadata": custom}, content_type="application/merge-patch+json"
        )
        if status == 404:
            raise OpenBaoError(f"PATCH {path}: HTTP 404: no leaf {leaf}", status)

    def read(self, leaf: str, version: int | None = None) -> Version | None:
        """A version of the leaf's data, the current one by default; None when the leaf does not
        exist or that version is deleted or destroyed."""
        query = None if version is None else {"version": str(version)}
        status, doc = self.call("GET", f"{MOUNT}/data/{leaf}", query=query)
        if status == 404:
            return None
        return Version(doc["data"]["metadata"]["version"], doc["data"]["data"])

    def write(self, leaf: str, data: dict[str, str], cas: int | None = None) -> int:
        """Writes the leaf's whole data as a new version, whose number it returns. With cas, only
        while the current version is cas (0: the leaf does not exist yet)."""
        body: dict = {"data": data}
        if cas is not None:
            body["options"] = {"cas": cas}
        _, doc = self.call("POST", f"{MOUNT}/data/{leaf}", body)
        self._wrote(leaf)
        return doc["data"]["version"]

    def create(self, leaf: str, data: dict[str, str]) -> None:
        """Writes the first version of a leaf; refused when the leaf already exists."""
        self.write(leaf, data, cas=0)

    def patch(self, leaf: str, data: dict[str, str | None], cas: int) -> int:
        """A KV v2 merge patch of the leaf's data, only while its current version is cas: keys it
        does not name are kept, a None value removes its key. Returns the new version's number."""
        path = f"{MOUNT}/data/{leaf}"
        status, doc = self.call(
            "PATCH",
            path,
            {"data": data, "options": {"cas": cas}},
            content_type="application/merge-patch+json",
        )
        if status == 404:
            raise OpenBaoError(f"PATCH {path}: HTTP 404: no leaf {leaf}", status)
        self._wrote(leaf)
        return doc["data"]["version"]

    def value(self, leaf: str, key: str) -> str:
        """One key of the leaf's current version; refused as a 404 when there is none."""
        version = self.read(leaf)
        if version is None or not version.data.get(key):
            raise OpenBaoError(
                f"{leaf}#{key} cannot be read: no such leaf, or no such key in its current version",
                404,
            )
        return version.data[key]

    def destroy(self, leaf: str) -> None:
        """Deletes the leaf with every version and its metadata."""
        self.call("DELETE", f"{MOUNT}/metadata/{leaf}")
