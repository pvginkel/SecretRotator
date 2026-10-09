"""The Kubernetes activation of a plan (design §4.3): auto derived live, the named specs built, the
leaf's ExternalSecrets synced before every rollout, named rollouts included, one step per target
and each once, every sync before every rollout; the consumers the stamp records; such a plan run,
failed, rolled back and dry run by the executor; and a plan in flight rebuilt from what it derived
when it started, so Retry and Abort complete once its targets are gone (design §4.5, 045's B10)."""

import dataclasses
import datetime
import io

import pytest
from fake_cluster import FakeCluster, externalsecret, pod_spec, workload
from fixtures import edit
from plans import (
    COPY,
    LEAF,
    NOW,
    Recorder,
    client,
    fake_of,
    flight_of,
    lock,
    put_flight,
    put_state,
    run_state,
    state_of,
)
from test_kinds import KINDS, store_of

from secret_rotator import cli, terminal
from secret_rotator.audit import audit
from secret_rotator.cluster import Cluster, Derived, Ref, Workload
from secret_rotator.console import Console
from secret_rotator.contract import MAX_VALUE_BYTES
from secret_rotator.executor import Abandon, Executor, Outcome
from secret_rotator.kvsteps import KvStamp
from secret_rotator.model import Action, Skipped
from secret_rotator.plan import PlanError, StepFactory, make, target

CATALOG = "eso/prd/kc/prd/catalog"
CONTROLLER = "k8s-rollout:kubecoder-prd/deployment/kubecoder-controller"
DERIVED = ["k8s.rollout:app-prd/deployment/app", "k8s.rollout:app-prd/statefulset/app-db"]


def copied_into_catalog(store, activate=CONTROLLER):
    """LEAF's token copied into the KubeCoder-like catalog too, activated by its controller."""
    store[CATALOG].keys.add("app-token")
    edit(store[CATALOG].meta, "app-token", kind=f"copy:{LEAF}#token", activate=activate)
    return store


def plan_of(store=None, fake=None, leaf=LEAF):
    store = store or store_of()
    cluster = Cluster((fake or FakeCluster()).kube())
    return make(KINDS, leaf, "random", ["token"], audit(store), cluster)


def ids(plan):
    return [s.id for s in plan.steps]


class TestAuto:
    def test_it_syncs_the_leaf_s_external_secrets_then_rolls_each_workload_reading_them(self):
        plan = plan_of()
        assert ids(plan) == [
            "random.generate:token",
            "kv.write",
            f"kv.copy:{COPY}#token",
            "eso.sync:app-prd/app-token",
            *DERIVED,
            "kv.stamp",
        ]
        assert plan.steps[-1].consumers == (
            "app-prd/externalsecret/app-token",
            "app-prd/deployment/app",
            "app-prd/statefulset/app-db",
        )

    def test_plan_prints_each_activation_step_with_its_target(self):
        lines = terminal.plan_lines(plan_of())
        assert (
            "  4  tool  eso.sync                      sync ExternalSecret app-prd/app-token"
            in lines
        )
        assert (
            "  6  tool  k8s.rollout                   roll out app-prd/statefulset/app-db" in lines
        )

    def test_no_external_secret_refuses_auto_on_the_leaf_or_a_copy_s_leaf(self):
        fake = FakeCluster()
        del fake.objects["externalsecrets", "app-prd", "app-token"]
        with pytest.raises(
            PlanError,
            match=f"{LEAF}: rotation_token activate eso: no ExternalSecret references {LEAF}",
        ):
            plan_of(fake=fake)
        store = store_of(eso__prd__app__prd__token="none", iac__copy="auto")
        with pytest.raises(
            PlanError,
            match=(
                f"{LEAF}: {COPY}'s rotation_token activate eso: no ExternalSecret references {COPY}"
            ),
        ):
            plan_of(store)

    def test_eso_alone_syncs_and_rolls_nothing(self):
        plan = plan_of(store_of(eso__prd__app__prd__token="eso"))
        assert ids(plan)[3:] == ["eso.sync:app-prd/app-token", "kv.stamp"]
        assert plan.steps[-1].consumers == ("app-prd/externalsecret/app-token",)


