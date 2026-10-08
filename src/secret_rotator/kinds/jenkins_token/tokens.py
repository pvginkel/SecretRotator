"""The API tokens of one Jenkins account, as much as the jenkins-token kind uses, over the core's
Jenkins client: the account's security page, which lists each token's name and uuid, since
Jenkins' JSON API exports nothing of them (2.568.3, /user/<id>/api/json); and ApiTokenProperty's
generateNewToken and revoke, which an account may call on its own tokens without
adminCanGenerateNewTokens."""

import html.parser
import json
import urllib.parse
from dataclasses import dataclass

from secret_rotator.jenkins import Jenkins, JenkinsError

PROPERTY = "descriptorByName/jenkins.security.ApiTokenProperty"
FORM = "application/x-www-form-urlencoded"


@dataclass(frozen=True)
class Token:
    uuid: str
    name: str


def user_path(user: str) -> str:
    return f"/user/{urllib.parse.quote(user, safe='')}"


class _SecurityPage(html.parser.HTMLParser):
    """The tokens of the page: in each div.token-card, the text of its span.token-name, then the
    data-token-uuid of its revoke button. The page's template card for a new token has an empty
    name and no uuid."""

    def __init__(self):
        super().__init__()
        self.tokens: list[Token] = []
        self.name = ""
        self.reading = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        found = dict(attrs)
        classes = (found.get("class") or "").split()
        if "token-card" in classes:
            self.name = ""
        elif tag == "span" and "token-name" in classes:
            self.reading = True
        elif found.get("data-token-uuid") and self.name:
            self.tokens.append(Token(found["data-token-uuid"], self.name))

    def handle_endtag(self, tag: str) -> None:
        if tag == "span":
            self.reading = False

    def handle_data(self, data: str) -> None:
        if self.reading:
            self.name += data


def listed(jenkins: Jenkins, user: str) -> list[Token]:
    """The account's tokens, as its security page lists them."""
    _, _, raw = jenkins.call("GET", f"{user_path(user)}/security/")
    page = _SecurityPage()
    page.feed(raw.decode())
    page.close()
    return page.tokens


def _form(fields: dict[str, str]) -> bytes:
    return urllib.parse.urlencode(fields).encode()


def generate(jenkins: Jenkins, user: str, name: str) -> tuple[str, str]:
    """A new token of the account, named name, which never expires: its uuid and its value. Jenkins
    answers a refusal of its own as HTTP 200 with status error, raised here with that status."""
    path = f"{user_path(user)}/{PROPERTY}/generateNewToken"
    _, _, raw = jenkins.call("POST", path, _form({"newTokenName": name}), FORM)
    try:
        doc = json.loads(raw)
    except ValueError:
        raise JenkinsError(f"POST {path}: not a JSON answer") from None
    if doc.get("status") != "ok":
        raise JenkinsError(f"POST {path}: Jenkins answers {doc.get('message')}", 200)
    return doc["data"]["tokenUuid"], doc["data"]["tokenValue"]


def revoke(jenkins: Jenkins, user: str, uuid: str) -> None:
    """Revokes the account's token by its uuid. Jenkins answers 200 for a uuid it does not hold."""
    path = f"{user_path(user)}/{PROPERTY}/revoke"
    jenkins.call("POST", path, _form({"tokenUuid": uuid}), FORM)


def who(jenkins: Jenkins) -> str:
    """The user id Jenkins takes the client's credentials for."""
    return jenkins.json("/whoAmI/api/json")["name"]
