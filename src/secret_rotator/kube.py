"""The prd cluster's Kubernetes API, as much of it as the rotator uses, under the token of the
secret-rotator ServiceAccount (k8s/cluster-identity.yaml): srviac holds no kubeconfig."""

import functools
import http.client
import json
import ssl
import time
import urllib.error
import urllib.request
from collections.abc import Callable

# The prd apiserver's keepalived VIP. Only to a client that asks for this name does the apiserver
# serve its step-ca leaf, which the iac image's trust store verifies; by address it serves a
# certificate of microk8s's own CA.
ADDR = "https://kubernetes-api.home:16443"
TIMEOUT = 30


class KubeError(Exception):
    """A refused request (status set) or a transport failure (status None)."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class Unanswered(KubeError):
    """No connection to the apiserver: it is off, or nothing listens. A TLS failure is no such
    error: the apiserver answered."""


class Kube:
    """The API client. Its sleep and clock pace the steps that wait on the cluster."""

    def __init__(
        self,
        token: str,
        addr: str = ADDR,
        opener: Callable | None = None,
        *,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.addr = addr.rstrip("/")
        # Each request's bearer: a k8s-sa-token plan of the rotator's own token switches it.
        self.token = token
        self.open = opener or functools.partial(
            urllib.request.urlopen, context=ssl.create_default_context(), timeout=TIMEOUT
        )
        self.sleep = sleep
        self.clock = clock

    def bearing(self, token: str) -> "Kube":
        """A client of the same apiserver under another token."""
        return Kube(token, self.addr, self.open, sleep=self.sleep, clock=self.clock)

    def send(
        self, method: str, path: str, data: bytes | None, content_type: str, accept: str
    ) -> tuple[int, bytes]:
        """The status and the answer as sent; a transport failure raised."""
        req = urllib.request.Request(
            self.addr + path,
            method=method,
            data=data,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Accept": accept,
                "Content-Type": content_type,
            },
        )
        try:
            with self.open(req) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()
        except urllib.error.URLError as e:
            error = KubeError if isinstance(e.reason, ssl.SSLError) else Unanswered
            raise error(f"{method} {path}: transport error: {e.reason}") from None
        except (OSError, http.client.HTTPException) as e:
            raise KubeError(f"{method} {path}: transport error: {e!r}") from None

    def call(
        self,
        method: str,
        path: str,
        body: dict | None = None,
        content_type: str = "application/json",
    ) -> tuple[int, dict | None]:
        """The status and the JSON answer; a 404 is returned, any other status >= 400 raised."""
        data = None if body is None else json.dumps(body).encode()
        status, raw = self.send(method, path, data, content_type, "application/json")
        try:
            doc = json.loads(raw) if raw else None
        except ValueError:
            raise KubeError(f"{method} {path}: HTTP {status}, not a JSON answer", status) from None
        if status >= 400 and status != 404:
            message = " ".join(str((doc or {}).get("message", "")).split())
            raise KubeError(
                f"{method} {path}: HTTP {status}" + (f": {message}" if message else ""), status
            )
        return status, doc

    def get(self, path: str) -> dict | None:
        """The object; None when there is no such object."""
        status, doc = self.call("GET", path)
        return None if status == 404 else doc

    def items(self, path: str) -> list[dict]:
        """The items of a list; KubeError when the API does not serve the list."""
        status, doc = self.call("GET", path)
        if status == 404:
            raise KubeError(f"GET {path}: HTTP 404: the API does not serve it", status)
        return doc["items"]

    def merge_patch(self, path: str, body: dict) -> dict:
        """A JSON merge patch of the object, which it returns as patched; KubeError when there is
        no such object."""
        status, doc = self.call("PATCH", path, body, content_type="application/merge-patch+json")
        if status == 404:
            raise KubeError(f"PATCH {path}: HTTP 404: no such object", status)
        return doc

    def put_text(self, path: str, text: str, content_type: str) -> None:
        """A PUT of a text body to a service behind the API's service proxy, which answers in its
        own format; KubeError for any status >= 400, with the answer's message: the apiserver's
        own refusal is a JSON Status, the service's is its text."""
        status, raw = self.send("PUT", path, text.encode(), content_type, "*/*")
        if status < 400:
            return
        answer = raw.decode(errors="replace")
        try:
            doc = json.loads(answer)
        except ValueError:
            doc = None
        if isinstance(doc, dict):
            answer = str(doc.get("message", ""))
        message = " ".join(answer.split())
        raise KubeError(f"PUT {path}: HTTP {status}" + (f": {message}" if message else ""), status)
