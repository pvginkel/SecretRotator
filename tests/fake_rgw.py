"""Ceph RGW's admin API (Admin Ops, /admin/user), as an opener for the rgw-admin kind: at one
cluster's instances and nowhere else. It checks a request as RGW does (rgw_auth_s3.cc): signed by
AWS Signature Version 4 with an S3 key of a user, over the canonical request RGW rebuilds from what
it receives — the query split on &, a name without = taking an empty value, each part decoded and
encoded again, sorted; the signed headers as sent; UNSIGNED-PAYLOAD for a request without
x-amz-content-sha256. An access key no user has answers 403 InvalidAccessKeyId, a signature that
does not match 403 SignatureDoesNotMatch, a signer without a users cap 403 AccessDenied, an
unsigned request 403 AccessDenied. GET ?uid answers the user with its keys, secrets included; PUT
?key&generate-key adds a generated S3 key and answers every key of the user; DELETE
?key&access-key removes one, InvalidAccessKeyId for one the user lacks. Each instance serves users
from its cache: after another instance changes a user's keys, it answers from the keys as they
were for its next `lag` requests."""

import copy
import email.message
import hashlib
import hmac
import io
import itertools
import json
import re
import urllib.error
import urllib.parse

from secret_rotator.kinds.rgw_admin.rgw import Key

USERS_CAPS = "users=*;buckets=*"
AUTH = re.compile(
    r"AWS4-HMAC-SHA256 Credential=([^/]+)/(\d{8})/([^/]+)/([^/]+)/aws4_request, "
    r"SignedHeaders=([a-z0-9;-]+), Signature=([0-9a-f]{64})"
)
NO_ROUTE = urllib.error.URLError(OSError(113, "No route to host"))


class FakeResponse(io.BytesIO):
    def __init__(self, status, body):
        super().__init__(body)
        self.status = status


def error(url, status, code):
    body = json.dumps({"Code": code, "RequestId": "tx0-fake", "HostId": "fake"}).encode()
    raise urllib.error.HTTPError(url, status, code, email.message.Message(), io.BytesIO(body))


def parsed(query):
    """The query's (name, value) pairs as RGW parses them."""
    pairs = []
    for part in query.split("&") if query else []:
        name, _, value = part.partition("=")
        pairs.append((urllib.parse.unquote(name), urllib.parse.unquote(value)))
    return pairs


def _quote(text):
    return urllib.parse.quote(text, safe="-_.~")


def _mac(key, message):
    return hmac.new(key, message.encode(), hashlib.sha256).digest()


