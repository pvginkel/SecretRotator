"""The terraform kind's own steps (design §4.2: custom steps are the plugin's):
terraform.commit_keeper and terraform.prove_remint. GitHub is reached under the token
rotator/terraform/credentials holds, read when a step runs: the kind's own, not rotator/github's.
No detail or error they report carries a credential or a Secret's fingerprint."""

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass

from secret_rotator.argocdsteps import ArgocdSync
from secret_rotator.cluster import APPLICATIONS, Cluster, SecretRef
from secret_rotator.github import GitHub, GitHubError
from secret_rotator.githubsteps import connect
from secret_rotator.kinds.terraform import keepers
from secret_rotator.model import Context, Step, StepFailed, not_landed

CREDENTIALS = "rotator/terraform/credentials"  # key token
BRANCH = "main"
COMMIT = "terraform:commit"  # the staging name of the commit's SHA
CONFLICT_TRIES = 5
# hook.repo as the releases chart renders it (ArgoCDDeploy releases/templates/_helpers.tpl).
HOOK_REPO = re.compile(r"https://github\.com/(?P<repo>[^/]+/[^/]+?)(?:\.git)?")
NO_UNDO = (
    "a committed keeper cannot be taken back: committing the value it replaced mints a third "
    "credential, not the old one"
)


@dataclass(frozen=True)
class Keeper:
    """A marker's args: the deploy repo and the app's directory in it ("" for its root), the Argo
    CD Application whose hook applies it, the keeper's name in rotation_epoch and the Secret
    Terraform writes the credential to."""

    repo: str
    path: str
    app: str
    name: str
    secret: SecretRef

    @classmethod
    def of(cls, args: Mapping) -> "Keeper":
        namespace, name = args["secret"].split("/")
        return cls(
            args["repo"],
            args.get("path", ""),
            args["app"],
            args["keeper"],
            SecretRef(namespace, name),
        )


@dataclass(frozen=True)
class Made:
    """A commit the process made: the Secret's fingerprint before it, and the keeper value it
    set in the file it wrote."""

    before: str
    value: str
    file: str


def fingerprint(cluster: Cluster, secret: SecretRef) -> str:
    """A digest that tells two versions of the Secret's data apart."""
    obj = cluster.kube.get(secret.path)
    if obj is None:
        raise StepFailed(f"Secret {secret} does not exist")
    return hashlib.sha256(json.dumps(obj.get("data") or {}, sort_keys=True).encode()).hexdigest()


def keeper_file(application: dict, keeper: Keeper) -> str:
    """The keeper file the Application's PreSync hook reads: config/<hook.stage>/rotation.tfvars
    in the directory of the repo it applies. StepFailed when the hook applies another repo or
    directory than the marker names, or the Application tracks another branch than main: no sync
    of it would take the commit."""
    source = (application.get("spec") or {}).get("source") or {}
    params = {
        p.get("name"): p.get("value") for p in (source.get("helm") or {}).get("parameters") or []
    }
    app = keeper.app
    stage = params.get("hook.stage")
    if not stage:
        raise StepFailed(f"Argo Application {app} has no hook.stage: no PreSync hook applies it")
    hook = params.get("hook.repo") or ""
    found = HOOK_REPO.fullmatch(hook)
    if found is None or found["repo"] != keeper.repo:
        raise StepFailed(
            f"Argo Application {app}'s hook applies {hook or 'no repo'}, not the marker's "
            f"{keeper.repo}"
        )
    path = params.get("hook.path") or ""
    if path != keeper.path:
        raise StepFailed(
            f"Argo Application {app}'s hook applies {path or 'the root'} of {keeper.repo}, not "
            f"the marker's {keeper.path or 'root'}"
        )
    if source.get("targetRevision") != BRANCH:
        raise StepFailed(
            f"Argo Application {app} tracks {source.get('targetRevision')}, not {BRANCH}"
        )
    return f"{path}/config/{stage}/rotation.tfvars" if path else f"config/{stage}/rotation.tfvars"


