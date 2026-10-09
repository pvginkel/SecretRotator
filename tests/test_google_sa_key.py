"""The google-sa-key kind (design §6) over the seed's two rows, which take no args; its plans,
which sync every ExternalSecret that reads the leaf before the proof and the delete, whatever the
leaf's activate; the runs, in which each service account creates its own new key with the key the
leaf holds, the proof waits for Google to take it, and the plan deletes the key the leaf held and
no other; their failures and rollbacks; and Google's client."""

import base64
import datetime
import email.message
import io
import json
import urllib.error
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, padding
from fake_cluster import FakeCluster, externalsecret, pod_spec, snapshot, workload
from fake_google import NOT_TAKEN, PERMISSION, FakeGoogle, b64url_json
from fixtures import edit
from plans import NOW, Recorder, client, fake_of, lock, run_state, state_of
from test_activation import ticking

from secret_rotator import annotate as ann
from secret_rotator.audit import audit
from secret_rotator.cluster import Cluster
from secret_rotator.executor import AbortRefused, Executor, Outcome
from secret_rotator.kinds.google_sa_key import GoogleSaKey, google
from secret_rotator.kinds.google_sa_key.google import Account, GoogleError, assertion, parse
from secret_rotator.kinds.google_sa_key.steps import OLD, PROVE_BOUND, SENT, Delete, Mint, Prove
from secret_rotator.model import Action, Finished, Progress, StepFailed, value_name
from secret_rotator.plan import PlanError, make

SEED = ann.load_seed(ann.DEFAULT_SEED)
KIND = "google-sa-key"
CALENDAR = "eso/prd/calendar-support/prd/google-service-account"
MEDIA = "eso/prd/media/prd/mydownloads-firebase"
LEAVES = {CALENDAR: "key_json", MEDIA: "firebase-service-account.json"}  # store-keys.json
ACCOUNTS = {
    CALENDAR: "calendar-support@calendar-display-437018.iam.gserviceaccount.com",
    MEDIA: "firebase-adminsdk-fbsvc@mydownloads-5c1e2.iam.gserviceaccount.com",
}
ES = {
    CALENDAR: "calendar-support-prd/calendar-support-sa-key",
    MEDIA: "media-prd/media-mydownloads-firebase",
}
ROLLED = {
    CALENDAR: "calendar-support-prd/deployment/calendar-support",
    MEDIA: "media-prd/deployment/mydownloads",
}
MINT, PROVE, DELETE = "google_sa_key.mint", "google_sa_key.prove", "google_sa_key.delete"
LOST = ConnectionResetError(104, "Connection reset by peer")
SENT_AT = "2026-10-05T04:30:00Z"  # the executor's clock at the mint's run: NOW
TOKEN_PATH = "/token"


def keys_path(leaf):
    return f"/v1/projects/-/serviceAccounts/{ACCOUNTS[leaf]}/keys"


def seed_store():
    return ann.offline_store(Path(str(ann.DEFAULT_KEYS)), SEED, lambda line: None)


def google_objects():
    """(resource, object) of the readers of the kind's leaves, as prd holds them (2026-10-09):
    calendar-support reads calendar-support-sa-key's key_json as a file of a projected volume,
    mydownloads media-mydownloads-firebase's key as a file of a secret volume, which it mounts by
    subPath."""
    return [
        (
            "externalsecrets",
            externalsecret(
                "calendar-support-prd",
                "calendar-support-sa-key",
                data=[(CALENDAR, LEAVES[CALENDAR])],
                target="calendar-support-sa-key",
            ),
        ),
        (
            "deployments",
            workload(
                "Deployment",
                "calendar-support-prd",
                "calendar-support",
                pod_spec(projected=["calendar-support-sa-key"]),
            ),
        ),
        (
            "externalsecrets",
            externalsecret(
                "media-prd",
                "media-mydownloads-firebase",
                data=[(MEDIA, LEAVES[MEDIA])],
                target="media-mydownloads-firebase",
            ),
        ),
        (
            "deployments",
            workload(
                "Deployment",
                "media-prd",
                "mydownloads",
                pod_spec(volume=["media-mydownloads-firebase"]),
            ),
        ),
    ]


def id_of(text):
    return json.loads(text)["private_key_id"]


