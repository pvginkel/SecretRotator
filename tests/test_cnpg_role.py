"""The cnpg-role kind (design §6) over the seed's rows: its args, and a prd leaf that names no role
refused; its plans, which sync every ExternalSecret that reads the leaf, CNPG's own last, before
CNPG applies the password and the login proves it, whatever the leaf's activate; the runs, the
wait for CNPG bounded and saying what CNPG reports, and their rollbacks; the dev twins as KV
writes; and the real login's failure."""

import json
import socket
from pathlib import Path

import pytest
from fake_cluster import externalsecret, snapshot
from fake_cnpg import CLUSTER_PATH, PGADMIN, TERRAFORM, FakeCnpg, FakePostgres, cnpg_objects
from plans import Recorder, client, fake_of, lock, run_state, state_of
from test_activation import ticking

from secret_rotator import annotate as ann
from secret_rotator.audit import audit
from secret_rotator.cluster import Cluster
from secret_rotator.contract import entry_name
from secret_rotator.executor import Executor, Outcome
from secret_rotator.kinds.cnpg_role import HOST, PORT, CnpgRole
from secret_rotator.kinds.cnpg_role.postgres import PostgresError, login
from secret_rotator.kinds.cnpg_role.steps import RELOADED_AT
from secret_rotator.plan import PlanError, make

SEED = ann.load_seed(ann.DEFAULT_SEED)
KIND = "cnpg-role"
DEV_PGADMIN = "eso/dev/postgres-pas/pgadmin-admin"
DEV_TERRAFORM = "eso/dev/postgres-pas/terraform-admin"
OLD = {"pgadmin_admin": "SECRET-old-pgadmin", "terraform_admin": "SECRET-old-terraform"}
DATA = {
    PGADMIN: {"username": "pgadmin_admin", "password": OLD["pgadmin_admin"]},
    TERRAFORM: {"username": "terraform_admin", "password": OLD["terraform_admin"]},
}
MEMBERSHIPS = "could not perform UPDATE_MEMBERSHIPS on role terraform_admin: "


def seed_store():
    return ann.offline_store(Path(str(ann.DEFAULT_KEYS)), SEED, lambda line: None)


def rows():
    """Every cnpg-role row of the seed, by leaf."""
    found = SEED.leaves.items()
    return {path: leaf.default for path, leaf in found if leaf.default.get("kind") == KIND}


def ids(plan):
    return [s.id for s in plan.steps]


def set_args(store, leaf, args):
    """The leaf's password entry with those args in place of the seed's."""
    fields = json.loads(store[leaf].meta[entry_name("password")])
    fields.pop("args", None)
    store[leaf].meta[entry_name("password")] = json.dumps(fields | ({"args": args} if args else {}))


class World:
    """The seed's store on the fake OpenBao, and the cluster with CNPG and the Postgres a plan
    reaches."""

    def __init__(self, store=None, objects=None):
        self.store = store or seed_store()
        self.bao = fake_of(self.store, DATA)
        self.postgres = FakePostgres(OLD)
        self.cluster = FakeCnpg(self.bao, self.postgres, objects)
        self.kind = CnpgRole(login=self.postgres)

    def plan(self, leaf, *, cluster=True):
        return make(
            {KIND: self.kind},
            leaf,
            KIND,
            ["password"],
            audit(self.store),
            Cluster(self.cluster.kube()) if cluster else None,
        )

    def executor(self, leaf, *, cluster=True):
        self.recorder = Recorder()
        return Executor(
            client(self.bao),
            self.plan(leaf, cluster=cluster),
            self.recorder,
            lock(self.bao),
            state=run_state(self.bao),
            dry_run=False,
            clock=ticking(),
        )

    def run(self, leaf, *, cluster=True):
        return self.executor(leaf, cluster=cluster).run()

    def failure(self):
        (failure,) = self.recorder.failures()
        return failure

    def texts(self):
        """Every detail, error and technical text the run reported."""
        return [
            getattr(e, name, "")
            for e in self.recorder.events
            for name in ("detail", "error", "technical")
        ]

    def password(self, leaf):
        return self.bao.data(leaf)["password"]


