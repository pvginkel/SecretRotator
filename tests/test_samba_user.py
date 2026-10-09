"""The samba-user kind (design §6) over the seed's two Samba accounts. mydownloads' app account
(`account: app`): the plan generates the password, writes it, syncs samba-creds and restarts the
media pod, whose Samba server takes the password at start, before mydownloads, which re-stages its
share; it has no operator step, so it runs nightly. The personal account pvginkel: the plan takes
the password the operator types (050 F1), writes it, syncs every ExternalSecret that reads the leaf,
restarts the four Samba servers and no KubeCoder environment pod, and ends with the Windows
confirm; mvdbovenkamp keeps its password. Under the entries annotated before the account arg, no
plan generates the personal password. The runs, a failed restart and the Aborts."""

from pathlib import Path

import pytest
from fake_cluster import FakeCluster, externalsecret, pod_spec, workload
from fixtures import edit
from plans import Recorder, client, fake_of, lock, run_state, state_of
from test_activation import ticking

from secret_rotator import annotate as ann
from secret_rotator import registry
from secret_rotator.audit import audit
from secret_rotator.cluster import Cluster
from secret_rotator.executor import Abandon, Executor, Outcome
from secret_rotator.kinds.samba_user import SambaUser
from secret_rotator.listing import credential
from secret_rotator.opsteps import ConfirmRequest, CredentialRequest
from secret_rotator.plan import PlanError, make, of_leaf

KINDS = registry.load()
SEED = ann.load_seed(ann.DEFAULT_SEED)
KIND = "samba-user"
APP = "eso/prd/media/prd/mydownloads-user"
USERS = "shared/samba/users"
MEDIA = "media-prd/deployment/media"
MYDOWNLOADS = "media-prd/deployment/mydownloads"
# The Samba servers that serve the personal account (prd, read 2026-10-09), as the plan orders them.
SERVERS = (
    MEDIA,
    "newsfilter-prd/deployment/samba",
    "scantopdf-prd/deployment/samba",
    "storage-prd/deployment/storage",
)
# Every ExternalSecret that reads shared/samba/users, as the plan orders them.
READERS = (
    "kubecoder-dev/kubecoder-samba-credential",
    "kubecoder-prd/kubecoder-samba-credential",
    "media-prd/media-passwords",
    "newsfilter-prd/newsfilter-passwords",
    "scantopdf-prd/scantopdf-passwords",
    "storage-prd/storage-passwords",
)
WINDOWS = (
    "set the new password in the Windows environments that mount the shares and restart the "
    "KubeCoder environments that mount them"
)
OLD = "SECRET-old-samba-password"
THEIRS = "SECRET-mvdbovenkamp-own-password"
TYPED = "SECRET-typed-new-password"


def seed_store():
    return ann.offline_store(Path(str(ann.DEFAULT_KEYS)), SEED, lambda line: None)


def annotated_before_052():
    """The seed's store with the two accounts' entries as annotated before this kind: no args,
    mydownloads restarted alone, the Windows confirm without the KubeCoder environments."""
    store = seed_store()
    edit(store[APP].meta, "password", args=None, activate=f"k8s-rollout:{MYDOWNLOADS}")
    edit(
        store[USERS].meta,
        "pvginkel",
        activate="eso,k8s-rollout,manual:set the new password in the Windows environments that "
        "mount the shares",
    )
    return store


def application(name):
    return {
        "metadata": {"namespace": "argocd-prd", "name": name},
        "status": {"health": {"status": "Healthy"}},
    }


