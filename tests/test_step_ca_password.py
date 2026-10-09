"""The step-ca-password kind (design §6) over the seed's KubeCoder step-ca provisioner password and
its dev copy: the plan reads the kubecoder-jwk key step-ca serves, generates the password and
shows it; the operator gives kubecoder-jwk a new key pair under it (050 I2), and Done runs the CA
check (052 F1). While the check fails the step stays open, its reason under the instruction and
Abort enabled; once it passes the tool writes both leaves and restarts both KubeCoder controllers,
and Abort is disabled. The key of the plan's start survives a second Done and a Resume. step-ca
over HTTPS, step-cli, the terminal and the UI."""

import datetime
import io
import json
import sys
import urllib.error
from pathlib import Path

import pytest
from fake_cluster import FakeCluster, externalsecret, pod_spec, workload
from fake_step import REFUSED
from fake_step_ca import FakeStepCa
from fixtures import edit
from plans import Recorder, client, fake_of, flight_of, lock, run_state, state_of
from sim import rotation
from test_activation import ticking
from test_ui import SIZE, app_of, in_box, select, until
from test_wizard import go, labels, phase, specs

from secret_rotator import annotate as ann
from secret_rotator import registry, terminal
from secret_rotator.audit import audit
from secret_rotator.cluster import Cluster
from secret_rotator.console import Console
from secret_rotator.executor import Abandon, AbortRefused, Executor, Outcome
from secret_rotator.kinds.step_ca_password import (
    INSTRUCTION,
    IRREVERSIBLE,
    PROVISIONER,
    TITLE,
    StepCaPassword,
)
from secret_rotator.kinds.step_ca_password.stepca import (
    NotOpened,
    StepCa,
    StepCaError,
    material,
)
from secret_rotator.kinds.step_ca_password.steps import AT_START, FAILED, CheckedShow, ReadKey
from secret_rotator.listing import credential
from secret_rotator.model import value_name
from secret_rotator.opsteps import ShowRequest
from secret_rotator.plan import PlanError, make, of_leaf
from secret_rotator.staging import staging_leaf
from secret_rotator.ui.item import Phase
from secret_rotator.ui.widgets import Instruction, ValueBox

KINDS = registry.load()
SEED = ann.load_seed(ann.DEFAULT_SEED)
KIND = "step-ca-password"
LEAF = "eso/prd/kubecoder/prd/step-ca-provisioner-password"
DEV = "eso/prd/kubecoder/dev/step-ca-provisioner-password"
KEY = "password"
ES = "kubecoder-step-ca-provisioner-password"
STAGES = {"kubecoder-prd": LEAF, "kubecoder-dev": DEV}
FAKE = str(Path(__file__).with_name("fake_step.py"))
OLD = "SECRET-old-provisioner-password"
SAME_KEY = (
    "step-ca serves kubecoder-jwk under the key it served when the rotation started: the "
    "playbook has not run, or the key was re-encrypted, not replaced"
)


def seed_store():
    return ann.offline_store(Path(str(ann.DEFAULT_KEYS)), SEED, lambda line: None)


def controller_objects():
    """(resource, object) of the password's readers on prd, as read 2026-10-09: in each KubeCoder
    stage, the ExternalSecret of its leaf and the controller, which takes the Secret's password as
    an env var by secretKeyRef, with its Argo Application."""
    objects = []
    for ns, leaf in STAGES.items():
        objects += [
            ("externalsecrets", externalsecret(ns, ES, data=[(leaf, KEY)], target=ES)),
            (
                "deployments",
                workload("Deployment", ns, "kubecoder-controller", pod_spec(env=[ES]), app=ns),
            ),
            (
                "applications",
                {
                    "metadata": {"namespace": "argocd-prd", "name": ns},
                    "status": {"health": {"status": "Healthy"}},
                },
            ),
        ]
    return objects


class Operator(Recorder):
    """A Recorder whose answer may be what the operator does first: a callable of the request,
    which returns the answer."""

    def ask(self, step, request):
        self.asked.append((step.id, request))
        answer = self.answers.pop(0)
        return answer(request) if callable(answer) else answer


def does(action, answer=None):
    """The operator does the action with the shown password, then answers: Done by default."""

    def act(request):
        action(request.value)
        return {} if answer is None else answer

    return act


