"""CloudNativePG on the fake prd apiserver, and the Postgres it manages, for the cnpg-role kind.

The cluster holds the readers of the postgres-pas leaves as prd does (2026-10-08): CNPG's Cluster
postgres in postgres-pas-prd with its two managed roles, each reading a passwordSecret that an
ExternalSecret of its leaf writes; pgadmin's ExternalSecret and Deployment; and the Argo CD hooks'
ExternalSecret, which reads terraform-admin among other leaves. Its ESO writes an ExternalSecret's
Secret from the fake OpenBao when it syncs, a new resourceVersion where the data changed. CNPG
applies a role's password when the Cluster object changes, as CNPG 1.30.1 does, once the fake
clock has passed its lag; a change of a Secret alone has it apply nothing."""

import base64
import urllib.parse

from fake_cluster import FakeCluster, compliant_objects, externalsecret, pod_spec, workload

from secret_rotator.kinds.cnpg_role.postgres import PostgresError

NAMESPACE, NAME = "postgres-pas-prd", "postgres"
CLUSTER_PATH = f"/apis/postgresql.cnpg.io/v1/namespaces/{NAMESPACE}/clusters/{NAME}"
PGADMIN = "eso/prd/postgres-pas/pgadmin-admin"
TERRAFORM = "eso/prd/postgres-pas/terraform-admin"
ROLES = {"pgadmin_admin": "postgres-pgadmin-admin", "terraform_admin": "postgres-terraform-admin"}


def secret_of(ns, name, data, version):
    return {
        "metadata": {"namespace": ns, "name": name, "resourceVersion": str(version)},
        "data": {k: base64.b64encode(v.encode()).decode() for k, v in data.items()},
    }


def cnpg_cluster(versions):
    """The Cluster, each role's password applied from its Secret at that resourceVersion."""
    return {
        "metadata": {"namespace": NAMESPACE, "name": NAME, "annotations": {}},
        "spec": {
            "managed": {
                "roles": [
                    {"name": role, "login": True, "passwordSecret": {"name": secret}}
                    for role, secret in ROLES.items()
                ]
            }
        },
        "status": {
            "managedRolesStatus": {
                "byStatus": {"reconciled": sorted(ROLES)},
                "passwordStatus": {
                    role: {"resourceVersion": str(versions[role]), "transactionID": 1}
                    for role in ROLES
                },
            }
        },
    }


def cnpg_objects():
    """(resource, object) of the compliant cluster with the postgres-pas readers."""
    hooks = externalsecret(
        "argocd-hooks",
        "argocd-hook-credentials",
        data=[(TERRAFORM, "password"), ("eso/prd/argocd/prd/webhook", "secret")],
    )
    return [
        *compliant_objects(),
        (
            "externalsecrets",
            externalsecret(
                NAMESPACE,
                "postgres-pgadmin-admin",
                data=[(PGADMIN, "username"), (PGADMIN, "password")],
            ),
        ),
        (
            "externalsecrets",
            externalsecret(
                NAMESPACE,
                "postgres-terraform-admin",
                data=[(TERRAFORM, "username"), (TERRAFORM, "password")],
            ),
        ),
        (
            "externalsecrets",
            externalsecret("pgadmin-prd", "postgres-pgadmin-admin", data=[(PGADMIN, "password")]),
        ),
        ("externalsecrets", hooks),
        (
            "deployments",
            workload(
                "Deployment",
                "pgadmin-prd",
                "pgadmin",
                pod_spec(env=["postgres-pgadmin-admin"]),
                app="pgadmin-prd",
            ),
        ),
        (
            "applications",
            {
                "metadata": {"namespace": "argocd-prd", "name": "pgadmin-prd"},
                "status": {"health": {"status": "Healthy"}},
            },
        ),
    ]


