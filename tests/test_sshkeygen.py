"""ssh.keygen (design §4.2): a new ed25519 keypair for one of the plan's keys, the private half in
the OpenSSH form ssh reads from a file and the public half one `ssh-ed25519 <base64> <comment>`
line, with the pair the leaf's key holds before the plan, all staged before anything uses them; a
re-run keeps them, and a leaf whose key is no unencrypted OpenSSH private key fails it. ssh-keygen
reads the keys back."""

import datetime
import subprocess

import pytest

from secret_rotator.model import StepFailed, value_name
from secret_rotator.openbao import Version
from secret_rotator.plan import StepFactory, Target
from secret_rotator.sshsteps import (
    SshKeygen,
    fingerprint,
    old_name,
    old_public_name,
    public_name,
)

LEAF = "iac/ansible-ssh-key"
KEY = "private"
NOW = datetime.datetime(2026, 10, 10, 4, 30, tzinfo=datetime.UTC)
NAMES = (value_name(KEY), public_name(KEY), old_name(KEY), old_public_name(KEY))


class Bao:
    def __init__(self, data):
        self.leaves = data
        self.reads = 0

    def read(self, leaf, version=None):
        self.reads += 1
        held = self.leaves.get(leaf)
        return None if held is None else Version(1, dict(held), NOW)


class Ctx:
    def __init__(self, held, **staged):
        self.bao = Bao({LEAF: {KEY: held}} if held is not None else {})
        self.values = dict(staged)

    def progress(self, detail):
        pass

    def stage(self, name, value):
        self.values[name] = value

    def staged(self, name):
        return self.values.get(name)


def keygen(path, *options):
    """A key ssh-keygen makes, as the operator made the one the leaf holds; its private half."""
    subprocess.run(
        ["ssh-keygen", "-q", "-t", "ed25519", "-C", "operator", "-f", str(path), *options],
        check=True,
    )
    return path.read_text()


def derived(tmp_path, private):
    """The public half ssh-keygen derives from a private half ssh reads from a file, its type and
    base64 without the comment the private half carries, and its fingerprint."""
    path = tmp_path / "derive"
    path.write_text(private)
    path.chmod(0o600)
    public = subprocess.run(
        ["ssh-keygen", "-y", "-f", str(path)], capture_output=True, text=True, check=True
    ).stdout.split()[:2]
    listed = subprocess.run(
        ["ssh-keygen", "-l", "-f", str(path)], capture_output=True, text=True, check=True
    ).stdout.split()[1]
    path.unlink()
    return " ".join(public), listed


@pytest.fixture
def old(tmp_path):
    return keygen(tmp_path / "old", "-N", "")


class TestTheNewPair:
    def test_the_private_half_is_one_ssh_reads_from_a_file_and_the_public_half_its_line(
        self, tmp_path, old
    ):
        ctx = Ctx(old)
        SshKeygen(LEAF, KEY, "ansible").run(ctx)
        private, line = ctx.values[value_name(KEY)], ctx.values[public_name(KEY)]
        assert private.startswith("-----BEGIN OPENSSH PRIVATE KEY-----\n")
        assert private.endswith("-----END OPENSSH PRIVATE KEY-----\n")
        public, _ = derived(tmp_path, private)
        assert public.startswith("ssh-ed25519 ")
        assert line == f"{public} ansible" and len(line.split()) == 3

    def test_each_plan_draws_a_new_pair(self, old):
        drawn = set()
        for _ in range(3):
            ctx = Ctx(old)
            SshKeygen(LEAF, KEY, "ansible").run(ctx)
            drawn.add(ctx.values[value_name(KEY)])
        assert len(drawn) == 3

    def test_its_detail_names_both_fingerprints_and_carries_no_private_half(self, tmp_path, old):
        ctx = Ctx(old)
        detail = SshKeygen(LEAF, KEY, "ansible").run(ctx)
        new = ctx.values[value_name(KEY)]
        assert detail == (
            f"ed25519 {derived(tmp_path, new)[1]}, replacing {derived(tmp_path, old)[1]}"
        )
        assert fingerprint(ctx.values[public_name(KEY)]) == derived(tmp_path, new)[1]
        assert new not in detail and old not in detail


