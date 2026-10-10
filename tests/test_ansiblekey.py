"""The rotator's Ansible key (Ansible slice 060): the file its logins as ansible read follows the
key the leaf iac/ansible-ssh-key holds, replaced whole, mode 0600, and only when it holds another;
in a plan with a step that logs in, the executor has it follow before each step, undo and re-run,
so a kv.write of a new key switches the rotator to it from there on and the rollback switches it
back, a rotator that outlived a rotation logs in with the key the leaf holds, and a VM is started
and shut down with it; a plan with no such step never reads the leaf. ssh.key_copy switches
srviac's copy to the new key and its undo back to the old one, from what the plan staged and never
from the leaf, and fails not landed where it cannot write the copy."""

import os
import stat
from pathlib import Path

import pytest
from plans import LEAF as PLAN_LEAF
from plans import NOW, Journal, Recorder, Tool, client, fake, lock, plan_of, put_flight, run_state
from test_vmsteps import RECORD, World

from secret_rotator.ansiblekey import COPY, KEY, LEAF, AnsibleKey
from secret_rotator.ansiblesteps import AnsibleRun
from secret_rotator.executor import Executor, Outcome
from secret_rotator.kvsteps import KvWrite, RandomGenerate
from secret_rotator.model import Step, StepFailed, value_name
from secret_rotator.openbao import OpenBaoError
from secret_rotator.plan import StepFactory, Target
from secret_rotator.sshsteps import (
    SshKeyCopy,
    SshKeygen,
    SshSetPassword,
    fingerprint,
    generate,
    old_name,
    public_line,
)
from secret_rotator.vmsteps import VmStart

K0 = "SECRET-the-key-the-container-started-with\n"
K1 = "SECRET-the-key-a-rotation-wrote\n"


def files(tmp_path):
    (tmp_path / "ssh").mkdir()
    (tmp_path / "srviac").mkdir()
    return AnsibleKey(tmp_path / "ssh" / "id_ed25519_ansible", tmp_path / "srviac" / "copy")


def holding(private):
    """The fake OpenBao with the compliant store, and the Ansible key's leaf holding private."""
    bao = fake()
    bao.leaves[LEAF] = {"data": {KEY: private}, "meta": {}}
    return bao


def mode(path):
    return stat.S_IMODE(path.stat().st_mode)


class Login(Step):
    """A mutating step that logs in as ansible: each run and undo records the key the login file
    holds then."""

    type = "test.login"
    mutates = True
    logs_in = True

    def __init__(self, id, key, seen):
        super().__init__(id, f"log in at {id}")
        self.key = key
        self.seen = seen

    def run(self, ctx):
        self.seen.append(("run", self.id, self.key.login.read_text()))

    def undo(self, ctx):
        self.seen.append(("undo", self.id, self.key.login.read_text()))


class Rekey(Step):
    """The Ansible key's leaf rewritten to K1, as the ssh-key plan's kv.write does, and back."""

    type = "test.rekey"
    mutates = True

    def __init__(self):
        super().__init__("rekey", "write the new key")

    def run(self, ctx):
        ctx.bao.write(LEAF, {KEY: K1})

    def undo(self, ctx):
        ctx.bao.write(LEAF, {KEY: K0})


def executor(bao, plan, key=None):
    return Executor(
        client(bao),
        plan,
        Recorder(),
        lock(bao),
        state=run_state(bao),
        dry_run=False,
        clock=lambda: NOW,
        ansible_key=key,
    )


class TestFollow:
    def test_the_login_file_takes_the_key_the_leaf_holds_mode_0600(self, tmp_path):
        key = files(tmp_path)
        key.follow(client(holding(K1)))
        assert key.login.read_text() == K1 and mode(key.login) == 0o600
        assert os.listdir(key.login.parent) == [key.login.name]

    def test_a_file_holding_another_key_is_replaced_whole(self, tmp_path):
        key = files(tmp_path)
        key.login.write_text(K0 + "and more than the key\n")
        key.login.chmod(0o644)
        before = key.login.stat().st_ino
        key.follow(client(holding(K1)))
        assert key.login.read_text() == K1 and mode(key.login) == 0o600
        assert key.login.stat().st_ino != before  # a rename over it: no reader sees half of it
        assert os.listdir(key.login.parent) == [key.login.name]

    def test_a_file_holding_the_key_is_left_as_it_is(self, tmp_path):
        key = files(tmp_path)
        key.login.write_text(K1)
        before = key.login.stat()
        key.follow(client(holding(K1)))
        after = key.login.stat()
        assert (after.st_ino, after.st_mtime_ns) == (before.st_ino, before.st_mtime_ns)

    def test_a_leaf_without_the_key_fails_it_and_leaves_the_file(self, tmp_path):
        key = files(tmp_path)
        key.login.write_text(K0)
        with pytest.raises(OpenBaoError, match=f"{LEAF}#{KEY} cannot be read"):
            key.follow(client(fake()))
        assert key.login.read_text() == K0

    def test_the_real_files_are_the_one_every_login_names_and_srviac_s_copy(self):
        key = AnsibleKey()
        assert key.login == Path(os.path.expanduser("~/.ssh/id_ed25519_ansible"))
        assert key.copy == COPY
        assert str(COPY) == "/var/lib/iac/ansible-ssh-key/id_ed25519_ansible"


