"""OpenBao over HTTP, as an opener for secret_rotator.openbao.OpenBao: AppRole login and the kv
mount (KV v2 data, metadata and subkeys)."""

import copy
import io
import json
import urllib.error
import urllib.parse

from secret_rotator.openbao import ADDR

ROLE_ID = "role-id-of-the-rotator"
SECRET_ID = "SECRET-secret-id-of-the-rotator"
TOKEN = "token-of-the-rotator"


class FakeResponse(io.BytesIO):
    def __init__(self, status, body):
        super().__init__(body)
        self.status = status


class FakeOpenBao:
    def __init__(self, leaves=None):
        self.leaves = copy.deepcopy(leaves or {})  # path -> {"data": dict | None, "meta": dict}
        self.requests = []  # (method, path, query, body, content type)
        self.refuse = {}  # (method, request path) -> the HTTP status it answers
        self.broken = {}  # request path -> the OSError it raises (a transport failure)

    def __call__(self, req):
        url = urllib.parse.urlsplit(req.full_url)
        assert f"{url.scheme}://{url.netloc}" == ADDR, url
        path = urllib.parse.unquote(url.path)[len("/v1/") :]
        query = dict(urllib.parse.parse_qsl(url.query))
        method = req.get_method()
        body = json.loads(req.data) if req.data else None
        self.requests.append((method, path, query, body, req.get_header("Content-type")))
        if path in self.broken:
            raise self.broken[path]
        if path == "auth/approle/login":
            if body == {"role_id": ROLE_ID, "secret_id": SECRET_ID}:
                return self.answer(200, {"auth": {"client_token": TOKEN, "lease_duration": 3600}})
            return self.answer(400, {"errors": ["invalid role or secret ID"]})
        if req.get_header("X-vault-token") != TOKEN:
            return self.answer(403, {"errors": ["permission denied"]})
        if (method, path) in self.refuse:
            errors = ["1 error occurred:\n\t* permission denied\n\n"]
            return self.answer(self.refuse[method, path], {"errors": errors})
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
        return self.answer(200, {"data": {"custom_metadata": meta or None, "current_version": 1}})

    def get_subkeys(self, leaf, body, query, req):
        assert query == {"depth": "1"}, query
        data = self.leaves.get(leaf, {}).get("data")
        if leaf not in self.leaves:
            return self.answer(404, {"errors": []})
        if data is None:
            return self.answer(404, {"data": {"subkeys": None, "metadata": {"version": 1}}})
        return self.answer(200, {"data": {"subkeys": dict.fromkeys(data), "metadata": {}}})

    def get_data(self, leaf, body, query, req):
        data = self.leaves.get(leaf, {}).get("data")
        if data is None:
            return self.answer(404, {"data": {"data": None, "metadata": {}}})
        return self.answer(200, {"data": {"data": data, "metadata": {"version": 1}}})

    def post_data(self, leaf, body, query, req):
        cas = body.get("options", {}).get("cas")
        if cas == 0 and leaf in self.leaves:
            errors = ["check-and-set parameter did not match the current version"]
            return self.answer(400, {"errors": errors})
        entry = self.leaves.setdefault(leaf, {"data": None, "meta": {}})
        entry["data"] = dict(body["data"])
        return self.answer(200, {"data": {"version": 1}})

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