class World:
    """The seed's store on the fake OpenBao, both leaves holding the old password; step-ca serving
    kubecoder-jwk under a key that opens with it; and both controllers on the fake cluster."""

    def __init__(self, tmp_path):
        self.tmp_path = tmp_path
        self.store = seed_store()
        self.bao = fake_of(self.store, {LEAF: {KEY: OLD}, DEV: {KEY: OLD}})
        self.ca = FakeStepCa(OLD)
        self.start = material(self.ca.provisioner()["key"])
        self.step = (sys.executable, FAKE, str(tmp_path))
        self.kinds = {**KINDS, KIND: StepCaPassword(self.ca, self.step)}
        self.cluster = FakeCluster(controller_objects())

    def password(self, leaf=LEAF):
        return self.bao.data(leaf)[KEY]

    def plan(self):
        return make(self.kinds, LEAF, KIND, [KEY], audit(self.store), Cluster(self.cluster.kube()))

    def executor(self, *answers):
        self.recorder = Operator(*answers)
        self.running = Executor(
            client(self.bao),
            self.plan(),
            self.recorder,
            lock(self.bao),
            state=run_state(self.bao),
            dry_run=False,
            clock=ticking(),
        )
        return self.running

    def requests(self):
        return [request for _, request in self.recorder.asked]

    def texts(self):
        """Every detail, error and technical text the run reported."""
        return [
            getattr(e, name, "")
            for e in self.recorder.events
            for name in ("detail", "error", "technical")
        ]

    def generations(self):
        return [
            self.cluster.get("deployments", ns, "kubecoder-controller")["metadata"]["generation"]
            for ns in STAGES
        ]

    def staged(self):
        return self.bao.leaves[staging_leaf(KIND, LEAF)]["data"]

    def calls(self):
        """How step-cli was run: its argv and the password it read."""
        path = self.tmp_path / "calls.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def ids(plan):
    return [s.id for s in plan.steps]


class TestTheSeed:
    def test_the_password_rotates_yearly_activating_what_its_externalsecrets_feed(self):
        entries = audit(seed_store()).entries
        entry = entries[LEAF][KEY]
        assert (entry.kind, entry.args, entry.interval) == (KIND, {}, 365)
        # No manual: StepCaDeploy is gone; the operator step covers the CA side.
        assert [str(spec) for spec in entry.activate] == ["eso", "k8s-rollout"]
        assert StepCaPassword().args_problems(entry.args) == []

    def test_its_dev_copy_activates_what_its_externalsecret_feeds(self):
        copy = audit(seed_store()).entries[DEV][KEY]
        assert copy.kind == f"copy:{LEAF}#{KEY}"
        assert [str(spec) for spec in copy.activate] == ["eso", "k8s-rollout"]

    def test_an_arg_is_named(self):
        assert StepCaPassword().args_problems({"provisioner": PROVISIONER}) == [
            "provisioner: step-ca-password takes no args"
        ]


