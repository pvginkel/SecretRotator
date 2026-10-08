"""The jenkins-token kind's own steps (design §4.2: custom steps are the plugin's):
jenkins_token.mint, jenkins_token.login and jenkins_token.revoke, through Jenkins' API as the
account rotator/jenkins names (045 D4), which owns every token they touch. No detail or error they
report carries a token's value; a uuid names a token without being one."""

from secret_rotator.jenkins import Jenkins, JenkinsError
from secret_rotator.jenkinssteps import connect, refused
from secret_rotator.kinds.jenkins_token import tokens
from secret_rotator.model import Context, Step, StepFailed, not_landed, value_name

# The staging name of the uuid of the token the plan minted, beside its value's.
UUID = "jenkins-token:uuid"


def replaced(name: str, legacy: str | None) -> str:
    """The tokens a plan revokes, in words."""
    return f"any other token named {name}" + (f" and any named {legacy}" if legacy else "")


class Mint(Step):
    """Mints a new token of the account, named after the leaf and key it is for, which never
    expires, stages its value and uuid, and verifies it by finding that uuid under that name on the
    account's security page. A re-run with a token staged mints none: it looks that one up again.

    Its undo revokes the token it minted, which Jenkins answers alike when it holds no such token
    any more. A mint whose answer is lost leaves a token no one holds under the plan's name, which
    the revoke of a plan of the leaf revokes with the old ones."""

    type = "jenkins_token.mint"
    mutates = True

    def __init__(self, jenkins: Jenkins, name: str, key: str):
        super().__init__("jenkins_token.mint", f"mint a new Jenkins API token named {name}")
        self.jenkins = jenkins
        self.name = name
        self.key = key  # the data key the new token is staged for

    def run(self, ctx: Context) -> str:
        user = connect(self.jenkins, ctx)
        if ctx.staged(value_name(self.key)) is None:
            uuid, value = tokens.generate(self.jenkins, user, self.name)
            ctx.stage(UUID, uuid)
            ctx.stage(value_name(self.key), value)
        uuid = ctx.staged(UUID)
        if tokens.Token(uuid, self.name) not in tokens.listed(self.jenkins, user):
            raise StepFailed(
                f"the security page of Jenkins account {user} lists no token {uuid} named "
                f"{self.name}"
            )
        return f"token {uuid} of Jenkins account {user}"

    def undo(self, ctx: Context) -> str:
        uuid = ctx.staged(UUID)
        if uuid is None:
            return "nothing was minted"
        tokens.revoke(self.jenkins, connect(self.jenkins, ctx), uuid)
        return f"token {uuid} revoked"


class Login(Step):
    """Logs in to Jenkins with the token the leaf holds, from a client of its own, as the account
    rotator/jenkins names: the leaf must hold the token the plan minted, and Jenkins must take it
    as that account."""

    type = "jenkins_token.login"
    silent = True

    def __init__(self, jenkins: Jenkins, leaf: str, key: str):
        super().__init__("jenkins_token.login", "log in to Jenkins with the new token")
        self.jenkins = jenkins
        self.leaf = leaf
        self.key = key

    def run(self, ctx: Context) -> str:
        minted = ctx.staged(value_name(self.key))
        if minted is None:
            raise StepFailed("no new token is staged")
        version = ctx.bao.read(self.leaf)
        if version is None or version.data.get(self.key) != minted:
            raise StepFailed(f"{self.leaf}#{self.key} does not hold the token the plan minted")
        user = connect(self.jenkins, ctx)
        proof = Jenkins(self.jenkins.addr, opener=self.jenkins.open)
        proof.authenticate(user, minted)
        try:
            taken = tokens.who(proof)
        except JenkinsError as e:
            if e.status is None:
                raise
            raise StepFailed(
                f"Jenkins refuses the login as {user} with the new token: {e}"
            ) from None
        if taken != user:
            raise StepFailed(f"Jenkins takes the new token as {taken}, not {user}")
        return f"logged in as {user}"


class Revoke(Step):
    """Revokes each token of the account named the plan's name but the one the plan minted, and
    each named the args' legacy name: the tokens the consumers held before, and a lost mint's. It
    finds them on the account's security page, which must list the minted one, and verifies by
    reading the page again. The plan puts it after the login with the new token.

    It has no undo. A failure before its first revoke, and that revoke refused (a 4xx answer),
    report that the step did not land."""

    type = "jenkins_token.revoke"
    mutates = True

    def __init__(self, jenkins: Jenkins, name: str, legacy: str | None):
        self.names = (name, legacy) if legacy else (name,)
        super().__init__("jenkins_token.revoke", f"revoke {replaced(name, legacy)}")
        self.jenkins = jenkins
        self.no_undo = "a revoked Jenkins API token cannot be restored"

    def run(self, ctx: Context) -> str:
        revoked: list[tokens.Token] = []
        sending = False
        try:
            new = ctx.staged(UUID)
            if new is None:
                raise StepFailed("no new token is staged")
            user = connect(self.jenkins, ctx)
            found = tokens.listed(self.jenkins, user)
            if new not in {token.uuid for token in found}:
                raise StepFailed(
                    f"the security page of Jenkins account {user} lists no token {new}, the one "
                    f"the plan minted: which tokens it replaces is not known"
                )
            old = [token for token in found if token.uuid != new and token.name in self.names]
            for token in old:
                sending = True
                tokens.revoke(self.jenkins, user, token.uuid)
                revoked.append(token)
        except Exception as e:
            if not revoked and (not sending or refused(e)):
                raise not_landed(e) from e
            raise
        held = {token.uuid for token in tokens.listed(self.jenkins, user)}
        if kept := [token.uuid for token in old if token.uuid in held]:
            raise StepFailed(
                f"the security page of Jenkins account {user} still lists token(s) "
                f"{', '.join(kept)} after their revoke"
            )
        if not old:
            return f"nothing to revoke: no other token is named {' or '.join(self.names)}"
        return "revoked " + ", ".join(f"{t.name} ({t.uuid})" for t in old)
