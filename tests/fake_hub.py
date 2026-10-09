"""YouTrack's /api/users/me and its built-in Hub's /users/me and permanent tokens over HTTP, as an
opener for secret_rotator.youtrack.YouTrack: users by Hub id, each token with its owner, name,
scope and value, perm:<login>.<name>.<secret> as YouTrack 2026.2 makes them, and a bearer taken
only by the services its token's scope names. An admin lists, mints and revokes any user's tokens;
any other user only their own. A mint's answer carries the new token's value, as Hub's
documentation has it."""

import base64
import io
import json
import re
import urllib.error
import urllib.parse

from secret_rotator.youtrack import ADDR

YOUTRACK = "5e9f1c2a-youtrack"  # YouTrack's service id in Hub
HUB = "0-0-0-0-0"  # Hub's own
PREFIX = "/hub/api/rest"
TOKENS = re.compile(rf"{PREFIX}/users/([^/]+)/permanenttokens(?:/([^/]+))?")


def b64(text):
    return base64.b64encode(text.encode()).decode()


def value_of(login, name, secret):
    """A permanent token's value as YouTrack makes it."""
    return f"perm:{b64(login)}.{b64(name)}.{secret}"


class FakeResponse(io.BytesIO):
    def __init__(self, status, body):
        super().__init__(body)
        self.status = status


class FakeHub:
    def __init__(self):
        self.users = {}  # id -> {"login", "admin"}
        self.tokens = {}  # id -> {"user", "name", "scope", "value"}
        self.requests = []  # (method, path, query, body)
        self.bearers = []  # each request's bearer token
        self.refused = {}  # (method, route) -> status; routes: me, hub-me, tokens, token
        self.broken = {}  # (method, route) -> the exception the transport raises
        self.lost = set()  # (method, route) whose answer is lost after it is done
        self.after = {}  # (method, route) -> called once Hub has answered such a request
        self.kept = set()  # ids of tokens whose revoke Hub answers but does not do
        self.before_revoke = None  # called with a token's id as its revoke is done
        self.mint_answer = lambda made: made  # what a mint answers, from what it would
        self.minted = 0
        self.top = None  # the largest page Hub answers; None: as asked

    def user(self, id, login, *, admin=False):
        self.users[id] = {"login": login, "admin": admin}
        return id

    def token(self, user, name, scope=(YOUTRACK,), secret=None):
        """A token of the user, made by hand; its value."""
        n = len(self.tokens) + 1
        value = value_of(self.users[user]["login"], name, secret or f"SECRET-hand-made-{n}")
        self.tokens[f"t-{n}"] = {"user": user, "name": name, "scope": list(scope), "value": value}
        return value

    def owned(self, user):
        """(name, value) of the user's tokens."""
        return sorted((t["name"], t["value"]) for t in self.tokens.values() if t["user"] == user)

    def values(self):
        return {t["value"] for t in self.tokens.values()}

    def takes(self, value, service=YOUTRACK):
        """Whether the service takes the value as bearer."""
        return any(t["value"] == value and service in t["scope"] for t in self.tokens.values())

    def __call__(self, req):
        url = urllib.parse.urlsplit(req.full_url)
        assert f"{url.scheme}://{url.netloc}" == ADDR, url
        method, path = req.get_method(), url.path
        query = dict(urllib.parse.parse_qsl(url.query))
        body = json.loads(req.data) if req.data else None
        bearer = (req.get_header("Authorization") or "").removeprefix("Bearer ")
        self.requests.append((method, path, query, body))
        self.bearers.append(bearer)
        route = self.route(path)
        if (method, route) in self.broken:
            raise self.broken[method, route]
        if (method, route) in self.refused:
            return self.answer(self.refused[method, route], {"error": "refused"})
        caller = next((t for t in self.tokens.values() if t["value"] == bearer), None)
        service = YOUTRACK if route == "me" else HUB
        if caller is None or service not in caller["scope"]:
            granted = "" if caller is None else f"Token is granted for {caller['scope']} and "
            return self.answer(
                401,
                {"error": "Unauthorized", "error_description": f"{granted}can't be used here"},
            )
        found = self.handle(method, route, path, query, body, caller)
        if (method, route) in self.after:
            self.after[method, route]()
        if (method, route) in self.lost:
            raise TimeoutError("timed out")
        return found

    @staticmethod
    def route(path):
        if path == "/api/users/me":
            return "me"
        if path == f"{PREFIX}/users/me":
            return "hub-me"
        found = TOKENS.fullmatch(path)
        assert found, path
        return "token" if found[2] else "tokens"

    def handle(self, method, route, path, query, body, caller):
        me = self.users[caller["user"]]
        if route == "me":
            assert (method, query) == ("GET", {"fields": "login,ringId"}), (method, query)
            return self.answer(200, {"login": me["login"], "ringId": caller["user"]})
        if route == "hub-me":
            assert (method, query) == ("GET", {"fields": "id,login"}), (method, query)
            return self.answer(200, {"id": caller["user"], "login": me["login"]})
        user, token = (urllib.parse.unquote(p) if p else p for p in TOKENS.fullmatch(path).groups())
        if user not in self.users:
            return self.answer(404, {"error": "Not Found", "error_description": "no such user"})
        if not me["admin"] and user != caller["user"]:
            return self.answer(403, {"error": "Forbidden"})
        if route == "tokens" and method == "GET":
            return self.page(user, query)
        if route == "tokens":
            assert method == "POST" and query == {"fields": "id,token"}, (method, query)
            return self.mint(user, body)
        assert method == "DELETE", method
        held = self.tokens.get(token)
        if held is None or held["user"] != user:
            return self.answer(404, {"error": "Not Found", "error_description": "no such token"})
        if self.before_revoke:
            self.before_revoke(token)
        if token not in self.kept:
            del self.tokens[token]
        return FakeResponse(200, b"")

    def page(self, user, query):
        assert query["fields"] == "id,name,scope(id)", query
        skip, top = int(query["$skip"]), int(query["$top"])
        top = min(top, self.top or top)
        mine = [
            {
                "id": id,
                "name": t["name"],
                "scope": [{"id": s, "$type": "Service"} for s in t["scope"]],
                "$type": "PermanentToken",
            }
            for id, t in self.tokens.items()
            if t["user"] == user
        ]
        doc = {"skip": skip, "top": top, "total": len(mine)}
        if batch := mine[skip : skip + top]:
            doc["permanenttokens"] = batch
        return self.answer(200, doc)

    def mint(self, user, body):
        assert set(body) == {"name", "scope"}, body
        self.minted += 1
        id = f"m-{self.minted}"
        value = value_of(self.users[user]["login"], body["name"], f"SECRET-minted-{self.minted}")
        scope = [s["id"] for s in body["scope"]]
        self.tokens[id] = {"user": user, "name": body["name"], "scope": scope, "value": value}
        made = {"id": id, "token": value, "$type": "PermanentToken"}
        return self.answer(200, self.mint_answer(made))

    @staticmethod
    def answer(status, doc):
        raw = json.dumps(doc).encode()
        if status >= 400:
            raise urllib.error.HTTPError(ADDR, status, "err", {}, io.BytesIO(raw))
        return FakeResponse(status, raw)
