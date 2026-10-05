"""Jenkins' REST API, as much of it as the rotator's Jenkins steps use, under the admin account's
API token from rotator/jenkins (ruling D4). Requests authenticated by an API token need no CSRF
crumb."""

import base64
import functools
import http.client
import json
import re
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from email.message import Message

ADDR = "https://jenkins.webathome.org"
TIMEOUT = 30
QUEUE_ITEM = re.compile(r"/queue/item/(\d+)/?$")


class JenkinsError(Exception):
    """A refused request (status set) or a transport failure (status None)."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


def job_path(job: str) -> str:
    """The URL path of a job by its full name: `YouTrack/YouTrackConfiguration` is
    /job/YouTrack/job/YouTrackConfiguration."""
    return "".join(f"/job/{urllib.parse.quote(part, safe='')}" for part in job.split("/"))


def credential_path(credential: str) -> str:
    """The URL path of a credential of the system store's global domain, by its id."""
    return (
        f"/credentials/store/system/domain/_/credential/{urllib.parse.quote(credential, safe='')}"
    )


class Jenkins:
    """The API client. Its sleep and clock pace the steps that wait on a build."""

    def __init__(
        self,
        addr: str = ADDR,
        opener: Callable | None = None,
        *,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.addr = addr.rstrip("/")
        self.open = opener or functools.partial(
            urllib.request.urlopen, context=ssl.create_default_context(), timeout=TIMEOUT
        )
        self.sleep = sleep
        self.clock = clock
        self.auth = ""

    def authenticate(self, user: str, token: str) -> None:
        self.auth = "Basic " + base64.b64encode(f"{user}:{token}".encode()).decode()

    def call(
        self, method: str, path: str, body: bytes | None = None, content_type: str = ""
    ) -> tuple[int, Message, bytes]:
        """The status, headers and body of the answer; any status >= 400 is raised."""
        headers = {"Authorization": self.auth}
        if content_type:
            headers["Content-Type"] = content_type
        req = urllib.request.Request(self.addr + path, method=method, data=body, headers=headers)
        try:
            with self.open(req) as resp:
                status, head, raw = resp.status, resp.headers, resp.read()
        except urllib.error.HTTPError as e:
            raise JenkinsError(f"{method} {path}: HTTP {e.code}", e.code) from None
        except urllib.error.URLError as e:
            raise JenkinsError(f"{method} {path}: transport error: {e.reason}") from None
        except (OSError, http.client.HTTPException) as e:
            raise JenkinsError(f"{method} {path}: transport error: {e!r}") from None
        return status, head, raw

    def json(self, path: str) -> dict:
        _, _, raw = self.call("GET", path)
        try:
            return json.loads(raw)
        except ValueError:
            raise JenkinsError(f"GET {path}: not a JSON answer") from None

    def trigger(self, job: str, params: Mapping[str, str]) -> int:
        """Queues a build of the job with its parameters; the queue item's number."""
        if params:
            path = f"{job_path(job)}/buildWithParameters?{urllib.parse.urlencode(params)}"
        else:
            path = f"{job_path(job)}/build"
        _, head, _ = self.call("POST", path, b"")
        found = QUEUE_ITEM.search(urllib.parse.urlsplit(head.get("Location") or "").path)
        if found is None:
            raise JenkinsError(f"POST {path}: the answer names no queue item")
        return int(found[1])

    def queued(self, item: int) -> dict:
        """The queue item: `executable.number` once its build started, `cancelled`, `why`."""
        return self.json(f"/queue/item/{item}/api/json?tree=cancelled,why,executable[number]")

    def build(self, job: str, number: int) -> dict:
        """The build: `building`, and `result` once it ended."""
        return self.json(f"{job_path(job)}/{number}/api/json?tree=building,result")

    def credential_xml(self, credential: str) -> str:
        """The credential's config.xml, as Jenkins gives it: every secret field redacted."""
        _, _, raw = self.call("GET", f"{credential_path(credential)}/config.xml")
        return raw.decode()

    def update_credential(self, credential: str, xml: str) -> None:
        path = f"{credential_path(credential)}/config.xml"
        self.call("POST", path, xml.encode(), "application/xml; charset=utf-8")
