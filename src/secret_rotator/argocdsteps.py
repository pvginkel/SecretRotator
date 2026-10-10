"""The generic Argo CD step of design §4.2, argocd.sync: one Application synced and verified through
the Application object in Argo CD's namespace, since the rotator holds no Argo CD API token. An
activator: its undo is to run again after a rollback's undos (design §4.5). An Application that does
not exist when the step starts counts as done, forward and in a rollback, with a line saying so."""

from typing import Protocol

from secret_rotator.cluster import APPLICATIONS, Cluster, sync_state
from secret_rotator.k8ssteps import GONE
from secret_rotator.kube import KubeError
from secret_rotator.model import Context, Step, StepFailed, wait

# A sync runs the PreSync hook's terraform init and apply first, and auto-sync retries a failed one
# three times at a backoff from 30 s that doubles.
SYNC_BOUND, SYNC_POLL = 900, 5  # seconds
# How long a pushed commit is left to the auto-sync GitHub's push webhook starts, before the step
# requests the sync itself.
AUTO_SYNC_GRACE = 60
INITIATOR = "secret-rotator"  # the username of the operations the step requests
RUNNING = ("Running", "Terminating")
FAILED = ("Failed", "Error")


class Pushed(Protocol):
    """A commit a step before argocd.sync pushed to the branch the Application tracks. The
    Application carries revisions, not their ancestry, so that step tells whether a revision
    contains the commit."""

    staged: str  # the staging name of the commit's SHA

    def contains(self, ctx: Context, revision: str, commit: str) -> bool:
        """Whether the revision is the commit or a later head of the branch that has it."""


def _revision(op: dict) -> str | None:
    """The revision an operation syncs: as resolved once it started, else as requested."""
    resolved = (op.get("syncResult") or {}).get("revision")
    return resolved or ((op.get("operation") or {}).get("sync") or {}).get("revision")


class ArgocdSync(Step):
    """Waits for a sync of one Application that succeeded, then verifies the Application Synced
    and, unless healthy is False, Healthy; healthy False leaves its health to the steps after it.

    With pushed, the sync is one at a revision that contains the commit: the commit or any later
    head of the branch, which moves on its own. An Application that auto-syncs gets
    AUTO_SYNC_GRACE to start it. Without pushed, as an argocd-sync: activator after a KV write the
    hook's Terraform reads, it is any sync that started after this run of the step read the
    Application.

    The step requests a sync itself only while no operation runs or is requested, by a patch the
    apiserver refuses once the Application changed since it was read, and with no revision: Argo
    CD then syncs the head of the branch the Application tracks, with the prune, sync options and
    retry the sync policy gives auto-sync. A sync it waits for that failed fails the step with
    Argo CD's message, and the step stages the sync's start, so a Retry requests a new sync rather
    than failing on that one again."""

    type = "argocd.sync"
    mutates = True
    activator = True

    def __init__(
        self, cluster: Cluster, app: str, pushed: Pushed | None = None, *, healthy: bool = True
    ):
        super().__init__(f"argocd.sync:{app}", f"sync Argo CD Application {app}")
        self.cluster = cluster
        self.app = app
        self.pushed = pushed
        self.healthy = healthy
        self.path = f"{APPLICATIONS}/{app}"
        self.memo = f"{self.id}:failed"  # the start of the sync a run failed on

    def run(self, ctx: Context) -> str:
        application = self.cluster.kube.get(self.path)
        if application is None:
            return GONE.format("sync")
        commit = None
        if self.pushed is not None:
            commit = ctx.staged(self.pushed.staged)
            if commit is None:
                raise StepFailed(f"no commit is staged as {self.pushed.staged}")
        sync = _Sync(self, ctx, commit, application)
        wanted = "Synced and Healthy" if self.healthy else "Synced"
        wait(
            self.cluster.kube,
            ctx,
            SYNC_BOUND,
            SYNC_POLL,
            sync.why_not,
            f"Argo Application {self.app} not {wanted}",
        )
        return sync.detail


