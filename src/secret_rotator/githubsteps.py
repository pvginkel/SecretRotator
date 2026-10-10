"""The generic GitHub steps of design §4.2. github.webhook: a repository hook's secret set from KV,
proven by a ping, and the deliveries that failed meanwhile redelivered, with the token
rotator/github holds. github.commit: one file's new text committed to a repository's main through
the contents API, with the token a leaf its plan names holds; a kind's own commit step is built on
Commit as it is. Each reads its token when it runs. No detail or error they report carries a
secret."""

import datetime
import functools
from collections.abc import Callable

from secret_rotator.github import GitHub, GitHubError, delivered_at
from secret_rotator.model import Context, Step, StepFailed, not_landed, wait

CREDENTIALS = "rotator/github"  # key token
PING_BOUND, PING_POLL = 120, 5  # seconds, from the ping's request to its delivery
BRANCH = "main"  # the branch every commit goes to
CONFLICT_TRIES = 5  # reads and commits of a file that changes as it is committed
SIGNATURE = "(secret-rotator)"  # the end of every commit message


def connect(github: GitHub, ctx: Context, credentials: str = CREDENTIALS) -> None:
    """Authenticates the client with the token the credentials leaf holds, rotator/github's
    unless a kind names its own."""
    version = ctx.bao.read(credentials)
    if version is None:
        raise StepFailed(
            f"{credentials} cannot be read: no such leaf, or its current version is deleted"
        )
    if not version.data.get("token"):
        raise StepFailed(f"{credentials} has no token")
    github.authenticate(version.data["token"])


def parse_hook(spec: str) -> tuple[str, int]:
    """A github-webhook activator's argument, `<owner>/<repo>/<hook-id>`: the repository and the
    hook's id."""
    repo, _, hook = spec.rpartition("/")
    return repo, int(hook)


def _ok(delivery: dict) -> bool:
    return 200 <= (delivery.get("status_code") or 0) < 300


class GitHubWebhook(Step):
    """Sets a repository hook's secret to the leaf's key as KV holds it when the step runs, the
    rest of the hook's config as it was, and verifies by re-reading the config, which GitHub gives
    with the secret masked. The ping proves the value: GitHub signs it with the hook's secret and
    delivers it asynchronously, so the step waits for a new ping among the hook's deliveries, which
    must be answered 2xx. Last it redelivers, oldest first, each delivery but a ping that GitHub
    first made since the leaf's version the step's first run read was written, and none of whose
    attempts succeeded; that run stages the time for the later ones.

    An activator: a rollback runs it again after the KV undos, which puts the previous secret on
    the hook and redelivers what failed during the plan."""

    type = "github.webhook"
    mutates = True
    activator = True

    def __init__(self, github: GitHub, spec: str, leaf: str, key: str):
        super().__init__(
            f"github.webhook:{spec}", f"set the secret of GitHub hook {spec} from {leaf}#{key}"
        )
        self.github = github
        self.spec = spec
        self.repo, self.hook = parse_hook(spec)
        self.leaf = leaf
        self.key = key

    def run(self, ctx: Context) -> str:
        version = ctx.bao.read(self.leaf)
        if version is None or version.data.get(self.key) is None:
            raise StepFailed(f"{self.leaf}#{self.key} cannot be read")
        memo = f"{self.id}:since"
        if ctx.staged(memo) is None:
            ctx.stage(memo, version.created.isoformat())
        connect(self.github, ctx)
        since = datetime.datetime.fromisoformat(ctx.staged(memo))
        self._set(version.data[self.key])
        ping = self._ping(ctx)
        redelivered = self._redeliver(since)
        detail = f"secret set; ping answered HTTP {ping['status_code']}"
        if not redelivered:
            return f"{detail}; no failed delivery to redeliver"
        return f"{detail}; redelivered {len(redelivered)}: {', '.join(redelivered)}"

    def _set(self, value: str) -> None:
        before = self.github.hook_config(self.repo, self.hook)
        kept = {name: v for name, v in before.items() if name != "secret"}
        self.github.set_hook_config(self.repo, self.hook, kept | {"secret": value})
        after = self.github.hook_config(self.repo, self.hook)
        reread = {name: v for name, v in after.items() if name != "secret"}
        if reread != kept:
            raise StepFailed(
                f"the re-read config of hook {self.spec} is not the config written: "
                f"{sorted(kept.items())} → {sorted(reread.items())}"
            )
        if "secret" not in after:
            raise StepFailed(f"the re-read config of hook {self.spec} holds no secret")

    def _ping(self, ctx: Context) -> dict:
        """The delivery of a new ping, which must be answered 2xx."""
        seen = {d["id"] for d in self.github.deliveries(self.repo, self.hook)}
        self.github.ping(self.repo, self.hook)
        found: list[dict] = []

        def why_not() -> str | None:
            new = self.github.deliveries(self.repo, self.hook)
            found[:] = [d for d in new if d["event"] == "ping" and d["id"] not in seen]
            return None if found else "waiting for GitHub to deliver the ping"

        wait(self.github, ctx, PING_BOUND, PING_POLL, why_not, f"no ping of hook {self.spec}")
        ping = found[-1]
        if not _ok(ping):
            raise StepFailed(
                f"the ping of hook {self.spec} failed: {ping.get('status')} (delivery {ping['id']})"
            )
        return ping

    def _redeliver(self, since: datetime.datetime) -> list[str]:
        """The guids it redelivered."""
        attempts: dict[str, list[dict]] = {}
        for d in self.github.deliveries(self.repo, self.hook, since):
            attempts.setdefault(d["guid"], []).append(d)
        failed = [
            original
            for tries in attempts.values()
            if (original := next((d for d in tries if not d.get("redelivery")), None))
            and original["event"] != "ping"
            and not any(_ok(d) for d in tries)
        ]
        failed.sort(key=delivered_at)
        for d in failed:
            self.github.redeliver(self.repo, self.hook, d["id"])
        return [d["guid"] for d in failed]


