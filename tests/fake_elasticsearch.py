"""Elasticsearch's security API over HTTP, as an opener for the elastic-user kind's Elasticsearch:
at the address the kind reaches it at and nowhere else, every request logged in by basic auth as a
native or reserved user. GET /_security/_authenticate answers the user the login is; POST
/_security/user/<user>/_password sets a user's password for the user itself or a superuser. A
refused login answers 401 with the body prd's Elasticsearch answered at elasticsearch.home on
2026-10-09, and counts as a failed login."""

import base64
import email.message
import io
import json
import re
import urllib.error
import urllib.parse

BASE = "http://elasticsearch.home"
SUPERUSER = "elastic"
PASSWORDS = {
    "elastic": "SECRET-old-elastic",
    "reader": "SECRET-old-reader",
    "kibana_system": "SECRET-old-kibana-system",
    "filebeat_writer": "SECRET-old-filebeat-writer",
    "iotsupport": "SECRET-old-iotsupport",
}
AUTHENTICATE = "/_security/_authenticate"
CHALLENGE = ['Basic realm="security", charset="UTF-8"', "ApiKey"]


def refusal(user, path):
    """The answer of Elasticsearch to a login it refuses."""
    reason = f"unable to authenticate user [{user}] for REST request [{path}]"
    cause = {
        "type": "security_exception",
        "reason": reason,
        "header": {"WWW-Authenticate": CHALLENGE},
    }
    return {"error": {"root_cause": [cause], **cause}, "status": 401}


def password_path(user):
    return f"/_security/user/{user}/_password"


class FakeResponse(io.BytesIO):
    def __init__(self, status, body):
        super().__init__(body)
        self.status = status


class FakeElasticsearch:
    def __init__(self):
        self.users = {user: {"password": password} for user, password in PASSWORDS.items()}
        self.requests = []  # (method, path, login)
        self.failed = 0  # logins refused
        self.broken = {}  # (method, path) -> the OSError it raises
        self.refused = {}  # (method, path) -> the HTTP status it answers
        # (method, path) -> the HTTP status it answers after the request took effect
        self.lost = {}
        self.before_post = lambda user, password: None  # each password set, before it is

    def password(self, user):
        return self.users[user]["password"]

    def posts(self):
        return [r for r in self.requests if r[0] == "POST"]

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
            status = self.refused[method, path]
            return self.error(status, {"error": {"type": "exception", "reason": "refused"}})
        user = self.users.get(login)
        if user is None or user["password"] != password:
            self.failed += 1
            return self.error(401, refusal(login, path))
        if (method, path) == ("GET", AUTHENTICATE):
            roles = ["superuser"] if login == SUPERUSER else [login]
            answer = self.json({"username": login, "roles": roles, "enabled": True})
        elif found := re.fullmatch(r"/_security/user/([^/]+)/_password", path):
            assert method == "POST", method
            answer = self.set(login, found[1], req)
        else:
            answer = self.error(404, {"error": "no handler found"})
        if (method, path) in self.lost:
            return self.error(self.lost[method, path], None)
        return answer

    def set(self, login, user, req):
        if login not in (SUPERUSER, user):
            reason = (
                f"action [cluster:admin/xpack/security/user/change_password] is unauthorized for "
                f"user [{login}]"
            )
            return self.error(403, {"error": {"type": "security_exception", "reason": reason}})
        assert req.get_header("Content-type") == "application/json"
        assert user in self.users, user
        new = json.loads(req.data)["password"]
        self.before_post(user, new)
        self.users[user]["password"] = new
        return self.json({})

    @staticmethod
    def json(doc):
        return FakeResponse(200, json.dumps(doc).encode())

    @staticmethod
    def error(status, doc):
        body = b"" if doc is None else json.dumps(doc).encode()
        raise urllib.error.HTTPError("", status, "err", email.message.Message(), io.BytesIO(body))