def readers():
    """(resource, object) of the two leaves' readers on prd, read 2026-10-09. samba-creds feeds the
    media pod's Samba server and, as the PV samba-pv's node-stage secret, mydownloads' share, which
    no pod template names; mydownloads-users is another leaf. Each Samba server reads its
    <app>-passwords, and every KubeCoder environment, a bare pod, kubecoder-samba-credential."""
    objects = [
        (
            "externalsecrets",
            externalsecret(
                "media-prd",
                "samba-creds",
                data=[(APP, "username"), (APP, "password")],
                target="samba-creds",
            ),
        ),
        (
            "externalsecrets",
            externalsecret(
                "media-prd",
                "media-mydownloads-users",
                extract=["eso/prd/media/prd/mydownloads-users"],
                target="media-mydownloads-users",
            ),
        ),
        (
            "deployments",
            workload("Deployment", "media-prd", "mydownloads", pod_spec(), app="media-prd"),
        ),
    ]
    for server in SERVERS:
        ns, _, name = server.split("/")
        passwords = f"{ns.removesuffix('-prd')}-passwords"
        objects.append(
            (
                "externalsecrets",
                externalsecret(
                    ns,
                    passwords,
                    data=[(USERS, "mvdbovenkamp"), (USERS, "pvginkel")],
                    target=passwords,
                ),
            )
        )
        env = [passwords, "samba-creds"] if server == MEDIA else [passwords]
        objects.append(("deployments", workload("Deployment", ns, name, pod_spec(env=env), app=ns)))
        objects.append(("applications", application(ns)))
    for ns in ("kubecoder-prd", "kubecoder-dev"):
        es = externalsecret(
            ns,
            "kubecoder-samba-credential",
            data=[(USERS, "pvginkel")],
            target="kubecoder-samba-credential",
        )
        objects.append(("externalsecrets", es))
    pod = {
        "metadata": {"namespace": "kubecoder-prd", "name": "env-1"},
        "spec": pod_spec(env_from=["kubecoder-samba-credential"]),
    }
    objects.append(("pods", pod))
    return objects


class World:
    """The store on the fake OpenBao, the two leaves holding the old passwords, and their readers
    on the fake cluster."""

    def __init__(self, store=None):
        self.store = store or seed_store()
        data = {
            APP: {"username": "mydownloads", "password": OLD},
            USERS: {"mvdbovenkamp": THEIRS, "pvginkel": OLD},
        }
        self.bao = fake_of(self.store, data)
        self.cluster = FakeCluster(readers())

    def plan(self, leaf, key):
        return make(KINDS, leaf, KIND, [key], audit(self.store), Cluster(self.cluster.kube()))

    def executor(self, leaf, key, *answers):
        self.recorder = Recorder(*answers)
        return Executor(
            client(self.bao),
            self.plan(leaf, key),
            self.recorder,
            lock(self.bao),
            state=run_state(self.bao),
            dry_run=False,
            clock=ticking(),
        )

    def restarts(self):
        """The workloads restarted, in order: <ns>/deployment/<name>."""
        return [
            f"{path.split('/')[5]}/deployment/{path.split('/')[7]}"
            for path, _ in self.cluster.patches()
            if path.split("/")[6] == "deployments"
        ]

    def synced(self):
        """The ExternalSecrets synced, in order: <ns>/<name>."""
        return [
            f"{path.split('/')[5]}/{path.split('/')[7]}"
            for path, _ in self.cluster.patches()
            if path.split("/")[6] == "externalsecrets"
        ]

    def texts(self):
        """Every detail, error and technical text the run reported."""
        return [
            getattr(e, name, "")
            for e in self.recorder.events
            for name in ("detail", "error", "technical")
        ]


def ids(plan):
    return [s.id for s in plan.steps]