class CommitKeeper(Step):
    """Commits a new value of the marker's keeper to the keeper file the Application's hook reads,
    on main, as `rotate <keeper> (secret-rotator)`, and stages the commit's SHA. Before it commits
    it fingerprints the Secret the marker names, into the process's memory alone, with the commit:
    what the proof compares the Secret with.

    The value is the UTC time the step started, to the second; the step refuses one that does not
    sort after the value the file holds, so the file never held it. A conflict, the file changed
    since the step read it, is read again and retried. A re-run in the process that made the staged
    commit commits nothing; any other commits afresh.

    Every failure before the commit, and the commit refused (a 4xx answer but a conflict), report
    that the step did not land."""

    type = "terraform.commit_keeper"
    mutates = True
    no_undo = NO_UNDO
    staged = COMMIT

    def __init__(self, github: GitHub, cluster: Cluster, made: dict[str, Made], keeper: Keeper):
        super().__init__(
            f"{self.type}:{keeper.app}/{keeper.name}",
            f"commit a new {keeper.name} keeper to {keeper.repo}",
        )
        self.github = github
        self.cluster = cluster
        self.made = made  # the process's commits by SHA: the kind's memory
        self.keeper = keeper

    def run(self, ctx: Context) -> str:
        staged = ctx.staged(COMMIT)
        if staged in self.made:
            return f"{staged[:7]} committed"
        try:
            application = self.cluster.kube.get(f"{APPLICATIONS}/{self.keeper.app}")
            if application is None:
                raise StepFailed(f"Argo Application {self.keeper.app} does not exist")
            file = keeper_file(application, self.keeper)
            connect(self.github, ctx, CREDENTIALS)
            before = fingerprint(self.cluster, self.keeper.secret)
        except Exception as e:
            raise not_landed(e) from e
        value = ctx.now.strftime("%Y-%m-%dT%H:%M:%SZ")
        sha = self._commit(ctx, file, value)
        self.made[sha] = Made(before, value, file)
        ctx.stage(COMMIT, sha)
        return f"{sha[:7]}: {self.keeper.name} = {value} in {file}"

    def _commit(self, ctx: Context, file: str, value: str) -> str:
        repo, name = self.keeper.repo, self.keeper.name
        for _ in range(CONFLICT_TRIES):
            try:
                found = self.github.contents(repo, file, BRANCH)
                if found is None:
                    # GitHub answers a token that cannot see a private repository the same.
                    raise StepFailed(
                        f"{repo} has no {file} on {BRANCH}, or the token in {CREDENTIALS} lacks "
                        "the repository"
                    )
                text, blob = found
                try:
                    head, epochs = keepers.parse(text)
                except ValueError as e:
                    raise StepFailed(f"{repo} {file}: {e}") from None
                held = epochs.get(name)
                if held is not None and held >= value:
                    raise StepFailed(
                        f"{repo} {file} holds {name} = {held}, which does not sort before the new "
                        f"{value}: a value it held re-mints nothing"
                    )
                new = keepers.render(head, epochs | {name: value})
            except Exception as e:
                raise not_landed(e) from e
            try:
                return self.github.commit_file(
                    repo,
                    file,
                    new,
                    blob=blob,
                    branch=BRANCH,
                    message=f"rotate {name} (secret-rotator)",
                )
            except GitHubError as e:
                if e.status == 409:
                    ctx.progress(f"{file} changed as it was committed: reading it again")
                    continue
                if e.status is not None and e.status < 500:
                    raise not_landed(e) from e
                raise
        raise StepFailed(
            f"{repo} {file} changed under each of {CONFLICT_TRIES} commits", landed=False
        )

    def contains(self, ctx: Context, revision: str, commit: str) -> bool:
        """Whether the revision is the commit or a later head that has it, by GitHub's compare."""
        connect(self.github, ctx, CREDENTIALS)
        return self.github.compare(self.keeper.repo, commit, revision) in ("identical", "ahead")


class ProveRemint(Step):
    """Proves the hook's Terraform re-minted the credential (ruling F1): the Secret the marker
    names holds other data than before the commit, once a sync of a revision that contains the
    commit succeeded, which the step waits for again first. An unchanged Secret fails it, naming
    the keeper the commit set and the file it wrote.

    The fingerprint before the commit lives in the memory of the process that made it. In any
    other process, one that took the plan up past its commit, the step commits a fresh keeper
    itself, fingerprinted first, and waits for that commit's sync before it proves anything."""

    type = "terraform.prove_remint"
    mutates = True  # it may commit
    no_undo = NO_UNDO

    def __init__(self, commit: CommitKeeper, sync: ArgocdSync):
        secret = commit.keeper.secret
        super().__init__(f"{self.type}:{secret}", f"prove Terraform re-minted Secret {secret}")
        self.commit = commit
        self.sync = sync

    def run(self, ctx: Context) -> str:
        staged = ctx.staged(COMMIT)
        again = ""
        if staged not in self.commit.made:
            ctx.progress(f"{staged[:7]} was committed by another process: committing afresh")
            again = f"; committed afresh, {self.commit.run(ctx)}"
        self.sync.run(ctx)
        commit = ctx.staged(COMMIT)
        made = self.commit.made[commit]
        keeper = self.commit.keeper
        if fingerprint(self.commit.cluster, keeper.secret) == made.before:
            raise StepFailed(
                f"Secret {keeper.secret} did not change: the sync of {commit[:7]} took "
                f"{keeper.name} = {made.value} in {keeper.repo} {made.file}, and its Terraform "
                f"re-minted nothing"
            )
        return f"Secret {keeper.secret} changed{again}"
