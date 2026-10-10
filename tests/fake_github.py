"""GitHub's REST API over HTTP, as an opener for secret_rotator.github.GitHub: repository hooks'
configs, which it gives with the secret masked; their pings, which it delivers once the fake clock,
which the client's sleep advances, has passed their lag; their deliveries, listed newest first a
page at a time with the next page's cursor in the Link header; and their redeliveries. A delivery
is signed with the hook's secret when GitHub makes it, and the hook's receiver answers 200 when
that is the secret it holds, else 401.

Its repositories each have one branch, main, a line of commits: a file's contents at the head, a
commit of a new text over the blob it held, answered 409 once the file is another, and how two
commits compare. Hooks take TOKEN, rotator/github's, and repositories CONTENTS_TOKEN, the terraform
kind's, as fine-grained tokens each scoped to its own."""

import base64
import datetime
import email.message
import hashlib
import io
import json
import re
import urllib.error
import urllib.parse
import uuid

from secret_rotator.github import ADDR, GitHub

TOKEN = "SECRET-github-token"
CREDENTIALS = {"token": TOKEN}  # rotator/github
CONTENTS_TOKEN = "SECRET-github-contents-token"  # rotator/terraform/credentials
REPO = "pvginkel/Fieldnotes"
HOOK = 682399688
SPEC = f"{REPO}/{HOOK}"
OLD = "SECRET-old-hook-secret"
MASK = "********"
CONFIG = {
    "content_type": "json",
    "insecure_ssl": "0",
    "url": "https://fieldnotes-hooks.webathome.org/api/webhook",
}
START = datetime.datetime(2026, 10, 5, 4, 0, tzinfo=datetime.UTC)  # the fake clock's 0
HOOKS = re.compile(
    r"/repos/([^/]+/[^/]+)/hooks/(\d+)/(config|pings|deliveries)(?:/(\d+)/attempts)?"
)
CONTENTS = re.compile(r"/repos/([^/]+/[^/]+)/contents/(.+)")
COMPARE = re.compile(r"/repos/([^/]+/[^/]+)/compare/([0-9a-f]+)\.\.\.([0-9a-f]+)")


class FakeResponse(io.BytesIO):
    def __init__(self, status, body, headers=None):
        super().__init__(body)
        self.status = status
        self.headers = email.message.Message()
        for name, value in (headers or {}).items():
            self.headers[name] = value


def stamp(at):
    return at.strftime("%Y-%m-%dT%H:%M:%SZ")


def blob(text):
    """A git object SHA of the text, as GitHub names a blob."""
    return hashlib.sha1(text.encode()).hexdigest()