class TestTheSeed:
    def test_mydownloads_is_an_app_account_restarting_the_media_pod_before_mydownloads(self):
        entries = audit(seed_store()).entries[APP]
        entry = entries["password"]
        assert (entry.kind, entry.args, entry.interval) == (KIND, {"account": "app"}, 14)
        assert [str(spec) for spec in entry.activate] == [f"k8s-rollout:{MEDIA},{MYDOWNLOADS}"]
        assert entries["username"].kind == "none"
        assert SambaUser().args_problems(entry.args) == []

    def test_pvginkel_is_a_personal_account_and_mvdbovenkamp_stays_manual(self):
        entries = audit(seed_store()).entries[USERS]
        entry = entries["pvginkel"]
        assert (entry.kind, entry.args, entry.interval) == (KIND, {}, 365)
        assert [str(spec) for spec in entry.activate] == ["eso", "k8s-rollout", f"manual:{WINDOWS}"]
        theirs = entries["mvdbovenkamp"]
        assert (theirs.kind, theirs.interval) == ("manual", None)

    def test_an_arg_or_an_account_it_does_not_know_is_named(self):
        assert SambaUser().args_problems({"account": "personal"}) == []
        assert SambaUser().args_problems({"account": "guest", "user": "x"}) == [
            "user: not samba-user's; its one arg is account",
            "account: 'guest' is not one of personal, app",
        ]


class TestTheAppAccount:
    def test_it_generates_writes_syncs_then_restarts_the_media_pod_before_mydownloads(self):
        plan = World().plan(APP, "password")
        assert ids(plan) == [
            "random.generate:password",
            "kv.write",
            "eso.sync:media-prd/samba-creds",
            f"k8s.rollout:{MEDIA}",
            f"k8s.rollout:{MYDOWNLOADS}",
            "kv.stamp",
        ]
        assert plan.steps[0].length == 43
        assert not plan.needs_operator  # so the nightly run takes it
        assert plan.ask == ""
        assert credential(KINDS[KIND], plan.target) == "Samba password"
        assert plan.description == (
            "The tool generates a new Samba password and writes it to the leaf and activates what "
            "reads it."
        )

    def test_secret_rotator_plan_builds_it_for_the_leaf(self):
        world = World()
        cluster = Cluster(world.cluster.kube())
        (planned,), unplanned = of_leaf(APP, world.store, audit(world.store), KINDS, cluster)
        assert (planned.kind, planned.keys, planned.error) == (KIND, ("password",), "")
        assert unplanned == {"username": "none: not a secret, never rotated"}
        assert ids(planned.plan) == ids(world.plan(APP, "password"))

    def test_a_rotation_restarts_the_server_with_the_new_password_then_its_client(self):
        world = World()
        assert world.executor(APP, "password").run() is Outcome.DONE
        new = world.bao.data(APP)["password"]
        assert new != OLD and len(new) == 43
        assert world.bao.data(APP)["username"] == "mydownloads"
        assert world.synced() == ["media-prd/samba-creds"]
        assert world.restarts() == [MEDIA, MYDOWNLOADS]
        assert world.recorder.asked == []
        assert state_of(world.bao, APP).stamps == {"password": "2026-10-05"}
        assert not any(OLD in text or new in text for text in world.texts())

    def test_a_media_pod_that_does_not_come_back_stops_it_before_mydownloads(self):
        world = World()
        world.cluster.stuck.add("media-prd/media")
        executor = world.executor(APP, "password")
        assert executor.run() is Outcome.FAILED
        assert world.recorder.failures()[0].step.id == f"k8s.rollout:{MEDIA}"
        assert state_of(world.bao, APP).status == "failed-activation"
        assert world.restarts() == [MEDIA]
        world.cluster.stuck.clear()
        assert executor.abort() is Outcome.ROLLED_BACK
        assert world.bao.data(APP)["password"] == OLD
        assert world.restarts() == [MEDIA, MEDIA]  # mydownloads never left the old password