class FakeRgw:
    def __init__(self, endpoints, *, boot=120):
        self.endpoints = tuple(endpoints)
        self.users = {}  # uid -> {"caps": str, "keys": {access: secret}}
        self.serial = itertools.count(1)
        self.lag = 0  # requests an instance answers from its cache after another's change
        self.views = {}  # endpoint -> [the users as cached, the requests left to answer from them]
        self.down = set()  # instances that take no connection
        self.off = False  # the VM it runs in is off: no instance takes a connection
        self.boot = boot  # seconds from a VM start until it takes connections
        self.waking = None  # when it takes connections again
        self.time = 0.0
        self.requests = []  # (endpoint, method, op, the access key that signed it, or None)
        self.refused = {}  # (method, op) -> (status, code) it answers, having done nothing
        # (method, op, the access key that signed it) -> (status, code); refused's by default
        self.refuse = lambda method, op, access: self.refused.get((method, op))
        self.lost = {}  # (method, op) -> the OSError its answer is lost to, once it took effect
        self.before = {}  # (method, op) -> called with its params before it takes effect

    # --- the clock its waits are paced by, and the VM it runs in --------------------------------

    def clock(self):
        return self.time

    def sleep(self, seconds):
        self.time += seconds

    def started(self):
        self.waking = self.time + self.boot

    def stopped(self):
        self.off, self.waking = True, None

    # --- users -----------------------------------------------------------------------------------

    def user(self, uid, caps=USERS_CAPS):
        """A new user with one S3 key: that key."""
        self.users[uid] = {"caps": caps, "keys": {}}
        return self.key(uid)

    def key(self, uid):
        """A new S3 key of the user, as radosgw-admin key create makes one."""
        n = next(self.serial)
        access, secret = f"RGWACCESSKEY{n:08d}", f"SECRET-rgw-{n}"
        self.users[uid]["keys"][access] = secret
        return Key(access, secret)

    def keys(self, uid):
        return set(self.users[uid]["keys"])

    def done(self, method, op):
        """The access keys that signed each request of the method and op RGW took, in order."""
        return [r[3] for r in self.requests if (r[1], r[2]) == (method, op)]

    # --- requests --------------------------------------------------------------------------------

    def __call__(self, req, *, timeout=None):
        url = urllib.parse.urlsplit(req.full_url)
        endpoint = f"{url.scheme}://{url.netloc}"
        assert endpoint in self.endpoints, req.full_url
        if self.waking is not None and self.time >= self.waking:
            self.off, self.waking = False, None
        if self.off or endpoint in self.down:
            raise NO_ROUTE
        method, params = req.get_method(), parsed(url.query)
        headers = {name.lower(): value for name, value in req.header_items()}
        if "authorization" not in headers:
            self.requests.append((endpoint, method, url.path, None))
            error(req.full_url, 403, "AccessDenied")
        assert url.path == "/admin/user", req.full_url
        names = dict(params)
        op = "user?key" if "key" in names else "user"
        view = self.view(endpoint)
        signer = self.verify(req, method, url.path, params, headers, view)
        self.requests.append((endpoint, method, op, signer))
        uid = next(u for u, user in view.items() if signer in user["keys"])
        if not view[uid]["caps"].startswith("users="):
            error(req.full_url, 403, "AccessDenied")
        if refusal := self.refuse(method, op, signer):
            error(req.full_url, *refusal)
        if (method, op) in self.before:
            self.before[method, op](names)
        answer = self.answer(req.full_url, endpoint, method, op, names, view)
        if (method, op) in self.lost:
            raise self.lost[method, op]
        return answer

    def view(self, endpoint):
        """The users as the instance sees them: from its cache, while it answers from it."""
        cached = self.views.get(endpoint)
        if cached is None or cached[1] == 0:
            return self.users
        cached[1] -= 1
        return cached[0]

    def changed(self, endpoint, before):
        """Every other instance answers from the users as they were for its next lag requests."""
        for other in self.endpoints:
            if other != endpoint and self.lag:
                self.views[other] = [before, self.lag]

    def verify(self, req, method, path, params, headers, view):
        """The access key that signed the request, its signature checked."""
        match = AUTH.fullmatch(headers["authorization"])
        assert match, headers["authorization"]
        access, date, region, service, signed, signature = match.groups()
        secret = next((u["keys"][access] for u in view.values() if access in u["keys"]), None)
        if secret is None:
            error(req.full_url, 403, "InvalidAccessKeyId")
        names = signed.split(";")
        assert {"host", "x-amz-date"} <= set(names), signed
        canonical = "\n".join(
            [
                method,
                path,
                "&".join(f"{_quote(k)}={_quote(v)}" for k, v in sorted(params)),
                "".join(f"{n}:{headers[n].strip()}\n" for n in names),
                signed,
                headers.get("x-amz-content-sha256", "UNSIGNED-PAYLOAD"),
            ]
        )
        scope = f"{date}/{region}/{service}/aws4_request"
        digest = hashlib.sha256(canonical.encode()).hexdigest()
        to_sign = f"AWS4-HMAC-SHA256\n{headers['x-amz-date']}\n{scope}\n{digest}"
        key = _mac(f"AWS4{secret}".encode(), date)
        for part in (region, service, "aws4_request"):
            key = _mac(key, part)
        if hmac.new(key, to_sign.encode(), hashlib.sha256).hexdigest() != signature:
            error(req.full_url, 403, "SignatureDoesNotMatch")
        return access

    def answer(self, url, endpoint, method, op, names, view):
        assert names.get("format") == "json", names
        uid = names["uid"]
        if uid not in view:
            error(url, 404, "NoSuchUser")
        if (method, op) == ("GET", "user"):
            return self.json(self.info(uid, view))
        before = copy.deepcopy(self.users)
        if (method, op) == ("PUT", "user?key"):
            assert (names["key-type"], names["generate-key"]) == ("s3", "true"), names
            assert "secret-key" not in names and "access-key" not in names, names
            self.key(uid)
            self.changed(endpoint, before)
            return self.json(self.info(uid, self.users)["keys"])
        assert (method, op) == ("DELETE", "user?key"), (method, op)
        assert names["key-type"] == "s3", names
        if names["access-key"] not in view[uid]["keys"]:
            error(url, 403, "InvalidAccessKeyId")
        del self.users[uid]["keys"][names["access-key"]]
        self.changed(endpoint, before)
        return FakeResponse(200, b"")

    @staticmethod
    def info(uid, users):
        keys = users[uid]["keys"]
        return {
            "user_id": uid,
            "display_name": uid,
            "keys": [{"user": uid, "access_key": a, "secret_key": s} for a, s in keys.items()],
            "swift_keys": [],
        }

    @staticmethod
    def json(doc):
        return FakeResponse(200, json.dumps(doc).encode())