class TestTheExecutor:
    def test_from_the_write_on_the_rotator_logs_in_with_the_new_key_and_back_on_rollback(
        self, tmp_path
    ):
        key, seen, journal = files(tmp_path), [], Journal()
        key.login.write_text(K0)
        bao = holding(K0)
        plan = plan_of(
            Login("before", key, seen),
            Rekey(),
            Login("after", key, seen),
            Tool("fails", journal, fail=1),
        )
        run = executor(bao, plan, key)
        assert run.run() is Outcome.FAILED
        assert run.abort() is Outcome.ROLLED_BACK
        assert seen == [
            ("run", "before", K0),
            ("run", "after", K1),
            ("undo", "after", K1),
            ("undo", "before", K0),
        ]
        assert key.login.read_text() == K0 and bao.data(LEAF) == {KEY: K0}

    def test_a_rotator_that_outlived_a_rotation_logs_in_with_the_key_the_leaf_holds(self, tmp_path):
        key, seen = files(tmp_path), []
        key.login.write_text(K0)
        bao = holding(K1)  # another process rotated it
        assert executor(bao, plan_of(Login("x", key, seen)), key).run() is Outcome.DONE
        assert seen == [("run", "x", K1)]

    def test_a_retry_logs_in_with_the_key_the_leaf_holds_by_then(self, tmp_path):
        key, seen, journal = files(tmp_path), [], Journal()
        key.login.write_text(K0)
        bao = holding(K0)
        plan = plan_of(Tool("fails", journal, fail=1), Login("x", key, seen))
        assert executor(bao, plan, key).run() is Outcome.FAILED
        bao.leaves[LEAF]["data"] = {KEY: K1}
        assert executor(bao, plan, key).run() is Outcome.DONE
        assert seen == [("run", "x", K1)]

    def test_a_key_that_cannot_be_read_fails_the_first_step_before_it_runs(self, tmp_path):
        key, seen = files(tmp_path), []
        run = executor(fake(), plan_of(Login("x", key, seen)), key)
        assert run.run() is Outcome.FAILED
        (failure,) = run.renderer.failures()
        assert failure.step.id == "random.generate:token"
        assert f"{LEAF}#{KEY} cannot be read" in failure.error
        assert seen == [] and not key.login.exists()
        assert run.abort() is Outcome.CANCELLED

    def test_a_plan_with_no_step_that_logs_in_never_reads_the_key(self, tmp_path):
        key, bao = files(tmp_path), fake()
        plan = plan_of()
        assert not plan.logs_in
        assert executor(bao, plan, key).run() is Outcome.DONE
        assert not key.login.exists()
        assert not any(r[1] == f"kv/data/{LEAF}" for r in bao.requests)

    def test_without_the_key_file_the_executor_leaves_it_as_it_is(self, tmp_path):
        key, seen = files(tmp_path), []
        key.login.write_text(K0)
        assert executor(holding(K1), plan_of(Login("x", key, seen))).run() is Outcome.DONE
        assert seen == [("run", "x", K0)]

    def test_the_plan_s_vm_start_reaches_pve_with_the_key_the_leaf_holds(self, tmp_path):
        key = files(tmp_path)
        key.login.write_text(K0)
        world = World()
        world.bao.leaves[LEAF] = {"data": {KEY: K1}, "meta": {}}
        held, call = [], world.fake.run

        def logged_in(host, remote, stdin):
            held.append(key.login.read_text())
            return call(host, remote, stdin)

        world.fake.run = logged_in
        plan = world.plan(world.on("a"))
        assert executor(world.bao, plan, key).run() is Outcome.DONE
        assert world.commands() == [("pve", "start"), ("pve", "shutdown")]
        assert held and set(held) == {K1}

    def test_the_vm_the_plan_started_is_shut_down_with_the_key_the_leaf_holds(self, tmp_path):
        key = files(tmp_path)
        key.login.write_text(K0)
        world = World(running=True)
        world.bao.leaves[LEAF] = {"data": {KEY: K1}, "meta": {}}
        held = []
        world.vm.stopped = lambda: held.append(key.login.read_text())
        plan = world.plan(world.on("a"))
        start = plan.steps[0]
        put_flight(world.bao, "random", PLAN_LEAF, ["token"], start.id, **{RECORD: "pve"})
        assert executor(world.bao, plan, key).abort() is Outcome.CANCELLED
        assert world.commands() == [("pve", "shutdown")] and held == [K1]


