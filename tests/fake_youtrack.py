"""YouTrack's REST API over HTTP, as an opener for secret_rotator.youtrack.YouTrack: the issue
search, the projects and tags it pages through, an issue's creation, its comments and a rewrite
of its description."""

import io
import json
import re
import urllib.error
import urllib.parse

from secret_rotator.youtrack import ADDR

TOKEN = "SECRET-token-of-jeeves"
TAG = "Rotator Standing Card"
SEARCH = re.compile(r"project: ANS tag: \{(?P<tag>[^}]+)\} #Unresolved sort by: created asc")
PROJECTS = [{"id": "0-7", "shortName": "KC"}, {"id": "0-3", "shortName": "ANS"}]


class FakeResponse(io.BytesIO):
    def __init__(self, status, body):
        super().__init__(body)
        self.status = status


class FakeYouTrack:
    def __init__(self, tags=(TAG, "Operator Action")):
        self.tags = {name: f"6-{n}" for n, name in enumerate(tags)}
        # each: id, idReadable, summary, description, project, customFields, tags, resolved,
        # comments
        self.issues = []
        self.requests = []  # (method, path, query, body)
        self.down = False

    def __call__(self, req):
        url = urllib.parse.urlsplit(req.full_url)
        assert f"{url.scheme}://{url.netloc}" == ADDR, url
        method, path = req.get_method(), url.path
        query = dict(urllib.parse.parse_qsl(url.query))
        body = json.loads(req.data) if req.data else None
        self.requests.append((method, path, query, body))
        if self.down:
            raise urllib.error.URLError(ConnectionRefusedError("connection refused"))
        if req.get_header("Authorization") != f"Bearer {TOKEN}":
            return self.answer(401, {"error": "Unauthorized"})
        if (method, path) == ("GET", "/api/issues"):
            found = SEARCH.fullmatch(query["query"])
            assert found and query["fields"] == "id,idReadable,description", query
            issues = [i for i in self.issues if found["tag"] in i["tags"] and not i["resolved"]]
            return self.answer(200, [self.fields(i) for i in issues][: int(query["$top"])])
        if (method, path) == ("GET", "/api/admin/projects"):
            return self.page(PROJECTS, query)
        if (method, path) == ("GET", "/api/tags"):
            return self.page([{"id": i, "name": n} for n, i in self.tags.items()], query)
        if (method, path) == ("POST", "/api/issues"):
            return self.create(body)
        found = re.fullmatch(r"/api/issues/([^/]+)(/comments)?", path)
        assert method == "POST" and found, (method, path)
        issue = next((i for i in self.issues if i["id"] == found[1]), None)
        if issue is None:
            return self.answer(404, {"error": "Not Found", "error_description": "no such issue"})
        if found[2]:
            issue["comments"].append(body["text"])
        else:
            assert set(body) == {"description"}, body
            issue["description"] = body["description"]
        return self.answer(200, {"id": issue["id"], "$type": "Issue"})

    def page(self, entries, query):
        skip, top = int(query["$skip"]), int(query["$top"])
        return self.answer(200, entries[skip : skip + top])

    def create(self, body):
        assert body["project"] == {"id": "0-3"}, body["project"]
        assert all(t["$type"] == "Tag" for t in body["tags"]), body["tags"]
        names = {i: n for n, i in self.tags.items()}
        n = len(self.issues) + 101
        issue = {
            "id": f"2-{n}",
            "idReadable": f"ANS-{n}",
            "summary": body["summary"],
            "description": body["description"],
            "customFields": {f["name"]: f["value"]["name"] for f in body["customFields"]},
            "tags": [names[t["id"]] for t in body["tags"]],
            "resolved": False,
            "comments": [],
        }
        self.issues.append(issue)
        return self.answer(200, {"id": issue["id"], "idReadable": issue["idReadable"]})

    def card(self, description="", comments=()):
        """An open card with the tag, as an earlier run left it."""
        n = len(self.issues) + 101
        self.issues.append(
            {
                "id": f"2-{n}",
                "idReadable": f"ANS-{n}",
                "summary": "Secret rotation: findings and failed rotations",
                "description": description,
                "customFields": {"Type": "Task", "State": "New"},
                "tags": [TAG],
                "resolved": False,
                "comments": list(comments),
            }
        )
        return self.issues[-1]

    @staticmethod
    def fields(issue):
        return {"id": issue["id"], "idReadable": issue["idReadable"], "$type": "Issue"} | (
            {"description": issue["description"]} if issue["description"] else {}
        )

    def writes(self):
        return [r for r in self.requests if r[0] == "POST"]

    def open(self):
        return [i for i in self.issues if not i["resolved"]]

    @staticmethod
    def answer(status, doc):
        body = json.dumps(doc).encode()
        if status >= 400:
            raise urllib.error.HTTPError(ADDR, status, "err", {}, io.BytesIO(body))
        return FakeResponse(status, body)
