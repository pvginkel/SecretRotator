"""The grafana-admin kind (design §6) over the seed's one row, Grafana's local admin: its args; its
plans, which log in with the password the leaf holds before they write and sync every
ExternalSecret that reads the leaf before Grafana's password is set, whatever the leaf's activate;
the runs, which leave Grafana taking the password KV holds; their rollbacks, which leave Grafana,
KV and the Secret on the password Grafana held; the failures that stop a plan before it writes;
and the client's answers."""

import datetime
import io
import json
import urllib.error
from pathlib import Path

import pytest
from fake_cluster import FakeCluster, externalsecret, pod_spec, snapshot, workload
from fake_grafana import ADMIN, BASE, PASSWORD, FakeGrafana, FakeResponse
from fixtures import edit
from plans import NOW, Recorder, client, fake_of, lock, run_state, state_of
from test_activation import ticking

from secret_rotator import annotate as ann
from secret_rotator.audit import audit
from secret_rotator.cluster import Cluster
from secret_rotator.executor import Executor, Outcome
from secret_rotator.kinds import grafana_admin
from secret_rotator.kinds.grafana_admin import GrafanaAdmin
from secret_rotator.kinds.grafana_admin.grafana import Grafana, GrafanaError
from secret_rotator.kinds.grafana_admin.steps import HELD, Login, SetAdminPassword
from secret_rotator.model import StepFailed, value_name
from secret_rotator.plan import PlanError, make

SEED = ann.load_seed(ann.DEFAULT_SEED)
KIND = "grafana-admin"
LEAF = "eso/prd/grafana/prd/admin"
KEY = "admin-password"
ES = "grafana-prd/grafana-admin"
ROLLED = "grafana-prd/deployment/grafana"
USER_PATH = "/api/user"
PUT_PATH = "/api/admin/users/1/password"
REFUSED = "GET /api/user: HTTP 401: Invalid username or password"


def seed_store():
    return ann.offline_store(Path(str(ann.DEFAULT_KEYS)), SEED, lambda line: None)


def grafana_objects():
    """(resource, object) of the leaf's readers as prd holds them (2026-10-08): the grafana
    Deployment reads grafana-admin's admin-user and admin-password into its env."""
    return [
        (
            "externalsecrets",
            externalsecret(
                "grafana-prd",
                "grafana-admin",
                data=[(LEAF, "admin-user"), (LEAF, KEY)],
                target="grafana-admin",
            ),
        ),
        (
            "deployments",
            workload("Deployment", "grafana-prd", "grafana", pod_spec(env=["grafana-admin"])),
        ),
    ]


class World:
    """The seed's store on the fake OpenBao, the leaf holding Grafana's admin login; Grafana,
    which takes it; and the leaf's readers on the fake cluster."""

    def __init__(self, store=None, user=ADMIN, password=PASSWORD):
        self.store = store or seed_store()
        self.bao = fake_of(self.store, {LEAF: {"admin-user": user, KEY: password}})
        self.grafana = FakeGrafana()
        self.cluster = FakeCluster(grafana_objects())
        self.kind = GrafanaAdmin(opener=self.grafana)
        # (the password set, what KV holds, the ExternalSecret's syncs), at each set
        self.posted = []
        self.grafana.before_put = lambda login, new: self.posted.append(
            (new, self.password(), self.cluster.syncs)
        )
        self.synced = []  # what KV holds at each sync of the ExternalSecret
        eso_sync = self.cluster.eso_sync

        def synced(es):
            self.synced.append(self.password())
            eso_sync(es)

        self.cluster.eso_sync = synced

    def password(self):
        return self.bao.data(LEAF)[KEY]

    def plan(self, *, cluster=True, store=None):
        return make(
            {KIND: self.kind},
            LEAF,
            KIND,
            [KEY],
            audit(store or self.store),
            Cluster(self.cluster.kube()) if cluster else None,
        )

    def executor(self, *, day=0, store=None):
        self.recorder = Recorder()
        tick = ticking()
        return Executor(
            client(self.bao),
            self.plan(store=store),
            self.recorder,
            lock(self.bao),
            state=run_state(self.bao),
            dry_run=False,
            clock=lambda: tick() + datetime.timedelta(days=day),
        )

    def run(self, *, day=0, store=None):
        return self.executor(day=day, store=store).run()

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


