"""The generic Kubernetes steps of design §4.2, eso.sync and k8s.rollout: activators with one target
each, whose undo is to run again after a rollback's undos (design §4.5). A target that does not
exist when the step starts counts as done, forward and in a rollback, with a line saying so."""

from secret_rotator.cluster import Cluster, Ref, Workload, condition, owning_app
from secret_rotator.kube import KubeError
from secret_rotator.model import Context, Step, StepFailed, wait

FORCE_SYNC = "force-sync"
RESTARTED_AT = "kubectl.kubernetes.io/restartedAt"
SYNC_BOUND, SYNC_POLL = 120, 2  # seconds
ROLLOUT_BOUND, ROLLOUT_POLL = 300, 5
GONE = "does not exist: nothing to {}, counted as done"


def _mark(ctx: Context) -> str:
    """The value a step annotates its target with. ESO refreshes on a change of the
    ExternalSecret's metadata and a controller rolls out on a change of the pod template, so two
    runs of a step, a retry or a rollback's re-run, must write different values: microseconds."""
    return ctx.now.isoformat(timespec="microseconds")


class EsoSync(Step):
    """Forces ESO to sync one ExternalSecret now and waits for it to be Ready with a new
    syncedResourceVersion."""

    type = "eso.sync"
    mutates = True
    activator = True

    def __init__(self, cluster: Cluster, es: Ref):
        super().__init__(f"eso.sync:{es}", f"sync ExternalSecret {es}")
        self.cluster = cluster
        self.es = es

    def _read(self) -> dict:
        es = self.cluster.kube.get(self.es.path)
        if es is None:
            raise StepFailed(f"ExternalSecret {self.es} does not exist")
        return es

    def run(self, ctx: Context) -> str:
        es = self.cluster.kube.get(self.es.path)
        if es is None:
            return GONE.format("sync")
        before = (es.get("status") or {}).get("syncedResourceVersion")
        annotations = {"metadata": {"annotations": {FORCE_SYNC: _mark(ctx)}}}
        self.cluster.kube.merge_patch(self.es.path, annotations)

        def why_not() -> str | None:
            es = self._read()
            ready = condition(es, "Ready")
            synced = (es.get("status") or {}).get("syncedResourceVersion")
            if synced != before and ready.get("status") == "True":
                return None
            if ready.get("status") == "False":
                return ready.get("message") or "not Ready"
            return "waiting for ESO to sync it"

        wait(self.cluster.kube, ctx, SYNC_BOUND, SYNC_POLL, why_not, f"{self.es} did not sync")
        return "synced, Ready"


class K8sRollout(Step):
    """A rollout restart of one workload; done when every pod is Ready on the new template and
    the Argo Application that deployed it is Healthy, within its bound: ROLLOUT_BOUND unless a kind
    raises it for a workload that starts slower."""

    type = "k8s.rollout"
    mutates = True
    activator = True

    def __init__(self, cluster: Cluster, workload: Workload, *, bound: int = ROLLOUT_BOUND):
        super().__init__(f"k8s.rollout:{workload}", f"roll out {workload}")
        self.cluster = cluster
        self.workload = workload
        self.bound = bound  # seconds

    def run(self, ctx: Context) -> str:
        restart = {"spec": {"template": {"metadata": {"annotations": {RESTARTED_AT: _mark(ctx)}}}}}
        try:
            patched = self.cluster.kube.merge_patch(self.workload.path, restart)
        except KubeError as e:
            if e.status != 404:
                raise
            return GONE.format("roll out")

        def why_not() -> str | None:
            return self.cluster.health(self.workload)

        wait(
            self.cluster.kube,
            ctx,
            self.bound,
            ROLLOUT_POLL,
            why_not,
            f"{self.workload} not Ready",
        )
        app = owning_app(patched)
        return "Ready" + (f", Application {app} Healthy" if app else "")