class World:
    """The seed's store on the fake OpenBao; Google, where each leaf holds a key of its own
    service account, which has one more key minted elsewhere; and the leaves' readers on the fake
    cluster."""

    def __init__(self, store=None, *, may_key=True):
        self.store = store or seed_store()
        self.google = FakeGoogle()
        for address in ACCOUNTS.values():
            self.google.account(address, may_key=may_key)
        texts = {leaf: self.google.key(ACCOUNTS[leaf]) for leaf in LEAVES}
        self.made = {leaf: id_of(text) for leaf, text in texts.items()}
        self.elsewhere = {leaf: id_of(self.google.key(ACCOUNTS[leaf])) for leaf in LEAVES}
        self.initial = {leaf: self.keys(leaf) for leaf in LEAVES}
        data = {leaf: {key: texts[leaf]} for leaf, key in LEAVES.items()}
        self.bao = fake_of(self.store, data)
        self.cluster = FakeCluster(google_objects())
        self.kind = GoogleSaKey(self.google.google())

    def keys(self, leaf):
        """The ids of the keys of the leaf's account."""
        return self.google.keys(ACCOUNTS[leaf])

    def held(self, leaf):
        """The key file the leaf holds."""
        return self.bao.data(leaf)[LEAVES[leaf]]

    def holding(self, leaf):
        """The id of the key the leaf holds."""
        return id_of(self.held(leaf))

    def plan(self, leaf, *, cluster=None, store=None):
        return make(
            {KIND: self.kind},
            leaf,
            KIND,
            [LEAVES[leaf]],
            audit(store or self.store),
            cluster or Cluster(self.cluster.kube()),
        )

    def executor(self, leaf, *, day=0, store=None):
        self.recorder = Recorder()
        tick = ticking()
        return Executor(
            client(self.bao),
            self.plan(leaf, store=store),
            self.recorder,
            lock(self.bao),
            state=run_state(self.bao),
            dry_run=False,
            clock=lambda: tick() + datetime.timedelta(days=day),
        )

    def run(self, leaf, *, day=0, store=None):
        return self.executor(leaf, day=day, store=store).run()

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

    def finished(self, step, action=Action.RUN):
        """The detail of the step's last line of the action that finished ok."""
        (*_, last) = (
            e.detail
            for e in self.recorder.events
            if isinstance(e, Finished) and e.ok and e.step.id == step and e.action is action
        )
        return last

    def generation(self, leaf):
        ns, _, name = ROLLED[leaf].split("/")
        return self.cluster.get("deployments", ns, name)["metadata"]["generation"]


def ids(plan):
    return [s.id for s in plan.steps]


def plan_ids(leaf):
    return [
        MINT,
        "kv.write",
        f"eso.sync:{ES[leaf]}",
        f"k8s.rollout:{ROLLED[leaf]}",
        PROVE,
        DELETE,
        "kv.stamp",
    ]


class TestTheSeed:
    def test_each_leaf_of_the_kind_has_one_key_of_it_and_no_args(self):
        entries = audit(seed_store()).entries
        found = {
            leaf: (key, entry.args, entry.interval, [str(a) for a in entry.activate])
            for leaf, by_key in entries.items()
            for key, entry in by_key.items()
            if entry.kind == KIND
        }
        assert found == {
            CALENDAR: ("key_json", {}, 30, ["eso", "k8s-rollout"]),
            MEDIA: ("firebase-service-account.json", {}, 30, ["eso", "k8s-rollout"]),
        }

    def test_args_it_cannot_use_are_named(self):
        assert GoogleSaKey().args_problems({"account": "x"}) == [
            "account: google-sa-key takes no args"
        ]

    def test_it_reaches_google_s_token_endpoint_and_iam_api(self):
        assert google.TOKEN_URI == "https://oauth2.googleapis.com/token"
        assert google.IAM == "https://iam.googleapis.com/v1"


