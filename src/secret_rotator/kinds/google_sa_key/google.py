"""Google's OAuth 2.0 token endpoint and IAM API (v1), as much of them as the google-sa-key kind
uses: an access token of a service account, which Google answers an assertion one of the account's
keys signs with (the JWT bearer grant), and under it the account's user-managed keys, listed,
created and deleted. Google lists a key by its id, never with its private half, which only the
create's answer carries. No request or error carries a key's private half or an access token."""

import base64
import functools
import http.client
import json
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from secret_rotator.model import StepFailed

TOKEN_URI = "https://oauth2.googleapis.com/token"  # also the assertion's audience
IAM = "https://iam.googleapis.com/v1"
SCOPE = "https://www.googleapis.com/auth/cloud-platform"
GRANT = "urn:ietf:params:oauth:grant-type:jwt-bearer"
LIFETIME = 3600  # seconds from an assertion's iat to its exp: Google's maximum
TIMEOUT = 30


class GoogleError(StepFailed):
    """A request Google refused (status set) or a transport failure (status None). A StepFailed,
    so a step reports it by its one sentence, which carries Google's own message."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status

    @property
    def refused(self) -> bool:
        """Whether Google refused the request, which then changed nothing: a 4xx answer."""
        return self.status is not None and 400 <= self.status < 500


@dataclass(frozen=True)
class Key:
    """A service account key file, Google's credentials file, as much of it as the kind reads."""

    id: str  # private_key_id: the key's id among its account's keys
    email: str  # client_email: the service account's
    private: rsa.RSAPrivateKey = field(repr=False)


def parse(text: str) -> Key:
    """The key a key file holds; ValueError, in words that carry none of it, for a text that is
    not one with an RSA private key."""
    try:
        doc = json.loads(text)
    except ValueError:
        raise ValueError("not JSON") from None
    if not isinstance(doc, dict) or doc.get("type") != "service_account":
        raise ValueError("not a service account key file")
    found = [doc.get(name) for name in ("private_key_id", "client_email", "private_key")]
    if not all(isinstance(value, str) and value for value in found):
        raise ValueError("it lacks private_key_id, client_email or private_key")
    key_id, email, pem = found
    try:
        private = serialization.load_pem_private_key(pem.encode(), password=None)
    except (ValueError, TypeError):
        raise ValueError("its private_key is no PEM private key") from None
    if not isinstance(private, rsa.RSAPrivateKey):
        raise ValueError("its private_key is no RSA key")
    return Key(key_id, email, private)


def _b64(raw: bytes) -> bytes:
    return base64.urlsafe_b64encode(raw).rstrip(b"=")


def assertion(key: Key, now: int) -> str:
    """The JWT the key signs (RS256) to ask for an access token of its account, issued at now."""
    header = {"alg": "RS256", "typ": "JWT", "kid": key.id}
    claims = {"iss": key.email, "scope": SCOPE, "aud": TOKEN_URI, "iat": now, "exp": now + LIFETIME}
    signed = b".".join(_b64(json.dumps(part).encode()) for part in (header, claims))
    signature = key.private.sign(signed, padding.PKCS1v15(), hashes.SHA256())
    return (signed + b"." + _b64(signature)).decode()


def _message(raw: bytes) -> str:
    """Google's words for a refusal: the token endpoint's error and error_description, or the IAM
    API's error message."""
    try:
        doc = json.loads(raw)
    except ValueError:
        return ""
    error = doc.get("error") if isinstance(doc, dict) else None
    if isinstance(error, dict):
        return error.get("message") or ""
    if isinstance(error, str):
        why = doc.get("error_description")
        return f"{error}: {why}" if why else error
    return ""


class Google:
    """The client. Its sleep and clock pace the step that waits for Google to take a new key."""

    def __init__(
        self,
        opener: Callable | None = None,
        *,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.open = opener or functools.partial(
            urllib.request.urlopen, context=ssl.create_default_context(), timeout=TIMEOUT
        )
        self.sleep = sleep
        self.clock = clock

    def request(
        self, method: str, url: str, data: bytes | None = None, headers: dict | None = None
    ) -> dict:
        """The JSON answer, {} when it has no body; any status >= 400 is raised."""
        shown = urllib.parse.urlsplit(url).path
        req = urllib.request.Request(url, method=method, data=data, headers=headers or {})
        try:
            with self.open(req) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as e:
            why = _message(e.read())
            raise GoogleError(
                f"{method} {shown}: HTTP {e.code}" + (f": {why}" if why else ""), e.code
            ) from None
        except urllib.error.URLError as e:
            raise GoogleError(f"{method} {shown}: transport error: {e.reason}") from None
        except (OSError, http.client.HTTPException) as e:
            raise GoogleError(f"{method} {shown}: transport error: {e!r}") from None
        try:
            return json.loads(raw) if raw else {}
        except ValueError:
            raise GoogleError(f"{method} {shown}: not a JSON answer") from None

    def login(self, key: Key) -> "Account":
        """The key's account, under the access token Google answers an assertion the key signs."""
        form = {"grant_type": GRANT, "assertion": assertion(key, int(time.time()))}
        doc = self.request(
            "POST",
            TOKEN_URI,
            urllib.parse.urlencode(form).encode(),
            {"Content-Type": "application/x-www-form-urlencoded"},
        )
        return Account(self, key.email, doc["access_token"])


class Account:
    """A service account, reached under an access token of its own."""

    def __init__(self, google: Google, email: str, token: str):
        self.google = google
        self.email = email
        self.token = token
        self.path = f"{IAM}/projects/-/serviceAccounts/{urllib.parse.quote(email, safe='@')}/keys"

    def _call(self, method: str, url: str, body: dict | None = None) -> dict:
        headers = {"Authorization": f"Bearer {self.token}", "Accept": "application/json"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        data = None if body is None else json.dumps(body).encode()
        return self.google.request(method, url, data, headers)

    def key_ids(self) -> list[str]:
        """The ids of the account's user-managed keys."""
        doc = self._call("GET", f"{self.path}?keyTypes=USER_MANAGED")
        return [k["name"].rsplit("/", 1)[1] for k in doc.get("keys", [])]

    def create(self) -> str:
        """A new key of the account: the text of its key file, which only this answer carries."""
        body = {
            "privateKeyType": "TYPE_GOOGLE_CREDENTIALS_FILE",
            "keyAlgorithm": "KEY_ALG_RSA_2048",
        }
        doc = self._call("POST", self.path, body)
        return base64.b64decode(doc["privateKeyData"]).decode()

    def delete(self, key: str) -> None:
        """Deletes the account's key by its id; Google answers 404 for an id it holds no key by."""
        self._call("DELETE", f"{self.path}/{urllib.parse.quote(key, safe='')}")