class TestTheArgs:
    def test_the_prd_rows_name_their_role_and_the_dev_rows_none(self):
        found = rows()
        assert {leaf: row.get("args") for leaf, row in found.items()} == {
            PGADMIN: {"role": "pgadmin_admin"},
            TERRAFORM: {"role": "terraform_admin"},
            DEV_PGADMIN: None,
            DEV_TERRAFORM: None,
        }
        for row in found.values():
            assert CnpgRole().args_problems(row.get("args", {})) == []
        assert {leaf: row["activate"] for leaf, row in found.items()} == {
            PGADMIN: "k8s-rollout:pgadmin-prd/deployment/pgadmin",
            TERRAFORM: "none",
            DEV_PGADMIN: "none",
            DEV_TERRAFORM: "none",
        }

    @pytest.mark.parametrize(
        ("args", "problem"),
        [
            ({"role": "Pgadmin"}, "role: not a Postgres role name"),
            ({"role": "pgadmin admin"}, "role: not a Postgres role name"),
            ({"role": ["pgadmin_admin"]}, "role: not a Postgres role name"),
            ({"role": "pgadmin_admin", "cluster": "x"}, "cluster: not one of cnpg-role's role"),
        ],
    )
    def test_args_it_cannot_use_are_named(self, args, problem):
        assert problem in CnpgRole().args_problems(args)


class TestThePlans:
    def test_pgadmin_s_readers_sync_cnpg_s_last_before_the_login_and_the_rollout(self):
        assert ids(World().plan(PGADMIN)) == [
            "random.generate:password",
            "kv.write",
            "eso.sync:pgadmin-prd/postgres-pgadmin-admin",
            "eso.sync:postgres-pas-prd/postgres-pgadmin-admin",
            "cnpg.reconcile",
            "cnpg.login",
            "k8s.rollout:pgadmin-prd/deployment/pgadmin",
            "kv.stamp",
        ]

    def test_terraform_admin_s_readers_sync_though_its_activate_is_none(self):
        assert ids(World().plan(TERRAFORM)) == [
            "random.generate:password",
            "kv.write",
            "eso.sync:argocd-hooks/argocd-hook-credentials",
            "eso.sync:postgres-pas-prd/postgres-terraform-admin",
            "cnpg.reconcile",
            "cnpg.login",
            "kv.stamp",
        ]

    def test_cnpg_s_externalsecret_syncs_last_where_another_reader_sorts_after_it(self):
        reader = externalsecret("zz-prd", "reader", data=[(TERRAFORM, "password")])
        world = World(objects=[*cnpg_objects(), ("externalsecrets", reader)])
        assert ids(world.plan(TERRAFORM))[2:5] == [
            "eso.sync:argocd-hooks/argocd-hook-credentials",
            "eso.sync:zz-prd/reader",
            "eso.sync:postgres-pas-prd/postgres-terraform-admin",
        ]

    @pytest.mark.parametrize("leaf", [DEV_PGADMIN, DEV_TERRAFORM])
    def test_a_dev_twin_is_a_kv_write_also_offline(self, leaf):
        plan = World().plan(leaf, cluster=False)
        assert ids(plan) == ["random.generate:password", "kv.write", "kv.stamp"]
        assert plan.description == (
            "The tool generates a new password and writes it to the leaf: the role is on the dev "
            "cluster, which the tool does not reach."
        )

    def test_a_prd_leaf_whose_args_name_no_role_has_no_plan(self):
        store = seed_store()
        set_args(store, TERRAFORM, None)
        with pytest.raises(PlanError, match="its args name no role, and only an eso/dev/ leaf"):
            World(store).plan(TERRAFORM)

    def test_a_dev_leaf_whose_args_name_a_role_has_no_plan(self):
        store = seed_store()
        set_args(store, DEV_PGADMIN, {"role": "pgadmin_admin"})
        with pytest.raises(PlanError, match="role pgadmin_admin, on the dev cluster, which the"):
            World(store).plan(DEV_PGADMIN)

    def test_a_leaf_no_externalsecret_of_cnpg_s_namespace_reads_has_no_plan(self):
        objects = [
            (r, o)
            for r, o in cnpg_objects()
            if (o["metadata"]["namespace"], o["metadata"]["name"])
            != ("postgres-pas-prd", "postgres-terraform-admin")
        ]
        with pytest.raises(PlanError, match="no ExternalSecret in postgres-pas-prd references it"):
            World(objects=objects).plan(TERRAFORM)

    def test_offline_without_a_snapshot_a_prd_leaf_has_no_plan(self):
        with pytest.raises(PlanError, match="offline plan without a snapshot does not reach"):
            World().plan(PGADMIN, cluster=False)

    def test_against_a_snapshot_it_plans_without_reading_cnpg_or_a_secret(self, tmp_path):
        path = tmp_path / "snapshot.json"
        path.write_text(json.dumps(snapshot(cnpg_objects())))
        world = World()
        plan = make(
            {KIND: world.kind},
            PGADMIN,
            KIND,
            ["password"],
            audit(world.store),
            Cluster.of_snapshot(path),
        )
        assert ids(plan) == ids(world.plan(PGADMIN))

    def test_the_steps_the_login_reaches_and_the_descriptions(self):
        world = World()
        plan = world.plan(PGADMIN)
        reconcile, login_step = plan.steps[4:6]
        assert (reconcile.mutates, reconcile.activator, reconcile.undo) == (True, True, None)
        assert not login_step.mutates
        assert (login_step.host, login_step.port) == ("postgres-pas.home", 5432) == (HOST, PORT)
        assert not plan.needs_operator and plan.ask == ""
        assert plan.description == (
            "The tool generates a new password for role pgadmin_admin and writes it to the leaf "
            "and activates what reads it. Before it activates, every ExternalSecret that reads the "
            "leaf syncs, CNPG applies the password to the role, and the tool logs in as "
            "pgadmin_admin with it."
        )
        assert world.plan(TERRAFORM).description == (
            "The tool generates a new password for role terraform_admin and writes it to the "
            "leaf. Every ExternalSecret that reads the leaf syncs, CNPG applies the password to "
            "the role, and the tool logs in as terraform_admin with it."
        )