class Commit(Step):
    """A step that commits a file of a repository on main through GitHub's contents API, with the
    token its credentials leaf holds: one its plan names, never rotator/github's, which may change
    webhooks only. github.commit and a kind's own commit step (terraform.commit_keeper) are built
    on it. The commit's SHA is staged (staged), as argocd.sync takes it (argocdsteps.Pushed)."""

    mutates = True

    def __init__(self, id: str, title: str, github: GitHub, repo: str, credentials: str):
        super().__init__(id, title)
        self.github = github
        self.repo = repo
        self.credentials = credentials
        self.staged = f"{id}:commit"

    def commit(
        self,
        ctx: Context,
        path: str,
        text: Callable[[str], str],
        message: str,
        *,
        keep: str | None = None,
    ) -> str | None:
        """Commits what text makes of the file's text over it as `<message> (secret-rotator)`,
        stages the commit's SHA and reads the file back, which must hold what it committed: the
        SHA, or None when the file holds that text already. A conflict, the file changed since it
        was read, is read again and retried, CONFLICT_TRIES times; a push to another file
        meanwhile is none. keep: the staging name of the text the commit replaces, staged before
        it until a commit of the step landed.

        A failure before the commit, the commit refused (a 4xx answer but a conflict) and a
        conflict at every try report that it did not land; a commit whose answer is lost (a 5xx, a
        transport error) counts as landed."""
        for _ in range(CONFLICT_TRIES):
            try:
                found = self.github.contents(self.repo, path, BRANCH)
                if found is None:
                    # GitHub answers a token that cannot see a private repository the same.
                    raise StepFailed(
                        f"{self.repo} has no {path} on {BRANCH}, or the token in "
                        f"{self.credentials} lacks the repository"
                    )
                held, blob = found
                new = text(held)
                if new == held:
                    return None
                if keep is not None and ctx.staged(self.staged) is None:
                    ctx.stage(keep, held)
            except Exception as e:
                raise not_landed(e) from e
            try:
                sha = self.github.commit_file(
                    self.repo, path, new, blob=blob, branch=BRANCH, message=f"{message} {SIGNATURE}"
                )
            except GitHubError as e:
                if e.status == 409:
                    ctx.progress(f"{path} changed as it was committed: reading it again")
                    continue
                if e.status is not None and e.status < 500:
                    raise not_landed(e) from e
                raise
            ctx.stage(self.staged, sha)
            back = self.github.contents(self.repo, path, BRANCH)
            if back is None or back[0] != new:
                raise StepFailed(
                    f"{self.repo} {path} on {BRANCH} does not hold the text {sha[:7]} committed"
                )
            return sha
        raise StepFailed(
            f"{self.repo} {path} changed under each of {CONFLICT_TRIES} commits", landed=False
        )

    def contains(self, ctx: Context, revision: str, commit: str) -> bool:
        """Whether the revision is the commit or a later head that has it, by GitHub's compare."""
        connect(self.github, ctx, self.credentials)
        return self.github.compare(self.repo, commit, revision) in ("identical", "ahead")


class GitHubCommit(Commit):
    """Commits new text for one file to a repository's main: what text makes of the text the file
    holds there, which the plan gives. A run that finds the file holding it commits nothing more,
    as a re-run after its commit landed does.

    Its undo commits back the text the step's commit replaced, as `revert "<message>"
    (secret-rotator)`, and leaves a file that holds it already as it is, as after a commit that
    did not land. With no_undo, why the commit cannot be taken back, the step has none.

    Every failure before the commit, the token's leaf unreadable included, reports that the step
    did not land, as Commit.commit's do."""

    type = "github.commit"

    def __init__(
        self,
        github: GitHub,
        repo: str,
        path: str,
        text: Callable[[Context, str], str],
        *,
        credentials: str,
        message: str,
        no_undo: str = "",
    ):
        super().__init__(
            f"{self.type}:{repo}/{path}", f"commit {path} to {repo}", github, repo, credentials
        )
        self.path = path
        self.text = text
        self.message = message
        self.before = f"{self.id}:before"  # the staging name of the text the commit replaced
        if no_undo:
            self.undo = None
            self.no_undo = no_undo

    def run(self, ctx: Context) -> str:
        try:
            connect(self.github, ctx, self.credentials)
        except Exception as e:
            raise not_landed(e) from e
        text = functools.partial(self.text, ctx)
        keep = None if self.undo is None else self.before
        sha = self.commit(ctx, self.path, text, self.message, keep=keep)
        if sha is None:
            return f"{self.path} on {BRANCH} holds the new text already"
        return f"{sha[:7]}: {self.path} committed to {BRANCH}"

    def undo(self, ctx: Context) -> str:
        before = ctx.staged(self.before)
        if before is None:
            return f"nothing to commit back: the step committed nothing to {self.path}"
        connect(self.github, ctx, self.credentials)
        sha = self.commit(ctx, self.path, lambda held: before, f'revert "{self.message}"')
        if sha is None:
            return f"{self.path} on {BRANCH} holds the text it held before"
        return f"{sha[:7]}: {self.path} committed back"
