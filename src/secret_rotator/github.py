"""GitHub's REST API, as much of it as the rotator uses: a repository hook's config, its pings and
its deliveries for the github.webhook step, under the token rotator/github holds; a file's contents,
a commit of a new text over it, and how two commits compare for the terraform kind, under its own
token. No request or error carries the token or a hook's secret."""

import base64
import datetime
import functools
import http.client
import json
import re
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable

ADDR = "https://api.github.com"
TIMEOUT = 30
VERSION = "2022-11-28"  # the X-GitHub-Api-Version the client is written against
PAGE = 100  # deliveries per page, GitHub's maximum
NEXT = re.compile(r'<([^>]+)>;\s*rel="next"')


class GitHubError(Exception):
    """A refused request (status set) or a transport failure (status None)."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


def _message(raw: bytes) -> str:
    try:
        doc = json.loads(raw)
    except ValueError:
        return ""
    if not isinstance(doc, dict):
        return ""
    return doc.get("message") or ""


def delivered_at(delivery: dict) -> datetime.datetime:
    return datetime.datetime.fromisoformat(delivery["delivered_at"])


class GitHub:
    """The API client. Its sleep and clock pace the step that waits on a ping."""

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
        self.token = ""

    def authenticate(self, token: str) -> None:
        self.token = token

    def call(self, method: str, path: str, body: dict | None = None) -> tuple[object, str]:
        """The JSON answer, None when it has no body, and its Link header; any status >= 400 is
        raised. path may be a whole URL of the API, as a Link header gives it."""
        url = path if path.startswith(self.addr + "/") else self.addr + path
        shown = urllib.parse.urlsplit(url).path
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": VERSION,
            "User-Agent": "secret-rotator",
        }
        if body is not None:
            headers["Content-Type"] = "application/json"
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(url, method=method, data=data, headers=headers)
        try:
            with self.open(req) as resp:
                raw, link = resp.read(), resp.headers.get("Link") or ""
        except urllib.error.HTTPError as e:
            why = _message(e.read())
            raise GitHubError(
                f"{method} {shown}: HTTP {e.code}" + (f": {why}" if why else ""), e.code
            ) from None
        except urllib.error.URLError as e:
            raise GitHubError(f"{method} {shown}: transport error: {e.reason}") from None
        except (OSError, http.client.HTTPException) as e:
            raise GitHubError(f"{method} {shown}: transport error: {e!r}") from None
        try:
            return (json.loads(raw) if raw else None), link
        except ValueError:
            raise GitHubError(f"{method} {shown}: not a JSON answer") from None

    def hook_config(self, repo: str, hook: int) -> dict:
        """The hook's config: url, content_type, insecure_ssl, and secret masked when it has one."""
        doc, _ = self.call("GET", f"/repos/{repo}/hooks/{hook}/config")
        return doc

    def set_hook_config(self, repo: str, hook: int, config: dict) -> None:
        self.call("PATCH", f"/repos/{repo}/hooks/{hook}/config", config)

    def ping(self, repo: str, hook: int) -> None:
        """Asks GitHub to send the hook a ping, which it delivers asynchronously."""
        self.call("POST", f"/repos/{repo}/hooks/{hook}/pings")

    def deliveries(
        self, repo: str, hook: int, since: datetime.datetime | None = None
    ) -> list[dict]:
        """The hook's deliveries, newest first: the newest page's, or with since every one
        delivered at or after it. Each: id, guid, delivered_at, redelivery, status, status_code,
        event. GitHub lists them newest first, so the pages stop at the first that reaches back
        before since."""
        found: list[dict] = []
        path = f"/repos/{repo}/hooks/{hook}/deliveries?per_page={PAGE}"
        while True:
            page, link = self.call("GET", path)
            found += page
            more = NEXT.search(link)
            if since is None or not page or more is None or delivered_at(page[-1]) < since:
                break
            path = more[1]
        return found if since is None else [d for d in found if delivered_at(d) >= since]

    def redeliver(self, repo: str, hook: int, delivery: int) -> None:
        self.call("POST", f"/repos/{repo}/hooks/{hook}/deliveries/{delivery}/attempts")

    def contents(self, repo: str, path: str, branch: str) -> tuple[str, str] | None:
        """A file's text on the branch and its blob's SHA; None when the branch has no such file."""
        quoted = urllib.parse.quote(path)
        try:
            doc, _ = self.call("GET", f"/repos/{repo}/contents/{quoted}?ref={branch}")
        except GitHubError as e:
            if e.status == 404:
                return None
            raise
        return base64.b64decode(doc["content"]).decode(), doc["sha"]

    def commit_file(
        self, repo: str, path: str, text: str, *, blob: str, branch: str, message: str
    ) -> str:
        """Commits the file's new text to the branch, over the blob it held; the commit's SHA.
        GitHub answers 409 when the file is no longer that blob."""
        body = {
            "message": message,
            "content": base64.b64encode(text.encode()).decode(),
            "sha": blob,
            "branch": branch,
        }
        doc, _ = self.call("PUT", f"/repos/{repo}/contents/{urllib.parse.quote(path)}", body)
        return doc["commit"]["sha"]

    def compare(self, repo: str, base: str, head: str) -> str:
        """How head stands to base: identical, ahead (it has base), behind or diverged."""
        doc, _ = self.call("GET", f"/repos/{repo}/compare/{base}...{head}?per_page=1")
        return doc["status"]