def test_the_steps_that_log_in_as_ansible_say_so():
    assert AnsibleRun.logs_in and SshSetPassword.logs_in and VmStart.logs_in
    assert not any(s.logs_in for s in (SshKeygen, SshKeyCopy, RandomGenerate, KvWrite, Step))


class Ctx:
    """A context with what the plan staged and no store: ssh.key_copy reads nothing else."""

    def __init__(self, **staged):
        self.values = dict(staged)

    def progress(self, detail):
        pass

    def staged(self, name):
        return self.values.get(name)


@pytest.fixture
def keys():
    return generate(), generate()


class TestTheKeyCopy:
    def test_it_switches_the_copy_to_the_new_key_mode_0600(self, tmp_path, keys):
        new, old = keys
        key = files(tmp_path)
        key.copy.write_text(old)
        before = key.copy.stat().st_ino
        step = SshKeyCopy(key, "private")
        detail = step.run(Ctx(**{value_name("private"): new, old_name("private"): old}))
        assert key.copy.read_text() == new and mode(key.copy) == 0o600
        assert key.copy.stat().st_ino != before
        assert os.listdir(key.copy.parent) == [key.copy.name]
        assert detail == f"{key.copy} holds the new key, {fingerprint(public_line(new, ''))}"
        assert new not in detail

    def test_its_undo_puts_back_the_key_the_plan_s_keygen_staged_as_the_old_one(
        self, tmp_path, keys
    ):
        new, old = keys
        key = files(tmp_path)
        step = SshKeyCopy(key, "private")
        ctx = Ctx(**{value_name("private"): new, old_name("private"): old})
        step.run(ctx)
        detail = step.undo(ctx)
        assert key.copy.read_text() == old and mode(key.copy) == 0o600
        assert detail == f"{key.copy} holds the old key again, {fingerprint(public_line(old, ''))}"

    def test_a_copy_it_cannot_write_fails_it_not_landed_and_leaves_nothing(self, tmp_path, keys):
        new, _ = keys
        key = AnsibleKey(tmp_path / "login", tmp_path / "no-mount" / "id_ed25519_ansible")
        with pytest.raises(StepFailed) as failed:
            SshKeyCopy(key, "private").run(Ctx(**{value_name("private"): new}))
        assert failed.value.error == f"{key.copy} cannot be written: No such file or directory"
        assert not failed.value.landed
        assert os.listdir(tmp_path) == []

    def test_without_a_new_key_staged_it_fails_not_landed_and_writes_nothing(self, tmp_path):
        key = files(tmp_path)
        with pytest.raises(StepFailed, match="no new key is staged") as failed:
            SshKeyCopy(key, "private").run(Ctx())
        assert not failed.value.landed and not key.copy.exists()

    def test_without_the_old_key_staged_its_undo_fails_and_writes_nothing(self, tmp_path, keys):
        key = files(tmp_path)
        key.copy.write_text(keys[0])
        with pytest.raises(StepFailed, match="no old key is staged"):
            SshKeyCopy(key, "private").undo(Ctx(**{value_name("private"): keys[0]}))
        assert key.copy.read_text() == keys[0]

    def test_it_mutates_and_has_an_undo(self, tmp_path):
        step = SshKeyCopy(files(tmp_path), "private")
        assert (step.type, step.id) == ("ssh.key_copy", "ssh.key_copy:private")
        assert step.mutates and step.undo is not None and not step.silent

    def test_the_factory_builds_it_on_its_key_files(self, tmp_path):
        key = files(tmp_path)
        target = Target(LEAF, "ssh-key", (KEY,), {}, (), ())
        [step] = StepFactory(target, ansible_key=key).key_copy(KEY)
        assert isinstance(step, SshKeyCopy) and (step.path, step.key) == (key.copy, KEY)
        [real] = StepFactory(target).key_copy(KEY)
        assert real.path == COPY
