"""The cnpg-role kind's own steps (design §4.2: custom steps are the plugin's): cnpg.reconcile,
through the Kubernetes API with the rotator's cluster identity, and cnpg.login, to Postgres as the
role. No detail or error they report carries a password."""

from collections.abc import Callable

from secret_rotator.cluster import Cluster
from secret_rotator.model import Context, Step, StepFailed, value_name, wait

CNPG = "/apis/postgresql.cnpg.io/v1"
# The annotation `kubectl cnpg reload` sets. CNPG 1.30.1 reconciles managed roles when the
# Cluster object changes, and on a passwordSecret's change only when that Secret carries the
# cnpg.io/reload label, which the ExternalSecrets' Secrets do not.
RELOADED_AT = "cnpg.io/reloadedAt"
APPLY_BOUND, APPLY_POLL = 300, 5  # seconds

# Logs in as a role: host, port, role, password; a model.StepFailed when the login fails.
Login = Callable[[str, int, str, str], None]


class Reconcile(Step):
    """Has CNPG apply the role's password from its passwordSecret, and waits until the Cluster's
    status.managedRolesStatus.passwordStatus records the Secret's current resourceVersion as the
    role's. Unless it records that already, the step annotates the Cluster as `kubectl cnpg
    reload` does. An activator: a rollback re-runs it once the leaf and the Secrets are back on
    the old password, which CNPG then applies."""

    type = "cnpg.reconcile"
    mutates = True
    activator = True

    def __init__(self, cluster: Cluster, namespace: str, name: str, role: str):
        super().__init__("cnpg.reconcile", f"have CNPG apply the password of role {role}")
        self.cluster = cluster
        self.namespace = namespace
        self.ref = f"{namespace}/{name}"
        self.path = f"{CNPG}/namespaces/{namespace}/clusters/{name}"
        self.role = role

    def _cluster(self) -> dict:
        obj = self.cluster.kube.get(self.path)
        if obj is None:
            raise StepFailed(f"CNPG Cluster {self.ref} does not exist")
        return obj

    def _password_secret(self, cluster: dict) -> str:
        roles = ((cluster.get("spec") or {}).get("managed") or {}).get("roles") or []
        role = next((r for r in roles if r.get("name") == self.role), None)
        if role is None:
            raise StepFailed(f"CNPG Cluster {self.ref} manages no role {self.role}")
        name = (role.get("passwordSecret") or {}).get("name")
        if not name:
            raise StepFailed(f"role {self.role} of CNPG Cluster {self.ref} has no passwordSecret")
        return name

    def _resource_version(self, name: str) -> str:
        secret = self.cluster.kube.get(f"/api/v1/namespaces/{self.namespace}/secrets/{name}")
        if secret is None:
            raise StepFailed(
                f"Secret {self.namespace}/{name}, role {self.role}'s passwordSecret, does not exist"
            )
        return secret["metadata"]["resourceVersion"]

    def _why_not(self) -> str | None:
        """None when CNPG applied the passwordSecret's current version; else what it reports."""
        cluster = self._cluster()
        name = self._password_secret(cluster)
        want = self._resource_version(name)
        status = (cluster.get("status") or {}).get("managedRolesStatus") or {}
        applied = ((status.get("passwordStatus") or {}).get(self.role) or {}).get("resourceVersion")
        if applied == want:
            return None
        reported = [s for s, roles in (status.get("byStatus") or {}).items() if self.role in roles]
        reported += [
            " ".join(m.split()) for m in (status.get("cannotReconcile") or {}).get(self.role) or []
        ]
        why = f"role {self.role} has Secret {name} version {applied or 'none'} applied, not {want}"
        return why + (f"; CNPG reports {'; '.join(reported)}" if reported else "")

    def run(self, ctx: Context) -> str:
        if self._why_not() is not None:
            mark = ctx.now.isoformat(timespec="microseconds")
            annotations = {"metadata": {"annotations": {RELOADED_AT: mark}}}
            self.cluster.kube.merge_patch(self.path, annotations)
            wait(
                self.cluster.kube,
                ctx,
                APPLY_BOUND,
                APPLY_POLL,
                self._why_not,
                f"CNPG did not apply the password of role {self.role}",
            )
        return f"CNPG Cluster {self.ref} applied it"


class PostgresLogin(Step):
    """Logs in to Postgres as the role with the new password, through the address Terraform
    reaches the primary by."""

    type = "cnpg.login"

    def __init__(self, login: Login, host: str, port: int, role: str, key: str):
        super().__init__("cnpg.login", f"log in to Postgres as {role} with the new password")
        self.login = login
        self.host = host
        self.port = port
        self.role = role
        self.key = key  # the data key whose new password it logs in with

    def run(self, ctx: Context) -> str:
        password = ctx.staged(value_name(self.key))
        if password is None:
            raise StepFailed("no new password is staged")
        self.login(self.host, self.port, self.role, password)
        return f"logged in to {self.host}:{self.port} as {self.role}"