class TestTheRuns:
    def test_every_reader_holds_the_new_password_before_cnpg_applies_it(self):
        world = World()
        assert world.run(PGADMIN) is Outcome.DONE
        new = world.password(PGADMIN)
        assert new != OLD["pgadmin_admin"] and len(new) == 43
        assert world.postgres.passwords["pgadmin_admin"] == new
        assert world.cluster.secret("pgadmin-prd", "postgres-pgadmin-admin")["password"] == new
        assert world.cluster.journal[-3:] == [
            ("secret", "pgadmin-prd/postgres-pgadmin-admin", new),
            ("secret", "postgres-pas-prd/postgres-pgadmin-admin", new),
            ("apply", "pgadmin_admin"),
        ]
        assert world.postgres.logins == [("postgres-pas.home", 5432, "pgadmin_admin")]
        assert RELOADED_AT in world.cluster.annotations()
        rolled = world.cluster.get("deployments", "pgadmin-prd", "pgadmin")
        assert rolled["metadata"]["generation"] == 2
        assert state_of(world.bao, PGADMIN).stamps == {"password": "2026-10-05"}
        assert not any(new in text for text in world.texts())

    def test_terraform_admin_s_hook_secret_holds_it_before_cnpg_applies_it(self):
        world = World()
        assert world.run(TERRAFORM) is Outcome.DONE
        new = world.password(TERRAFORM)
        assert world.postgres.passwords["terraform_admin"] == new
        assert world.cluster.journal[-3:] == [
            ("secret", "argocd-hooks/argocd-hook-credentials", new),
            ("secret", "postgres-pas-prd/postgres-terraform-admin", new),
            ("apply", "terraform_admin"),
        ]
        assert world.postgres.passwords["pgadmin_admin"] == OLD["pgadmin_admin"]

    def test_cnpg_applying_the_password_already_needs_no_annotation(self):
        world = World()
        synced = world.cluster.eso_sync

        def sync_and_reconcile(es):
            synced(es)
            world.cluster.reconcile()

        world.cluster.eso_sync = sync_and_reconcile
        assert world.run(TERRAFORM) is Outcome.DONE
        assert world.postgres.passwords["terraform_admin"] == world.password(TERRAFORM)
        patched = [path for path, _ in world.cluster.patches()]
        assert CLUSTER_PATH not in patched

    def test_a_role_cnpg_does_not_apply_fails_the_wait_with_what_cnpg_reports(self):
        world = World()
        world.cluster.unreconciled["terraform_admin"] = MEMBERSHIPS
        secret = ("secrets", "postgres-pas-prd", "postgres-terraform-admin")
        before = world.cluster.get(*secret)["metadata"]["resourceVersion"]
        assert world.run(TERRAFORM) is Outcome.FAILED
        after = world.cluster.get(*secret)["metadata"]["resourceVersion"]
        failure = world.failure()
        assert failure.step.id == "cnpg.reconcile"
        assert failure.error == (
            "CNPG did not apply the password of role terraform_admin within 5 min: role "
            f"terraform_admin has Secret postgres-terraform-admin version {before} applied, not "
            f"{after}; CNPG reports pending-reconciliation; could not perform UPDATE_MEMBERSHIPS "
            "on role terraform_admin:"
        )
        assert world.postgres.passwords["terraform_admin"] == OLD["terraform_admin"]
        assert state_of(world.bao, TERRAFORM).status == "failed-activation"

    def test_its_rollback_puts_the_old_password_back_and_has_cnpg_apply_that(self):
        world = World()
        world.cluster.unreconciled["terraform_admin"] = MEMBERSHIPS
        executor = world.executor(TERRAFORM)
        assert executor.run() is Outcome.FAILED
        del world.cluster.unreconciled["terraform_admin"]
        assert executor.abort() is Outcome.ROLLED_BACK
        old = OLD["terraform_admin"]
        assert world.password(TERRAFORM) == old
        assert world.cluster.secret("argocd-hooks", "argocd-hook-credentials")["password"] == old
        assert world.cluster.secret("postgres-pas-prd", "postgres-terraform-admin") == {
            "username": "terraform_admin",
            "password": old,
        }
        assert world.postgres.passwords["terraform_admin"] == old

    def test_a_retry_after_the_wait_failed_annotates_again_and_finishes(self):
        world = World()
        world.cluster.unreconciled["terraform_admin"] = MEMBERSHIPS
        assert world.run(TERRAFORM) is Outcome.FAILED
        first = world.cluster.annotations()[RELOADED_AT]
        del world.cluster.unreconciled["terraform_admin"]
        assert world.run(TERRAFORM) is Outcome.DONE
        assert world.cluster.annotations()[RELOADED_AT] != first
        assert world.postgres.passwords["terraform_admin"] == world.password(TERRAFORM)

    def test_a_refused_login_fails_before_the_rollout_and_the_rollback_restores_the_old(self):
        world = World()
        world.postgres.refused.add("pgadmin_admin")
        executor = world.executor(PGADMIN)
        assert executor.run() is Outcome.FAILED
        failure = world.failure()
        assert failure.step.id == "cnpg.login"
        assert failure.error == (
            "the login to postgres-pas.home:5432 as pgadmin_admin fails: connection failed: "
            'FATAL: pg_hba.conf rejects connection for host "10.1.0.9", user "pgadmin_admin", '
            'database "postgres"'
        )
        rolled = world.cluster.get("deployments", "pgadmin-prd", "pgadmin")
        assert rolled["metadata"]["generation"] == 1
        assert executor.abort() is Outcome.ROLLED_BACK
        old = OLD["pgadmin_admin"]
        assert world.password(PGADMIN) == old
        assert world.cluster.secret("pgadmin-prd", "postgres-pgadmin-admin")["password"] == old
        assert world.postgres.passwords["pgadmin_admin"] == old

    def test_a_password_cnpg_did_not_get_fails_the_login(self):
        world = World()
        # The Cluster reads pgadmin_admin's password from a Secret no reader of the leaf writes.
        managed = world.cluster.get("clusters", "postgres-pas-prd", "postgres")["spec"]["managed"]
        managed["roles"][0]["passwordSecret"]["name"] = "postgres-terraform-admin"
        assert world.run(PGADMIN) is Outcome.FAILED
        assert world.failure().step.id == "cnpg.login"
        assert "password authentication failed" in world.failure().error

    @pytest.mark.parametrize(
        ("setup", "error"),
        [
            (
                lambda c: c.objects.pop(("clusters", "postgres-pas-prd", "postgres")),
                "CNPG Cluster postgres-pas-prd/postgres does not exist",
            ),
            (
                lambda c: c.get("clusters", "postgres-pas-prd", "postgres")["spec"]["managed"][
                    "roles"
                ].pop(),
                "CNPG Cluster postgres-pas-prd/postgres manages no role terraform_admin",
            ),
            (
                lambda c: c.get("clusters", "postgres-pas-prd", "postgres")["spec"]["managed"][
                    "roles"
                ][1].pop("passwordSecret"),
                "role terraform_admin of CNPG Cluster postgres-pas-prd/postgres has no "
                "passwordSecret",
            ),
            (
                lambda c: c.get("clusters", "postgres-pas-prd", "postgres")["spec"]["managed"][
                    "roles"
                ][1]["passwordSecret"].update(name="postgres-gone"),
                "Secret postgres-pas-prd/postgres-gone, role terraform_admin's passwordSecret, "
                "does not exist",
            ),
        ],
    )
    def test_what_cnpg_does_not_hold_fails_the_reconcile(self, setup, error):
        world = World()
        setup(world.cluster)
        assert world.run(TERRAFORM) is Outcome.FAILED
        assert (world.failure().step.id, world.failure().error) == ("cnpg.reconcile", error)

    @pytest.mark.parametrize("leaf", [DEV_PGADMIN, DEV_TERRAFORM])
    def test_a_dev_twin_writes_the_leaf_and_reaches_no_cluster_and_no_postgres(self, leaf):
        world = World()
        before = world.password(leaf)
        assert world.run(leaf, cluster=False) is Outcome.DONE
        assert world.password(leaf) != before
        assert world.cluster.requests == [] and world.postgres.logins == []
        assert state_of(world.bao, leaf).stamps == {"password": "2026-10-05"}


class TestTheRealLogin:
    def test_a_login_it_cannot_make_fails_with_libpq_s_error_and_not_the_password(self):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        with pytest.raises(PostgresError) as e:
            login("127.0.0.1", port, "terraform_admin", "SECRET-new-password")
        assert e.value.error.startswith(
            f"the login to 127.0.0.1:{port} as terraform_admin fails: connection failed: "
        )
        assert "SECRET" not in e.value.error and "\n" not in e.value.error