class TestThePlans:
    @pytest.mark.parametrize("leaf", [CALENDAR, MEDIA])
    def test_each_leaf_syncs_and_rolls_out_its_reader_before_the_proof_and_the_delete(self, leaf):
        assert ids(World().plan(leaf)) == plan_ids(leaf)

    def test_the_externalsecret_syncs_though_the_activate_is_none(self):
        store = seed_store()
        edit(store[MEDIA].meta, LEAVES[MEDIA], activate="none")
        assert ids(World().plan(MEDIA, store=store)) == [
            MINT,
            "kv.write",
            f"eso.sync:{ES[MEDIA]}",
            PROVE,
            DELETE,
            "kv.stamp",
        ]

    def test_offline_without_a_snapshot_no_leaf_has_a_plan(self):
        world = World()
        for leaf, key in LEAVES.items():
            with pytest.raises(PlanError, match="offline plan without a snapshot does not reach"):
                make({KIND: world.kind}, leaf, KIND, [key], audit(world.store), None)

    def test_against_a_snapshot_it_plans_without_reaching_google(self, tmp_path):
        path = tmp_path / "snapshot.json"
        path.write_text(json.dumps(snapshot(google_objects())))
        world = World()
        for leaf in LEAVES:
            assert ids(world.plan(leaf, cluster=Cluster.of_snapshot(path))) == plan_ids(leaf)
        assert world.google.requests == []

    def test_a_leaf_with_two_keys_of_the_kind_has_no_plan(self):
        store = seed_store()
        store[CALENDAR].keys.add("other")
        edit(store[CALENDAR].meta, "other", kind=KIND, interval="30d", activate="none")
        with pytest.raises(PlanError, match="a google-sa-key plan rotates one key, not 2"):
            make(
                {KIND: GoogleSaKey()},
                CALENDAR,
                KIND,
                ["key_json", "other"],
                audit(store),
                Cluster(FakeCluster(google_objects()).kube()),
            )

    def test_the_steps_and_the_description(self):
        plan = World().plan(CALENDAR)
        mint, prove, delete = plan.steps[0], plan.steps[4], plan.steps[5]
        assert (mint.mutates, mint.activator, mint.undo is not None) == (True, False, True)
        assert (prove.mutates, prove.silent) == (False, True)
        assert (delete.mutates, delete.undo) == (True, None)
        assert delete.no_undo == "a deleted Google service account key cannot be restored"
        assert mint.title == "create a new key of the service account"
        assert prove.title == "log in with the new key"
        assert delete.title == "delete the key the leaf held"
        assert not plan.needs_operator and plan.ask == ""
        assert plan.description == (
            "The tool creates a new key of the service account with the key the leaf holds, and "
            "writes it to the leaf and activates what reads it. Once every ExternalSecret that "
            "reads the leaf has synced, it logs in with the new key and deletes the one the leaf "
            "held."
        )
        assert GoogleSaKey().credential(plan.target) == "Google service account key"