def ids(plan):
    return [s.id for s in plan.steps]


def activate_auto():
    store = seed_store()
    edit(store[LEAF].meta, KEY, activate="auto")
    return store


class TestTheSeed:
    def test_the_admin_password_rotates_yearly_and_activates_nothing(self):
        entries = audit(seed_store()).entries[LEAF]
        entry = entries[KEY]
        assert (entry.kind, entry.args, entry.interval, entry.activate) == (KIND, {}, 365, ())
        assert entries["admin-user"].kind == "none"
        assert GrafanaAdmin().args_problems(entry.args) == []

    def test_an_arg_is_named(self):
        assert GrafanaAdmin().args_problems({"url": BASE}) == ["url: grafana-admin takes no args"]

    def test_it_reaches_grafana_at_its_root_url(self):
        # GrafanaDeploy config/prd/values.yaml's root_url; 12.3.1 answered there on 2026-10-08.
        assert grafana_admin.BASE == BASE


class TestThePlans:
    def test_it_logs_in_before_it_writes_and_syncs_before_the_password_is_set(self):
        assert ids(World().plan()) == [
            "grafana.login",
            f"random.generate:{KEY}",
            "kv.write",
            f"eso.sync:{ES}",
            "grafana.set_admin_password",
            "kv.stamp",
        ]

    def test_an_activation_comes_before_the_set(self):
        assert ids(World().plan(store=activate_auto())) == [
            "grafana.login",
            f"random.generate:{KEY}",
            "kv.write",
            f"eso.sync:{ES}",
            f"k8s.rollout:{ROLLED}",
            "grafana.set_admin_password",
            "kv.stamp",
        ]

    def test_offline_without_a_snapshot_it_has_no_plan(self):
        with pytest.raises(PlanError, match="offline plan without a snapshot does not reach"):
            World().plan(cluster=False)

    def test_against_a_snapshot_it_plans_without_reaching_grafana(self, tmp_path):
        path = tmp_path / "snapshot.json"
        path.write_text(json.dumps(snapshot(grafana_objects())))
        world = World()
        plan = make(
            {KIND: world.kind}, LEAF, KIND, [KEY], audit(world.store), Cluster.of_snapshot(path)
        )
        assert ids(plan) == ids(world.plan())
        assert world.grafana.requests == []

    def test_a_leaf_with_two_keys_of_the_kind_has_no_plan(self):
        store = seed_store()
        store[LEAF].keys.add("other-password")
        edit(store[LEAF].meta, "other-password", kind=KIND, interval="365d", activate="none")
        with pytest.raises(PlanError, match="a grafana-admin plan rotates one key, not 2"):
            make(
                {KIND: GrafanaAdmin()},
                LEAF,
                KIND,
                [KEY, "other-password"],
                audit(store),
                Cluster(FakeCluster(grafana_objects()).kube()),
            )

    def test_the_steps_and_the_description(self):
        plan = World().plan()
        login, set_password = plan.steps[0], plan.steps[4]
        assert (login.mutates, login.silent) == (False, True)
        assert (set_password.mutates, set_password.activator) == (True, False)
        assert set_password.undo is not None
        assert login.title == "log in to Grafana with the admin password the leaf holds"
        assert set_password.title == "set Grafana's admin password to the new one"
        assert not plan.needs_operator and plan.ask == ""
        assert plan.description == (
            "The tool generates a new password for Grafana's admin and writes it to the leaf. "
            "Once every ExternalSecret that reads the leaf has synced, it sets the password in "
            "Grafana, logged in with the one Grafana held, and logs in with the new one."
        )