class TestThePlan:
    def test_it_reads_the_key_generates_and_shows_then_writes_both_leaves_and_restarts(
        self, tmp_path
    ):
        assert ids(World(tmp_path).plan()) == [
            f"step_ca.read_key:{PROVISIONER}",
            f"random.generate:{KEY}",
            f"operator.show:value:{KEY}",
            "kv.write",
            f"kv.copy:{DEV}#{KEY}",
            f"eso.sync:kubecoder-prd/{ES}",
            f"eso.sync:kubecoder-dev/{ES}",
            "k8s.rollout:kubecoder-prd/deployment/kubecoder-controller",
            "k8s.rollout:kubecoder-dev/deployment/kubecoder-controller",
            "kv.stamp",
        ]

    def test_the_steps_the_ask_and_the_description(self, tmp_path):
        plan = World(tmp_path).plan()
        read, generate, show = plan.steps[:3]
        assert isinstance(read, ReadKey) and read.silent and not read.mutates
        assert generate.length == 43
        assert isinstance(show, CheckedShow) and show.type == "operator.show"
        assert show.title == TITLE == "Give kubecoder-jwk a new key pair under the new password"
        assert show.instruction == INSTRUCTION
        assert INSTRUCTION == (
            "The new password of step-ca's kubecoder-jwk provisioner. Follow Ansible "
            'docs/runbooks/step-ca-bootstrap.md, "JWK provisioner password rotation", for '
            "kubecoder-jwk: give it a new key pair under this password in the step_ca role's "
            "ca.json, run playbooks/step-ca.yml and commit. Done then checks that step-ca "
            "serves the new key and that this password opens it."
        )
        # It mutates with no undo, so Abort is disabled once the plan is past it.
        assert show.mutates and show.undo is None and show.no_undo == IRREVERSIBLE
        assert plan.needs_operator  # so the nightly run never starts it
        assert plan.ask == "give kubecoder-jwk a new key pair under the new password"
        assert credential(KINDS[KIND], plan.target) == "step-ca provisioner password"
        assert plan.description == (
            "The tool generates a new password for step-ca's kubecoder-jwk provisioner. You give "
            "kubecoder-jwk a new key pair under it and run the step-ca playbook; Done checks that "
            "step-ca serves it. The tool then writes it to the leaf and its 1 copy and activates "
            "what reads it."
        )

    def test_secret_rotator_plan_builds_it_for_the_leaf(self, tmp_path):
        world = World(tmp_path)
        cluster = Cluster(world.cluster.kube())
        (planned,), unplanned = of_leaf(LEAF, world.store, audit(world.store), KINDS, cluster)
        assert (planned.kind, planned.keys, planned.error, unplanned) == (KIND, (KEY,), "", {})
        assert ids(planned.plan) == ids(world.plan())

    def test_offline_without_a_snapshot_it_has_no_plan(self):
        with pytest.raises(PlanError, match="offline plan without a snapshot does not reach"):
            make(KINDS, LEAF, KIND, [KEY], audit(seed_store()))

    def test_a_leaf_with_two_keys_of_the_kind_has_no_plan(self, tmp_path):
        world = World(tmp_path)
        world.store[LEAF].keys.add("other")
        edit(world.store[LEAF].meta, "other", kind=KIND, interval="365d", activate="none")
        with pytest.raises(PlanError, match="a step-ca-password plan rotates one key, not 2"):
            make(
                KINDS,
                LEAF,
                KIND,
                [KEY, "other"],
                audit(world.store),
                Cluster(world.cluster.kube()),
            )


