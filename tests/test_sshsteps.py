"""ssh.set_password (design §4.2): one user's password on one host by `sudo -n chpasswd` over SSH,
logged in as ansible with the Ansible key, the host key checked against the homelab SSH host CA and
never with checking off; the password on chpasswd's stdin, never on its command line, in a detail
or in an error; its undo the password the leaf holds; a failure to reach the host before chpasswd
not landed. ssh is played by fake_ssh.py, and the real ssh -G reads the options back."""

import datetime
import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

from secret_rotator.model import StepFailed, value_name
from secret_rotator.openbao import Version
from secret_rotator.plan import StepFactory, Target
from secret_rotator.sshsteps import CHPASSWD, KNOWN_HOSTS, REACH, Ssh, SshSetPassword

FAKE = str(Path(__file__).with_name("fake_ssh.py"))
LEAF = "iac/proxmox"
KEY = "password"
NEW = "SECRET-new-root-password"
OLD = "SECRET-old-root-password"
NOW = datetime.datetime(2026, 10, 9, 4, 30, tzinfo=datetime.UTC)
HOST_KEYS = "ssh-ed25519-cert-v01@openssh.com,ssh-ed25519"


class Bao:
    def __init__(self, data):
        self.leaves = data

    def read(self, leaf, version=None):
        held = self.leaves.get(leaf)
        return None if held is None else Version(1, dict(held), NOW)


class Ctx:
    def __init__(self, staged=None, held=None):
        self.values = {value_name(KEY): NEW} if staged is None else dict(staged)
        self.bao = Bao({LEAF: {KEY: OLD}} if held is None else held)

    def progress(self, detail):
        pass

    def staged(self, name):
        return self.values.get(name)


def ssh(tmp_path, **kwargs):
    known_hosts = tmp_path / "homelab"
    known_hosts.write_text("@cert-authority * ssh-ed25519 AAAAC3Nza homelab-ssh-host-ca\n")
    return Ssh(known_hosts, (sys.executable, FAKE, str(tmp_path)), **kwargs)


def step(tmp_path, host="pve1", **kwargs):
    return SshSetPassword(ssh(tmp_path, **kwargs), host, "root", LEAF, KEY)


def calls(tmp_path):
    path = tmp_path / "calls.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def sets(tmp_path):
    """The calls that ran chpasswd."""
    return [c for c in calls(tmp_path) if c["remote"] == list(CHPASSWD)]


def passwords(tmp_path):
    path = tmp_path / "passwords.json"
    return json.loads(path.read_text()) if path.exists() else {}


def scenario(tmp_path, **hosts):
    (tmp_path / "scenarios.json").write_text(json.dumps(hosts))


