"""The permanent tokens of YouTrack's built-in Hub (YouTrack 2026.2, /hub/api/rest), as much as the
youtrack-token kind uses, over the core's YouTrack client: whom a token belongs to, asked with the
token itself, and a user's tokens, listed, minted and revoked by an account with Hub's scope. Hub
lists a token by its id, name and scope, never by its value; an id names a token without being
one."""

import base64
import binascii
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass

from secret_rotator.youtrack import YouTrack, YouTrackError

HUB = "/hub/api/rest"
PAGE = 100


@dataclass(frozen=True)
class Owner:
    id: str  # the user's id in Hub
    login: str


@dataclass(frozen=True)
class Token:
    id: str
    name: str
    scope: tuple[str, ...]  # the ids of the services it reaches


def client(token: str, opener: Callable | None) -> YouTrack:
    return YouTrack(token, opener=opener)


def owner(token: str, opener: Callable | None) -> Owner:
    """Whom the token belongs to, asked with the token as bearer: YouTrack's /api/users/me, which a
    token with YouTrack's scope reaches; refused there, Hub's /users/me, which a token with Hub's
    scope reaches. Hub's refusal is raised."""
    asking = client(token, opener)
    try:
        me = asking.call("GET", "/api/users/me", query={"fields": "login,ringId"})
        return Owner(me["ringId"], me["login"])
    except YouTrackError as e:
        if e.status not in (401, 403):
            raise
    me = asking.call("GET", f"{HUB}/users/me", query={"fields": "id,login"})
    return Owner(me["id"], me["login"])


def carried(value: str) -> tuple[str, str] | None:
    """The login and the token name a permanent token's value carries, base64 in its first two
    parts: perm:<login>.<name>.<secret>. None for a value not of that form."""
    prefix, _, rest = value.partition(":")
    parts = rest.split(".")
    if prefix != "perm" or len(parts) != 3 or not parts[2]:
        return None
    try:
        login, name = (base64.b64decode(part, validate=True).decode() for part in parts[:2])
    except (binascii.Error, UnicodeDecodeError):
        return None
    return (login, name) if login and name else None


def _tokens_path(user: str) -> str:
    return f"{HUB}/users/{urllib.parse.quote(user, safe='')}/permanenttokens"


def tokens(admin: YouTrack, user: str) -> list[Token]:
    """The user's permanent tokens."""
    found: list[Token] = []
    while True:
        query = {"fields": "id,name,scope(id)", "$skip": str(len(found)), "$top": str(PAGE)}
        page = admin.call("GET", _tokens_path(user), query=query)
        batch = page.get("permanenttokens", [])
        found += [
            Token(t["id"], t.get("name", ""), tuple(s["id"] for s in t.get("scope", [])))
            for t in batch
        ]
        if not batch or len(found) >= page["total"]:
            return found


def mint(admin: YouTrack, user: str, name: str, scope: tuple[str, ...]) -> tuple[str, str | None]:
    """A new permanent token of the user, named name, reaching the services of scope: its id, and
    its value, which only this answer carries (None when it carries none)."""
    body = {"name": name, "scope": [{"id": service} for service in scope]}
    made = admin.call("POST", _tokens_path(user), body, {"fields": "id,token"})
    return made["id"], made.get("token")


def revoke(admin: YouTrack, user: str, token: str) -> None:
    """Revokes the user's permanent token by its id; Hub answers 404 for an id it does not hold."""
    admin.call("DELETE", f"{_tokens_path(user)}/{urllib.parse.quote(token, safe='')}")