class TestTheRuns:
    def test_a_rotation_leaves_grafana_taking_the_password_kv_holds(self):
        world = World()
        assert world.run() is Outcome.DONE
        new = world.password()
        assert new != PASSWORD and len(new) == 43
        assert world.grafana.password() == new
        assert world.grafana.failed == 0
        assert state_of(world.bao, LEAF).stamps == {KEY: "2026-10-05"}
        assert world.bao.data(LEAF)["admin-user"] == ADMIN
        assert not any(PASSWORD in text or new in text for text in world.texts())

    def test_the_password_is_set_only_once_kv_and_the_secret_hold_it(self):
        world = World()
        assert world.run() is Outcome.DONE
        new = world.password()
        assert world.posted == [(new, new, 1)]
        assert world.synced == [new]

    def test_with_activate_auto_grafana_rolls_out_before_the_set(self):
        world = World()
        grafana = world.cluster.get("deployments", "grafana-prd", "grafana")
        world.grafana.before_put = lambda login, new: world.posted.append(
            grafana["metadata"]["generation"]
        )
        assert world.run(store=activate_auto()) is Outcome.DONE
        assert world.posted == [2]

    def test_the_next_rotation_logs_in_with_the_password_the_first_set(self):
        world = World()
        assert world.run() is Outcome.DONE
        first = world.password()
        assert world.run(day=1) is Outcome.DONE
        assert world.grafana.password() == world.password() != first
        assert world.grafana.failed == 0

    def test_a_set_grafana_refuses_rolls_back_and_leaves_everything_on_the_old_password(self):
        world = World()
        world.grafana.refused["PUT", PUT_PATH] = 500
        executor = world.executor()
        assert executor.run() is Outcome.FAILED
        failure = world.failure()
        assert failure.step.id == "grafana.set_admin_password"
        assert failure.error == f"PUT {PUT_PATH}: HTTP 500: refused"
        assert executor.abort_blocker() is None
        assert executor.abort() is Outcome.ROLLED_BACK
        assert world.password() == PASSWORD and world.grafana.password() == PASSWORD
        (new, old) = world.synced
        assert (new != PASSWORD, old) == (True, PASSWORD)
        assert len(world.grafana.puts()) == 1

    def test_a_set_whose_answer_is_lost_is_undone_once_kv_holds_the_old_password_again(self):
        world = World()
        world.grafana.lost["PUT", PUT_PATH] = 502
        executor = world.executor()
        assert executor.run() is Outcome.FAILED
        assert world.failure().error == f"PUT {PUT_PATH}: HTTP 502"
        new = world.password()
        assert world.grafana.password() == new
        world.grafana.lost.clear()
        assert executor.abort() is Outcome.ROLLED_BACK
        assert world.password() == PASSWORD and world.grafana.password() == PASSWORD
        assert world.posted == [(new, new, 1), (PASSWORD, new, 1)]
        assert world.synced == [new, PASSWORD]
        assert world.grafana.failed == 0
        assert not any(PASSWORD in text or new in text for text in world.texts())

    def test_a_set_whose_answer_is_lost_is_retried_and_finishes(self):
        world = World()
        world.grafana.lost["PUT", PUT_PATH] = 502
        assert world.run() is Outcome.FAILED
        world.grafana.lost.clear()
        assert world.run() is Outcome.DONE
        assert world.grafana.password() == world.password() != PASSWORD
        assert len(world.grafana.puts()) == 1 and world.grafana.failed == 1
        assert state_of(world.bao, LEAF).stamps == {KEY: "2026-10-05"}

    def test_a_set_that_did_not_reach_grafana_is_left_as_it_is_by_the_rollback(self):
        world = World()
        world.grafana.broken["PUT", PUT_PATH] = TimeoutError("timed out")
        executor = world.executor()
        assert executor.run() is Outcome.FAILED
        assert (
            world.failure().error == f"PUT {PUT_PATH}: transport error: TimeoutError('timed out')"
        )
        assert executor.abort() is Outcome.ROLLED_BACK
        assert world.password() == PASSWORD and world.grafana.password() == PASSWORD
        assert world.posted == [] and world.grafana.failed == 1

    def test_a_rollback_grafana_takes_neither_password_for_fails(self):
        world = World()
        world.grafana.refused["PUT", PUT_PATH] = 500
        executor = world.executor()
        assert executor.run() is Outcome.FAILED
        world.grafana.users[ADMIN]["password"] = "SECRET-someone-else-s"
        assert executor.abort() is Outcome.ROLLBACK_FAILED
        assert world.recorder.failures()[-1].error == (
            f"Grafana refuses user {ADMIN} with the new password or the password it held: {REFUSED}"
        )

    @pytest.mark.parametrize(
        ("world", "error"),
        [
            (
                lambda: World(password="SECRET-not-grafana-s"),
                f"Grafana refuses user {ADMIN} with the password {LEAF} holds: {REFUSED}",
            ),
            (
                lambda: World(user="viewer", password="SECRET-viewer"),
                "Grafana user viewer is no Grafana server admin",
            ),
            (lambda: World(user=""), f"{LEAF} holds no admin-user"),
            (lambda: World(password=""), f"{LEAF} holds no {KEY}"),
        ],
    )
    def test_a_login_grafana_does_not_take_as_its_admin_stops_the_plan_before_it_writes(
        self, world, error
    ):
        world = world()
        before = dict(world.bao.data(LEAF))
        executor = world.executor()
        assert executor.run() is Outcome.FAILED
        assert world.failure().step.id == "grafana.login"
        assert world.failure().error == error
        assert world.bao.data(LEAF) == before and world.grafana.puts() == []
        assert executor.abort() is Outcome.CANCELLED

    def test_an_unreachable_grafana_stops_the_plan_before_it_writes(self):
        world = World()
        world.grafana.broken["GET", USER_PATH] = ConnectionRefusedError(111, "Connection refused")
        assert world.run() is Outcome.FAILED
        assert world.failure().error == (
            f"GET {USER_PATH}: transport error: ConnectionRefusedError(111, 'Connection refused')"
        )
        assert world.password() == PASSWORD


