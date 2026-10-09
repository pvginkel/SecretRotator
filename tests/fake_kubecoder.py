"""KubeCoder's controller's client routes, as an opener for the kubecoder-client kind's KubeCoder:
at the address the kind reaches it at and nowhere else, every request with a named client's
credential as bearer. GET /clients lists the named clients, static and minted; POST /clients mints
a client's credential, which ends the one it held, and refuses a static client's name or an
unreadable store with the controller's 409 titles (KubeCoder controller app.py). A bearer the
controller does not hold answers 401 with the body prd's controller answered at kubecoder.home on
2026-10-09."""

import email.message
import io
import json
import urllib.error
import urllib.parse

BASE = "https://kubecoder.home"
CLIENT = "fieldnotes"
OLD = "SECRET-old-fieldnotes"
STATIC = ("bot", "mcp")
CREDENTIALS = {
    "bot": "SECRET-bot",
    "macbook": "SECRET-macbook",
    "mcp": "SECRET-mcp",
    CLIENT: OLD,
}
UNAUTHENTICATED = {
    "type": "unauthenticated",
    "title": "Missing or invalid API token",
    "status": 401,
}
CLIENTS = "/clients"


def conflict(title):
    return {"type": "conflict", "title": title, "status": 409, "detail": "SECRET-free detail"}


class FakeResponse(io.BytesIO):
    def __init__(self, status, body):
        super().__init__(body)
        self.status = status


class FakeController:
    def __init__(self):
        self.credentials = dict(CREDENTIALS)  # each named client's credential, by its name
        self.requests = []  # (method, path, the client the bearer is or None)
        self.broken = {}  # (method, path) -> the OSError it raises
        self.refused = {}  # (method, path) -> (the HTTP status, the problem) it answers
        # (method, path) -> the HTTP status it answers after the request took effect
        self.lost = {}
        self.unreadable = False  # the minted store's file present and unread
        self.minted = 0
        self.before_mint = lambda client: None  # each mint, before it takes effect

    def whose(self, credential):
        """The client the credential is, or None."""
        return next((n for n, c in self.credentials.items() if c == credential), None)

    def mints(self):
        return [r for r in self.requests if r[0] == "POST"]

    def __call__(self, req):
        url = urllib.parse.urlsplit(req.full_url)
        assert f"{url.scheme}://{url.netloc}" == BASE, req.full_url
        method, path = req.get_method(), url.path
        auth = req.get_header("Authorization") or ""
        assert auth.startswith("Bearer "), auth
        assert req.get_header("Accept") == "application/json"
        who = self.whose(auth.removeprefix("Bearer "))
        self.requests.append((method, path, who))
        if (method, path) in self.broken:
            raise self.broken[method, path]
        if (method, path) in self.refused:
            return self.error(*self.refused[method, path])
        if who is None:
            return self.error(401, UNAUTHENTICATED)
        if (method, path) == ("GET", CLIENTS):
            items = [
                {"name": name, "source": "static" if name in STATIC else "minted"}
                for name in sorted(self.credentials)
            ]
            answer = self.json({"items": items})
        elif (method, path) == ("POST", CLIENTS):
            answer = self.mint(req)
        else:
            answer = self.error(404, {"type": "not-found", "title": "not found", "status": 404})
        if (method, path) in self.lost:
            return self.error(self.lost[method, path], None)
        return answer

    def mint(self, req):
        assert req.get_header("Content-type") == "application/json"
        client = json.loads(req.data)["name"].strip().lower()
        if client in STATIC:
            return self.error(409, conflict("that client is chart-provisioned"))
        if self.unreadable:
            return self.error(409, conflict("the minted client store is unreadable"))
        self.before_mint(client)
        replaced = client in self.credentials
        self.minted += 1
        credential = f"SECRET-minted-{self.minted}"
        self.credentials[client] = credential
        return self.json({"name": client, "credential": credential, "replaced": replaced})

    @staticmethod
    def json(doc):
        return FakeResponse(200, json.dumps(doc).encode())

    @staticmethod
    def error(status, doc):
        body = b"" if doc is None else json.dumps(doc).encode()
        raise urllib.error.HTTPError("", status, "err", email.message.Message(), io.BytesIO(body))
