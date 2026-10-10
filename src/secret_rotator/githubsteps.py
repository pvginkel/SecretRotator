"""The generic GitHub step of design §4.2, github.webhook: a repository hook's secret set from KV,
proven by a ping, and the deliveries that failed meanwhile redelivered. It reaches GitHub with the
token rotator/github holds, read when it runs. No detail or error it reports carries a secret."""

import datetime

from secret_rotator.github import GitHub, delivered_at
from secret_rotator.model import Context, Step, StepFailed, wait

CREDENTIALS = "rotator/github"  # key token
PING_BOUND, PING_POLL = 120, 5  # seconds, from the ping's request to its delivery


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