class _Sync:
    """One run of an argocd.sync step, from its first read of the Application."""

    def __init__(self, step: ArgocdSync, ctx: Context, commit: str | None, application: dict):
        self.step = step
        self.ctx = ctx
        self.kube = step.cluster.kube
        self.commit = commit
        # The operation the Application held when the run read it first, by its start.
        self.before = _operation(application).get("startedAt")
        self.failed = ctx.staged(step.memo)
        self.contains: dict[str, bool] = {}
        auto = "automated" in ((application.get("spec") or {}).get("syncPolicy") or {})
        grace = commit is not None and auto
        # The earliest the step requests a sync, and what it waits for until then.
        self.request_at = self.kube.clock() + (AUTO_SYNC_GRACE if grace else 0)
        self.waiting = f"waiting for auto-sync to start a sync of {commit[:7]}" if grace else ""
        self.detail = ""

    def why_not(self) -> str | None:
        application = self.kube.get(self.step.path)
        if application is None:
            raise StepFailed(f"Argo Application {self.step.app} does not exist")
        op = _operation(application)
        phase, revision = op.get("phase"), _revision(op)
        awaited = bool(op) and self.awaited(op, revision)
        retry = awaited and phase in FAILED and op["startedAt"] == self.failed
        if awaited and not retry:
            return self.judge(application, op, phase, revision)
        if phase in RUNNING:
            return f"waiting for the sync of {_shown(revision)} under way to end"
        if application.get("operation") is not None:
            return "waiting for Argo CD to start the requested operation"
        if not retry and self.kube.clock() < self.request_at:
            return self.waiting
        return self.request(application)

    def awaited(self, op: dict, revision: str | None) -> bool:
        """Whether the operation is a sync the run waits for."""
        if self.commit is None:
            return op["startedAt"] != self.before
        if revision is None:
            return False
        if revision not in self.contains:
            self.contains[revision] = revision == self.commit or self.step.pushed.contains(
                self.ctx, revision, self.commit
            )
        return self.contains[revision]

    def judge(
        self, application: dict, op: dict, phase: str | None, revision: str | None
    ) -> str | None:
        shown = _shown(revision)
        message = op.get("message") or ""
        if phase in RUNNING:
            return f"syncing {shown}" + (f": {message}" if message else "")
        if phase in FAILED:
            self.ctx.stage(self.step.memo, op["startedAt"])
            failed = f"the sync of {self.step.app} at {shown} ended {phase}"
            raise StepFailed(failed + (f": {message}" if message else ""))
        if application.get("operation") is not None:
            return "waiting for the operation requested after it"
        sync, health = sync_state(application)
        if sync != "Synced":
            return f"{sync} after the sync of {shown}"
        if self.step.healthy and health != "Healthy":
            return f"Synced, {health}"
        initiated = (op.get("operation") or {}).get("initiatedBy") or {}
        by = "auto-sync" if initiated.get("automated") else initiated.get("username", "Argo CD")
        self.detail = f"{shown} synced by {by}: Synced" + (", Healthy" if self.step.healthy else "")
        return None

    def request(self, application: dict) -> str:
        policy = application["spec"].get("syncPolicy") or {}
        sync = {"prune": bool((policy.get("automated") or {}).get("prune"))}
        if policy.get("syncOptions"):
            sync["syncOptions"] = policy["syncOptions"]
        operation = {"initiatedBy": {"username": INITIATOR}, "sync": sync}
        if policy.get("retry"):
            operation["retry"] = policy["retry"]
        version = application["metadata"]["resourceVersion"]
        patch = {"metadata": {"resourceVersion": version}, "operation": operation}
        try:
            self.kube.merge_patch(self.step.path, patch)
        except KubeError as e:
            if e.status != 409:
                raise
            return "the Application changed as the sync was requested: reading it again"
        self.request_at = self.kube.clock() + AUTO_SYNC_GRACE
        self.waiting = "waiting for the sync it requested to start"
        return "requested a sync of the head of the branch it tracks"


def _shown(revision: str | None) -> str:
    return revision[:7] if revision else "the branch's head"


def _operation(application: dict) -> dict:
    """The Application's last operation; empty when it has none."""
    return (application.get("status") or {}).get("operationState") or {}
