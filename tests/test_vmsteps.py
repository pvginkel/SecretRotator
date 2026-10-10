"""The VM that may be off (vmsteps, slice 061 ruling D3): a plan with a step on such a VM starts
with a vm.start of it, which finds the VM's node through the PVE cluster, starts it there if it is
stopped, its start staged first, and waits until every step on it answers; the executor has the VM
up for each step or undo on it, in a run, a resume, a Retry or a rollback, and shuts it down when
the run or abort ends, whatever its outcome, if and only if the plan started it, also when an
earlier process did; an unattended run keeps it up for the rollback that follows a failure; a VM
that does not answer skips an unattended run and fails an attended one; PVE refusing the start
fails the plan before anything changes; a shutdown fails nothing; a dry run starts nothing."""

import dataclasses

import pytest
from fake_pve import FakePve, FakeVm
from fixtures import compliant_store
from plans import (
    LEAF,
    NOW,
    Confirm,
    Journal,
    RandomLike,
    Recorder,
    Tool,
    client,
    fake,
    flight_of,
    lock,
    put_flight,
    run_state,
    state_of,
)

from secret_rotator.audit import audit
from secret_rotator.executor import Abandon, Executor, Outcome
from secret_rotator.model import Action, Finished, Progress, Skipped
from secret_rotator.plan import StepFactory, build, target
from secret_rotator.sshsteps import Ssh
from secret_rotator.staging import staging_leaf
from secret_rotator.state import LeafState
from secret_rotator.vmsteps import (
    DEV_VM,
    PVE_BOUND,
    PVE_NODES,
    RESOURCES,
    Pve,
    VmStart,
    started_name,
)

STAGING = staging_leaf("random", LEAF)
SHUTDOWN = ("--timeout", "300", "--forceStop", "1")
BOOT = 120  # seconds the fake VM takes to answer once started
START = f"vm.start:{DEV_VM}"
RECORD = started_name(DEV_VM)
NO_ANSWER = f"{DEV_VM} did not answer within 15 min: the system does not answer"


