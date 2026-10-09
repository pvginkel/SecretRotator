"""step-ca, as much of it as the step-ca-password kind uses: GET /provisioners, which serves each
JWK provisioner's public key and its private key encrypted under the provisioner's password
(`encryptedKey`, a compact JWE); and step-cli's `step crypto jwe decrypt`, which opens such a key
with a password. step-ca has no remote provisioner API here: its ca.json has no
authority.enableAdmin. No error carries a password or a private key."""

import functools
import http.client
import json
import os
import ssl
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable

from secret_rotator.ansiblesteps import redact

# step-ca's URL. The iac image trusts the homelab root and carries step-cli (Ansible
# support/iac-image/Dockerfile).
BASE = "https://ca.home"
STEP = ("step",)
TIMEOUT = 30  # seconds: a request's, and a decrypt's
# The members that make a JWK the key it is, public or private: RFC 7638 §3.2, by its kty.
MATERIAL = {"EC": ("crv", "kty", "x", "y"), "OKP": ("crv", "kty", "x"), "RSA": ("e", "kty", "n")}


class StepCaError(Exception):
    """What keeps the kind from reading a key or opening it, in one sentence."""


class NotOpened(StepCaError):
    """step-cli refused to open the key with the password: what it said."""


def material(jwk: dict) -> str:
    """What makes the JWK the key it is, as canonical JSON: never a private member."""
    members = MATERIAL.get(jwk.get("kty"))
    if members is None:
        raise StepCaError(f"a key of kty {jwk.get('kty')!r} is not an EC, OKP or RSA key")
    return json.dumps({m: jwk.get(m) for m in members}, sort_keys=True, separators=(",", ":"))


class StepCa:
    """A step-ca, by the base URL it is served at, and the step-cli command that opens its keys."""

    def __init__(
        self, base: str = BASE, opener: Callable | None = None, step: tuple[str, ...] = STEP
    ):
        self.base = base.rstrip("/")
        self.open = opener or functools.partial(
            urllib.request.urlopen, context=ssl.create_default_context(), timeout=TIMEOUT
        )
        self.step = step

    def _get(self, path: str) -> dict:
        url = self.base + path
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        try:
            with self.open(req) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as e:
            raise StepCaError(f"GET {url}: HTTP {e.code}") from None
        except urllib.error.URLError as e:
            raise StepCaError(f"GET {url}: transport error: {e.reason}") from None
        except (OSError, http.client.HTTPException) as e:
            raise StepCaError(f"GET {url}: transport error: {e!r}") from None
        try:
            doc = json.loads(raw)
        except ValueError:
            raise StepCaError(f"GET {url}: not a JSON answer") from None
        if not isinstance(doc, dict) or not isinstance(doc.get("provisioners"), list):
            raise StepCaError(f"GET {url}: not step-ca's list of provisioners")
        return doc

    def provisioners(self) -> list[dict]:
        """Every provisioner step-ca serves, page by page: an empty nextCursor ends the list."""
        found: list[dict] = []
        query = ""
        while True:
            page = self._get(f"/provisioners{query}")
            found += page["provisioners"]
            cursor = page.get("nextCursor")
            if not cursor:
                return found
            query = "?" + urllib.parse.urlencode({"cursor": cursor})

    def jwk(self, name: str) -> tuple[dict, str]:
        """The public key and the encryptedKey of the JWK provisioner by its name."""
        found = [p for p in self.provisioners() if p.get("name") == name]
        if not found or found[0].get("type") != "JWK":
            raise StepCaError(f"step-ca serves no JWK provisioner {name}")
        key, encrypted = found[0].get("key"), found[0].get("encryptedKey")
        if not isinstance(key, dict) or not isinstance(encrypted, str):
            raise StepCaError(f"step-ca serves {name} without its key and encryptedKey")
        return key, encrypted

    def opens(self, encrypted: str, password: str) -> str:
        """The material of the private key the password opens the encryptedKey to. step-cli reads
        the password from a pipe, never from its command line or a file, and the key from its
        stdin; what it prints, the private key, never leaves this method. NotOpened when step-cli
        refuses the password."""
        read, write = os.pipe()
        with os.fdopen(write, "w") as pipe:
            pipe.write(password)  # a pipe holds 64 KiB unread: never blocks on a password
        argv = [*self.step, "crypto", "jwe", "decrypt", "--password-file", f"/dev/fd/{read}"]
        try:
            done = subprocess.run(
                argv,
                input=encrypted,
                capture_output=True,
                text=True,
                timeout=TIMEOUT,
                pass_fds=(read,),
            )
        except FileNotFoundError:
            raise StepCaError(f"{self.step[0]} is not on the PATH") from None
        except subprocess.TimeoutExpired:
            raise StepCaError(
                f"step crypto jwe decrypt did not finish within {TIMEOUT} s"
            ) from None
        finally:
            os.close(read)
        if done.returncode != 0:
            said = redact(done.stderr, {"password": password}).strip().splitlines()
            raise NotOpened(said[-1] if said else f"step-cli exited {done.returncode}")
        try:
            opened = json.loads(done.stdout)
        except ValueError:
            raise StepCaError("step crypto jwe decrypt printed no JWK") from None
        if not isinstance(opened, dict):
            raise StepCaError("step crypto jwe decrypt printed no JWK")
        return material(opened)
