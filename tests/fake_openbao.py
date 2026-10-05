"""OpenBao over HTTP, as an opener for secret_rotator.openbao.OpenBao: AppRole login and the kv
mount (KV v2 data with its versions, metadata and subkeys); and the AppRoles' secret_ids as the
approle kind uses them, answering as OpenBao 2.5.4 does."""

import copy
import datetime
import io
import json
import re
import urllib.error
import urllib.parse

from secret_rotator.openbao import ADDR

ROLE_ID = "role-id-of-the-rotator"
SECRET_ID = "SECRET-secret-id-of-the-rotator"
TOKEN = "token-of-the-rotator"

APPROLE = re.compile(
    r"auth/approle/role/(?P<role>[^/]+)/(?P<what>role-id|secret-id|secret-id/lookup"
    r"|secret-id-accessor/lookup|secret-id-accessor/destroy)"
)
MINTED_AT = datetime.datetime(2026, 10, 5, 4, 30, tzinfo=datetime.UTC)  # plans.NOW
MAX_TTL = 8640 * 3600  # the approle mount's max_lease_ttl, as the openbao role tunes it
# OpenBao reports times in its host's zone, to the nanosecond; in this one the date is not UTC's.
ZONE = datetime.timezone(datetime.timedelta(hours=-5))


def approle(role_id, **secret_ids):
    """An AppRole whose live secret_ids are secret_id -> accessor, minted never to expire."""
    return {
        "role_id": role_id,
        "secret_ids": {s: {"accessor": a, "ttl": 0} for s, a in secret_ids.items()},
    }


def reported(when):
    if when is None:
        return "0001-01-01T00:00:00Z"
    local = when.astimezone(ZONE)
    offset = local.strftime("%z")
    return f"{local:%Y-%m-%dT%H:%M:%S}.61674539{offset[:3]}:{offset[3:]}"


class FakeResponse(io.BytesIO):
    def __init__(self, status, body):
        super().__init__(body)
        self.status = status


