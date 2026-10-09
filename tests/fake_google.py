"""Google's OAuth 2.0 token endpoint and IAM API over HTTP, as an opener for
secret_rotator.kinds.google_sa_key.google.Google: service accounts and their user-managed keys, of
which it keeps the public half and the time of the fake clock, which the client's sleep advances,
from which it takes each; access tokens, which it answers an assertion signed by a key it takes
with (else 400 invalid_grant, as Google answers a key it does not hold); and an account's keys,
listed, created and deleted under an access token of that account's own, refused (403) on an
account that may not manage its own keys. A key the API creates is taken lag seconds after."""

import base64
import email.message
import io
import itertools
import json
import re
import time
import urllib.error
import urllib.parse

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from secret_rotator.kinds.google_sa_key.google import GRANT, IAM, LIFETIME, SCOPE, TOKEN_URI, Google

KEYS = re.compile(r"/v1/projects/-/serviceAccounts/([^/]+)/keys(?:/([^/]+))?")
NOT_TAKEN = "Invalid JWT Signature."
PERMISSION = "Permission 'iam.serviceAccountKeys.{}' denied on resource (or it may not exist)."
STATUS = {400: "INVALID_ARGUMENT", 403: "PERMISSION_DENIED", 404: "NOT_FOUND", 503: "UNAVAILABLE"}
FOREVER = "9999-12-31T23:59:59Z"  # a user-managed key's validBeforeTime without an expiry policy


class FakeResponse(io.BytesIO):
    def __init__(self, status, body):
        super().__init__(body)
        self.status = status


def b64url_json(part):
    return json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))


def key_file(email, id, private):
    """A key file as Google gives it."""
    pem = private.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    return json.dumps(
        {
            "type": "service_account",
            "project_id": email.split("@")[1].split(".")[0],
            "private_key_id": id,
            "private_key": pem,
            "client_email": email,
            "client_id": "104857600000000000001",
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": TOKEN_URI,
            "auth_provider_x509_cert_url": "https://www.googleapis.com/oauth2/v1/certs",
            "client_x509_cert_url": "https://www.googleapis.com/robot/v1/metadata/x509/"
            + urllib.parse.quote(email),
            "universe_domain": "googleapis.com",
        },
        indent=2,
    )


