"""Keycloak over HTTP, as an opener for the keycloak-client kind's Keycloak: each realm at the base
URL the kind reaches it at and nowhere else, its token endpoint's client_credentials grant, and the
admin API's clients and their secrets for a token of a client with manage-clients. A regenerate
ends the client's secret; the tokens issued before it stay valid."""

import email.message
import io
import json
import re
import urllib.error
import urllib.parse

BASES = {"https://auth.ginbov.nl": "homelab", "http://keycloak-dev.home": "homelab-dev"}
COUNTERPART = "secret-rotator"  # each realm's counterpart client's client_id


class FakeResponse(io.BytesIO):
    def __init__(self, status, body):
        super().__init__(body)
        self.status = status


class FakeKeycloak:
    def __init__(self):
        # realm -> client_id -> {"id", "secret", "service_account", "manage"}
        self.realms = {realm: {} for realm in BASES.values()}
        for realm in self.realms:
            self.add(realm, COUNTERPART, f"SECRET-{realm}-counterpart", counterpart=True)
        self.tokens = {}  # token -> (realm, client_id)
        self.regenerated = 0
        self.requests = []  # (method, base URL, path, query, form)
        self.broken = {}  # (method, path) -> the OSError it raises
        self.refused = {}  # (method, path) -> the HTTP status it answers
        # (method, path) -> the HTTP status it answers after the request took effect
        self.lost = {}

    def add(self, realm, client_id, secret, *, counterpart=False):
        """A client of the realm; a counterpart is a service account with manage-clients."""
        self.realms[realm][client_id] = {
            "id": f"uuid-of-{realm}-{client_id}",
            "secret": secret,
            "service_account": counterpart,
            "manage": counterpart,
        }

    def secret(self, realm, client_id):
        return self.realms[realm][client_id]["secret"]

    def logins(self):
        """(base URL, client_id) of every login."""
        return [
            (base, form["client_id"])
            for _, base, path, _, form in self.requests
            if path.endswith("/protocol/openid-connect/token")
        ]

    def regenerates(self):
        return [r for r in self.requests if r[0] == "POST" and r[2].endswith("/client-secret")]

    def __call__(self, req):
        url = urllib.parse.urlsplit(req.full_url)
        base = f"{url.scheme}://{url.netloc}"
        assert base in BASES, base
        method, path = req.get_method(), urllib.parse.unquote(url.path)
        query = dict(urllib.parse.parse_qsl(url.query))
        form = dict(urllib.parse.parse_qsl(req.data.decode())) if req.data else {}
        self.requests.append((method, base, path, query, form))
        if (method, path) in self.broken:
            raise self.broken[method, path]
        if (method, path) in self.refused:
            return self.error(self.refused[method, path], {"error": "refused"})
        if found := re.fullmatch(r"/realms/([^/]+)/protocol/openid-connect/token", path):
            answer = self.token(BASES[base], found[1], req, form)
        elif found := re.fullmatch(r"/admin/realms/([^/]+)(/.*)", path):
            answer = self.admin(BASES[base], found[1], found[2], method, query, req)
        else:
            answer = self.error(404, {"error": "Not Found"})
        if (method, path) in self.lost:
            return self.error(self.lost[method, path], None)
        return answer

    def token(self, served, realm, req, form):
        if realm != served:
            return self.error(404, {"error": "Realm does not exist"})
        assert req.get_header("Content-type") == "application/x-www-form-urlencoded"
        assert form["grant_type"] == "client_credentials"
        client = self.realms[realm].get(form["client_id"])
        if client is None or client["secret"] != form["client_secret"]:
            return self.error(
                401,
                {
                    "error": "unauthorized_client",
                    "error_description": "Invalid client or Invalid client credentials",
                },
            )
        if not client["service_account"]:
            return self.error(
                400,
                {
                    "error": "unauthorized_client",
                    "error_description": "Client not enabled to retrieve service account",
                },
            )
        token = f"token-{len(self.tokens) + 1}"
        self.tokens[token] = (realm, form["client_id"])
        return self.json({"access_token": token, "expires_in": 300, "token_type": "Bearer"})

    def admin(self, served, realm, rest, method, query, req):
        if realm != served:
            return self.error(404, {"error": "Realm not found."})
        auth = req.get_header("Authorization") or ""
        holder = self.tokens.get(auth.removeprefix("Bearer "))
        if holder is None or holder[0] != realm:
            return self.error(401, {"error": "HTTP 401 Unauthorized"})
        if not self.realms[realm][holder[1]]["manage"]:
            return self.error(403, {"error": "HTTP 403 Forbidden"})
        clients = self.realms[realm]
        if (method, rest) == ("GET", "/clients"):
            want = query.get("clientId")
            return self.json(
                [{"id": c["id"], "clientId": i} for i, c in clients.items() if i == want]
            )
        if found := re.fullmatch(r"/clients/([^/]+)/client-secret", rest):
            client = next((c for c in clients.values() if c["id"] == found[1]), None)
            if client is None:
                return self.error(404, {"error": "Could not find client"})
            if method == "POST":
                self.regenerated += 1
                client["secret"] = f"SECRET-{realm}-regenerated-{self.regenerated}"
            else:
                assert method == "GET", method
            return self.json({"type": "secret", "value": client["secret"]})
        return self.error(404, {"error": "Not Found"})

    @staticmethod
    def json(doc):
        return FakeResponse(200, json.dumps(doc).encode())

    @staticmethod
    def error(status, doc):
        body = b"" if doc is None else json.dumps(doc).encode()
        raise urllib.error.HTTPError("", status, "err", email.message.Message(), io.BytesIO(body))