class TestNamedRollouts:
    def test_a_named_rollout_is_preceded_by_the_sync_of_the_leaf_s_external_secrets(self):
        plan = plan_of(store_of(eso__prd__app__prd__token="k8s-rollout:es-prd/deployment/a"))
        assert ids(plan)[3:] == [
            "eso.sync:app-prd/app-token",
            "k8s.rollout:es-prd/deployment/a",
            "kv.stamp",
        ]
        assert plan.steps[-1].consumers == ("app-prd/externalsecret/app-token",)

    def test_a_leaf_no_external_secret_references_still_rolls_its_named_targets(self):
        fake = FakeCluster()
        del fake.objects["externalsecrets", "app-prd", "app-token"]
        store = store_of(
            eso__prd__app__prd__token="k8s-rollout:es-prd/deployment/a,es-prd/statefulset/b"
        )
        plan = plan_of(store, fake)
        assert ids(plan)[3:] == [
            "k8s.rollout:es-prd/deployment/a",
            "k8s.rollout:es-prd/statefulset/b",
            "kv.stamp",
        ]
        assert plan.steps[-1].consumers == ()

    def test_the_kubecoder_catalog_is_synced_by_its_data_from_extract_before_its_controller(self):
        plan = plan_of(copied_into_catalog(store_of()))
        assert ids(plan) == [
            "random.generate:token",
            "kv.write",
            f"kv.copy:{CATALOG}#app-token",
            f"kv.copy:{COPY}#token",
            "eso.sync:app-prd/app-token",
            "eso.sync:kubecoder-prd/kubecoder-secret-catalog",
            *DERIVED,
            "k8s.rollout:kubecoder-prd/deployment/kubecoder-controller",
            "kv.stamp",
        ]
        assert (
            plan.steps[-1].consumers[-1] == "kubecoder-prd/externalsecret/kubecoder-secret-catalog"
        )

    def test_a_raised_bound_reaches_the_rollout_of_the_target_it_names_alone(self):
        factory = StepFactory(
            target(LEAF, "random", ["token"], audit(store_of())), Cluster(FakeCluster().kube())
        )
        steps = factory.activate(bounds={Workload("app-prd", "deployment", "app"): 900})
        bounds = {s.id: s.bound for s in steps if s.type == "k8s.rollout"}
        assert bounds == {DERIVED[0]: 900, DERIVED[1]: 300}


class TestComposition:
    def test_each_target_once_and_every_sync_before_every_rollout(self):
        fake = FakeCluster()
        shared = externalsecret("app-prd", "shared", data=[(LEAF, "token"), (CATALOG, "app-token")])
        fake.add("externalsecrets", shared)
        fake.add(
            "deployments", workload("Deployment", "app-prd", "reader", pod_spec(env=["shared"]))
        )
        plan = plan_of(copied_into_catalog(store_of(), activate="auto"), fake)
        assert ids(plan)[4:] == [
            "eso.sync:app-prd/app-token",
            "eso.sync:app-prd/shared",
            "eso.sync:kubecoder-prd/kubecoder-secret-catalog",
            "k8s.rollout:app-prd/deployment/app",
            "k8s.rollout:app-prd/deployment/reader",
            "k8s.rollout:app-prd/statefulset/app-db",
            "k8s.rollout:kubecoder-prd/deployment/kubecoder-controller",
            "kv.stamp",
        ]
        consumers = plan.steps[-1].consumers
        assert len(consumers) == len(set(consumers)) == 7

    def test_the_kubernetes_steps_stand_at_the_first_kubernetes_spec_s_place(self):
        store = store_of(
            eso__prd__app__prd__token="manual:restart it by hand,eso,k8s-rollout",
            iac__copy="manual:tell the copy's reader",
        )
        assert ids(plan_of(store))[3:] == [
            f"operator.confirm:{LEAF}:1",
            "eso.sync:app-prd/app-token",
            *DERIVED,
            f"operator.confirm:{COPY}:1",
            "kv.stamp",
        ]