class Ctx:
    def __init__(self, bao, staged=None):
        self.bao = client(bao)
        self.now = NOW
        self.values = dict(staged or {})

    def progress(self, detail):
        pass

    def stage(self, name, value):
        self.values[name] = value

    def staged(self, name):
        return self.values.get(name)


def grafana(world):
    return Grafana(BASE, world.grafana)


class TestTheSteps:
    def test_a_login_stages_the_password_grafana_held_and_a_re_run_keeps_it(self):
        world = World()
        ctx = Ctx(world.bao)
        login = Login(grafana(world), LEAF, KEY)
        assert login.run(ctx) == f"Grafana user {ADMIN}, a server admin"
        assert ctx.values == {HELD: PASSWORD}
        ctx.values[HELD] = "SECRET-staged-before"
        login.run(ctx)
        assert ctx.values == {HELD: "SECRET-staged-before"}

    def test_a_set_on_a_grafana_that_takes_the_new_password_already_sets_nothing(self):
        world = World()
        world.grafana.users[ADMIN]["password"] = "SECRET-new"
        ctx = Ctx(world.bao, {HELD: PASSWORD, value_name(KEY): "SECRET-new"})
        set_password = SetAdminPassword(grafana(world), LEAF, KEY)
        assert set_password.run(ctx) == "Grafana takes the new password already"
        assert world.grafana.puts() == [] and world.grafana.failed == 1

    def test_a_set_logs_in_with_the_new_password_after_it_sets_it(self):
        world = World()
        world.grafana.set = lambda admin, user_id, req: FakeResponse(200, b"{}")
        ctx = Ctx(world.bao, {HELD: PASSWORD, value_name(KEY): "SECRET-new"})
        with pytest.raises(StepFailed) as e:
            SetAdminPassword(grafana(world), LEAF, KEY).run(ctx)
        assert str(e.value) == f"Grafana refuses user {ADMIN} with the new password: {REFUSED}"

    def test_a_login_grafana_answers_otherwise_than_refusing_is_its_failure(self):
        world = World()
        world.grafana.refused["GET", USER_PATH] = 503
        ctx = Ctx(world.bao, {HELD: PASSWORD, value_name(KEY): "SECRET-new"})
        with pytest.raises(GrafanaError) as e:
            SetAdminPassword(grafana(world), LEAF, KEY).run(ctx)
        assert str(e.value) == f"GET {USER_PATH}: HTTP 503: refused"
        assert world.grafana.requests == [("GET", USER_PATH, ADMIN)]

    def test_an_undo_on_a_grafana_that_takes_the_held_password_sets_nothing(self):
        world = World()
        ctx = Ctx(world.bao, {HELD: PASSWORD, value_name(KEY): "SECRET-new"})
        set_password = SetAdminPassword(grafana(world), LEAF, KEY)
        assert set_password.undo(ctx) == "Grafana takes the password it held: nothing to set back"
        assert world.grafana.puts() == []

    def test_an_undo_sets_the_held_password_back(self):
        world = World()
        ctx = Ctx(world.bao, {HELD: PASSWORD, value_name(KEY): "SECRET-new"})
        set_password = SetAdminPassword(grafana(world), LEAF, KEY)
        assert set_password.run(ctx) == "set; Grafana takes a login with it"
        assert world.grafana.password() == "SECRET-new"
        assert set_password.undo(ctx) == (
            "set back; Grafana takes a login with the password it held"
        )
        assert world.grafana.password() == PASSWORD

    @pytest.mark.parametrize(
        ("staged", "error"),
        [
            ({value_name(KEY): "SECRET-new"}, "no password Grafana held before the plan is staged"),
            ({HELD: PASSWORD}, "no new password is staged"),
        ],
    )
    def test_a_set_and_its_undo_need_both_passwords_staged(self, staged, error):
        world = World()
        set_password = SetAdminPassword(grafana(world), LEAF, KEY)
        for do in (set_password.run, set_password.undo):
            with pytest.raises(StepFailed, match=f"^{error}$"):
                do(Ctx(world.bao, staged))
        assert world.grafana.requests == []