class TestTheCheck:
    def test_a_new_key_pair_under_the_shown_password_passes_and_the_tool_writes_and_restarts(
        self, tmp_path
    ):
        world = World(tmp_path)
        assert world.executor(does(world.ca.new_pair)).run() is Outcome.DONE
        new = world.password()
        assert new != OLD and len(new) == 43 and world.password(DEV) == new
        assert world.generations() == [2, 2]
        (request,) = world.requests()
        assert request == ShowRequest(TITLE, INSTRUCTION, new)
        assert state_of(world.bao, LEAF).stamps == {KEY: "2026-10-05"}
        # step-cli read the password from a pipe, not from its command line.
        (call,) = world.calls()
        assert call["password"] == new and call["argv"][4].startswith("/dev/fd/")
        assert not any(new in arg for arg in call["argv"])
        secrets = (OLD, new, world.ca.private()["d"])
        assert not any(s in text for s in secrets for text in world.texts())

    def test_done_before_the_playbook_ran_keeps_the_step_open_with_the_reason_and_abort(
        self, tmp_path
    ):
        world = World(tmp_path)
        blockers = []

        def nothing_yet(request):
            return {}

        def then_the_runbook(request):
            blockers.append(world.running.abort_blocker())
            world.ca.new_pair(request.value)
            return {}

        assert world.executor(nothing_yet, then_the_runbook).run() is Outcome.DONE
        first, second = world.requests()
        assert second == ShowRequest(
            TITLE, f"{INSTRUCTION}\n\n{FAILED.format(SAME_KEY)}", first.value
        )
        assert FAILED.format(SAME_KEY) == (
            f"The CA check failed: {SAME_KEY}. Redo the key pair or the playbook, then press Done "
            f"again."
        )
        assert blockers == [None]  # Abort was open while the step stayed open
        assert world.password() == world.password(DEV) == first.value

    @pytest.mark.parametrize(
        ("what", "operator", "reason"),
        [
            ("a re-encrypted key", lambda ca: ca.reencrypt, SAME_KEY),
            (
                "a key made under a mistyped password",
                lambda ca: lambda password: ca.new_pair(password + "x"),
                f"the new password does not open the kubecoder-jwk key step-ca serves: {REFUSED}",
            ),
            (
                "a key and an encryptedKey of two pairs",
                lambda ca: ca.mismatched,
                "the key the new password opens is not the kubecoder-jwk key step-ca serves",
            ),
            (
                "ca.home out of reach",
                lambda ca: (
                    lambda password: setattr(
                        ca, "broken", urllib.error.URLError(ConnectionRefusedError(111, "refused"))
                    )
                ),
                "GET https://ca.home/provisioners: transport error: [Errno 111] refused",
            ),
            (
                "no kubecoder-jwk",
                lambda ca: lambda password: ca.provisioners.pop(),
                "step-ca serves no JWK provisioner kubecoder-jwk",
            ),
        ],
    )
    def test_a_check_that_fails_asks_again_and_an_abort_there_changes_nothing(
        self, tmp_path, what, operator, reason
    ):
        world = World(tmp_path)
        executor = world.executor(does(operator(world.ca)), Abandon.ABORT)
        assert executor.run() is Outcome.CANCELLED
        first, second = world.requests()
        assert second.instruction == f"{INSTRUCTION}\n\n{FAILED.format(reason)}"
        assert second.value == first.value
        assert world.password() == world.password(DEV) == OLD
        assert world.generations() == [1, 1]
        assert flight_of(world.bao, LEAF) is None
        assert not any(first.value in text for text in world.texts())

    def test_once_done_passes_abort_is_disabled(self, tmp_path):
        world = World(tmp_path)
        world.cluster.stuck.add("kubecoder-dev/kubecoder-controller")
        executor = world.executor(does(world.ca.new_pair))
        assert executor.run() is Outcome.FAILED
        assert world.recorder.failures()[0].step.id == (
            "k8s.rollout:kubecoder-dev/deployment/kubecoder-controller"
        )
        new = world.password()
        assert new != OLD and world.password(DEV) == new
        assert (
            executor.abort_blocker()
            == IRREVERSIBLE
            == ("step-ca serves kubecoder-jwk's new key pair, which only the new password opens")
        )
        with pytest.raises(AbortRefused, match=IRREVERSIBLE):
            executor.abort()
        world.cluster.stuck.clear()
        assert world.executor().run() is Outcome.DONE
        assert world.password() == new and world.generations() == [2, 3]

    def test_the_key_of_the_plan_s_start_survives_an_exit_and_a_resume(self, tmp_path):
        world = World(tmp_path)
        assert world.executor(Abandon.EXIT).run() is Outcome.EXITED
        assert flight_of(world.bao, LEAF).step == f"operator.show:value:{KEY}"
        assert world.staged()[AT_START] == world.start
        (shown,) = world.requests()
        world.ca.new_pair(shown.value)  # the runbook, done while the UI was gone
        assert (
            world.executor({}).run() is Outcome.DONE
        )  # compared against the start, not a fresh read
        assert world.password() == shown.value
        assert world.ca.requests == ["/provisioners", "/provisioners?cursor=2"] * 2

    def test_an_abort_asked_for_while_the_check_runs_stops_the_step_passed_or_not(self, tmp_path):
        world = World(tmp_path)

        def runbook_then_abort(request):
            world.ca.new_pair(request.value)
            world.ca.on_get = lambda: world.running.stop(Abandon.ABORT)  # Abort, pressed
            return {}

        assert world.executor(runbook_then_abort).run() is Outcome.CANCELLED
        assert world.password() == world.password(DEV) == OLD
        assert world.generations() == [1, 1]

    def test_ca_home_out_of_reach_at_the_start_stops_the_plan_before_it_generates(self, tmp_path):
        world = World(tmp_path)
        world.ca.status = 503
        executor = world.executor()
        assert executor.run() is Outcome.FAILED
        (failure,) = world.recorder.failures()
        assert failure.step.id == f"step_ca.read_key:{PROVISIONER}"
        assert failure.error == "GET https://ca.home/provisioners: HTTP 503"
        assert world.requests() == []
        assert executor.abort() is Outcome.CANCELLED

    def test_a_read_with_a_key_staged_keeps_it(self, tmp_path):
        world = World(tmp_path)

        class Ctx:
            values = {AT_START: "the key of the plan's start"}

            def staged(self, name):
                return self.values.get(name)

        assert ReadKey(StepCa(opener=world.ca), PROVISIONER).run(Ctx()) == "read before"
        assert world.ca.requests == []