class TestEntriesCombined:
    """A plan that writes several keys runs every spec of their entries, each once where they
    repeat (design §4.3)."""

    def two_keys(self, first, second):
        store = store_of()
        store[LEAF].keys.add("second")
        edit(store[LEAF].meta, "token", activate=first)
        edit(store[LEAF].meta, "second", kind="random", interval="14d", activate=second)
        return store

    def plan_of(self, store):
        cluster = Cluster(FakeCluster().kube())
        return make(KINDS, LEAF, "random", ["second", "token"], audit(store), cluster)

    def test_the_kubernetes_specs_of_both_entries_are_one_sync_and_rollout(self):
        plan = self.plan_of(self.two_keys("auto", "k8s-rollout:es-prd/deployment/a"))
        assert ids(plan)[4:] == [
            "eso.sync:app-prd/app-token",
            "k8s.rollout:es-prd/deployment/a",
            *DERIVED,
            "kv.stamp",
        ]

    def test_a_job_runs_once_and_a_confirm_text_once_per_leaf(self):
        job = "jenkins-job:YouTrack/YouTrackConfiguration?ROTATE_TOKEN=true"
        plan = self.plan_of(
            self.two_keys(f"{job},manual:say so", f"manual:say so,{job},manual:and this")
        )
        # second's entry first: the rotated keys' entries go in key order.
        assert ids(plan)[4:] == [
            f"operator.confirm:{LEAF}:1",
            "jenkins.job:YouTrack/YouTrackConfiguration?ROTATE_TOKEN=true",
            f"operator.confirm:{LEAF}:2",
            "kv.stamp",
        ]
        assert plan.target.confirms == ("say so", "and this")
        assert [s.title for s in plan.steps if s.type == "operator.confirm"] == [
            "say so",
            "and this",
        ]

    def test_a_confirm_text_on_two_leaves_is_confirmed_for_each(self):
        store = store_of(eso__prd__app__prd__token="manual:say so", iac__copy="manual:say so")
        assert ids(plan_of(store))[3:] == [
            f"operator.confirm:{LEAF}:1",
            f"operator.confirm:{COPY}:1",
            "kv.stamp",
        ]


class TestConsumers:
    def test_the_stamp_records_them_and_removes_them_when_there_are_none(self):
        bao = fake_of(store_of())
        run(bao, plan_of())
        assert state_of(bao, LEAF).consumers == (
            "app-prd/externalsecret/app-token",
            "app-prd/deployment/app",
            "app-prd/statefulset/app-db",
        )
        run(bao, plan_of(store_of(eso__prd__app__prd__token="none")))
        assert state_of(bao, LEAF).consumers == ()

    def test_they_are_kept_whole_past_a_metadata_value_s_cap(self):
        items = tuple(f"ns-{n:03}/deployment/a-long-workload-name-{n:03}" for n in range(30))
        assert len(",".join(items).encode()) > MAX_VALUE_BYTES
        bao = fake_of(store_of())
        _, outcome = run(bao, plan_of(), steps=(KvStamp(LEAF, ("token",), items),))
        assert outcome is Outcome.DONE
        assert state_of(bao, LEAF).consumers == items


def ticking():
    """An executor clock a second further on each call: each run of a step writes its own value."""
    times = iter(NOW + datetime.timedelta(seconds=n) for n in range(10_000))
    return lambda: next(times)


def run(bao, plan, *, dry_run=False, recorder=None, steps=None):
    if steps is not None:
        plan = dataclasses.replace(plan, steps=steps)
    executor = Executor(
        client(bao),
        plan,
        recorder or Recorder(),
        lock(bao),
        state=run_state(bao),
        dry_run=dry_run,
        clock=ticking(),
    )
    return executor, executor.run()


