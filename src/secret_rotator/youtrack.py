"""YouTrack's REST API, as much of it as the standing card uses (design §3.3), under Jeeves's
permanent token from rotator/youtrack; and the client the youtrack-token kind reaches YouTrack and
its Hub through. No request or error carries the token."""

import functools
import http.client
import json
import ssl
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass

ADDR = "https://issues.webathome.org"
TIMEOUT = 30
TOKEN = ("rotator/youtrack", "token")  # the leaf and key of Jeeves's token
PROJECT = "ANS"
PAGE = 500


class YouTrackError(Exception):
    """A refused request (status set) or a transport failure (status None)."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class Card:
    id: str  # the entity id the API addresses it by
    readable: str  # ANS-123
    description: str


def _message(raw: bytes) -> str:
    try:
        doc = json.loads(raw)
    except ValueError:
        return ""
    if not isinstance(doc, dict):
        return ""
    return doc.get("error_description") or doc.get("error") or ""


class YouTrack:
    def __init__(
        self, token: str | Callable[[], str], addr: str = ADDR, opener: Callable | None = None
    ):
        """token: the bearer token, or what reads it at each request."""
        self.addr = addr.rstrip("/")
        self.token = token
        self.open = opener or functools.partial(
            urllib.request.urlopen, context=ssl.create_default_context(), timeout=TIMEOUT
        )

    def call(
        self, method: str, path: str, body: dict | None = None, query: dict[str, str] | None = None
    ):
        """The JSON answer; any status >= 400 is raised."""
        url = self.addr + path + ("?" + urllib.parse.urlencode(query) if query else "")
        token = self.token() if callable(self.token) else self.token
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(
            url,
            method=method,
            data=None if body is None else json.dumps(body).encode(),
            headers=headers,
        )
        try:
            with self.open(req) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as e:
            why = _message(e.read())
            raise YouTrackError(
                f"{method} {path}: HTTP {e.code}" + (f": {why}" if why else ""), e.code
            ) from None
        except urllib.error.URLError as e:
            raise YouTrackError(f"{method} {path}: transport error: {e.reason}") from None
        except (OSError, http.client.HTTPException) as e:
            raise YouTrackError(f"{method} {path}: transport error: {e!r}") from None
        try:
            return json.loads(raw) if raw else None
        except ValueError:
            raise YouTrackError(f"{method} {path}: not a JSON answer") from None

    def _all(self, path: str, fields: str) -> list[dict]:
        found: list[dict] = []
        while True:
            query = {"fields": fields, "$top": str(PAGE), "$skip": str(len(found))}
            batch = self.call("GET", path, query=query)
            found += batch
            if len(batch) < PAGE:
                return found

    def open_card(self, tag: str) -> Card | None:
        """The oldest unresolved ANS issue with the tag; None when there is none."""
        query = {
            "query": f"project: {PROJECT} tag: {{{tag}}} #Unresolved sort by: created asc",
            "fields": "id,idReadable,description",
            "$top": "1",
        }
        found = self.call("GET", "/api/issues", query=query)
        if not found:
            return None
        issue = found[0]
        return Card(issue["id"], issue["idReadable"], issue.get("description") or "")

    def _named(self, path: str, fields: str, key: str, name: str, what: str) -> str:
        ids = [e["id"] for e in self._all(path, fields) if e.get(key) == name]
        if not ids:
            raise YouTrackError(f"YouTrack shows its token no {what} {name}")
        return ids[0]

    def create_card(self, tag: str, summary: str, description: str) -> Card:
        """A Task in ANS, its State New, with the tag."""
        project = self._named(
            "/api/admin/projects", "id,shortName", "shortName", PROJECT, "project"
        )
        tag_id = self._named("/api/tags", "id,name", "name", tag, "tag")
        body = {
            "project": {"id": project},
            "summary": summary,
            "description": description,
            "customFields": [
                {
                    "name": "Type",
                    "$type": "SingleEnumIssueCustomField",
                    "value": {"name": "Task", "$type": "EnumBundleElement"},
                },
                {
                    "name": "State",
                    "$type": "StateIssueCustomField",
                    "value": {"name": "New", "$type": "StateBundleElement"},
                },
            ],
            "tags": [{"id": tag_id, "$type": "Tag"}],
        }
        created = self.call("POST", "/api/issues", body, {"fields": "id,idReadable"})
        return Card(created["id"], created["idReadable"], description)

    def comment(self, card: Card, text: str) -> None:
        self.call("POST", f"/api/issues/{card.id}/comments", {"text": text}, {"fields": "id"})

    def describe(self, card: Card, description: str) -> None:
        """Rewrites the card's description."""
        self.call("POST", f"/api/issues/{card.id}", {"description": description}, {"fields": "id"})
