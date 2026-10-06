"""The Kubernetes activation of a plan (design §4.3): auto derived live, the named specs built, the
leaf's ExternalSecrets synced before every rollout, named rollouts included, one step per target
and each once, every sync before every rollout; rotator_consumers; and such a plan run, failed,
rolled back and dry run by the executor."""

import datetime
import io

import pytest
from fake_cluster import FakeCluster, externalsecret, pod_spec, workload
from plans import COPY, LEAF, NOW, Recorder, client, fake_of, lock
from test_kinds import KINDS, store_of

from secret_rotator import cli, terminal
from secret_rotator.audit import audit
from secret_rotator.cluster import Cluster
from secret_rotator.console import Console
from secret_rotator.contract import CONSUMERS, MAX_VALUE_BYTES, consumers_text
from secret_rotator.executor import Executor, Outcome
from secret_rotator.model import Action, Skipped
from secret_rotator.plan import PlanError, make

CATALOG = "eso/prd/kc/prd/catalog"
CONTROLLER = "k8s-rollout:kubecoder-prd/deployment/kubecoder-controller"
DERIVED = ["k8s.rollout:app-prd/deployment/app", "k8s.rollout:app-prd/statefulset/app-db"]


def copied_into_catalog(store, activate=CONTROLLER):
    """LEAF's token copied into the KubeCoder-like catalog too, activated by its controller."""
    store[CATALOG].keys.add("app-token")
    store[CATALOG].meta["key_app-token"] = f"copy:{LEAF}#token"
    store[CATALOG].meta["rotation_activate"] = activate
    return store


def plan_of(store=None, fake=None, leaf=LEAF):
    store = store or store_of()
    cluster = Cluster((fake or FakeCluster()).kube())
    return make(KINDS, leaf, "random", ["token"], store, audit(store), cluster)


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
            PlanError, match=f"{LEAF}: rotation_activate eso: no ExternalSecret references {LEAF}"
        ):
            plan_of(fake=fake)
        store = store_of(eso__prd__app__prd__token="none", iac__copy="auto")
        with pytest.raises(
            PlanError,
            match=f"{LEAF}: {COPY}'s rotation_activate eso: no ExternalSecret references {COPY}",
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


class TestConsumers:
    def test_the_stamp_records_them_and_removes_them_when_there_are_none(self):
        bao = fake_of(store_of())
        run(bao, plan_of())
        assert bao.meta(LEAF)[CONSUMERS] == (
            "app-prd/externalsecret/app-token,app-prd/deployment/app,app-prd/statefulset/app-db"
        )
        run(bao, plan_of(store_of(eso__prd__app__prd__token="none")))
        assert CONSUMERS not in bao.meta(LEAF)

    def test_too_many_are_cut_with_how_many_more(self):
        items = [f"ns-{n:03}/deployment/a-long-workload-name-{n:03}" for n in range(30)]
        text = consumers_text(items)
        assert len(text.encode()) <= MAX_VALUE_BYTES < len(",".join(items).encode())
        shown = text.rsplit(" +", 1)[0].split(",")
        assert shown == items[: len(shown)] and text.endswith(f" +{30 - len(shown)}")
        assert consumers_text(["x" * 600]) == "+1"
        assert consumers_text(items[:2]) == ",".join(items[:2])


def ticking():
    """An executor clock a second further on each call: each run of a step writes its own value."""
    times = iter(NOW + datetime.timedelta(seconds=n) for n in range(10_000))
    return lambda: next(times)


def run(bao, plan, *, dry_run=False, recorder=None):
    executor = Executor(
        client(bao), plan, recorder or Recorder(), lock(bao), dry_run=dry_run, clock=ticking()
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
        assert bao.meta(LEAF)["rotator_status"] == "ok"

    def test_a_rollout_that_fails_stops_the_plan_and_abort_undoes_kv_then_re_activates(self):
        fake = FakeCluster()
        fake.stuck.add("app-prd/app-db")
        bao = fake_of(store_of())
        old = bao.data(LEAF)["token"]
        recorder = Recorder()
        executor, outcome = run(bao, plan_of(fake=fake), recorder=recorder)
        assert outcome is Outcome.FAILED
        meta = bao.meta(LEAF)
        assert meta["rotator_status"] == "failed-activation"
        assert meta["rotator_step"] == "random/token/k8s.rollout:app-prd/statefulset/app-db"
        assert "app-prd/statefulset/app-db not Ready within 5 min" in meta["rotator_last_error"]
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