class TestExecution:
    def test_a_plan_with_auto_runs_to_done(self):
        fake = FakeCluster()
        bao = fake_of(store_of())
        _, outcome = run(bao, plan_of(fake=fake))
        assert outcome is Outcome.DONE
        assert fake.syncs == 1
        assert fake.get("deployments", "app-prd", "app")["metadata"]["generation"] == 2
        assert state_of(bao, LEAF).status == "ok"

    def test_a_rollout_that_fails_stops_the_plan_and_abort_undoes_kv_then_re_activates(self):
        fake = FakeCluster()
        fake.stuck.add("app-prd/app-db")
        bao = fake_of(store_of())
        old = bao.data(LEAF)["token"]
        recorder = Recorder()
        executor, outcome = run(bao, plan_of(fake=fake), recorder=recorder)
        assert outcome is Outcome.FAILED
        state = state_of(bao, LEAF)
        assert state.status == "failed-activation"
        flight = flight_of(bao, LEAF)
        assert (flight.kind, flight.keys) == ("random", ("token",))
        assert flight.step == "k8s.rollout:app-prd/statefulset/app-db"
        assert "app-prd/statefulset/app-db not Ready within 5 min" in state.last_error
        fake.stuck.clear()
        recorder.events.clear()
        assert executor.abort() is Outcome.ROLLED_BACK
        done = [(line[1], line[2]) for line in recorder.lines() if line[0] == "ok"]
        assert done == [
            (f"kv.copy:{COPY}#token", Action.UNDO),
            ("kv.write", Action.UNDO),
            ("eso.sync:app-prd/app-token", Action.RERUN),
            ("k8s.rollout:app-prd/deployment/app", Action.RERUN),
            ("k8s.rollout:app-prd/statefulset/app-db", Action.RERUN),
        ]
        assert bao.data(LEAF)["token"] == old and fake.syncs == 2

    def test_a_dry_run_touches_no_cluster_object(self):
        fake = FakeCluster()
        bao = fake_of(store_of())
        recorder = Recorder()
        _, outcome = run(bao, plan_of(fake=fake), dry_run=True, recorder=recorder)
        assert outcome is Outcome.DRY_RUN
        assert all(isinstance(e, Skipped) for e in recorder.events)
        assert fake.patches() == [] and bao.writes() == []


class TestCommands:
    ENV = {
        cli.ROLE_ID_ENV: "role-id-of-the-rotator",
        cli.SECRET_ID_ENV: "SECRET-secret-id-of-the-rotator",
        cli.K8S_TOKEN_ENV: "SECRET-token-of-the-secret-rotator-sa",
    }

    def test_the_live_plan_derives_auto_from_the_cluster(self):
        lines = []
        bao = fake_of(store_of())
        code = cli.main(
            ["plan", LEAF], opener=bao, out=lines.append, environ=self.ENV, kube=FakeCluster().kube
        )
        assert code == 0, lines
        assert any(
            "k8s.rollout                   roll out app-prd/deployment/app" in line
            for line in lines
        )
        assert not [line for line in lines if "SECRET" in line]

    def test_the_live_plan_reads_the_run_state_and_the_plan_in_flight(self):
        lines = []
        bao = fake_of(store_of())
        put_state(bao, LEAF, stamps={"token": "2026-10-01"})
        put_flight(bao, "random", LEAF, ["token"], "k8s.rollout:app-prd/statefulset/app-db")
        code = cli.main(
            ["plan", LEAF], opener=bao, out=lines.append, environ=self.ENV, kube=FakeCluster().kube
        )
        assert code == 0, lines
        at = "k8s.rollout:app-prd/statefulset/app-db"
        assert f"  in flight: its random plan of token, at {at}" in lines
        assert any(line.startswith("  random plan of token · due") for line in lines), lines
        assert any("2026-10-15" in line for line in lines), lines
        assert not [line for line in lines if "never rotated" in line]

    def test_run_reads_a_key_whose_args_its_kind_cannot_use_as_blocked(self):
        store = store_of()
        edit(store[LEAF].meta, "token", args={"length": 0})
        con = Console(io.StringIO(), io.StringIO())
        code = terminal.run_leaf(
            client(fake_of(store)),
            LEAF,
            KINDS,
            con,
            holder="run test",
            today=datetime.date(2026, 10, 5),
            cluster=Cluster(FakeCluster().kube()),
        )
        out = con.stdout.getvalue()
        assert code == 1, out
        assert "token: blocked: rotation_token: args: length: not a whole number from 1" in out

    def test_run_rolls_out_in_the_terminal(self):
        bao = fake_of(store_of())
        con = Console(io.StringIO("y\n"), io.StringIO())
        cluster = Cluster(FakeCluster().kube())
        today = datetime.date(2026, 10, 5)
        code = terminal.run_leaf(
            client(bao), LEAF, KINDS, con, holder="run test", today=today, cluster=cluster
        )
        out = con.stdout.getvalue()
        assert code == 0, out
        assert "✓ roll out app-prd/deployment/app · Ready, Application app-prd Healthy" in out
        assert "✓ sync ExternalSecret app-prd/app-token · synced, Ready" in out