class TestTheOldPair:
    def test_it_stages_the_key_the_leaf_holds_and_its_public_half_with_the_same_comment(
        self, tmp_path, old
    ):
        ctx = Ctx(old)
        SshKeygen(LEAF, KEY, "ansible").run(ctx)
        assert ctx.values[old_name(KEY)] == old
        public, _ = derived(tmp_path, old)
        assert ctx.values[old_public_name(KEY)] == f"{public} ansible"
        assert ctx.values[old_public_name(KEY)] != ctx.values[public_name(KEY)]

    def test_a_leaf_without_the_key_fails_it_and_stages_nothing(self):
        ctx = Ctx(None)
        with pytest.raises(StepFailed, match=f"{LEAF} holds no {KEY}"):
            SshKeygen(LEAF, KEY, "ansible").run(ctx)
        assert ctx.values == {}

    @pytest.mark.parametrize("held", ["SECRET-not-a-key", "-----BEGIN OPENSSH PRIVATE KEY-----\n"])
    def test_a_leaf_whose_key_is_no_openssh_private_key_fails_it_and_says_nothing_of_it(self, held):
        ctx = Ctx(held)
        with pytest.raises(StepFailed) as failed:
            SshKeygen(LEAF, KEY, "ansible").run(ctx)
        assert failed.value.error == f"{LEAF}#{KEY} holds no unencrypted OpenSSH private key"
        assert held not in failed.value.error + failed.value.technical
        assert ctx.values == {}

    def test_an_encrypted_key_in_the_leaf_fails_it(self, tmp_path):
        ctx = Ctx(keygen(tmp_path / "encrypted", "-N", "a passphrase"))
        with pytest.raises(StepFailed, match="holds no unencrypted OpenSSH private key"):
            SshKeygen(LEAF, KEY, "ansible").run(ctx)


class TestARerun:
    def test_it_keeps_every_half_already_staged(self, old):
        staged = {name: f"staged {name}" for name in NAMES}
        staged[public_name(KEY)] = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5 ansible"
        staged[old_public_name(KEY)] = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAA ansible"
        ctx = Ctx("SECRET-the-leaf-moved-on", **staged)
        SshKeygen(LEAF, KEY, "ansible").run(ctx)
        assert ctx.values == staged
        assert ctx.bao.reads == 0

    def test_after_only_the_old_key_was_staged_it_keeps_that_one(self, tmp_path, old):
        other = keygen(tmp_path / "other", "-N", "")
        ctx = Ctx(other, **{old_name(KEY): old})
        SshKeygen(LEAF, KEY, "ansible").run(ctx)
        assert ctx.values[old_name(KEY)] == old
        assert ctx.values[old_public_name(KEY)] == f"{derived(tmp_path, old)[0]} ansible"
        assert set(ctx.values) == set(NAMES)

    def test_after_the_new_key_was_staged_its_public_half_is_its_own(self, tmp_path, old):
        new = keygen(tmp_path / "new", "-N", "")
        ctx = Ctx(old, **{value_name(KEY): new})
        SshKeygen(LEAF, KEY, "ansible").run(ctx)
        assert ctx.values[value_name(KEY)] == new
        assert ctx.values[public_name(KEY)] == f"{derived(tmp_path, new)[0]} ansible"


@pytest.mark.parametrize("comment", ["", "two words", "line\nbreak"])
def test_a_comment_that_is_not_one_word_is_refused(comment):
    with pytest.raises(ValueError):
        SshKeygen(LEAF, KEY, comment)


def test_it_is_silent_does_not_mutate_and_has_no_undo():
    step = SshKeygen(LEAF, KEY, "ansible")
    assert (step.type, step.id) == ("ssh.keygen", f"ssh.keygen:{KEY}")
    assert step.silent and not step.mutates and step.undo is None and not step.logs_in


def test_the_factory_builds_it_for_the_plan_s_leaf():
    target = Target(LEAF, "ssh-key", (KEY,), {}, (), ())
    [step] = StepFactory(target).keygen(KEY, "ansible")
    assert isinstance(step, SshKeygen)
    assert (step.leaf, step.key, step.comment) == (LEAF, KEY, "ansible")