class TestStepCa:
    def test_it_lists_every_provisioner_page_by_page(self, tmp_path):
        ca = FakeStepCa(OLD)
        found = StepCa(opener=ca).provisioners()
        assert [p["name"] for p in found] == ["admin", "acme", "ansible-jwk", "kubecoder-jwk"]
        assert ca.requests == ["/provisioners", "/provisioners?cursor=2"]

    def test_a_refused_request_names_its_status(self):
        ca = FakeStepCa(OLD)
        ca.status = 500
        with pytest.raises(StepCaError, match=r"^GET https://ca.home/provisioners: HTTP 500$"):
            StepCa(opener=ca).jwk(PROVISIONER)

    def test_a_provisioner_that_is_no_jwk_one_has_no_key(self):
        with pytest.raises(StepCaError, match="^step-ca serves no JWK provisioner acme$"):
            StepCa(opener=FakeStepCa(OLD)).jwk("acme")

    def test_the_password_opens_the_key_to_its_material_and_only_by_a_pipe(self, tmp_path):
        world = World(tmp_path)
        ca = StepCa(opener=world.ca, step=world.step)
        key, encrypted = ca.jwk(PROVISIONER)
        assert ca.opens(encrypted, OLD) == material(key)
        assert world.ca.private()["d"] not in material(world.ca.private())
        with pytest.raises(NotOpened, match=f"^{REFUSED}$"):
            ca.opens(encrypted, "SECRET-wrong")
        with pytest.raises(NotOpened, match="compact JWE format must have five parts"):
            ca.opens("garbage", OLD)
        assert [c["password"] for c in world.calls()] == [OLD, "SECRET-wrong", OLD]
        assert all(c["argv"][4].startswith("/dev/fd/") for c in world.calls())

    def test_without_step_cli_it_says_so(self):
        with pytest.raises(StepCaError, match="^no-such-step-cli is not on the PATH$"):
            StepCa(step=("no-such-step-cli",)).opens("FAKE-JWE.e30", OLD)

    def test_a_key_of_another_kty_is_no_key(self):
        with pytest.raises(StepCaError, match="a key of kty 'oct' is not an EC, OKP or RSA key"):
            material({"kty": "oct", "k": "SECRET"})


class Typing(io.StringIO):
    """A console's stdin answering from these lines in order; a callable answer is run first and
    answers what it returns."""

    def __init__(self, *answers):
        super().__init__()
        self.answers = list(answers)

    def readline(self, *args):
        answer = self.answers.pop(0)
        return f"{answer() if callable(answer) else answer}\n"


class TestTheFrontEnds:
    def test_run_keeps_the_step_open_with_the_reason_until_done_passes(self, tmp_path):
        world = World(tmp_path)

        def runbook():
            world.ca.new_pair(world.staged()[value_name(KEY)])
            return "d"

        con = Console(Typing("y", "d", runbook), io.StringIO())
        code = terminal.run_leaf(
            client(world.bao),
            LEAF,
            world.kinds,
            con,
            holder="run test",
            today=datetime.date(2026, 10, 9),
            cluster=Cluster(world.cluster.kube()),
        )
        out = con.stdout.getvalue()
        assert code == 0
        assert out.count(INSTRUCTION) == 2
        assert FAILED.format(SAME_KEY) in out
        assert f"Done: {KEY} of {LEAF} rotated." in out
        new = world.password()
        assert new != OLD and new not in out and world.generations() == [2, 2]

    async def test_the_ui_keeps_the_step_open_with_the_reason_until_done_passes(self, tmp_path):
        world = World(tmp_path)
        app = app_of(world.bao, rotations=[rotation(world.plan())])
        rid = f"{LEAF}#{KEY}"
        reason = FAILED.format(SAME_KEY)

        def reopened():
            request = app.items[rid].request
            return phase(app, rid) is Phase.WAITING and reason in request.instruction

        async with app.run_test(size=SIZE) as pilot:
            await select(pilot, app, rid)
            await go(pilot, app)
            await until(pilot, lambda: phase(app, rid) is Phase.WAITING and in_box(app))
            value = app.items[rid].request.value
            app.action_press_button("done")
            await until(pilot, reopened)
            box = app.box(rid)
            assert reason in box.query_one(Instruction).content.plain
            assert labels(app, rid) == ["Done", "Reveal", "Copy", "Abort"]
            assert specs(app, rid)["abort"].enabled and specs(app, rid)["done"].enabled
            app.action_press_button("reveal")
            await pilot.pause(0.05)
            assert box.query_one(ValueBox).content.plain == value
            world.ca.new_pair(value)
            app.action_press_button("done")
            await until(pilot, lambda: rid not in app.order)
        assert world.password() == world.password(DEV) == value
