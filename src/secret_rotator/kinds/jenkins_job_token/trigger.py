"""A Jenkins job's remote trigger, as much as the jenkins-job-token kind uses: the token parameter
of a URL that triggers the job ("Trigger builds remotely", /job/<…>/build?token=…), and the
authToken under the root of the job's config.xml, the token Jenkins checks that parameter
against. config.xml is read and written over the core's Jenkins client; a GET of it gives the
authToken in plain text (AaC/IoTSupport's, dumped over REST on 2026-09-30)."""

import urllib.parse
import xml.etree.ElementTree as ET

from secret_rotator.jenkins import Jenkins, job_path

PARAM = "token"
AUTH_TOKEN = "authToken"
XML = "application/xml; charset=utf-8"


def _params(url: str) -> tuple[urllib.parse.SplitResult, list[str], list[int]]:
    """The URL's parts, its query's parameters as written, and where its token parameters are."""
    parts = urllib.parse.urlsplit(url)
    params = parts.query.split("&") if parts.query else []
    at = [
        n for n, p in enumerate(params) if urllib.parse.unquote_plus(p.partition("=")[0]) == PARAM
    ]
    return parts, params, at


def token_problem(url: str) -> str | None:
    """What keeps the URL from carrying one token, in words that never carry the URL; None when
    nothing does."""
    _, params, at = _params(url)
    if len(at) != 1:
        return f"has {len(at)} {PARAM} parameters, not one"
    if not params[at[0]].partition("=")[2]:
        return f"has an empty {PARAM} parameter"
    return None


def token_of(url: str) -> str:
    """The token of a URL token_problem finds nothing wrong with."""
    _, params, (at,) = _params(url)
    return urllib.parse.unquote_plus(params[at].partition("=")[2])


def with_token(url: str, token: str) -> str:
    """A URL token_problem finds nothing wrong with, the token in its token parameter and every
    other part of it as written."""
    parts, params, (at,) = _params(url)
    params[at] = f"{params[at].partition('=')[0]}={urllib.parse.quote(token, safe='')}"
    return urllib.parse.urlunsplit(parts._replace(query="&".join(params)))


def auth_token(xml: bytes) -> str | None:
    """The token of a job's config.xml; None when it has none: remote triggering is off."""
    found = ET.fromstring(xml).find(AUTH_TOKEN)
    return None if found is None else found.text or None


def with_auth_token(xml: bytes, token: str) -> bytes:
    """A config.xml that has an authToken, with the token in it."""
    root = ET.fromstring(xml)
    root.find(AUTH_TOKEN).text = token
    return ET.tostring(root, encoding="utf-8")


def but_token(xml: bytes) -> str:
    """The config.xml canonical, its authToken's text left out: what a change of the token alone
    leaves as it is."""
    root = ET.fromstring(xml)
    for found in root.findall(AUTH_TOKEN):
        found.text = None
    return ET.canonicalize(ET.tostring(root, encoding="unicode"), strip_text=True)


def config_xml(jenkins: Jenkins, job: str) -> bytes:
    _, _, raw = jenkins.call("GET", f"{job_path(job)}/config.xml")
    return raw


def update_config(jenkins: Jenkins, job: str, xml: bytes) -> None:
    jenkins.call("POST", f"{job_path(job)}/config.xml", xml, XML)
