"""RGW's admin API (Admin Ops, /admin/user), as much of it as the rgw-admin kind uses: a user's S3
keys listed, one added with a key RGW generates, one removed by its access key, each request signed
with an S3 key by AWS Signature Version 4 as go-ceph's rgw/admin signs it (the client
HomelabTerraformProvider reaches the same API with). An access key id names a key without being
one; a secret key never rides a URL, which RGW's access log carries, and no message or error
carries one or an answer that holds one."""

import datetime
import functools
import hashlib
import hmac
import http.client
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from secret_rotator.model import Context, StepFailed, wait
from secret_rotator.vmsteps import DEV_VM

TIMEOUT = 30  # seconds
PATH = "/admin/user"
ALGORITHM = "AWS4-HMAC-SHA256"
# go-ceph rgw/admin's scope: RGW checks a signature in the scope the request names.
SERVICE, REGION = "s3", "default"
# The payload hash of a request without x-amz-content-sha256, which RGW signs with (go-ceph's).
UNSIGNED = "UNSIGNED-PAYLOAD"
FORBIDDEN = 403  # RGW refusing a key it does not know, or a signature it does not take
# An instance serves a user from its cache for a moment after another changed the user's keys.
CACHE_BOUND, CACHE_POLL = 60, 5  # seconds


class RgwError(StepFailed):
    """A request RGW refused (status set: its HTTP status, code its error code) or a transport
    failure (status None). A StepFailed, so a step reports it by its one sentence."""

    def __init__(self, message: str, status: int | None = None, code: str = ""):
        super().__init__(message)
        self.status = status
        self.code = code

    @property
    def refused(self) -> bool:
        """Whether RGW refused the request and changed nothing: a 4xx answer."""
        return self.status is not None and 400 <= self.status < 500


class Unanswered(RgwError):
    """No connection to the instance: it is off, or nothing listens."""


@dataclass(frozen=True)
class Key:
    access: str
    secret: str = field(repr=False)


@dataclass(frozen=True)
class Site:
    """A cluster's RGW: its instances' admin API on the storage backplane, so an answer that
    carries a secret stays off the house LAN; the admin user whose key the leaf holds; and the VM
    that may be off it runs in (Step.vm)."""

    cluster: str
    endpoints: tuple[str, ...]
    uid: str
    vm: str | None = None


# srvceph1-3's backplane addresses, port 7480 (read live 2026-10-10; no inventory holds them).
PRD = Site(
    "prd",
    ("http://192.168.188.24:7480", "http://192.168.188.25:7480", "http://192.168.188.26:7480"),
    "k8s",
)
# srvk8sdev's backplane address (Ansible host_vars/srvk8sdev.yml), port 80
# (group_vars/ceph_dev.yml microceph_rgw_port).
DEV = Site("dev", ("http://192.168.188.17",), "k8s", DEV_VM)


def _quote(text: str) -> str:
    return urllib.parse.quote(text, safe="-_.~")


def canonical_query(query: Sequence[tuple[str, str]]) -> str:
    """The query as SigV4 signs it, and as the client sends it: each name=value URI-encoded,
    sorted; a name without a value takes `=`."""
    return "&".join(f"{_quote(k)}={_quote(v)}" for k, v in sorted(query))


def _hmac(key: bytes, message: str) -> bytes:
    return hmac.new(key, message.encode(), hashlib.sha256).digest()