class TestTheRuns:
    @pytest.mark.parametrize("leaf", [CALENDAR, MEDIA])
    def test_a_rotation_creates_the_account_s_new_key_with_its_own_and_deletes_only_the_old(
        self, leaf
    ):
        world = World()
        old = world.made[leaf]
        assert world.run(leaf) is Outcome.DONE
        new = world.holding(leaf)
        assert parse(world.held(leaf)).email == ACCOUNTS[leaf]
        assert world.keys(leaf) == (world.initial[leaf] - {old}) | {new}
        assert world.elsewhere[leaf] in world.keys(leaf)
        other = next(other for other in LEAVES if other != leaf)
        assert world.keys(other) == world.initial[other]
        assert state_of(world.bao, leaf).stamps == {LEAVES[leaf]: "2026-10-05"}
        (create,) = world.google.done("create")
        assert create[2] == keys_path(leaf)
        assert world.google.done("delete") == [
            ("delete", "DELETE", f"{keys_path(leaf)}/{old}", old)
        ]
        pem_lines = [
            line
            for text in world.google.texts
            for line in json.loads(text)["private_key"].splitlines()
            if "PRIVATE KEY" not in line
        ]
        secrets = [*world.google.texts, *world.google.access, *pem_lines]
        assert not any(s in text for text in world.texts() for s in secrets)
        assert not any("PRIVATE KEY" in text for text in world.texts())

    def test_the_old_key_is_deleted_only_once_the_secret_holds_the_new_and_the_reader_rolled(self):
        world = World()
        at_delete = []
        world.google.before["delete"] = lambda key: at_delete.append(
            (key, world.holding(MEDIA), world.cluster.syncs, world.generation(MEDIA))
        )
        assert world.run(MEDIA) is Outcome.DONE
        assert at_delete == [(world.made[MEDIA], world.holding(MEDIA), 1, 2)]

    def test_the_proof_waits_for_google_to_take_the_new_key(self):
        world = World()
        assert world.run(CALENDAR) is Outcome.DONE
        new = world.holding(CALENDAR)
        assert world.google.now >= world.google.lag
        tries = [r for r in world.google.done("token") if r[3] == new]
        assert len(tries) == 5  # refused at 0, 10 and 20 s; taken at 30 s; the delete's login
        waiting = [
            e.detail
            for e in world.recorder.events
            if isinstance(e, Progress) and e.step.id == PROVE
        ]
        assert waiting == 3 * [
            f"Google refuses it: POST {TOKEN_PATH}: HTTP 400: invalid_grant: {NOT_TAKEN}"
        ]
        assert world.finished(PROVE) == f"logged in as {ACCOUNTS[CALENDAR]} with key {new}"
        assert world.finished(MINT) == f"key {new} of {ACCOUNTS[CALENDAR]}"
        assert world.finished(DELETE) == f"deleted key {world.made[CALENDAR]}"

    def test_the_next_rotation_creates_with_the_first_s_new_key_and_deletes_it(self):
        world = World()
        assert world.run(CALENDAR) is Outcome.DONE
        first = world.holding(CALENDAR)
        assert world.run(CALENDAR, day=1) is Outcome.DONE
        second = world.holding(CALENDAR)
        assert world.keys(CALENDAR) == (world.initial[CALENDAR] - {world.made[CALENDAR]}) | {second}
        assert first not in world.keys(CALENDAR)

    def test_an_account_that_may_not_key_itself_stops_the_plan_before_it_writes(self):
        world = World(may_key=False)
        executor = world.executor(CALENDAR)
        assert executor.run() is Outcome.FAILED
        assert world.failure().step.id == MINT
        assert world.failure().error == (
            f"POST {keys_path(CALENDAR)}: HTTP 403: {PERMISSION.format('create')}"
        )
        assert executor.abort() is Outcome.CANCELLED
        assert world.holding(CALENDAR) == world.made[CALENDAR]
        assert world.keys(CALENDAR) == world.initial[CALENDAR]

    def test_an_unreachable_google_stops_the_plan_before_it_writes(self):
        world = World()
        world.google.broken["token"] = ConnectionRefusedError(111, "Connection refused")
        executor = world.executor(MEDIA)
        assert executor.run() is Outcome.FAILED
        assert world.failure().error == (
            f"POST {TOKEN_PATH}: transport error: ConnectionRefusedError(111, 'Connection refused')"
        )
        assert executor.abort() is Outcome.CANCELLED
        assert world.holding(MEDIA) == world.made[MEDIA]

    def test_a_key_google_does_not_hold_stops_the_plan_before_it_writes(self):
        world = World()
        del world.google.accounts[ACCOUNTS[MEDIA]]["keys"][world.made[MEDIA]]
        executor = world.executor(MEDIA)
        assert executor.run() is Outcome.FAILED
        assert world.failure().step.id == MINT
        assert world.failure().error == (
            f"Google refuses the key {MEDIA}#{LEAVES[MEDIA]} holds: POST {TOKEN_PATH}: HTTP 400: "
            f"invalid_grant: {NOT_TAKEN}"
        )
        assert executor.abort() is Outcome.CANCELLED
        assert world.google.done("create") == []

    @pytest.mark.parametrize(
        ("value", "error"),
        [
            ("", f"{CALENDAR}#key_json holds no key file"),
            (
                "SECRET-no-key-file",
                f"{CALENDAR}#key_json holds no service account key file: not JSON",
            ),
            (
                '{"type": "authorized_user"}',
                f"{CALENDAR}#key_json holds no service account key file: not a service account "
                f"key file",
            ),
        ],
    )
    def test_a_leaf_without_a_key_file_stops_the_plan_before_it_writes(self, value, error):
        world = World()
        world.bao.new_version(CALENDAR, {"key_json": value})
        executor = world.executor(CALENDAR)
        assert executor.run() is Outcome.FAILED
        assert world.failure().error == error
        assert executor.abort() is Outcome.CANCELLED
        assert world.google.requests == []

    def test_a_create_whose_answer_is_lost_leaves_a_key_no_one_holds_and_a_retry_finishes(self):
        world = World()
        world.google.lost["create"] = LOST
        executor = world.executor(CALENDAR)
        assert executor.run() is Outcome.FAILED
        assert world.failure().error == f"POST {keys_path(CALENDAR)}: transport error: {LOST!r}"
        (lost,) = world.keys(CALENDAR) - world.initial[CALENDAR]
        world.google.lost.clear()
        assert executor.run() is Outcome.DONE
        new = world.holding(CALENDAR)
        assert world.keys(CALENDAR) == (world.initial[CALENDAR] - {world.made[CALENDAR]}) | {
            lost,
            new,
        }

    def test_a_create_google_failed_mid_way_did_land_and_rolls_back_with_nothing_created(self):
        world = World()
        world.google.refused["create"] = 503
        executor = world.executor(CALENDAR)
        assert executor.run() is Outcome.FAILED
        assert executor.abort() is Outcome.ROLLED_BACK
        assert world.finished(MINT, Action.UNDO) == (
            f"nothing to delete: the create sent at {SENT_AT} failed, and a key it made, if any, "
            f"is one no one holds, which Google keeps"
        )
        assert world.holding(CALENDAR) == world.made[CALENDAR]
        assert world.keys(CALENDAR) == world.initial[CALENDAR]

    def test_a_create_whose_answer_is_lost_rolls_back_saying_its_key_is_left(self):
        world = World()
        world.google.lost["create"] = LOST
        executor = world.executor(CALENDAR)
        assert executor.run() is Outcome.FAILED
        assert executor.abort() is Outcome.ROLLED_BACK
        assert world.finished(MINT, Action.UNDO).startswith(
            f"nothing to delete: the create sent at {SENT_AT} failed"
        )
        assert len(world.keys(CALENDAR) - world.initial[CALENDAR]) == 1
        assert world.holding(CALENDAR) == world.made[CALENDAR]

    def test_a_new_key_google_never_takes_rolls_back_to_the_old_and_deletes_the_new(self):
        world = World()
        world.google.lag = 10 * PROVE_BOUND
        executor = world.executor(MEDIA)
        assert executor.run() is Outcome.FAILED
        new = world.holding(MEDIA)
        assert world.failure().step.id == PROVE
        assert world.failure().error == (
            f"Google did not take key {new} within 5 min: Google refuses it: POST {TOKEN_PATH}: "
            f"HTTP 400: invalid_grant: {NOT_TAKEN}"
        )
        assert executor.abort() is Outcome.ROLLED_BACK
        assert world.finished(MINT, Action.UNDO) == f"deleted key {new}"
        assert world.holding(MEDIA) == world.made[MEDIA]
        assert world.keys(MEDIA) == world.initial[MEDIA]
        assert world.cluster.syncs == 2 and world.generation(MEDIA) == 3

    def test_a_refused_delete_did_not_land_and_rolls_back_to_the_old_key(self):
        world = World()
        world.google.refused["delete"] = 403
        executor = world.executor(CALENDAR)
        assert executor.run() is Outcome.FAILED
        assert world.failure().step.id == DELETE
        assert world.made[CALENDAR] in world.keys(CALENDAR)
        assert executor.abort_blocker() is None
        world.google.refused.clear()
        assert executor.abort() is Outcome.ROLLED_BACK
        assert world.holding(CALENDAR) == world.made[CALENDAR]
        assert world.keys(CALENDAR) == world.initial[CALENDAR]

    def test_a_delete_whose_answer_is_lost_cannot_be_aborted_and_a_retry_finishes(self):
        world = World()
        world.google.lost["delete"] = LOST
        executor = world.executor(CALENDAR)
        assert executor.run() is Outcome.FAILED
        assert world.made[CALENDAR] not in world.keys(CALENDAR)
        assert executor.abort_blocker() == "a deleted Google service account key cannot be restored"
        with pytest.raises(AbortRefused):
            executor.abort()
        world.google.lost.clear()
        assert executor.run() is Outcome.DONE
        assert world.finished(DELETE) == f"key {world.made[CALENDAR]} was deleted already"
        assert state_of(world.bao, CALENDAR).stamps == {"key_json": "2026-10-05"}


