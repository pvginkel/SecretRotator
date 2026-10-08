"""Grafana 12 over HTTP, as an opener for the grafana-admin kind's Grafana: at the URL the kind
reaches it at and nowhere else, every request logged in by basic auth as a local user. GET
/api/user answers the user the login is; PUT /api/admin/users/:id/password sets a user's password
for a Grafana server admin. A refused login answers 401 as Grafana does (12.3.1's anonymous GET
/api/user, 2026-10-08) and counts as a failed login."""

import base64
import email.message
import io
import json
import re
import urllib.error
import urllib.parse

BASE = "http://grafana.home"
ADMIN = "admin"
PASSWORD = "SECRET-old-admin-password"
REFUSED = {
    "message": "Invalid username or password",
    "messageId": "password-auth.failed",
    "statusCode": 401,
    "traceID": "",
}


class FakeResponse(io.BytesIO):
    def __init__(self, status, body):
        super().__init__(body)
        self.status = status


class FakeGrafana:
    def __init__(self):
        # login -> {"id", "password", "admin"}
        self.users = {
            ADMIN: {"id": 1, "password": PASSWORD, "admin": True},
            "viewer": {"id": 2, "password": "SECRET-viewer", "admin": False},
        }
        self.requests = []  # (method, path, login)
        self.failed = 0  # logins refused
        self.broken = {}  # (method, path) -> the OSError it raises
        self.refused = {}  # (method, path) -> the HTTP status it answers
        # (method, path) -> the HTTP status it answers after the request took effect
        self.lost = {}
        self.before_put = lambda login, password: None  # each password set, before it is

    def password(self, login=ADMIN):
        return self.users[login]["password"]

    def puts(self):
        return [r for r in self.requests if r[0] == "PUT"]

    def __call__(self, req):
        url = urllib.parse.urlsplit(req.full_url)
        assert f"{url.scheme}://{url.netloc}" == BASE, req.full_url
        method, path = req.get_method(), url.path
        auth = req.get_header("Authorization") or ""
        assert auth.startswith("Basic "), auth
        login, _, password = base64.b64decode(auth.removeprefix("Basic ")).decode().partition(":")
        self.requests.append((method, path, login))
        if (method, path) in self.broken:
            raise self.broken[method, path]
        if (method, path) in self.refused:
            return self.error(self.refused[method, path], {"message": "refused"})
        user = self.users.get(login)
        if user is None or user["password"] != password:
            self.failed += 1
            return self.error(401, REFUSED)
        if (method, path) == ("GET", "/api/user"):
            answer = self.json(
                {"id": user["id"], "login": login, "isGrafanaAdmin": user["admin"], "orgId": 1}
            )
        elif found := re.fullmatch(r"/api/admin/users/(\d+)/password", path):
            assert method == "PUT", method
            answer = self.set(user, int(found[1]), req)
        else:
            answer = self.error(404, {"message": "Not found"})
        if (method, path) in self.lost:
            return self.error(self.lost[method, path], None)
        return answer

    def set(self, admin, user_id, req):
        if not admin["admin"]:
            return self.error(
                403,
                {
                    "accessErrorId": "ACE0000000000",
                    "message": "You'll need additional permissions to perform this action. "
                    "Permissions needed: users.password:write",
                    "title": "Access denied",
                },
            )
        assert req.get_header("Content-type") == "application/json"
        login = next((n for n, u in self.users.items() if u["id"] == user_id), None)
        if login is None:
            return self.error(404, {"message": "user not found"})
        new = json.loads(req.data)["password"]
        self.before_put(login, new)
        self.users[login]["password"] = new
        return self.json({"message": "User password updated"})

    @staticmethod
    def json(doc):
        return FakeResponse(200, json.dumps(doc).encode())

    @staticmethod
    def error(status, doc):
        body = b"" if doc is None else json.dumps(doc).encode()
        raise urllib.error.HTTPError("", status, "err", email.message.Message(), io.BytesIO(body))
