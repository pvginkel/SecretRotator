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
        self.token = token
        self.open = opener or functools.partial(
            urllib.request.urlopen, context=ssl.create_default_context(), timeout=TIMEOUT
        )
        self.sleep = sleep
        self.clock = clock

    def call(
        self,
        method: str,
        path: str,
        body: dict | None = None,
        content_type: str = "application/json",
    ) -> tuple[int, dict | None]:
        """The status and the JSON answer; a 404 is returned, any other status >= 400 raised."""
        req = urllib.request.Request(
            self.addr + path,
            method=method,
            data=None if body is None else json.dumps(body).encode(),
            headers={
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/json",
                "Content-Type": content_type,
            },
        )
        try:
            with self.open(req) as resp:
                status, raw = resp.status, resp.read()
        except urllib.error.HTTPError as e:
            status, raw = e.code, e.read()
        except urllib.error.URLError as e:
            raise KubeError(f"{method} {path}: transport error: {e.reason}") from None
        except (OSError, http.client.HTTPException) as e:
            raise KubeError(f"{method} {path}: transport error: {e!r}") from None
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