class Clock:
    """A fake clock whose sleep runs what falls due."""

    def __init__(self):
        self.now = 0.0
        self.events = []

    def clock(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds
        due = [e for e in self.events if e[0] <= self.now]
        self.events = [e for e in self.events if e[0] > self.now]
        for _, event in due:
            event()

    def later(self, lag, event):
        self.events.append((self.now + lag, event))


class OnVm(Tool):
    """A Tool step on the VM, whose run and undo need what runs in it to answer."""

    vm = DEV_VM

    def __init__(self, id, journal, world, **kwargs):
        super().__init__(id, journal, **kwargs)
        self.world = world

    def unanswered(self, bao):
        return None if self.world.answering else "the system does not answer"

    def run(self, ctx):
        assert self.world.answering, f"{self.id} ran with the VM down"
        return super().run(ctx)

    def undo(self, ctx):
        assert self.world.answering, f"{self.id} undone with the VM down"
        return super().undo(ctx)


class World:
    """The compliant store on the fake OpenBao and srvk8sdev on a node of the fake PVE cluster,
    stopped unless running; what runs in it answers BOOT seconds after a start, unless boots is
    False, and stops answering at a shutdown. Its starts see the staging leaf's record."""

    def __init__(self, *, running=False, node="pve", boots=True):
        self.clock = Clock()
        self.bao = fake()
        self.answering = running
        self.boots = boots
        self.records = []  # the staging leaf's record of the start, as each start found it
        self.vm = FakeVm(
            DEV_VM,
            919,
            node,
            "running" if running else "stopped",
            started=self.started,
            stopped=self.stopped,
        )
        self.fake = FakePve([self.vm])
        self.pve = Pve(self.fake, sleep=self.clock.sleep, clock=self.clock.clock)
        self.journal = Journal()

    def started(self):
        staged = self.bao.leaves.get(STAGING, {}).get("data", {})
        self.records.append(staged.get(RECORD))
        if self.boots:
            self.clock.later(BOOT, lambda: setattr(self, "answering", True))

    def stopped(self):
        self.answering = False

    def on(self, id, **kwargs):
        return OnVm(id, self.journal, self, **kwargs)

    def plan(self, *extra):
        leaf = target(LEAF, "random", ["token"], audit(compliant_store()))
        return build(RandomLike(*extra), leaf, pve=self.pve)

    def executor(self, plan, *answers, dry_run=False):
        self.recorder = Recorder(*answers)
        return Executor(
            client(self.bao),
            plan,
            self.recorder,
            lock(self.bao),
            state=run_state(self.bao),
            dry_run=dry_run,
            clock=lambda: NOW,
        )

    def commands(self):
        return self.fake.commands()


class TestThePlan:
    def test_a_plan_with_steps_on_a_vm_starts_with_one_vm_start_that_waits_on_them(self):
        world = World()
        a, b = world.on("a"), world.on("b")
        plan = world.plan(a, Tool("between", world.journal), b)
        assert [s.id for s in plan.steps] == [
            START,
            "random.generate:token",
            "kv.write",
            "kv.copy:iac/copy#token",
            "a",
            "between",
            "b",
            "kv.stamp",
        ]
        start = plan.steps[0]
        assert isinstance(start, VmStart) and start.steps == (a, b)
        assert (start.title, start.mutates, start.vm, start.undo) == (
            f"start {DEV_VM} if it is off",
            False,
            None,
            None,
        )
        no_vm = world.plan(Tool("t", world.journal))
        assert [s.id for s in no_vm.steps if s.type == "vm.start"] == []

    def test_each_vm_gets_a_start_in_the_order_the_steps_first_name_them(self):
        world = World()
        other = world.on("other")
        other.vm = "wrkscratchk8s1"
        plan = world.plan(other, world.on("dev"))
        assert [s.id for s in plan.steps[:2]] == ["vm.start:wrkscratchk8s1", START]

    def test_the_pve_cluster_is_reached_as_ansible_on_its_nodes_with_room_for_a_shutdown(self):
        pve = Pve()
        assert pve.nodes == PVE_NODES == ("pve", "pve1", "pve2")
        assert pve.ssh.bound == PVE_BOUND > 300
        argv = pve.ssh.argv("pve", RESOURCES)
        assert argv[-12:] == ["-l", "ansible", "pve", *RESOURCES]
        assert isinstance(StepFactory(None).pve, Pve)


class TestARun:
    def test_a_stopped_vm_is_started_on_its_node_for_the_plan_and_shut_down_after_it(self):
        world = World(node="pve1")
        executor = world.executor(world.plan(world.on("a")))
        assert executor.run() is Outcome.DONE
        assert world.fake.calls == [
            ("pve", RESOURCES),
            ("pve1", ("sudo", "-n", "qm", "start", "919")),
            ("pve", RESOURCES),
            ("pve1", ("sudo", "-n", "qm", "shutdown", "919", *SHUTDOWN)),
        ]
        assert world.records == ["pve1"]
        assert world.vm.status == "stopped" and world.journal == [("run", "a")]
        assert world.clock.now >= BOOT
        assert executor.settled == [f"{DEV_VM} shut down on pve1"]
        start = [e for e in world.recorder.events if e.step.id == START]
        assert start[-1].detail == f"{DEV_VM} started on pve1: it is shut down again after the plan"
        assert Progress(start[0].step, Action.RUN, f"{DEV_VM} is stopped on pve1: starting it") in (
            start
        )
        assert flight_of(world.bao, LEAF) is None and state_of(world.bao, LEAF).status == "ok"

    def test_a_running_vm_is_left_running(self):
        world = World(running=True)
        executor = world.executor(world.plan(world.on("a")))
        assert executor.run() is Outcome.DONE
        assert world.commands() == [] and world.vm.status == "running"
        assert executor.settled == []
        (finished,) = [
            e for e in world.recorder.events if e.step.id == START and isinstance(e, Finished)
        ]
        assert finished.detail == f"{DEV_VM} runs on pve: left running"

    def test_a_dry_run_starts_nothing(self):
        world = World()
        executor = world.executor(world.plan(world.on("a")), dry_run=True)
        assert executor.run() is Outcome.DRY_RUN
        assert world.fake.calls == [] and world.journal == []

    def test_a_node_that_does_not_answer_is_passed_over_and_none_answering_fails_the_start(self):
        world = World(node="pve1")
        world.fake.down = {"pve"}
        assert world.executor(world.plan(world.on("a"))).run() is Outcome.DONE
        assert [node for node, remote in world.fake.calls if remote == RESOURCES] == [
            "pve",
            "pve1",
            "pve",
            "pve1",
        ]
        world = World()
        world.fake.down = set(PVE_NODES)
        executor = world.executor(world.plan(world.on("a")))
        assert executor.run() is Outcome.FAILED
        unreachable = "it exited 255: ssh: connect to host {} port 22: No route to host"
        assert world.recorder.failures()[0].error == "no PVE node lists the cluster's VMs: " + (
            "; ".join(f"on {n} {unreachable.format(n)}" for n in PVE_NODES)
        )
        assert world.journal == []


class TestWhateverTheOutcome:
    def test_a_failed_run_shuts_the_vm_down_and_a_retry_starts_it_again(self):
        world = World()
        executor = world.executor(world.plan(world.on("a", fail=1)))
        assert executor.run() is Outcome.FAILED
        assert world.vm.status == "stopped"
        assert executor.settled == [f"{DEV_VM} shut down on pve"]
        assert executor.run() is Outcome.DONE
        assert world.commands() == [("pve", "start"), ("pve", "shutdown")] * 2
        assert world.journal == [("run", "a")] * 2

    def test_an_abort_has_the_vm_up_for_the_undos_on_it_and_shuts_it_down_after(self):
        world = World()
        executor = world.executor(world.plan(world.on("a"), Tool("b", world.journal, fail=1)))
        assert executor.run() is Outcome.FAILED
        assert world.vm.status == "stopped"
        assert executor.abort() is Outcome.ROLLED_BACK
        assert ("undo", "a") in world.journal and world.vm.status == "stopped"
        assert world.commands() == [("pve", "start"), ("pve", "shutdown")] * 2
        assert executor.settled == [f"{DEV_VM} shut down on pve"]
        assert flight_of(world.bao, LEAF) is None

    def test_unattended_a_failure_keeps_the_vm_up_for_the_rollback_that_follows(self):
        world = World()
        executor = world.executor(world.plan(world.on("a"), Tool("b", world.journal, fail=1)))
        assert executor.run(unattended=True) is Outcome.FAILED
        assert world.vm.status == "running" and executor.settled == []
        assert executor.abort() is Outcome.ROLLED_BACK
        assert world.commands() == [("pve", "start"), ("pve", "shutdown")]
        assert executor.settled == [f"{DEV_VM} shut down on pve"]

    def test_unattended_a_failure_abort_cannot_roll_back_shuts_the_vm_down(self):
        world = World()
        plan = world.plan(world.on("a", undoable=False), Tool("b", world.journal, fail=1))
        executor = world.executor(plan)
        assert executor.run(unattended=True) is Outcome.FAILED
        assert executor.abort_blocker() == "a cannot be taken back"
        assert world.vm.status == "stopped"
        assert executor.settled == [f"{DEV_VM} shut down on pve"]

    def test_a_plan_left_in_flight_at_an_operator_step_shuts_the_vm_down(self):
        world = World()
        executor = world.executor(world.plan(world.on("a"), Confirm("c")), Abandon.EXIT)
        assert executor.run() is Outcome.EXITED
        assert world.vm.status == "stopped"
        assert flight_of(world.bao, LEAF).step == "c"
        assert RECORD not in world.bao.data(STAGING)

    def test_a_resume_shuts_down_the_vm_an_earlier_process_started_and_leaves_another_s(self):
        world = World(running=True)
        plan = world.plan(world.on("a"))
        put_flight(world.bao, "random", LEAF, ["token"], "a", **{RECORD: "pve"})
        assert world.executor(plan).run() is Outcome.DONE
        assert world.commands() == [("pve", "shutdown")]
        world = World(running=True)
        plan = world.plan(world.on("a"))
        put_flight(world.bao, "random", LEAF, ["token"], "a")
        assert world.executor(plan).run() is Outcome.DONE
        assert world.commands() == [] and world.vm.status == "running"

    def test_a_resume_past_the_start_starts_the_stopped_vm_before_the_step_on_it(self):
        world = World()
        plan = world.plan(world.on("a"))
        put_flight(world.bao, "random", LEAF, ["token"], "a")
        executor = world.executor(plan)
        assert executor.run() is Outcome.DONE
        assert world.commands() == [("pve", "start"), ("pve", "shutdown")]
        assert world.recorder.lines()[:2] == [("started", "a", Action.RUN), ("ok", "a", Action.RUN)]

    def test_a_shutdown_that_fails_fails_no_rotation(self):
        world = World()
        world.fake.refused = {"shutdown"}
        executor = world.executor(world.plan(world.on("a")))
        assert executor.run() is Outcome.DONE
        assert executor.settled == [
            f"{DEV_VM} is not shut down: qm shutdown 919 --timeout 300 --forceStop 1 on pve "
            f"exited 255: shutdown failed: refused"
        ]
        assert state_of(world.bao, LEAF).stamps == {"token": "2026-10-05"}
        assert flight_of(world.bao, LEAF) is None


class TestAVmThatDoesNotComeUp:
    def test_unattended_the_plan_is_skipped_nothing_of_it_left_and_the_vm_shut_down(self):
        world = World(boots=False)
        executor = world.executor(world.plan(world.on("a")))
        assert executor.run(unattended=True) is Outcome.SKIPPED
        assert executor.skip == Skipped(executor.plan.steps[0], NO_ANSWER)
        assert world.recorder.events[-1] == executor.skip
        assert world.recorder.failures() == [] and world.journal == []
        assert world.vm.status == "stopped" and world.commands()[-1] == ("pve", "shutdown")
        assert world.clock.now >= 900
        assert flight_of(world.bao, LEAF) is None
        assert state_of(world.bao, LEAF) == LeafState()
        assert lock(world.bao).holder() is None

    def test_attended_it_fails_the_plan_and_the_vm_is_shut_down(self):
        world = World(boots=False)
        executor = world.executor(world.plan(world.on("a")))
        assert executor.run() is Outcome.FAILED
        assert world.recorder.failures()[0].error == NO_ANSWER
        assert world.vm.status == "stopped"
        assert executor.abort() is Outcome.CANCELLED

    def test_unattended_one_after_a_mutating_step_fails_the_plan(self):
        world = World(boots=False)
        plan = world.plan(world.on("a"))
        start, *rest = plan.steps
        after_write = [s.id for s in rest].index("kv.write") + 1
        steps = (*rest[:after_write], start, *rest[after_write:])
        executor = world.executor(dataclasses.replace(plan, steps=steps))
        assert executor.run(unattended=True) is Outcome.FAILED
        assert world.recorder.failures()[0].error == NO_ANSWER
        assert world.vm.status == "running"
        assert executor.abort() is Outcome.ROLLED_BACK
        assert world.vm.status == "stopped"

    def test_a_start_pve_refuses_fails_the_plan_before_anything_changes(self):
        world = World()
        world.fake.refused = {"start"}
        executor = world.executor(world.plan(world.on("a")))
        assert executor.run(unattended=True) is Outcome.FAILED
        (failed,) = world.recorder.failures()
        assert (failed.step.id, failed.error) == (
            START,
            "qm start 919 on pve exited 255: start failed: refused",
        )
        assert world.journal == [] and world.vm.status == "stopped"
        assert executor.abort() is Outcome.CANCELLED
        assert executor.settled == [f"{DEV_VM} is stopped already"]
        assert flight_of(world.bao, LEAF) is None


def test_a_vm_found_running_that_the_plan_started_is_the_plan_s_to_shut_down():
    world = World(running=True)
    put_flight(world.bao, "random", LEAF, ["token"], START, **{RECORD: "pve"})
    assert world.executor(world.plan(world.on("a"))).run() is Outcome.DONE
    (finished,) = [
        e for e in world.recorder.events if e.step.id == START and isinstance(e, Finished)
    ]
    assert finished.detail == f"{DEV_VM} runs on pve: the plan started it"
    assert world.commands() == [("pve", "shutdown")]


def test_no_test_reaches_the_real_ssh(tmp_path):
    known_hosts = tmp_path / "homelab"
    known_hosts.write_text("@cert-authority * ssh-ed25519 AAAA\n")
    with pytest.raises(AssertionError, match="^a test ran the real ssh: ssh -F none "):
        Pve(Ssh(known_hosts)).find(DEV_VM)