def authorization(
    method: str,
    path: str,
    query: Sequence[tuple[str, str]],
    headers: dict[str, str],
    payload: str,
    key: Key,
    at: datetime.datetime,
    *,
    region: str = REGION,
    service: str = SERVICE,
) -> str:
    """The Authorization header of a request signed by AWS Signature Version 4 with the key, at
    the UTC time at, every one of headers signed: host and x-amz-date among them. payload: the
    hash the canonical request names."""
    signed = sorted(name.lower() for name in headers)
    values = {name.lower(): " ".join(value.split()) for name, value in headers.items()}
    canonical = "\n".join(
        [
            method,
            path,
            canonical_query(query),
            "".join(f"{name}:{values[name]}\n" for name in signed),
            ";".join(signed),
            payload,
        ]
    )
    date = at.strftime("%Y%m%d")
    scope = f"{date}/{region}/{service}/aws4_request"
    digest = hashlib.sha256(canonical.encode()).hexdigest()
    to_sign = "\n".join([ALGORITHM, at.strftime("%Y%m%dT%H%M%SZ"), scope, digest])
    signing = _hmac(("AWS4" + key.secret).encode(), date)
    for part in (region, service, "aws4_request"):
        signing = _hmac(signing, part)
    signature = hmac.new(signing, to_sign.encode(), hashlib.sha256).hexdigest()
    return (
        f"{ALGORITHM} Credential={key.access}/{scope}, SignedHeaders={';'.join(signed)}, "
        f"Signature={signature}"
    )


def _code(raw: bytes) -> str:
    """The error code of RGW's error answer, JSON or XML; empty when it names none."""
    try:
        doc = json.loads(raw)
    except ValueError:
        match = re.search(rb"<Code>([^<]*)</Code>", raw)
        return match[1].decode(errors="replace") if match else ""
    return str(doc.get("Code", "")) if isinstance(doc, dict) else ""


def utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC)


class Admin:
    """The admin API of one RGW instance, every request signed with one S3 key."""

    def __init__(
        self, endpoint: str, key: Key, opener: Callable, now: Callable[[], datetime.datetime]
    ):
        self.endpoint = endpoint
        self.key = key
        self.open = opener
        self.now = now  # the UTC time a request is signed at

    def call(self, method: str, op: str, query: Sequence[tuple[str, str]]):
        """The JSON answer, None for an empty one; any status >= 300 raised. op names the request
        in a message: `user`, `user?key`."""
        what = f"{method} {self.endpoint}/admin/{op}"
        host = urllib.parse.urlsplit(self.endpoint).netloc
        at = self.now()
        headers = {"Host": host, "X-Amz-Date": at.strftime("%Y%m%dT%H%M%SZ")}
        auth = authorization(method, PATH, query, headers, UNSIGNED, self.key, at)
        req = urllib.request.Request(
            f"{self.endpoint}{PATH}?{canonical_query(query)}",
            method=method,
            headers={**headers, "Authorization": auth},
        )
        try:
            with self.open(req) as resp:
                status, raw = resp.status, resp.read()
        except urllib.error.HTTPError as e:
            code = _code(e.read())
            message = f"{what}: HTTP {e.code}" + (f": {code}" if code else "")
            raise RgwError(message, e.code, code) from None
        except urllib.error.URLError as e:
            raise Unanswered(f"{what}: transport error: {e.reason}") from None
        except (OSError, http.client.HTTPException) as e:
            raise RgwError(f"{what}: transport error: {e!r}") from None
        if status >= 300:
            raise RgwError(f"{what}: HTTP {status}", status)
        try:
            return json.loads(raw) if raw else None
        except ValueError:
            raise RgwError(f"{what}: HTTP {status}, not a JSON answer") from None

    def keys(self, uid: str) -> list[str]:
        """The access keys of the user's S3 keys."""
        user = self.call("GET", "user", [("format", "json"), ("uid", uid)])
        return [k["access_key"] for k in user["keys"]]

    def add_key(self, uid: str) -> list[Key]:
        """Adds an S3 key RGW generates to the user's: every S3 key the user has then."""
        query = [
            ("format", "json"),
            ("generate-key", "true"),
            ("key", ""),
            ("key-type", "s3"),
            ("uid", uid),
        ]
        added = self.call("PUT", "user?key", query)
        return [Key(k["access_key"], k.get("secret_key") or "") for k in added]

    def remove_key(self, uid: str, access: str) -> None:
        """Removes the user's S3 key by its access key."""
        query = [
            ("access-key", access),
            ("format", "json"),
            ("key", ""),
            ("key-type", "s3"),
            ("uid", uid),
        ]
        self.call("DELETE", "user?key", query)


