"""GitHub's REST API over HTTP, as an opener for secret_rotator.github.GitHub: repository hooks'
configs, which it gives with the secret masked; their pings, which it delivers once the fake clock,
which the client's sleep advances, has passed their lag; their deliveries, listed newest first a
page at a time with the next page's cursor in the Link header; and their redeliveries. A delivery
is signed with the hook's secret when GitHub makes it, and the hook's receiver answers 200 when
that is the secret it holds, else 401."""

import datetime
import email.message
import io
import json
import re
import urllib.error
import urllib.parse
import uuid

from secret_rotator.github import ADDR, GitHub

TOKEN = "SECRET-github-token"
CREDENTIALS = {"token": TOKEN}  # rotator/github
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


class FakeResponse(io.BytesIO):
    def __init__(self, status, body, headers=None):
        super().__init__(body)
        self.status = status
        self.headers = email.message.Message()
        for name, value in (headers or {}).items():
            self.headers[name] = value


def stamp(at):
    return at.strftime("%Y-%m-%dT%H:%M:%SZ")


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
        if req.get_header("Authorization") != f"Bearer {TOKEN}":
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

    @staticmethod
    def json(doc):
        return FakeResponse(200, json.dumps(doc).encode())

    @staticmethod
    def error(status, message):
        body = json.dumps({"message": message, "status": str(status)}).encode()
        raise urllib.error.HTTPError(ADDR, status, "err", email.message.Message(), io.BytesIO(body))
