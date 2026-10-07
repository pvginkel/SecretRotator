"""The generic Jenkins steps of design §4.2: jenkins.job, which triggers a job and waits for its
build to succeed, and jenkins.credential, which updates a credential by id. Both reach Jenkins with
the admin account's API token, read from rotator/jenkins when they run. No detail or error they
report carries a value."""

import xml.etree.ElementTree as ET
from collections.abc import Mapping

from secret_rotator.jenkins import Jenkins, JenkinsError, job_path
from secret_rotator.model import Context, Step, StepFailed, not_landed, wait

CREDENTIALS = "rotator/jenkins"  # keys user and token
JOB_BOUND, JOB_POLL = 1800, 10  # seconds, from the trigger: the queue and the build
# What Jenkins writes for a secret field in a credential's config.xml, which it never gives back.
REDACTED = "secret-redacted"


def connect(jenkins: Jenkins, ctx: Context) -> None:
    version = ctx.bao.read(CREDENTIALS)
    if version is None:
        raise StepFailed(
            f"{CREDENTIALS} cannot be read: no such leaf, or its current version is deleted"
        )
    if missing := [key for key in ("user", "token") if not version.data.get(key)]:
        raise StepFailed(f"{CREDENTIALS} has no {' or '.join(missing)}")
    jenkins.authenticate(version.data["user"], version.data["token"])


def parse_job(spec: str) -> tuple[str, dict[str, str]]:
    """A jenkins-job activator's argument, `<job>[?P=v&Q=w]`: the job's full name and its
    parameters, values as written."""
    job, _, query = spec.partition("?")
    pairs = (pair.partition("=") for pair in query.split("&")) if query else ()
    return job, {name: value for name, _, value in pairs}


class JenkinsJob(Step):
    """Triggers a build of a job by its full name, with its parameters, and waits for SUCCESS. An
    activator: a rollback runs it again after its undos, so the job fans the restored value out."""

    type = "jenkins.job"
    mutates = True
    activator = True

    def __init__(self, jenkins: Jenkins, job: str, params: Mapping[str, str]):
        self.jenkins = jenkins
        self.job = job
        self.params = dict(params)
        written = "&".join(f"{name}={value}" for name, value in self.params.items())
        shown = ", ".join(f"{name}={value}" for name, value in self.params.items())
        super().__init__(
            f"jenkins.job:{job}" + (f"?{written}" if written else ""),
            f"run Jenkins job {job}" + (f" with {shown}" if shown else ""),
        )

    def run(self, ctx: Context) -> str:
        connect(self.jenkins, ctx)
        item = self.jenkins.trigger(self.job, self.params)
        number = None

        def why_not() -> str | None:
            nonlocal number
            if number is None:
                queued = self.jenkins.queued(item)
                if queued.get("cancelled"):
                    raise StepFailed(f"the queued build of {self.job} was cancelled")
                number = (queued.get("executable") or {}).get("number")
                if number is None:
                    return f"queued: {queued.get('why') or 'waiting'}"
            build = self.jenkins.build(self.job, number)
            result = build.get("result")
            if build.get("building") or result is None:
                return f"build #{number} running"
            if result != "SUCCESS":
                console = f"{self.jenkins.addr}{job_path(self.job)}/{number}/console"
                raise StepFailed(f"{self.job} build #{number} ended {result}", console)
            return None

        wait(self.jenkins, ctx, JOB_BOUND, JOB_POLL, why_not, f"{self.job} did not succeed")
        return f"build #{number} SUCCESS"


def with_secret(xml: str, value: str, credential: str) -> str:
    """The credential's redacted config.xml with the value in its one secret field."""
    root = ET.fromstring(xml)
    fields = [el for el in root.iter() if any(child.tag == REDACTED for child in el)]
    if len(fields) != 1:
        raise StepFailed(
            f"credential {credential} has {len(fields)} secret fields, not one: which one takes "
            f"the value is not known"
        )
    field = fields[0]
    for child in list(field):
        field.remove(child)
    field.text = value
    return ET.tostring(root, encoding="unicode")


def _canonical(xml: str) -> str:
    return ET.canonicalize(xml, strip_text=True)


def refused(e: Exception) -> bool:
    """Whether Jenkins refused the request: a 4xx answer. Neither a 5xx answer nor a transport
    error says whether the request was carried out."""
    return isinstance(e, JenkinsError) and e.status is not None and e.status < 500


class JenkinsCredential(Step):
    """Updates the one secret field of a credential of Jenkins' system store by its id, and
    verifies it by re-reading the credential: the same class and fields, its secret redacted.
    Jenkins' API gives neither the value a credential holds nor, for a credential no build has
    used, its fingerprint, so the value itself cannot be verified or read back.

    kv: the value is the leaf's key as KV holds it when the step runs. The step is then an
    activator: a rollback runs it again after the KV undos, which writes the previous value back.
    staged: the value is the one the plan staged under that name; there is then no undo, and a
    failure before the write, or the write refused, reports that the step did not land; one of
    the write otherwise, or of the re-read after it, counts as landed (Step.no_undo)."""

    type = "jenkins.credential"
    mutates = True

    def __init__(
        self,
        jenkins: Jenkins,
        credential: str,
        *,
        kv: tuple[str, str] | None = None,
        staged: str | None = None,
    ):
        if (kv is None) == (staged is None):
            raise ValueError("a jenkins.credential takes its value from kv or from staged")
        title = f"update Jenkins credential {credential}"
        super().__init__(f"jenkins.credential:{credential}", title)
        self.jenkins = jenkins
        self.credential = credential
        self.kv = kv
        self.staged = staged
        if kv is not None:
            self.activator = True
            self.title = f"{title} from {kv[0]}#{kv[1]}"
        else:
            self.no_undo = (
                f"Jenkins does not give a credential's value back: the one {credential} held "
                f"cannot be written back"
            )

    def _value(self, ctx: Context) -> str:
        if self.kv is None:
            value = ctx.staged(self.staged)
            if value is None:
                raise StepFailed(f"no value is staged as {self.staged}")
            return value
        leaf, key = self.kv
        version = ctx.bao.read(leaf)
        if version is None or version.data.get(key) is None:
            raise StepFailed(f"{leaf}#{key} cannot be read")
        return version.data[key]

    def run(self, ctx: Context) -> str:
        sent = False
        try:
            value = self._value(ctx)
            connect(self.jenkins, ctx)
            before = self.jenkins.credential_xml(self.credential)
            xml = with_secret(before, value, self.credential)
            sent = True
            self.jenkins.update_credential(self.credential, xml)
        except Exception as e:
            if self.kv is None and (not sent or refused(e)):
                raise not_landed(e) from e
            raise
        after = self.jenkins.credential_xml(self.credential)
        if _canonical(after) != _canonical(before):
            raise StepFailed(
                f"the re-read of credential {self.credential} is not the credential written",
                after,
            )
        return "updated; the re-read is the same credential"