class FakeGitHub:
    def __init__(self, *, ping_lag=4, page=100):
        self.now = 0.0
        self.ping_lag = ping_lag
        self.page = page  # the most deliveries a page holds
        # (repo, hook) -> its config but the secret, its secret, its deliveries in the order made
        self.hooks = {(REPO, HOOK): {"config": dict(CONFIG), "secret": OLD, "deliveries": []}}
        self.held = lambda: OLD  # the secret the hooks' receiver holds
        self.redelivery_signed = "current"  # or "original": the original attempt's signature
        self.serial = 0
        self.pending = []  # (due, repo, hook) of the pings asked for
        self.signed = {}  # delivery id -> the secret it was signed with
        self.requests = []  # (method, path, query, body)
        self.broken = {}  # (method, path) -> the OSError it raises
        self.refused = {}  # (method, path) -> the HTTP status it answers
        self.mangle = False  # a config PATCH also changes the hook's content_type
        # repo -> its main branch's commits, oldest first: {"sha", "message", "files"}
        self.repos = {}
        self.on_commit = []  # called with (repo, sha) after each commit
        self.racing = []  # called with the repo, each once, as a commit arrives: a concurrent push

    def github(self):
        return GitHub(opener=self, sleep=self.sleep, clock=self.clock)

    def clock(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds
        due = [p for p in self.pending if p[0] <= self.now]
        self.pending = [p for p in self.pending if p[0] > self.now]
        for _, repo, hook in due:
            self.deliver("ping", repo=repo, hook=hook)

    def at(self):
        return START + datetime.timedelta(seconds=self.now)

    def secret(self, repo=REPO, hook=HOOK):
        return self.hooks[repo, hook]["secret"]

    def deliver(self, event, *, repo=REPO, hook=HOOK, at=None, guid=None, signed=None):
        """GitHub makes a delivery of the event, signed with the hook's secret unless signed says
        otherwise, and the receiver answers it; the delivery."""
        signed = self.secret(repo, hook) if signed is None else signed
        code = 200 if signed == self.held() else 401
        self.serial += 1
        delivery = {
            "id": 90_000_000_000 + self.serial,
            "guid": guid or str(uuid.UUID(int=self.serial)),
            "delivered_at": stamp(at or self.at()),
            "redelivery": guid is not None,
            "duration": 0.12,
            "status": "OK" if code == 200 else f"Invalid HTTP Response: {code}",
            "status_code": code,
            "event": event,
            "action": None,
            "installation_id": None,
            "repository_id": 1,
        }
        self.signed[delivery["id"]] = signed
        self.hooks[repo, hook]["deliveries"].append(delivery)
        return delivery

    def attempts(self, guid, repo=REPO, hook=HOOK):
        """The status codes of a delivery's attempts, in the order made."""
        found = self.hooks[repo, hook]["deliveries"]
        return [d["status_code"] for d in found if d["guid"] == guid]

    def redelivered(self, repo=REPO, hook=HOOK):
        """The guids of the redeliveries, in the order made."""
        return [d["guid"] for d in self.hooks[repo, hook]["deliveries"] if d["redelivery"]]

    def repo(self, repo, files, *, sha):
        """A repository whose main holds the files, at its first commit."""
        self.repos[repo] = [{"sha": sha, "message": "initial", "files": dict(files)}]

    def head(self, repo):
        return self.repos[repo][-1]

    def file_at(self, repo, sha, path):
        """The file's text at the commit; None when it has none."""
        return next(c for c in self.repos[repo] if c["sha"] == sha)["files"].get(path)

    def push(self, repo, path, text, message="a push"):
        """A commit of the file's new text on main, as another writer's push; its SHA."""
        files = self.head(repo)["files"] | {path: text}
        commit = {"sha": blob(f"{len(self.repos[repo])}{message}{files}"), "message": message}
        self.repos[repo].append(commit | {"files": files})
        for event in self.on_commit:
            event(repo, commit["sha"])
        return commit["sha"]

    def __call__(self, req):
        url = urllib.parse.urlsplit(req.full_url)
        assert f"{url.scheme}://{url.netloc}" == ADDR, url
        method, path = req.get_method(), url.path
        query = dict(urllib.parse.parse_qsl(url.query))
        body = json.loads(req.data) if req.data else None
        self.requests.append((method, path, query, body))
        if (method, path) in self.broken:
            raise self.broken[method, path]
        if (method, path) in self.refused:
            return self.error(self.refused[method, path], "Refused")
        bearer = req.get_header("Authorization")
        for pattern, answer in ((CONTENTS, self.contents), (COMPARE, self.compare)):
            if found := pattern.fullmatch(path):
                if bearer != f"Bearer {CONTENTS_TOKEN}" or found[1] not in self.repos:
                    return self.error(404 if bearer else 401, "Not Found")
                return answer(method, found, query, body)
        if bearer != f"Bearer {TOKEN}":
            return self.error(401, "Bad credentials")
        found = HOOKS.fullmatch(path)
        if found is None or (found[1], int(found[2])) not in self.hooks:
            return self.error(404, "Not Found")
        repo, hook, what = found[1], int(found[2]), found[3]
        if what == "config":
            return self.config(repo, hook, method, body, req)
        if what == "pings":
            assert method == "POST"
            self.pending.append((self.now + self.ping_lag, repo, hook))
            return FakeResponse(204, b"")
        if found[4]:
            assert method == "POST"
            return self.redeliver(repo, hook, int(found[4]))
        assert method == "GET"
        return self.deliveries(repo, hook, path, query)

    def masked(self, repo, hook):
        h = self.hooks[repo, hook]
        return h["config"] | ({"secret": MASK} if h["secret"] else {})

    def config(self, repo, hook, method, body, req):
        if method == "GET":
            return self.json(self.masked(repo, hook))
        assert method == "PATCH" and req.get_header("Content-type") == "application/json"
        h = self.hooks[repo, hook]
        for name, value in body.items():
            if name == "secret":
                h["secret"] = value
            else:
                h["config"][name] = value
        if self.mangle:
            h["config"]["content_type"] = "form"
        return self.json(self.masked(repo, hook))

    def deliveries(self, repo, hook, path, query):
        made = self.hooks[repo, hook]["deliveries"]
        newest = sorted(made, key=lambda d: (d["delivered_at"], d["id"]), reverse=True)
        per_page = min(int(query.get("per_page", 30)), self.page)
        start = int(query.get("cursor", "v1_0").removeprefix("v1_"))
        page = newest[start : start + per_page]
        headers = {}
        if start + per_page < len(newest):
            cursor = f"v1_{start + per_page}"
            more = urllib.parse.urlencode({"per_page": per_page, "cursor": cursor})
            headers["Link"] = f'<{ADDR}{path}?{more}>; rel="next"'
        return FakeResponse(200, json.dumps(page).encode(), headers)

    def redeliver(self, repo, hook, delivery):
        made = self.hooks[repo, hook]["deliveries"]
        original = next((d for d in made if d["id"] == delivery), None)
        if original is None:
            return self.error(404, "Not Found")
        signed = self.signed[delivery] if self.redelivery_signed == "original" else None
        self.deliver(original["event"], repo=repo, hook=hook, guid=original["guid"], signed=signed)
        return FakeResponse(202, b"{}")

    def contents(self, method, found, query, body):
        repo, path = found[1], urllib.parse.unquote(found[2])
        if method == "GET":
            assert query == {"ref": "main"}, query
            text = self.head(repo)["files"].get(path)
            if text is None:
                return self.error(404, "Not Found")
            content = base64.b64encode(text.encode()).decode()
            return self.json({"type": "file", "path": path, "sha": blob(text), "content": content})
        assert method == "PUT" and body["branch"] == "main", (method, body)
        if self.racing:
            self.racing.pop(0)(repo)
        held = self.head(repo)["files"].get(path)
        if held is None or blob(held) != body["sha"]:
            return self.error(409, f"{path} does not match {body['sha']}")
        sha = self.push(repo, path, base64.b64decode(body["content"]).decode(), body["message"])
        return self.json(
            {"content": {"path": path, "sha": blob(self.file_at(repo, sha, path))}}
            | {"commit": {"sha": sha, "message": body["message"]}}
        )

    def compare(self, method, found, query, body):
        assert method == "GET", method
        shas = [c["sha"] for c in self.repos[found[1]]]
        base, head = found[2], found[3]
        if base not in shas or head not in shas:
            return self.error(404, "Not Found")
        at = shas.index(head) - shas.index(base)
        status = "identical" if at == 0 else "ahead" if at > 0 else "behind"
        return self.json({"status": status, "ahead_by": max(at, 0), "behind_by": max(-at, 0)})

    @staticmethod
    def json(doc):
        return FakeResponse(200, json.dumps(doc).encode())

    @staticmethod
    def error(status, message):
        body = json.dumps({"message": message, "status": str(status)}).encode()
        raise urllib.error.HTTPError(ADDR, status, "err", email.message.Message(), io.BytesIO(body))