class TestTheRun:
    def test_it_reaches_the_host_then_sets_the_password_by_chpasswd_on_its_stdin(self, tmp_path):
        assert step(tmp_path).run(Ctx()) == "root's password set on pve1"
        reach, call = calls(tmp_path)
        assert (reach["host"], reach["remote"], reach["stdin"]) == ("pve1", list(REACH), "")
        assert (call["host"], call["remote"]) == ("pve1", ["sudo", "-n", "chpasswd"])
        assert call["stdin"] == f"root:{NEW}\n"
        assert NEW not in " ".join(call["argv"])
        assert passwords(tmp_path) == {"pve1": {"root": NEW}}

    def test_it_logs_in_as_ansible_with_the_ansible_key_and_checks_the_host_key_against_the_ca(
        self, tmp_path
    ):
        step(tmp_path).run(Ctx())
        reach, call = calls(tmp_path)
        assert reach["login"] == call["login"] == "ansible"
        assert reach["options"] == call["options"]
        assert call["options"] == {
            "-F": "none",
            "-T": True,
            "BatchMode": "yes",
            "ConnectTimeout": "15",
            "PreferredAuthentications": "publickey",
            "IdentityFile": "~/.ssh/id_ed25519_ansible",
            "IdentitiesOnly": "yes",
            "StrictHostKeyChecking": "yes",
            "UserKnownHostsFile": str(tmp_path / "homelab"),
            "GlobalKnownHostsFile": "/dev/null",
            "HostKeyAlgorithms": HOST_KEYS,
        }

    def test_the_ca_is_the_one_the_ansible_checkout_s_ansible_cfg_names(self):
        assert Path("/work/Ansible/ansible/files/known_hosts.d/homelab") == KNOWN_HOSTS
        assert Ssh().known_hosts == KNOWN_HOSTS

    def test_ssh_reads_its_options_back_as_given(self, tmp_path):
        argv = Ssh(tmp_path / "homelab", ("ssh", "-G")).argv("pve1", CHPASSWD)
        printed = subprocess.run(argv, capture_output=True, text=True, check=True).stdout
        config = dict(line.split(" ", 1) for line in printed.splitlines() if " " in line)
        want = {
            "user": "ansible",
            "hostname": "pve1",
            "batchmode": "yes",
            "connecttimeout": "15",
            "preferredauthentications": "publickey",
            "identityfile": "~/.ssh/id_ed25519_ansible",
            "identitiesonly": "yes",
            "stricthostkeychecking": "true",
            "userknownhostsfile": str(tmp_path / "homelab"),
            "globalknownhostsfile": "/dev/null",
            "hostkeyalgorithms": HOST_KEYS,
            "requesttty": "false",
        }
        assert {name: config[name] for name in want} == want

    def test_a_re_run_sets_it_again(self, tmp_path):
        set_password = step(tmp_path)
        set_password.run(Ctx())
        set_password.run(Ctx())
        assert [c["stdin"] for c in sets(tmp_path)] == [f"root:{NEW}\n"] * 2
        assert passwords(tmp_path) == {"pve1": {"root": NEW}}


class TestTheUndo:
    def test_it_sets_back_the_password_the_leaf_holds(self, tmp_path):
        set_password = step(tmp_path)
        set_password.run(Ctx())
        assert set_password.undo(Ctx()) == (
            "root's password on pve1 set back to the one iac/proxmox holds"
        )
        assert calls(tmp_path)[-1]["stdin"] == f"root:{OLD}\n"
        assert passwords(tmp_path) == {"pve1": {"root": OLD}}

    def test_without_a_new_value_staged_it_sets_the_leaf_s_password_all_the_same(self, tmp_path):
        step(tmp_path).undo(Ctx(staged={}))
        assert passwords(tmp_path) == {"pve1": {"root": OLD}}

    @pytest.mark.parametrize("held", [{LEAF: {}}, {}])
    def test_a_leaf_that_holds_no_password_has_none_to_set_back(self, tmp_path, held):
        with pytest.raises(StepFailed, match="iac/proxmox holds no password to set back"):
            step(tmp_path).undo(Ctx(held=held))
        assert calls(tmp_path) == []