class FakePostgres:
    """The Postgres the roles log in to, as the cnpg-role kind's login: each role's password."""

    def __init__(self, passwords):
        self.passwords = dict(passwords)
        self.logins = []  # (host, port, role)
        self.refused = set()  # roles whose logins pg_hba rejects

    def __call__(self, host, port, user, password):
        self.logins.append((host, port, user))
        if user in self.refused:
            raise PostgresError(
                f"the login to {host}:{port} as {user} fails: connection failed: FATAL: "
                f'pg_hba.conf rejects connection for host "10.1.0.9", user "{user}", database '
                f'"postgres"'
            )
        if self.passwords.get(user) != password:
            raise PostgresError(
                f"the login to {host}:{port} as {user} fails: connection failed: FATAL: password "
                f'authentication failed for user "{user}"'
            )


class FakeCnpg(FakeCluster):
    """The fake apiserver with CNPG's Cluster and the Secrets of the ExternalSecrets, written
    from the OpenBao fake."""

    def __init__(self, bao, postgres, objects=None, *, cnpg_lag=4):
        super().__init__(cnpg_objects() if objects is None else objects)
        self.bao = bao
        self.postgres = postgres
        self.cnpg_lag = cnpg_lag
        self.version = 100
        self.journal = []  # ("secret", "<ns>/<name>", its password) and ("apply", role)
        # role -> the cannotReconcile message CNPG reports instead of applying its password
        self.unreconciled = {}
        for es in [o for (r, _, _), o in list(self.objects.items()) if r == "externalsecrets"]:
            self.write_secret(es)
        none = {"metadata": {"resourceVersion": "0"}}
        versions = {
            role: self.objects.get(("secrets", NAMESPACE, secret), none)["metadata"][
                "resourceVersion"
            ]
            for role, secret in ROLES.items()
        }
        self.add("clusters", cnpg_cluster(versions))

    def __call__(self, req):
        response = super().__call__(req)
        path = urllib.parse.urlsplit(req.full_url).path
        if req.get_method() == "PATCH" and path == CLUSTER_PATH:
            self.later(self.cnpg_lag, self.reconcile)
        return response

    def secret(self, ns, name):
        """The Secret's data, decoded."""
        data = self.get("secrets", ns, name)["data"]
        return {k: base64.b64decode(v).decode() for k, v in data.items()}

    def write_secret(self, es):
        """What ESO writes into the ExternalSecret's Secret: each data entry from its leaf."""
        ns, name = es["metadata"]["namespace"], es["metadata"]["name"]
        data = {}
        for entry in es["spec"].get("data") or []:
            ref = entry["remoteRef"]
            held = self.bao.leaves.get(ref["key"])
            if held is not None and ref["property"] in held["data"]:
                data[entry["secretKey"]] = held["data"][ref["property"]]
        current = self.objects.get(("secrets", ns, name))
        if current is not None and self.secret(ns, name) == data:
            return
        self.version += 1
        self.add("secrets", secret_of(ns, name, data, self.version))
        self.journal.append(("secret", f"{ns}/{name}", data.get("password")))

    def eso_sync(self, es):
        super().eso_sync(es)
        ref = f"{es['metadata']['namespace']}/{es['metadata']['name']}"
        if ref not in self.eso_failing:
            self.write_secret(es)

    def reconcile(self):
        """CNPG's role reconcile: each role whose Secret's resourceVersion is not the one it
        applied gets the Secret's password, unless it is unreconciled."""
        cluster = self.get("clusters", NAMESPACE, NAME)
        status = cluster["status"]["managedRolesStatus"]
        for spec in cluster["spec"]["managed"]["roles"]:
            role, secret = spec["name"], spec["passwordSecret"]["name"]
            obj = self.get("secrets", NAMESPACE, secret)
            version = obj["metadata"]["resourceVersion"]
            if status["passwordStatus"][role]["resourceVersion"] == version:
                continue
            if role in self.unreconciled:
                status["byStatus"] = {"pending-reconciliation": [role]}
                status["cannotReconcile"] = {role: [self.unreconciled[role]]}
                continue
            self.postgres.passwords[role] = self.secret(NAMESPACE, secret)["password"]
            status["passwordStatus"][role] = {"resourceVersion": version, "transactionID": 2}
            self.journal.append(("apply", role))

    def annotations(self):
        return self.get("clusters", NAMESPACE, NAME)["metadata"]["annotations"]