class Ctx:
    def __init__(self, bao, staged=None):
        self.bao = client(bao)
        self.now = NOW
        self.values = dict(staged or {})
        self.details = []

    def progress(self, detail):
        self.details.append(detail)

    def stage(self, name, value):
        self.values[name] = value

    def staged(self, name):
        return self.values.get(name)


class TestTheSteps:
    def test_a_mint_stages_the_old_id_the_time_it_sent_the_create_and_the_new_key_file(self):
        world = World()
        ctx = Ctx(world.bao)
        detail = Mint(world.kind.google, CALENDAR, "key_json").run(ctx)
        (new,) = world.keys(CALENDAR) - world.initial[CALENDAR]
        assert ctx.values == {
            OLD: world.made[CALENDAR],
            SENT: SENT_AT,
            value_name("key_json"): world.google.texts[-1],
        }
        assert id_of(ctx.values[value_name("key_json")]) == new
        assert detail == f"key {new} of {ACCOUNTS[CALENDAR]}"

    def test_a_re_run_with_a_key_created_creates_none(self):
        world = World()
        ctx = Ctx(world.bao)
        mint = Mint(world.kind.google, CALENDAR, "key_json")
        mint.run(ctx)
        before = dict(ctx.values)
        mint.run(ctx)
        assert ctx.values == before
        assert len(world.google.done("create")) == 1

    def test_an_undo_with_nothing_created_reaches_nothing(self):
        world = World()
        mint = Mint(world.kind.google, CALENDAR, "key_json")
        assert mint.undo(Ctx(world.bao)) == "nothing was created"
        assert world.google.requests == []

    def test_an_undo_deletes_the_key_created_with_the_key_the_leaf_holds(self):
        world = World()
        ctx = Ctx(world.bao)
        mint = Mint(world.kind.google, CALENDAR, "key_json")
        mint.run(ctx)
        new = id_of(ctx.values[value_name("key_json")])
        assert mint.undo(ctx) == f"deleted key {new}"
        assert world.keys(CALENDAR) == world.initial[CALENDAR]
        assert [r[3] for r in world.google.done("token")][-1] == world.made[CALENDAR]
        assert mint.undo(ctx) == f"Google lists no key {new}: nothing to delete"

    def test_a_proof_needs_the_leaf_to_hold_the_key_the_plan_created(self):
        world = World()
        world.google.lag = 0
        ctx = Ctx(world.bao)
        Mint(world.kind.google, CALENDAR, "key_json").run(ctx)
        prove = Prove(world.kind.google, CALENDAR, "key_json")
        with pytest.raises(StepFailed, match="does not hold the key the plan created$"):
            prove.run(ctx)
        world.bao.new_version(CALENDAR, {"key_json": ctx.values[value_name("key_json")]})
        new = id_of(ctx.values[value_name("key_json")])
        assert prove.run(ctx) == f"logged in as {ACCOUNTS[CALENDAR]} with key {new}"
        assert ctx.details == []

    def test_a_proof_does_not_wait_out_a_transport_error(self):
        world = World()
        ctx = Ctx(world.bao)
        Mint(world.kind.google, CALENDAR, "key_json").run(ctx)
        world.bao.new_version(CALENDAR, {"key_json": ctx.values[value_name("key_json")]})
        world.google.broken["token"] = LOST
        with pytest.raises(GoogleError, match="transport error") as e:
            Prove(world.kind.google, CALENDAR, "key_json").run(ctx)
        assert e.value.status is None and world.google.now == 0

    @pytest.mark.parametrize("step", [Prove(None, CALENDAR, "key_json"), Delete(None, "key_json")])
    def test_a_proof_and_a_delete_need_the_new_key_staged(self, step):
        world = World()
        with pytest.raises(StepFailed, match="^no new key is staged$") as e:
            step.run(Ctx(world.bao, {OLD: world.made[CALENDAR]}))
        assert e.value.landed is (not isinstance(step, Delete))

    def test_a_delete_of_a_key_deleted_already_sends_none(self):
        world = World()
        gone = id_of(world.google.key(ACCOUNTS[CALENDAR]))
        world.google.remove(ACCOUNTS[CALENDAR], gone)
        ctx = Ctx(world.bao, {OLD: gone, value_name("key_json"): world.held(CALENDAR)})
        assert Delete(world.kind.google, "key_json").run(ctx) == f"key {gone} was deleted already"
        assert world.google.done("delete") == []

    def test_a_delete_google_answers_without_deleting_fails(self):
        world = World()
        world.google.remove = lambda account, key: None
        ctx = Ctx(
            world.bao,
            {OLD: world.made[CALENDAR], value_name("key_json"): world.held(CALENDAR)},
        )
        with pytest.raises(StepFailed, match="still lists key .* after its delete$") as e:
            Delete(world.kind.google, "key_json").run(ctx)
        assert e.value.landed