class TestTheFailures:
    @pytest.mark.parametrize(
        ("played", "error"),
        [
            (
                "unreachable",
                "ssh to pve1 as ansible failed: ssh: connect to host pve1 port 22: No route to "
                "host",
            ),
            ("hostkey", "ssh to pve1 as ansible failed: Host key verification failed."),
            ("sudo", "sudo -n true on pve1 exited 1: sudo: a password is required"),
        ],
    )
    def test_a_failure_to_reach_the_host_names_what_it_said_and_did_not_land(
        self, tmp_path, played, error
    ):
        scenario(tmp_path, pve1=played)
        with pytest.raises(StepFailed) as e:
            step(tmp_path).run(Ctx())
        assert e.value.error == error
        assert e.value.technical.splitlines()[-1] == error.split(": ", 1)[1]
        assert not e.value.landed
        assert [c["remote"] for c in calls(tmp_path)] == [list(REACH)]

    def test_a_connection_lost_after_chpasswd_ran_counts_as_landed(self, tmp_path):
        scenario(tmp_path, pve1="drop")
        with pytest.raises(StepFailed) as e:
            step(tmp_path).run(Ctx())
        assert e.value.error == (
            "ssh to pve1 as ansible failed: Connection to pve1 closed by remote host."
        )
        assert e.value.landed
        assert passwords(tmp_path) == {"pve1": {"root": NEW}}

    def test_what_chpasswd_says_is_kept_with_the_password_redacted(self, tmp_path):
        scenario(tmp_path, pve1="echo")
        with pytest.raises(StepFailed) as e:
            step(tmp_path).run(Ctx())
        assert e.value.error == (
            "sudo -n chpasswd on pve1 exited 1: chpasswd: error detected, changes ignored"
        )
        assert "cannot set root:<redacted>" in e.value.technical
        assert e.value.landed
        with pytest.raises(StepFailed) as undone:
            step(tmp_path).undo(Ctx())
        texts = [e.value.error, e.value.technical, undone.value.error, undone.value.technical]
        assert not any(NEW in text or OLD in text for text in texts)

    def test_a_run_past_its_bound_is_killed(self, tmp_path):
        scenario(tmp_path, pve1="hang")
        started = time.monotonic()
        with pytest.raises(StepFailed, match="ssh to pve1 did not finish within 1 s") as e:
            step(tmp_path, bound=1).run(Ctx())
        assert time.monotonic() - started < 30
        assert not e.value.landed and sets(tmp_path) == []

    def test_no_staged_value_fails_before_ssh_runs(self, tmp_path):
        with pytest.raises(StepFailed, match="no new password is staged") as e:
            step(tmp_path).run(Ctx(staged={}))
        assert not e.value.landed and calls(tmp_path) == []

    @pytest.mark.parametrize("value", ["", "SECRET\nroot:other"])
    def test_an_empty_or_multi_line_password_is_refused_before_ssh_runs(self, tmp_path, value):
        with pytest.raises(
            StepFailed, match="the new password is empty or more than one line"
        ) as refused:
            step(tmp_path).run(Ctx(staged={value_name(KEY): value}))
        assert not refused.value.landed
        with pytest.raises(
            StepFailed, match="the password iac/proxmox holds is empty or more than one line"
        ):
            step(tmp_path).undo(Ctx(held={LEAF: {KEY: value}}))
        assert calls(tmp_path) == []

    def test_no_ca_file_or_no_ssh_fails_the_step_not_landed(self, tmp_path):
        absent = Ssh(tmp_path / "absent", (sys.executable, FAKE, str(tmp_path)))
        with pytest.raises(StepFailed, match="there is no homelab SSH host CA file at") as e:
            SshSetPassword(absent, "pve1", "root", LEAF, KEY).run(Ctx())
        assert not e.value.landed
        no_ssh = Ssh(ssh(tmp_path).known_hosts, ("ssh-that-is-not-there",))
        with pytest.raises(StepFailed, match="ssh-that-is-not-there is not on the PATH") as e:
            SshSetPassword(no_ssh, "pve1", "root", LEAF, KEY).run(Ctx())
        assert not e.value.landed
        assert calls(tmp_path) == []


def test_the_factory_builds_one_mutating_set_per_host_with_an_undo(tmp_path):
    factory = StepFactory(Target(LEAF, "pve-root-password", (KEY,), {}, (), ()), ssh=ssh(tmp_path))
    steps = factory.set_password(KEY, "root", ("pve", "pve1", "pve2"))
    assert [(s.id, s.title) for s in steps] == [
        ("ssh.set_password:root@pve", "set root's password on pve"),
        ("ssh.set_password:root@pve1", "set root's password on pve1"),
        ("ssh.set_password:root@pve2", "set root's password on pve2"),
    ]
    for s in steps:
        assert (s.ssh, s.leaf, s.key, s.user) == (factory.ssh, LEAF, KEY, "root")
        assert s.mutates and not s.activator and not s.silent and s.undo is not None
    assert StepFactory(None).ssh.known_hosts == KNOWN_HOSTS
