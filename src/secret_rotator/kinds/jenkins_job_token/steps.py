"""The jenkins-job-token kind's own steps (design §4.2: custom steps are the plugin's):
jenkins_job_token.generate and jenkins_job_token.set, through Jenkins' API as the account
rotator/jenkins names (045 D4). No detail or error they report carries a token or a URL that
carries one."""

import secrets

from secret_rotator.jenkins import Jenkins
from secret_rotator.jenkinssteps import connect
from secret_rotator.kinds.jenkins_job_token import trigger
from secret_rotator.model import Context, Step, StepFailed, value_name

# 32 random bytes, 43 URL-safe characters: a token the URL carries as it is.
TOKEN_BYTES = 32


def held_token(ctx: Context, leaf: str, key: str) -> tuple[str, str]:
    """The URL the leaf's key holds and the token it carries."""
    version = ctx.bao.read(leaf)
    url = None if version is None else version.data.get(key)
    if url is None:
        raise StepFailed(f"{leaf}#{key} cannot be read")
    if problem := trigger.token_problem(url):
        raise StepFailed(f"the URL in {leaf}#{key} {problem}")
    return url, trigger.token_of(url)


def job_token(jenkins: Jenkins, job: str) -> tuple[bytes, str]:
    """The job's config.xml and the token it holds."""
    xml = trigger.config_xml(jenkins, job)
    token = trigger.auth_token(xml)
    if token is None:
        raise StepFailed(
            f"Jenkins job {job} has no remote-trigger token: Trigger builds remotely is off"
        )
    return xml, token


class Generate(Step):
    """Stages, as the new value of the leaf's key, the URL it holds with a new random token in its
    token parameter. The job must hold the token that URL carries, else the URL does not trigger
    the job the args name. A re-run with a value staged keeps it."""

    type = "jenkins_job_token.generate"
    silent = True

    def __init__(self, jenkins: Jenkins, job: str, leaf: str, key: str):
        super().__init__(
            "jenkins_job_token.generate",
            f"generate a new remote-trigger token for Jenkins job {job}",
        )
        self.jenkins = jenkins
        self.job = job
        self.leaf = leaf
        self.key = key

    def run(self, ctx: Context) -> str:
        name = value_name(self.key)
        if ctx.staged(name) is None:
            url, token = held_token(ctx, self.leaf, self.key)
            connect(self.jenkins, ctx)
            _, held = job_token(self.jenkins, self.job)
            if held != token:
                raise StepFailed(
                    f"Jenkins job {self.job} holds another remote-trigger token than the one "
                    f"{self.leaf}#{self.key} carries: the URL does not trigger that job"
                )
            ctx.stage(name, trigger.with_token(url, secrets.token_urlsafe(TOKEN_BYTES)))
        return f"a URL that carries a new token for Jenkins job {self.job}"


class SetToken(Step):
    """Sets the job's remote-trigger token to the one the URL in the leaf's key carries as KV holds
    it when the step runs, and verifies by re-reading the job's config.xml: it holds that token,
    and the rest of it is the job as read before. A job that holds that token already is left as
    it is.

    An activator: a rollback runs it again after the KV undos, and after the Secrets and the
    workloads that read the leaf are back on the old URL, which puts the old token back on the
    job."""

    type = "jenkins_job_token.set"
    mutates = True
    activator = True

    def __init__(self, jenkins: Jenkins, job: str, leaf: str, key: str):
        super().__init__(
            "jenkins_job_token.set",
            f"set the remote-trigger token of Jenkins job {job} from {leaf}#{key}",
        )
        self.jenkins = jenkins
        self.job = job
        self.leaf = leaf
        self.key = key

    def run(self, ctx: Context) -> str:
        _, token = held_token(ctx, self.leaf, self.key)
        connect(self.jenkins, ctx)
        before, held = job_token(self.jenkins, self.job)
        if held == token:
            return f"Jenkins job {self.job} holds that token already"
        trigger.update_config(self.jenkins, self.job, trigger.with_auth_token(before, token))
        after = trigger.config_xml(self.jenkins, self.job)
        if trigger.auth_token(after) != token:
            raise StepFailed(
                f"the re-read of Jenkins job {self.job} does not hold the token "
                f"{self.leaf}#{self.key} carries"
            )
        if trigger.but_token(after) != trigger.but_token(before):
            raise StepFailed(
                f"the re-read of Jenkins job {self.job} is not the job as read before but its "
                f"token: something else of it changed"
            )
        return "set; the re-read of the job holds it"
