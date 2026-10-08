"""Jenkins over HTTP, as an opener for secret_rotator.jenkins.Jenkins: jobs by full name, the build
queue and builds, the credentials of the system store's global domain, whose config.xml it gives
with every secret redacted, the admin account's API tokens, which its security page lists as
Jenkins 2.568.3 does and each of which authenticates a request as the user it is taken for, and
the config.xml of AaC/IoTSupport, which it gives as stored and stores as posted, re-serialized. A
queued build starts and ends once the fake clock, which the client's sleep advances, has passed
its lag."""

import base64
import email.message
import html
import io
import json
import re
import urllib.error
import urllib.parse
import uuid
import xml.etree.ElementTree as ET

from secret_rotator.jenkins import ADDR, Jenkins

USER = "admin"
TOKEN = "SECRET-api-token-of-the-admin"
CREDENTIALS = {"user": USER, "token": TOKEN}  # rotator/jenkins
YT = "YouTrack/YouTrackConfiguration"
APPROLE = "724520d1-a0c1-4fa3-8a9e-a027de7f469a"
REDACTED = "<secret-redacted/>"
FORM = "application/x-www-form-urlencoded"
IOT = "AaC/IoTSupport"
TRIGGER_TOKEN = "SECRET-old-trigger-token"  # the remote-trigger token AaC/IoTSupport holds

VAULT_APPROLE = f"""<com.datapipe.jenkins.vault.credentials.VaultAppRoleCredential plugin="vault">
  <scope>GLOBAL</scope>
  <id>{APPROLE}</id>
  <description>OpenBao AppRole</description>
  <roleId>role-id-of-jenkins</roleId>
  <secretId>
    {REDACTED}
  </secretId>
  <path>approle</path>
  <namespace></namespace>
</com.datapipe.jenkins.vault.credentials.VaultAppRoleCredential>"""

STRING = """<org.jenkinsci.plugins.plaincredentials.impl.StringCredentialsImpl>
  <scope>GLOBAL</scope>
  <id>{id}</id>
  <description>a token</description>
  <secret>
    {redacted}
  </secret>
</org.jenkinsci.plugins.plaincredentials.impl.StringCredentialsImpl>"""

CERTIFICATE = f"""<com.cloudbees.plugins.credentials.impl.CertificateCredentialsImpl>
  <scope>GLOBAL</scope>
  <id>a-certificate</id>
  <password>
    {REDACTED}
  </password>
  <keyStoreSource>
    <uploadedKeystoreBytes>
      {REDACTED}
    </uploadedKeystoreBytes>
  </keyStoreSource>
</com.cloudbees.plugins.credentials.impl.CertificateCredentialsImpl>"""

PROPERTY = f"/user/{USER}/descriptorByName/jenkins.security.ApiTokenProperty"
# The security page's token list as Jenkins 2.568.3 renders it (2026-10-08), its icons left out:
# the hidden template card of a new token first, then one card per token.
TEMPLATE_CARD = (
    '<div class="jenkins-hidden" id="api-token-row-template"><div class="token-card">'
    '<div class="token-card-inner"><div class="token-card__title"><span class="token-name"></span>'
    '</div><div class="token-stats"><span class="api-token-new-expiration token-creation"></span>'
    '<span class="token-last-used">Never used</span></div></div><div class="token-controls">'
    '<button tooltip="Show" type="button" class="api-token-property-token-show"></button>'
    f'<button data-target-url="{PROPERTY}/rename" type="button" '
    'class="api-token-property-token-rename"></button>'
    f'<button data-target-url="{PROPERTY}/revoke" type="button" '
    'class="api-token-property-token-revoke"></button></div></div></div>'
)
TOKEN_CARD = (
    '<div id="{uuid}" class="token-card "><div class="token-card-inner"><div tooltip="" '
    'class="token-card__title "><span class="token-name">{name}</span></div><div '
    'class="token-stats"><span class="warning token-creation">This token does not expire</span>'
    '<span class="token-last-used"><span class="jenkins-!-text-color-secondary">Last used today'
    '</span><span class="token-use-counter">7</span></span></div></div><div '
    f'class="token-controls"><button data-target-url="{PROPERTY}/rename" type="button" '
    'class="jenkins-button api-token-property-token-rename"></button><button '
    f'data-confirm-title="Revoke token" data-target-url="{PROPERTY}/revoke" type="button" '
    'class="jenkins-button api-token-property-token-revoke" data-token-uuid="{uuid}"></button>'
    "</div></div>"
)