class FakeOpenBao:
    def __init__(self, leaves=None, approles=None):
        # path -> {"data": dict | None, "meta": dict}, and once written "version" (the current
        # version's number, else 1) and "history" (version number -> that version's data)
        self.leaves = copy.deepcopy(leaves or {})
        self.requests = []  # (method, path, query, body, content type)
        self.refuse = {}  # (method, request path) -> the HTTP status it answers
        # request path, or (method, request path) -> the OSError it raises (a transport failure)
        self.broken = {}
        # role -> {"role_id", "secret_ids": {secret_id: {"accessor", "ttl" (seconds; 0: never)}}}
        self.approles = copy.deepcopy(approles or {})
        self.minted = 0
        self.refused_logins = set()  # roles whose logins it refuses, as a bound CIDR does

    def __call__(self, req):
        url = urllib.parse.urlsplit(req.full_url)
        assert f"{url.scheme}://{url.netloc}" == ADDR, url
        path = urllib.parse.unquote(url.path)[len("/v1/") :]
        query = dict(urllib.parse.parse_qsl(url.query))
        method = req.get_method()
        body = json.loads(req.data) if req.data else None
        self.requests.append((method, path, query, body, req.get_header("Content-type")))
        for key in ((method, path), path):
            if key in self.broken:
                raise self.broken[key]
        if path == "auth/approle/login":
            if body == {"role_id": ROLE_ID, "secret_id": SECRET_ID}:
                return self.answer(200, {"auth": {"client_token": TOKEN, "lease_duration": 3600}})
            for name, role in self.approles.items():
                if (
                    body["role_id"] == role["role_id"]
                    and body["secret_id"] in role["secret_ids"]
                    and name not in self.refused_logins
                ):
                    token = TOKEN if role["role_id"] == ROLE_ID else f"token-of-{name}"
                    return self.answer(
                        200, {"auth": {"client_token": token, "lease_duration": 3600}}
                    )
            return self.answer(400, {"errors": ["invalid role or secret ID"]})
        if req.get_header("X-vault-token") != TOKEN:
            return self.answer(403, {"errors": ["permission denied"]})
        if (method, path) in self.refuse:
            errors = ["1 error occurred:\n\t* permission denied\n\n"]
            return self.answer(self.refuse[method, path], {"errors": errors})
        if found := APPROLE.fullmatch(path):
            return self.approle(method, found["role"], found["what"], body)
        mount, area, leaf = path.split("/", 2)
        assert mount == "kv" and area in ("metadata", "data", "subkeys"), path
        handler = getattr(self, f"{method.lower()}_{area}", None)
        if handler is None:
            return self.answer(405, {"errors": [f"{method} {path} is not served here"]})
        return handler(leaf, body, query, req)

    def list_metadata(self, prefix, body, query, req):
        names = set()
        for leaf in self.leaves:
            if leaf.startswith(prefix):
                head, sep, _ = leaf[len(prefix) :].partition("/")
                names.add(head + sep)
        if not names:
            return self.answer(404, {"errors": []})
        return self.answer(200, {"data": {"keys": sorted(names)}})

    def get_metadata(self, leaf, body, query, req):
        if leaf not in self.leaves:
            return self.answer(404, {"errors": []})
        meta = self.leaves[leaf]["meta"]
        return self.answer(
            200, {"data": {"custom_metadata": meta or None, "current_version": self.version(leaf)}}
        )

    def get_subkeys(self, leaf, body, query, req):
        assert query == {"depth": "1"}, query
        data = self.leaves.get(leaf, {}).get("data")
        if leaf not in self.leaves:
            return self.answer(404, {"errors": []})
        if data is None:
            return self.answer(404, {"data": {"subkeys": None, "metadata": {"version": 1}}})
        return self.answer(200, {"data": {"subkeys": dict.fromkeys(data), "metadata": {}}})

    def get_data(self, leaf, body, query, req):
        entry = self.leaves.get(leaf)
        current = self.version(leaf)
        number = int(query.get("version", current))
        if entry is None:
            return self.answer(404, {"errors": []})
        data = entry["data"] if number == current else entry.get("history", {}).get(number)
        if data is None:
            return self.answer(404, {"data": {"data": None, "metadata": {"version": number}}})
        return self.answer(200, {"data": {"data": data, "metadata": {"version": number}}})

    def check_cas(self, leaf, body):
        cas = body.get("options", {}).get("cas")
        if cas is not None and cas != self.version(leaf):
            errors = ["check-and-set parameter did not match the current version"]
            return self.answer(400, {"errors": errors})
        return None

    def new_version(self, leaf, data):
        number = self.version(leaf)
        entry = self.leaves.setdefault(leaf, {"data": None, "meta": {}})
        if entry["data"] is not None:
            entry.setdefault("history", {})[number] = entry["data"]
        entry["version"] = number + 1
        entry["data"] = data
        return self.answer(200, {"data": {"version": number + 1}})

    def post_data(self, leaf, body, query, req):
        return self.check_cas(leaf, body) or self.new_version(leaf, dict(body["data"]))

    def patch_data(self, leaf, body, query, req):
        if req.get_header("Content-type") != "application/merge-patch+json":
            return self.answer(415, {"errors": ["unsupported content type"]})
        if self.leaves.get(leaf, {}).get("data") is None:
            return self.answer(404, {"errors": []})
        data = dict(self.leaves[leaf]["data"])
        for key, value in body["data"].items():
            if value is None:
                data.pop(key, None)
            else:
                data[key] = value
        return self.check_cas(leaf, body) or self.new_version(leaf, data)

    def delete_metadata(self, leaf, body, query, req):
        self.leaves.pop(leaf, None)
        return self.answer(204, None)

    def patch_metadata(self, leaf, body, query, req):
        if req.get_header("Content-type") != "application/merge-patch+json":
            return self.answer(415, {"errors": ["unsupported content type"]})
        if leaf not in self.leaves:
            return self.answer(404, {"errors": []})
        meta = self.leaves[leaf]["meta"]
        for key, value in body["custom_metadata"].items():
            if value is None:
                meta.pop(key, None)
            else:
                meta[key] = value
        return self.answer(204, None)

    def approle(self, method, name, what, body):
        role = self.approles.get(name)
        if role is None:
            return self.answer(404, {"errors": [f'role "{name}" does not exist']})
        ids = role["secret_ids"]
        if (method, what) == ("GET", "role-id"):
            return self.answer(200, {"data": {"role_id": role["role_id"]}})
        if (method, what) == ("POST", "secret-id"):
            hours = re.fullmatch(r"([0-9]+)h", body["ttl"])
            ttl = min(int(hours[1]) * 3600, MAX_TTL)
            self.minted += 1
            secret_id = f"SECRET-{name}-new-{self.minted}"
            accessor = f"accessor-{name}-new-{self.minted}"
            ids[secret_id] = {"accessor": accessor, "ttl": ttl}
            data = {
                "secret_id": secret_id,
                "secret_id_accessor": accessor,
                "secret_id_num_uses": 0,
                "secret_id_ttl": ttl,
            }
            return self.answer(200, {"data": data})
        if (method, what) == ("LIST", "secret-id"):
            if not ids:
                return self.answer(404, {"errors": []})
            return self.answer(200, {"data": {"keys": sorted(e["accessor"] for e in ids.values())}})
        assert method == "POST", (method, what)
        if what == "secret-id/lookup":
            entry = ids.get(body["secret_id"])
            return self.answer(204, None) if entry is None else self.described(entry)
        by_accessor = {e["accessor"]: s for s, e in ids.items()}
        secret_id = by_accessor.get(body["secret_id_accessor"])
        missing = (
            f'failed to find accessor entry for secret_id_accessor: "{body["secret_id_accessor"]}"'
        )
        if what == "secret-id-accessor/lookup":
            if secret_id is None:
                return self.answer(404, {"data": {"error": missing}})
            return self.described(ids[secret_id])
        if secret_id is None:
            return self.answer(500, {"errors": [f"1 error occurred:\n\t* {missing}\n\n"]})
        del ids[secret_id]
        return self.answer(204, None)

    def described(self, entry):
        ttl = entry["ttl"]
        expires = MINTED_AT + datetime.timedelta(seconds=ttl) if ttl else None
        data = {
            "secret_id_accessor": entry["accessor"],
            "secret_id_ttl": ttl,
            "secret_id_num_uses": 0,
            "creation_time": reported(MINTED_AT),
            "expiration_time": reported(expires),
            "metadata": {},
        }
        return self.answer(200, {"data": data})

    def live(self, role):
        """The role's live secret_ids: secret_id -> accessor."""
        return {s: e["accessor"] for s, e in self.approles[role]["secret_ids"].items()}

    @staticmethod
    def answer(status, doc):
        body = b"" if doc is None else json.dumps(doc).encode()
        if status >= 400:
            raise urllib.error.HTTPError(ADDR, status, "err", {}, io.BytesIO(body))
        return FakeResponse(status, body)

    def writes(self):
        return [
            r for r in self.requests if r[0] not in ("GET", "LIST") and r[1] != "auth/approle/login"
        ]

    def meta(self, leaf):
        return self.leaves[leaf]["meta"]

    def data(self, leaf):
        return self.leaves[leaf]["data"]

    def version(self, leaf):
        """The current version's number; 0 for no leaf."""
        entry = self.leaves.get(leaf)
        return 0 if entry is None else entry.get("version", 1)