class TestTheClient:
    def test_it_logs_in_by_basic_auth_and_sends_the_password_as_json(self):
        world = World()
        world.grafana.users[ADMIN]["password"] = "SECRET-a:b"
        client = grafana(world)
        assert client.user(ADMIN, "SECRET-a:b")["isGrafanaAdmin"] is True
        client.set_password(ADMIN, "SECRET-a:b", 1, 'SECRET-"c"')
        assert world.grafana.password() == 'SECRET-"c"'

    @pytest.mark.parametrize(
        ("raised", "error"),
        [
            (
                urllib.error.HTTPError(BASE, 502, "Bad Gateway", {}, io.BytesIO(b"<html>")),
                "GET /api/user: HTTP 502",
            ),
            (
                urllib.error.URLError("Name or service not known"),
                "GET /api/user: transport error: Name or service not known",
            ),
        ],
    )
    def test_a_failure_names_the_request(self, raised, error):
        def opener(req):
            raise raised

        with pytest.raises(GrafanaError) as e:
            Grafana(BASE, opener).user(ADMIN, PASSWORD)
        assert str(e.value) == error

    def test_an_answer_that_is_not_json_is_a_failure(self):
        with pytest.raises(GrafanaError, match="^GET /api/user: HTTP 200, not a JSON answer$"):
            Grafana(BASE, lambda req: FakeResponse(200, b"<html>")).user(ADMIN, PASSWORD)