# AaC/IoTSupport's config.xml as Jenkins stores it (2026-10-08), abridged, its authToken element a
# format field.
JOB_CONFIG = """<?xml version='1.1' encoding='UTF-8'?>
<flow-definition plugin="workflow-job@1602.v2a_70b_80a_2396">
  <actions/>
  <description></description>
  <keepDependencies>false</keepDependencies>
  <properties>
    <org.jenkinsci.plugins.workflow.job.properties.DisableConcurrentBuildsJobProperty>
      <abortPrevious>true</abortPrevious>
    </org.jenkinsci.plugins.workflow.job.properties.DisableConcurrentBuildsJobProperty>
  </properties>
  <definition class="org.jenkinsci.plugins.workflow.cps.CpsScmFlowDefinition">
    <scm class="hudson.plugins.git.GitSCM" plugin="git@5.10.1">
      <configVersion>2</configVersion>
      <userRemoteConfigs>
        <hudson.plugins.git.UserRemoteConfig>
          <url>https://github.com/pvginkel/IoTSupport.git</url>
        </hudson.plugins.git.UserRemoteConfig>
      </userRemoteConfigs>
    </scm>
    <scriptPath>Jenkinsfile.architecture</scriptPath>
    <lightweight>true</lightweight>
  </definition>
  <triggers/>
  {auth_token}
  <disabled>false</disabled>
</flow-definition>"""


def job_config(token):
    """AaC/IoTSupport's config.xml with that remote-trigger token; None: with none."""
    auth_token = "" if token is None else f"<authToken>{token}</authToken>"
    return JOB_CONFIG.format(auth_token=auth_token).encode()


USERNAME = """<com.cloudbees.plugins.credentials.impl.UsernameCredentialsImpl>
  <scope>GLOBAL</scope>
  <id>a-username</id>
  <username>someone</username>
</com.cloudbees.plugins.credentials.impl.UsernameCredentialsImpl>"""


class FakeResponse(io.BytesIO):
    def __init__(self, status, body, headers=None):
        super().__init__(body)
        self.status = status
        self.headers = email.message.Message()
        for name, value in (headers or {}).items():
            self.headers[name] = value


def security_page(tokens):
    """The account's security page listing the tokens, {uuid: {"name", "value"}}."""
    cards = "".join(
        TOKEN_CARD.format(uuid=uuid, name=html.escape(token["name"]))
        for uuid, token in tokens.items()
    )
    return (
        f'<html><body><div id="api-tokens">{TEMPLATE_CARD}<div id="api-token-list">{cards}'
        "</div></div></body></html>"
    )


def string_credential(id):
    return STRING.format(id=id, redacted=REDACTED)


def secret_fields(root):
    return [el for el in root.iter() if any(child.tag == "secret-redacted" for child in el)]