class TestThePersonalAccount:
    def test_the_operator_types_it_then_the_write_the_servers_and_the_windows_confirm(self):
        plan = World().plan(USERS, "pvginkel")
        assert ids(plan) == [
            "operator.credential:pvginkel",
            "kv.write",
            *(f"eso.sync:{es}" for es in READERS),
            *(f"k8s.rollout:{server}" for server in SERVERS),
            f"operator.confirm:{USERS}:1",
            "kv.stamp",
        ]
        typed, confirm = plan.steps[0], plan.steps[-2]
        assert typed.keys == ("pvginkel",) and typed.shape is None and not typed.expires
        assert typed.title == "Choose a new password for the Samba account pvginkel and enter it"
        assert typed.instruction == (
            "The new password of the Samba account pvginkel, which you type where you mount its "
            "shares: choose one you can type."
        )
        assert confirm.title == WINDOWS
        assert plan.needs_operator
        assert plan.ask == f"type the new password; {WINDOWS}"
        assert plan.description == (
            "You choose a new password for the Samba account pvginkel and type it. The tool writes "
            "it to the leaf and activates what reads it. You confirm what only you can do."
        )

    def test_secret_rotator_plan_builds_it_apart_from_mvdbovenkamp_s_never_due_manual(self):
        world = World()
        cluster = Cluster(world.cluster.kube())
        plans, unplanned = of_leaf(USERS, world.store, audit(world.store), KINDS, cluster)
        by_kind = {planned.kind: planned for planned in plans}
        assert (set(by_kind), unplanned) == ({KIND, "manual"}, {})
        planned, theirs = by_kind[KIND], by_kind["manual"]
        assert (planned.keys, planned.error) == (("pvginkel",), "")
        assert ids(planned.plan) == ids(world.plan(USERS, "pvginkel"))
        assert (theirs.keys, theirs.due_at) == (("mvdbovenkamp",), None)

    def test_a_rotation_takes_the_typed_password_restarts_the_servers_and_no_environment(self):
        world = World()
        assert world.executor(USERS, "pvginkel", {"pvginkel": TYPED}, {}).run() is Outcome.DONE
        assert world.bao.data(USERS) == {"mvdbovenkamp": THEIRS, "pvginkel": TYPED}
        assert world.synced() == list(READERS)
        assert world.restarts() == list(SERVERS)  # no mydownloads
        assert not any("/pods/" in path for path, _ in world.cluster.patches())
        (typed, request), (confirmed, confirm) = world.recorder.asked
        assert typed == "operator.credential:pvginkel" and isinstance(request, CredentialRequest)
        assert [field.key for field in request.fields] == ["pvginkel"]
        assert confirmed == f"operator.confirm:{USERS}:1"
        assert isinstance(confirm, ConfirmRequest) and confirm.title == WINDOWS
        assert state_of(world.bao, USERS).stamps == {"pvginkel": "2026-10-05"}
        assert not any(OLD in text or TYPED in text for text in world.texts())

    def test_an_abort_at_the_windows_confirm_sets_the_old_password_back_on_every_server(self):
        world = World()
        executor = world.executor(USERS, "pvginkel", {"pvginkel": TYPED}, Abandon.ABORT)
        assert executor.run() is Outcome.ROLLED_BACK
        assert world.bao.data(USERS) == {"mvdbovenkamp": THEIRS, "pvginkel": OLD}
        assert world.restarts() == [*SERVERS, *SERVERS]  # rolled again after the KV rollback

    def test_a_leaf_with_two_keys_of_the_kind_has_no_plan(self):
        world = World()
        edit(world.store[USERS].meta, "mvdbovenkamp", kind=KIND, interval="365d")
        with pytest.raises(PlanError, match="a samba-user plan rotates one key, not 2"):
            make(
                KINDS,
                USERS,
                KIND,
                ["mvdbovenkamp", "pvginkel"],
                audit(world.store),
                Cluster(world.cluster.kube()),
            )


class TestTheEntriesAnnotatedBeforeTheAccountArg:
    def test_no_plan_generates_the_personal_password(self):
        world = World(annotated_before_052())
        plan = world.plan(USERS, "pvginkel")
        assert ids(plan)[0] == "operator.credential:pvginkel"
        assert not any(step.type == "random.generate" for step in plan.steps)

    def test_mydownloads_takes_a_typed_password_and_so_never_runs_nightly(self):
        world = World(annotated_before_052())
        plan = world.plan(APP, "password")
        assert ids(plan)[:2] == ["operator.credential:password", "kv.write"]
        assert plan.needs_operator