class FakeGoogle:
    def __init__(self, *, lag=30):
        self.now = 0.0
        self.lag = lag
        # email -> {"keys": {id: {"public", "after"}}, "may_key": bool}
        self.accounts = {}
        self.access = {}  # access token -> the email of its account
        self.serial = itertools.count(1)
        self.texts = []  # every key file made: what a run must never report
        self.requests = []  # (what, method, path, the email or key id it acts as)
        self.broken = {}  # what -> the OSError it raises before it takes effect
        self.refused = {}  # what -> the HTTP status it answers, having done nothing
        self.lost = {}  # what -> the OSError its answer is lost to, once it took effect
        self.before = {}  # what -> called with the key id it deletes before it takes effect
        self.unlisted = set()  # ids of keys the list leaves out

    def google(self):
        return Google(opener=self, sleep=self.sleep, clock=self.clock)

    def clock(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds

    def account(self, email, *, may_key=True):
        self.accounts[email] = {"keys": {}, "may_key": may_key}

    def key(self, email, *, after=None):
        """A new key of the account: its key file. after: the time of the fake clock from which
        Google takes it; now by default."""
        n = next(self.serial)
        id = f"{n:040x}"
        private = rsa.generate_private_key(public_exponent=65537, key_size=1024)
        self.accounts[email]["keys"][id] = {
            "public": private.public_key(),
            "after": self.now if after is None else after,
        }
        text = key_file(email, id, private)
        self.texts.append(text)
        return text

    def keys(self, email):
        return set(self.accounts[email]["keys"])

    def done(self, what):
        return [r for r in self.requests if r[0] == what]

    def __call__(self, req):
        url = urllib.parse.urlsplit(req.full_url)
        method = req.get_method()
        if req.full_url == TOKEN_URI:
            return self.token(req)
        assert f"{url.scheme}://{url.netloc}{url.path}".startswith(IAM + "/"), url
        found = KEYS.fullmatch(url.path)
        assert found, url.path
        account, key = urllib.parse.unquote(found[1]), found[2]
        what = {("GET", False): "list", ("POST", False): "create", ("DELETE", True): "delete"}[
            method, key is not None
        ]
        self.requests.append((what, method, url.path, key or account))
        self.knobs(what)
        bearer = (req.get_header("Authorization") or "").removeprefix("Bearer ")
        if bearer not in self.access:
            return self.error(401, "Request had invalid authentication credentials.")
        if self.access[bearer] != account or not self.accounts[account]["may_key"]:
            return self.error(403, PERMISSION.format(what))
        if what == "list":
            assert url.query == "keyTypes=USER_MANAGED", url.query
            return self.list(account)
        if what == "create":
            assert req.get_header("Content-type") == "application/json"
            assert json.loads(req.data) == {
                "privateKeyType": "TYPE_GOOGLE_CREDENTIALS_FILE",
                "keyAlgorithm": "KEY_ALG_RSA_2048",
            }
            text = self.key(account, after=self.now + self.lag)
            id = json.loads(text)["private_key_id"]
            doc = self.described(account, id) | {
                "privateKeyType": "TYPE_GOOGLE_CREDENTIALS_FILE",
                "privateKeyData": base64.b64encode(text.encode()).decode(),
            }
            return self.answer(what, doc)
        if key not in self.accounts[account]["keys"]:
            return self.error(404, f"Service account key {key} does not exist.")
        if hook := self.before.get(what):
            hook(key)
        self.remove(account, key)
        return self.answer(what, {})

    def remove(self, account, key):
        del self.accounts[account]["keys"][key]

    def knobs(self, what):
        if what in self.broken:
            raise self.broken[what]
        if what in self.refused:
            self.error(self.refused[what], "Refused", token=what == "token")

    def answer(self, what, doc):
        if what in self.lost:
            raise self.lost[what]
        return FakeResponse(200, json.dumps(doc).encode())

    def described(self, account, id):
        return {
            "name": f"projects/{account.split('@')[1].split('.')[0]}/serviceAccounts/{account}"
            f"/keys/{id}",
            "validAfterTime": "2026-10-05T04:30:00Z",
            "validBeforeTime": FOREVER,
            "keyAlgorithm": "KEY_ALG_RSA_2048",
            "keyOrigin": "GOOGLE_PROVIDED",
            "keyType": "USER_MANAGED",
        }

    def list(self, account):
        listed = [
            self.described(account, id)
            for id in self.accounts[account]["keys"]
            if id not in self.unlisted
        ]
        # Google leaves out an empty repeated field.
        return FakeResponse(200, json.dumps({"keys": listed} if listed else {}).encode())

    def token(self, req):
        assert req.get_method() == "POST"
        assert req.get_header("Content-type") == "application/x-www-form-urlencoded"
        form = dict(urllib.parse.parse_qsl(req.data.decode()))
        assert form["grant_type"] == GRANT
        header, claims, signature = form["assertion"].split(".")
        head, body = b64url_json(header), b64url_json(claims)
        self.requests.append(("token", "POST", "/token", head["kid"]))
        self.knobs("token")
        assert head == {"alg": "RS256", "typ": "JWT", "kid": head["kid"]}, head
        assert (body["aud"], body["scope"], body["exp"] - body["iat"]) == (
            TOKEN_URI,
            SCOPE,
            LIFETIME,
        )
        assert abs(body["iat"] - time.time()) < 60
        account = self.accounts.get(body["iss"])
        if account is None:
            return self.error(400, "Invalid grant: account not found", token=True)
        key = account["keys"].get(head["kid"])
        if key is None or self.now < key["after"]:
            return self.error(400, NOT_TAKEN, token=True)
        signed = f"{header}.{claims}".encode()
        raw = base64.urlsafe_b64decode(signature + "=" * (-len(signature) % 4))
        try:
            key["public"].verify(raw, signed, padding.PKCS1v15(), hashes.SHA256())
        except InvalidSignature:
            return self.error(400, NOT_TAKEN, token=True)
        access = f"SECRET-access-{next(self.serial)}"
        self.access[access] = body["iss"]
        doc = {"access_token": access, "expires_in": 3599, "token_type": "Bearer"}
        return self.answer("token", doc)

    @staticmethod
    def error(status, message, *, token=False):
        if token:
            body = {"error": "invalid_grant", "error_description": message}
        else:
            body = {"error": {"code": status, "message": message, "status": STATUS.get(status)}}
        raw = io.BytesIO(json.dumps(body).encode())
        raise urllib.error.HTTPError(TOKEN_URI, status, "err", email.message.Message(), raw)