class TestAChangedCluster:
    """LEAF's plan stopped at its StatefulSet's rollout after kv.write and kv.copy landed (045's
    B10); then the cluster changes, and `run <path>` takes the plan up from its record."""

    TODAY = datetime.date(2026, 10, 5)
    DERIVED = Derived(
        {LEAF: [Ref("app-prd", "app-token")]},
        {
            LEAF: [
                Workload("app-prd", "deployment", "app"),
                Workload("app-prd", "statefulset", "app-db"),
            ]
        },
    )

    def stopped(self, store=None):
        fake = FakeCluster()
        fake.stuck.add("app-prd/app-db")
        bao = fake_of(store or store_of())
        _, outcome = run(bao, plan_of(store, fake))
        assert outcome is Outcome.FAILED
        assert flight_of(bao, LEAF).step == "k8s.rollout:app-prd/statefulset/app-db"
        fake.stuck.clear()
        return fake, bao

    def take_up(self, fake, bao, *answers):
        con = Console(io.StringIO("".join(f"{a}\n" for a in answers)), io.StringIO())
        code = terminal.run_leaf(
            client(bao),
            LEAF,
            KINDS,
            con,
            holder="run test",
            today=self.TODAY,
            cluster=Cluster(fake.kube()),
        )
        return code, con.stdout.getvalue()

    def test_its_record_holds_the_external_secrets_and_workloads_it_derived(self):
        fake = FakeCluster()
        plan = plan_of(fake=fake)
        assert plan.derived == self.DERIVED
        _, bao = self.stopped()
        assert flight_of(bao, LEAF).derived == self.DERIVED

    def test_a_plan_built_from_the_record_reads_none_of_it_from_the_cluster(self):
        store = store_of()
        empty = FakeCluster([])
        cluster = Cluster(empty.kube())
        plan = make(KINDS, LEAF, "random", ["token"], audit(store), cluster, derived=self.DERIVED)
        assert ids(plan) == ids(plan_of(store))
        assert plan.derived == self.DERIVED and empty.requests == []

    def test_retry_completes_once_its_workload_is_deleted(self):
        fake, bao = self.stopped()
        old = bao.data(LEAF)["token"]
        del fake.objects["statefulsets", "app-prd", "app-db"]
        code, out = self.take_up(fake, bao, "r")
        assert code == 0, out
        gone = "does not exist: nothing to roll out, counted as done"
        assert f"✓ roll out app-prd/statefulset/app-db · {gone}" in out
        assert f"Done: token of {LEAF} rotated." in out
        assert state_of(bao, LEAF).status == "ok" and flight_of(bao, LEAF) is None
        assert bao.data(LEAF)["token"] == old

    def test_abort_completes_once_its_workload_is_deleted(self):
        fake, bao = self.stopped()
        del fake.objects["statefulsets", "app-prd", "app-db"]
        code, out = self.take_up(fake, bao, "a", "y")
        assert code == 0, out
        gone = "does not exist: nothing to roll out, counted as done"
        assert f"✓ again: roll out app-prd/statefulset/app-db · {gone}" in out
        assert "Rolled back: the rotation of token is undone." in out
        assert bao.data(LEAF)["token"] == f"SECRET-{LEAF}-token"
        assert bao.data(COPY)["token"] == f"SECRET-{COPY}-token"
        assert flight_of(bao, LEAF) is None

    def orphaned(self):
        """LEAF without its copy, so its ExternalSecret's deletion makes it an orphan."""
        store = store_of()
        del store[COPY]
        fake, bao = self.stopped(store)
        del fake.objects["externalsecrets", "app-prd", "app-token"]
        referenced = Cluster(fake.kube()).referenced()
        assert LEAF in audit(store, referenced).blocked_leaves
        return fake, bao

    def test_retry_completes_once_its_external_secret_is_deleted(self):
        fake, bao = self.orphaned()
        code, out = self.take_up(fake, bao, "r")
        assert code == 0, out
        assert "✓ roll out app-prd/statefulset/app-db · Ready" in out
        assert state_of(bao, LEAF).status == "ok" and flight_of(bao, LEAF) is None

    def test_abort_completes_once_its_external_secret_is_deleted(self):
        fake, bao = self.orphaned()
        code, out = self.take_up(fake, bao, "a", "y")
        assert code == 0, out
        gone = "does not exist: nothing to sync, counted as done"
        assert f"✓ again: sync ExternalSecret app-prd/app-token · {gone}" in out
        assert "Rolled back: the rotation of token is undone." in out
        assert bao.data(LEAF)["token"] == f"SECRET-{LEAF}-token"
        assert flight_of(bao, LEAF) is None

    def exited(self, store):
        """LEAF's plan left at its first manual: confirm, after kv.write landed."""
        fake = FakeCluster()
        bao = fake_of(store)
        _, outcome = run(bao, plan_of(store, fake), recorder=Recorder(Abandon.EXIT))
        assert outcome is Outcome.EXITED
        assert flight_of(bao, LEAF).step == f"operator.confirm:{LEAF}:1"
        return fake, bao

    def manual_orphaned(self):
        """LEAF activated by hand, so its plan derives no ExternalSecret, and without its copy, so
        its ExternalSecret's deletion makes it an orphan."""
        store = store_of(eso__prd__app__prd__token="manual:restart the app by hand")
        del store[COPY]
        fake, bao = self.exited(store)
        del fake.objects["externalsecrets", "app-prd", "app-token"]
        assert LEAF in audit(store, Cluster(fake.kube()).referenced()).blocked_leaves
        return fake, bao

    def test_retry_completes_once_the_external_secret_of_a_plan_that_derived_none_is_deleted(self):
        fake, bao = self.manual_orphaned()
        code, out = self.take_up(fake, bao, "r", "d")
        assert code == 0, out
        assert f"Done: token of {LEAF} rotated." in out
        assert state_of(bao, LEAF).status == "ok" and flight_of(bao, LEAF) is None

    def test_abort_completes_once_the_external_secret_of_a_plan_that_derived_none_is_deleted(self):
        fake, bao = self.manual_orphaned()
        code, out = self.take_up(fake, bao, "a", "y")
        assert code == 0, out
        assert "Rolled back: the rotation of token is undone." in out
        assert bao.data(LEAF)["token"] == f"SECRET-{LEAF}-token"
        assert flight_of(bao, LEAF) is None

    def test_abort_completes_once_the_external_secret_of_a_leaf_holding_its_copy_is_deleted(self):
        store = copied_into_catalog(
            store_of(eso__prd__app__prd__token="manual:restart the app by hand"),
            activate="manual:restart the controller by hand",
        )
        fake, bao = self.exited(store)
        del fake.objects["externalsecrets", "kubecoder-prd", "kubecoder-secret-catalog"]
        assert CATALOG in audit(store, Cluster(fake.kube()).referenced()).blocked_leaves
        code, out = self.take_up(fake, bao, "a", "y")
        assert code == 0, out
        assert "Rolled back: the rotation of token is undone." in out
        assert bao.data(CATALOG)["app-token"] == f"SECRET-{CATALOG}-app-token"
        assert flight_of(bao, LEAF) is None