class TestTheClient:
    @pytest.mark.parametrize(
        ("status", "refused"), [(400, True), (403, True), (404, True), (503, False), (None, False)]
    )
    def test_a_4xx_answer_changed_nothing(self, status, refused):
        assert GoogleError("x", status).refused is refused

    def test_the_assertion_is_an_rs256_jwt_the_key_signs_for_its_account(self):
        world = World()
        key = parse(world.held(CALENDAR))
        jwt = assertion(key, 1_791_000_000)
        header, claims, signature = jwt.split(".")
        assert b64url_json(header) == {"alg": "RS256", "typ": "JWT", "kid": world.made[CALENDAR]}
        assert b64url_json(claims) == {
            "iss": ACCOUNTS[CALENDAR],
            "scope": "https://www.googleapis.com/auth/cloud-platform",
            "aud": "https://oauth2.googleapis.com/token",
            "iat": 1_791_000_000,
            "exp": 1_791_003_600,
        }
        raw = base64.urlsafe_b64decode(signature + "=" * (-len(signature) % 4))
        key.private.public_key().verify(
            raw, f"{header}.{claims}".encode(), padding.PKCS1v15(), hashes.SHA256()
        )

    def test_a_refusal_names_the_request_its_status_and_google_s_words(self):
        world = World()
        account = world.kind.google.login(parse(world.held(CALENDAR)))
        with pytest.raises(GoogleError) as e:
            account.delete("0" * 40)
        assert (str(e.value), e.value.status) == (
            f"DELETE {keys_path(CALENDAR)}/{'0' * 40}: HTTP 404: Service account key {'0' * 40} "
            f"does not exist.",
            404,
        )

    def test_an_account_reached_with_another_account_s_token_is_refused(self):
        world = World()
        own = world.kind.google.login(parse(world.held(CALENDAR)))
        other = Account(world.kind.google, ACCOUNTS[MEDIA], own.token)
        with pytest.raises(GoogleError) as e:
            other.key_ids()
        assert str(e.value) == f"GET {keys_path(MEDIA)}: HTTP 403: {PERMISSION.format('list')}"

    def test_an_account_without_keys_lists_none(self):
        world = World()
        account = world.kind.google.login(parse(world.held(CALENDAR)))
        world.google.unlisted = world.keys(CALENDAR)
        assert account.key_ids() == []

    @pytest.mark.parametrize(
        ("body", "error"),
        [(b"<html>bad gateway</html>", "HTTP 502"), (b'{"error": 7}', "HTTP 502")],
    )
    def test_a_refusal_without_google_s_words_names_its_status(self, body, error):
        def opener(req):
            raise urllib.error.HTTPError(
                req.full_url, 502, "err", email.message.Message(), io.BytesIO(body)
            )

        with pytest.raises(GoogleError) as e:
            google.Google(opener).request("GET", f"{google.IAM}/x")
        assert str(e.value) == f"GET /v1/x: {error}"

    def test_an_answer_that_is_not_json_fails(self):
        class Answer:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return None

            def read(self):
                return b"<html>"

        with pytest.raises(GoogleError, match="^GET /v1/x: not a JSON answer$"):
            google.Google(lambda req: Answer()).request("GET", f"{google.IAM}/x")


