"""step-ca over HTTPS, as an opener for the step-ca-password kind's StepCa: at https://ca.home and
nowhere else, GET /provisioners only, paged as step-ca pages it, an empty nextCursor on the last
page (ca.home answered {"provisioners": [...], "nextCursor": ""}, 2026-10-09). Each JWK
provisioner holds an EC key pair: its public key, and its private key encrypted under its password
in the fakes' own JWE, which fake_step.py opens. What the operator does to ca.json and the
playbook puts in step-ca is a method: a new key pair, the same key re-encrypted, a key and an
encryptedKey of two pairs."""

import base64
import email.message
import io
import itertools
import json
import urllib.error
import urllib.parse

BASE = "https://ca.home"
PAGE = 2  # provisioners per page: the four take two
PREFIX = "FAKE-JWE."  # the fakes' JWE: the prefix, then base64url JSON {"password", "jwk"}
_serial = itertools.count(1)


def keypair():
    """(public, private) of a new EC P-256 JWK, kid and all; the private one's d is SECRET-."""
    n = next(_serial)
    public = {
        "use": "sig",
        "kty": "EC",
        "kid": f"kid-{n}",
        "crv": "P-256",
        "alg": "ES256",
        "x": f"x-{n}",
        "y": f"y-{n}",
    }
    return public, public | {"d": f"SECRET-private-d-{n}"}


def sealed(password, private):
    """The private key encrypted under the password, as fake_step.py opens it."""
    doc = json.dumps({"password": password, "jwk": private}).encode()
    return PREFIX + base64.urlsafe_b64encode(doc).decode()


def jwk(name, password):
    public, private = keypair()
    return {"type": "JWK", "name": name, "key": public, "encryptedKey": sealed(password, private)}


class FakeResponse(io.BytesIO):
    def __init__(self, status, body):
        super().__init__(body)
        self.status = status


class FakeStepCa:
    """The homelab's step-ca as https://ca.home/provisioners lists it (2026-10-09): admin, acme,
    ansible-jwk and kubecoder-jwk, the JWK ones under the passwords given."""

    def __init__(self, kubecoder_password, *, others="SECRET-other-provisioner-password"):
        self.provisioners = [
            jwk("admin", others),
            {"type": "ACME", "name": "acme", "claims": {}, "options": {}},
            jwk("ansible-jwk", others),
            jwk("kubecoder-jwk", kubecoder_password),
        ]
        self.requests = []  # each GET's path and query
        self.broken = None  # the OSError every GET raises
        self.status = None  # the HTTP status every GET answers
        self.on_get = lambda: None  # runs on each GET, before it is answered

    def provisioner(self, name="kubecoder-jwk"):
        return next(p for p in self.provisioners if p["name"] == name)

    def private(self, name="kubecoder-jwk"):
        """The provisioner's private key, as only its password opens it."""
        doc = self.provisioner(name)["encryptedKey"].removeprefix(PREFIX)
        return json.loads(base64.urlsafe_b64decode(doc))["jwk"]

    def new_pair(self, password, name="kubecoder-jwk"):
        """The runbook done: a new key pair under the password in ca.json, the playbook run."""
        public, private = keypair()
        self.provisioner(name).update(key=public, encryptedKey=sealed(password, private))

    def reencrypt(self, password, name="kubecoder-jwk"):
        """The key it holds, its private half encrypted under the password instead."""
        found = self.provisioner(name)
        found["encryptedKey"] = sealed(password, self.private(name))

    def mismatched(self, password, name="kubecoder-jwk"):
        """A key of one new pair beside the encryptedKey of another, under the password."""
        public, _ = keypair()
        _, private = keypair()
        self.provisioner(name).update(key=public, encryptedKey=sealed(password, private))

    def __call__(self, req):
        url = urllib.parse.urlsplit(req.full_url)
        assert f"{url.scheme}://{url.netloc}" == BASE, req.full_url
        assert req.get_method() == "GET", req.get_method()
        self.requests.append(f"{url.path}?{url.query}" if url.query else url.path)
        self.on_get()
        if self.broken is not None:
            raise self.broken
        if self.status is not None:
            raise urllib.error.HTTPError(
                req.full_url, self.status, "error", email.message.Message(), io.BytesIO(b"{}")
            )
        assert url.path == "/provisioners", url.path
        cursor = urllib.parse.parse_qs(url.query).get("cursor", ["0"])[0]
        at = int(cursor)
        page = self.provisioners[at : at + PAGE]
        more = at + PAGE < len(self.provisioners)
        body = {"provisioners": page, "nextCursor": str(at + PAGE) if more else ""}
        return FakeResponse(200, json.dumps(body).encode())