class Gateway:
    """A Site's RGW as the kind's steps reach it, the first instance that answers. Its sleep and
    clock pace the retries of a key an instance does not know yet."""

    def __init__(
        self,
        site: Site,
        opener: Callable | None = None,
        *,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        now: Callable[[], datetime.datetime] = utcnow,
    ):
        self.site = site
        self.open = opener or functools.partial(urllib.request.urlopen, timeout=TIMEOUT)
        self.sleep = sleep
        self.clock = clock
        self.now = now

    def admin(self, endpoint: str, key: Key) -> Admin:
        return Admin(endpoint, key, self.open, self.now)

    def user(self, key: Key, ctx: Context | None = None) -> tuple[Admin, list[str]]:
        """The first instance that answers, as a client signed with the key, and the access keys
        of the Site's uid there. An instance that does not answer is passed over. With a ctx, an
        instance refusing the key (403) is asked again within CACHE_BOUND; without, it is
        raised."""
        if ctx is None:
            return self._first(key)
        found = []

        def refusing() -> str | None:
            try:
                found.append(self._first(key))
            except RgwError as e:
                if e.status != FORBIDDEN:
                    raise
                return e.error
            return None

        wait(self, ctx, CACHE_BOUND, CACHE_POLL, refusing, "RGW did not take the key")
        return found[-1]

    def _first(self, key: Key) -> tuple[Admin, list[str]]:
        failures = []
        for endpoint in self.site.endpoints:
            admin = self.admin(endpoint, key)
            try:
                return admin, admin.keys(self.site.uid)
            except Unanswered as e:
                failures.append(e.error)
        raise RgwError(f"no RGW instance of {self.site.cluster} answers: {'; '.join(failures)}")

    def prove(self, ctx: Context, key: Key) -> list[str]:
        """The instances that answer, each of which takes the key as one of the Site's uid's.
        One must."""
        taken = [e for e in self.site.endpoints if self._takes(ctx, e, key)]
        if not taken:
            raise RgwError(f"no RGW instance of {self.site.cluster} answers")
        return taken

    def _takes(self, ctx: Context, endpoint: str, key: Key) -> bool:
        """Whether the instance answers. One that does must take the key as one of the uid's,
        asked again within CACHE_BOUND while it refuses the key or does not list it."""
        admin = self.admin(endpoint, key)
        unanswered = []

        def missing() -> str | None:
            try:
                listed = admin.keys(self.site.uid)
            except Unanswered as e:
                unanswered.append(e)
                return None
            except RgwError as e:
                if e.status != FORBIDDEN:
                    raise
                return e.error
            if key.access not in listed:
                return f"{endpoint} lists no key {key.access} of {self.site.uid}"
            return None

        wait(self, ctx, CACHE_BOUND, CACHE_POLL, missing, f"{endpoint} did not take the key")
        return not unanswered

    def unanswered(self) -> str | None:
        """Why the RGW of a Site on a VM that may be off does not answer: no instance takes a
        connection. A refusal is an answer; a Site on no such VM always answers."""
        if self.site.vm is None:
            return None
        failures = []
        for endpoint in self.site.endpoints:
            try:
                with self.open(urllib.request.Request(f"{endpoint}/")):
                    return None
            except urllib.error.HTTPError:
                return None
            except urllib.error.URLError as e:
                failures.append(f"{endpoint}: transport error: {e.reason}")
            except (OSError, http.client.HTTPException) as e:
                failures.append(f"{endpoint}: transport error: {e!r}")
        return f"{self.site.cluster}'s RGW does not answer: {'; '.join(failures)}"