class TestTheKeyFile:
    def test_it_reads_the_id_the_account_and_the_private_key_and_shows_no_key(self):
        world = World()
        key = parse(world.held(MEDIA))
        assert (key.id, key.email) == (world.made[MEDIA], ACCOUNTS[MEDIA])
        assert "private" not in repr(key)

    @pytest.mark.parametrize(
        ("change", "error"),
        [
            ({"private_key_id": ""}, "it lacks private_key_id, client_email or private_key"),
            ({"client_email": None}, "it lacks private_key_id, client_email or private_key"),
            ({"private_key": "SECRET-no-pem"}, "its private_key is no PEM private key"),
        ],
    )
    def test_a_key_file_it_cannot_sign_with_is_named_without_its_key(self, change, error):
        world = World()
        doc = json.loads(world.held(MEDIA)) | change
        with pytest.raises(ValueError) as e:
            parse(json.dumps(doc))
        assert str(e.value) == error

    def test_a_key_file_with_another_algorithm_s_key_is_named(self):
        world = World()
        pem = (
            ec.generate_private_key(ec.SECP256R1())
            .private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
            .decode()
        )
        doc = json.loads(world.held(MEDIA)) | {"private_key": pem}
        with pytest.raises(ValueError, match="^its private_key is no RSA key$"):
            parse(json.dumps(doc))