class FakeJenkins:
    def __init__(self, *, queue_lag=15, build_lag=40):
        self.now = 0.0
        self.queue_lag = queue_lag
        self.build_lag = build_lag
        self.jobs = {YT: "SUCCESS", "Plain/Job": "SUCCESS", "Other/Job": "SUCCESS"}  # -> result
        self.cancelling = set()  # jobs whose queued builds are cancelled
        self.items = {}  # number -> {"job", "params", "queued", "number"}
        self.builds = {}  # (job, number) -> {"started", "result"}
        self.credentials = {
            APPROLE: {"xml": VAULT_APPROLE, "secrets": {"secretId": "SECRET-old-secret-id"}},
            "a-certificate": {"xml": CERTIFICATE, "secrets": {}},
            "a-username": {"xml": USERNAME, "secrets": {}},
        }
        # The admin account's API tokens: uuid -> {"name", "value", "as"}, "as" the user a request
        # with it is taken for, None: refused; rotator/jenkins holds one.
        self.tokens = {}
        self.serial = 0  # the last token uuid's number
        self.minted = 0
        self.mint_as = USER  # the "as" of the tokens it mints
        self.add_token("secret-rotator", TOKEN)
        self.before_revoke = None  # called with the uuid before each revoke
        self.page = security_page  # renders the security page from the tokens
        self.configs = {IOT: job_config(TRIGGER_TOKEN)}  # job -> its config.xml
        self.before_config = None  # called with the job and the posted XML before a config POST
        self.mangle = set()  # credential ids and jobs whose description a POST changes
        self.requests = []  # (method, path, query, body)
        self.broken = {}  # (method, path) -> the OSError it raises
        self.refused = {}  # (method, path) -> the HTTP status it answers
        self.no_location = False

    def jenkins(self):
        return Jenkins(opener=self, sleep=self.sleep, clock=self.clock)

    def clock(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds

    def add_token(self, name, value, taken_as=USER):
        """A token of the admin account; its uuid."""
        self.serial += 1
        key = str(uuid.UUID(int=self.serial))
        self.tokens[key] = {"name": name, "value": value, "as": taken_as}
        return key

    def token_names(self):
        return sorted(token["name"] for token in self.tokens.values())

    def add_string(self, id, value):
        self.credentials[id] = {"xml": string_credential(id), "secrets": {"secret": value}}

    def secret(self, id, field=None):
        secrets = self.credentials[id]["secrets"]
        return secrets[field] if field else next(iter(secrets.values()))

    def auth_token(self, job=IOT):
        """The remote-trigger token the job holds; None: none."""
        found = ET.fromstring(self.configs[job]).find("authToken")
        return None if found is None else found.text

    def triggered(self):
        """(job, params) of every build it queued, in order."""
        return [(item["job"], item["params"]) for _, item in sorted(self.items.items())]

    def __call__(self, req):
        url = urllib.parse.urlsplit(req.full_url)
        assert f"{url.scheme}://{url.netloc}" == ADDR, url
        method, path = req.get_method(), url.path
        query = dict(urllib.parse.parse_qsl(url.query))
        body = req.data.decode() if req.data else None
        self.requests.append((method, path, query, body))
        if (method, path) in self.broken:
            raise self.broken[method, path]
        if (method, path) in self.refused:
            return self.error(self.refused[method, path])
        taken = {
            "Basic " + base64.b64encode(f"{USER}:{token['value']}".encode()).decode(): token["as"]
            for token in self.tokens.values()
        }
        user = taken.get(req.get_header("Authorization"))
        if user is None:
            return self.error(401)
        if path == "/whoAmI/api/json":
            return self.json({"name": user, "anonymous": False, "authenticated": True})
        if user != USER:
            return self.error(403)
        if path == f"/user/{USER}/security/" and method == "GET":
            return FakeResponse(200, self.page(self.tokens).encode())
        if path == f"{PROPERTY}/generateNewToken":
            assert method == "POST" and req.get_header("Content-type") == FORM
            return self.generate(dict(urllib.parse.parse_qsl(body)))
        if path == f"{PROPERTY}/revoke":
            assert method == "POST" and req.get_header("Content-type") == FORM
            return self.revoke(dict(urllib.parse.parse_qsl(body)))
        if found := re.fullmatch(r"((?:/job/[^/]+)+)/config\.xml", path):
            return self.config(self.job_of(found[1]), method, req)
        if found := re.fullmatch(r"((?:/job/[^/]+)+)/(build|buildWithParameters)", path):
            assert method == "POST"
            return self.trigger(self.job_of(found[1]), found[2], query)
        if found := re.fullmatch(r"/queue/item/(\d+)/api/json", path):
            return self.queue_item(int(found[1]))
        if found := re.fullmatch(r"((?:/job/[^/]+)+)/(\d+)/api/json", path):
            return self.build(self.job_of(found[1]), int(found[2]))
        if found := re.fullmatch(
            r"/credentials/store/system/domain/_/credential/([^/]+)/config\.xml", path
        ):
            return self.credential(urllib.parse.unquote(found[1]), method, body, req)
        return self.error(404)

    def generate(self, form):
        name = form.get("newTokenName", "").strip()
        if not name:
            return self.json({"status": "error", "message": "a name is wanted"})
        self.minted += 1
        value = f"SECRET-minted-token-{self.minted}"
        key = self.add_token(name, value, self.mint_as)
        data = {"tokenUuid": key, "tokenName": name, "tokenValue": value}
        return self.json({"status": "ok", "data": data | {"expirationDate": "never"}})

    def revoke(self, form):
        if self.before_revoke:
            self.before_revoke(form["tokenUuid"])
        self.tokens.pop(form["tokenUuid"], None)
        return FakeResponse(200, b"")

    @staticmethod
    def job_of(path):
        return "/".join(urllib.parse.unquote(part) for part in path.split("/job/")[1:])

    def trigger(self, job, how, params):
        if job not in self.jobs:
            return self.error(404)
        assert (how == "buildWithParameters") == bool(params)
        number = len(self.items) + 1
        self.items[number] = {"job": job, "params": params, "queued": self.now, "number": None}
        location = {} if self.no_location else {"Location": f"{ADDR}/queue/item/{number}/"}
        return FakeResponse(201, b"", location)

    def queue_item(self, n):
        item = self.items.get(n)
        if item is None:
            return self.error(404)
        job = item["job"]
        if self.now < item["queued"] + self.queue_lag:
            return self.json(
                {"_class": "hudson.model.Queue$WaitingItem", "why": "In the quiet period"}
            )
        if job in self.cancelling:
            return self.json({"_class": "hudson.model.Queue$LeftItem", "cancelled": True})
        if item["number"] is None:
            item["number"] = 1 + sum(1 for j, _ in self.builds if j == job)
            started = item["queued"] + self.queue_lag
            self.builds[job, item["number"]] = {"started": started, "result": self.jobs[job]}
        executable = {"_class": "org.jenkinsci.plugins.workflow.job.WorkflowRun"}
        executable["number"] = item["number"]
        return self.json({"cancelled": False, "executable": executable})

    def build(self, job, number):
        build = self.builds.get((job, number))
        if build is None:
            return self.error(404)
        if self.now < build["started"] + self.build_lag:
            return self.json({"building": True, "result": None})
        return self.json({"building": False, "result": build["result"]})

    def credential(self, id, method, body, req):
        cred = self.credentials.get(id)
        if cred is None:
            return self.error(404)
        if method == "GET":
            return FakeResponse(200, cred["xml"].encode())
        assert method == "POST" and req.get_header("Content-type").startswith("application/xml")
        posted = ET.fromstring(body)
        stored = ET.fromstring(cred["xml"])
        if posted.tag != stored.tag:
            return self.error(400)
        for field in secret_fields(stored):
            new = posted.find(f".//{field.tag}")
            if new is not None and not len(new):
                cred["secrets"][field.tag] = new.text
                new.text = None
                ET.SubElement(new, "secret-redacted")
        if id in self.mangle:
            posted.find("description").text = "changed by someone else"
        ET.indent(posted)
        cred["xml"] = ET.tostring(posted, encoding="unicode")
        return FakeResponse(200, b"")

    def config(self, job, method, req):
        stored = self.configs.get(job)
        if stored is None:
            return self.error(404)
        if method == "GET":
            return FakeResponse(200, stored)
        assert method == "POST" and req.get_header("Content-type").startswith("application/xml")
        if self.before_config:
            self.before_config(job, req.data)
        posted = ET.fromstring(req.data)
        if posted.tag != ET.fromstring(stored).tag:
            return self.error(400)
        if job in self.mangle:
            posted.find("description").text = "changed by someone else"
        ET.indent(posted)
        self.configs[job] = ET.tostring(posted, encoding="utf-8")
        return FakeResponse(200, b"")

    @staticmethod
    def json(doc):
        return FakeResponse(200, json.dumps(doc).encode())

    @staticmethod
    def error(status):
        page = f"<html><body>Error {status}</body></html>".encode()
        raise urllib.error.HTTPError(ADDR, status, "err", email.message.Message(), io.BytesIO(page))
