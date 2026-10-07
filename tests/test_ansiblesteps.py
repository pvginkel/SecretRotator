"""ansible.run (design §4.2): a playbook run from the Ansible checkout with the container's
environment, its extra vars in a private file and never on its command line, verified by a PLAY
RECAP with at least one host and none failed or unreachable; its undo a counter-run where there is
one. ansible-playbook is played by fake_ansible_playbook.py."""

import json
import os
import re
import sys
import time
from pathlib import Path

import pytest

from secret_rotator.ansiblesteps import Ansible, AnsibleRun, Playbook, recap
from secret_rotator.model import StepFailed
from secret_rotator.plan import StepFactory

FAKE = str(Path(__file__).with_name("fake_ansible_playbook.py"))
SECRET = "SECRET-new-backup-secret-id"
DELIVER = Playbook(
    "playbooks/site-openbao.yml",
    tags=("openbao_backup_delivery",),
    staged={"openbao_backup_secret_id": "value:secret_id"},
    extra={"openbao_backup_role": "backup"},
)


class Ctx:
    def __init__(self, staged=None):
        self.values = {"value:secret_id": SECRET} if staged is None else dict(staged)
        self.progressed = []

    def progress(self, detail):
        self.progressed.append(detail)

    def staged(self, name):
        return self.values.get(name)


def ansible(tmp_path, scenario="ok", **kwargs):
    return Ansible(tmp_path, (sys.executable, FAKE, scenario), **kwargs)


def step(tmp_path, scenario="ok", counter=None, **kwargs):
    return AnsibleRun(
        ansible(tmp_path, scenario),
        "deliver",
        "deliver the backup secret_id",
        DELIVER,
        counter,
        **kwargs,
    )


def report(tmp_path):
    return json.loads((tmp_path / "report.json").read_text())


def gone(pid, within=5.0):
    """Whether the process has ended within that many seconds; a zombie nobody has reaped yet
    has."""
    deadline = time.monotonic() + within
    while True:
        try:
            stat = Path(f"/proc/{pid}/stat").read_text()
        except FileNotFoundError:
            return True
        if stat.rsplit(")", 1)[1].split()[0] == "Z":
            return True
        if time.monotonic() > deadline:
            return False
        time.sleep(0.05)


def child(tmp_path):
    """The pid of the hanging stand-in's child, which holds the output open as ssh does."""
    return int((tmp_path / "child.pid").read_text())


class TestTheRun:
    def test_it_passes_every_extra_var_in_a_private_file_never_on_the_command_line(self, tmp_path):
        assert step(tmp_path).run(Ctx()) == "3 hosts ok, 2 changed"
        seen = report(tmp_path)
        assert seen["argv"][:3] == [
            "playbooks/site-openbao.yml",
            "--tags",
            "openbao_backup_delivery",
        ]
        assert seen["argv"][3] == "--extra-vars" and seen["argv"][4].startswith("@")
        assert SECRET not in " ".join(seen["argv"])
        assert seen["extra_vars"] == {
            "openbao_backup_role": "backup",
            "openbao_backup_secret_id": SECRET,
        }
        assert (seen["mode"], seen["dir_mode"] & 0o777) == (0o600, 0o700)
        assert not Path(seen["extra_path"]).parent.exists()

    def test_it_runs_in_the_checkout_with_the_environment_and_no_colour(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setenv("ANSIBLE_FORCE_COLOR", "1")
        step(tmp_path).run(Ctx())
        seen = report(tmp_path)
        assert seen["cwd"] == str(tmp_path)
        assert seen["env"] == {"ANSIBLE_NOCOLOR": "1", "ANSIBLE_FORCE_COLOR": None}

    def test_each_task_is_a_progress_detail(self, tmp_path):
        ctx = Ctx()
        step(tmp_path).run(ctx)
        assert ctx.progressed == ["openbao : Prove the secret_id", "openbao : Write the secret_id"]

    def test_no_tags_no_tags_argument(self, tmp_path):
        book = Playbook("playbooks/x.yml")
        AnsibleRun(ansible(tmp_path), "x", "run x", book).run(Ctx())
        assert report(tmp_path)["argv"][:2] == ["playbooks/x.yml", "--extra-vars"]

    @pytest.mark.parametrize(
        ("scenario", "error", "landed"),
        [
            (
                "failed",
                "playbooks/site-openbao.yml --tags openbao_backup_delivery: srvvault2 failed",
                True,
            ),
            ("unchanged", "--tags openbao_backup_delivery: srvvault2 failed", False),
            ("unreachable", "--tags openbao_backup_delivery: srvvault3 unreachable", True),
            ("norecap", "openbao_backup_delivery ended without a PLAY RECAP (exit 1)", True),
            ("nohosts", "openbao_backup_delivery ran on no host", False),
            ("exit2", "openbao_backup_delivery exited 2", True),
        ],
    )
    def test_a_run_whose_recap_is_not_clean_fails_the_step_landed_if_a_host_changed(
        self, tmp_path, scenario, error, landed
    ):
        with pytest.raises(StepFailed, match=re.escape(error)) as e:
            step(tmp_path, scenario).run(Ctx())
        assert "PLAY [Deliver the backup secret_id]" in e.value.technical
        assert e.value.landed is landed

    def test_with_a_counter_run_a_run_that_changed_no_host_counts_as_landed(self, tmp_path):
        counter = Playbook("playbooks/site-openbao.yml", tags=("remove",))
        with pytest.raises(StepFailed, match="srvvault2 failed") as e:
            step(tmp_path, "unchanged", counter=counter).run(Ctx())
        assert e.value.landed
        with pytest.raises(StepFailed, match="no value is staged") as e:
            step(tmp_path, counter=counter).run(Ctx(staged={}))
        assert e.value.landed

    def test_the_output_kept_of_a_failed_run_has_every_staged_value_redacted(self, tmp_path):
        with pytest.raises(StepFailed) as e:
            step(tmp_path, "echo").run(Ctx())
        assert SECRET not in e.value.technical
        assert "refused <redacted> for <redacted>" in e.value.technical

    def test_a_task_line_has_every_staged_value_redacted(self, tmp_path):
        ctx = Ctx()
        step(tmp_path, "named").run(ctx)
        assert ctx.progressed[-1] == "openbao : Write <redacted>"
        assert not any(SECRET in detail for detail in ctx.progressed)

    def test_a_run_past_its_bound_is_killed_with_what_it_started(self, tmp_path):
        run = AnsibleRun(ansible(tmp_path, "hang", bound=1), "deliver", "deliver", DELIVER)
        started = time.monotonic()
        with pytest.raises(StepFailed, match="did not finish within 0 min") as e:
            run.run(Ctx())
        assert e.value.landed
        # The child holds the output for 60 s: only the session's kill ends the run at its bound.
        assert time.monotonic() - started < 30
        assert gone(child(tmp_path))

    def test_an_interrupted_rotator_takes_the_playbook_down_with_it(self, tmp_path):
        def interrupt(detail):
            raise KeyboardInterrupt

        values = {"openbao_backup_secret_id": SECRET}
        with pytest.raises(KeyboardInterrupt):
            ansible(tmp_path, "hang").run(DELIVER, values, interrupt)
        with pytest.raises(ProcessLookupError):
            os.kill(report(tmp_path)["pid"], 0)
        assert gone(child(tmp_path))

    def test_no_staged_value_fails_before_running_and_did_not_land(self, tmp_path):
        with pytest.raises(
            StepFailed, match="no value is staged for openbao_backup_secret_id"
        ) as e:
            step(tmp_path).run(Ctx(staged={}))
        assert not (tmp_path / "report.json").exists() and not e.value.landed

    def test_no_checkout_or_no_ansible_playbook_fails_the_step_and_did_not_land(self, tmp_path):
        missing = Ansible(tmp_path / "Ansible" / "ansible", (sys.executable, FAKE, "ok"))
        with pytest.raises(StepFailed, match="there is no Ansible checkout at") as e:
            AnsibleRun(missing, "x", "x", DELIVER).run(Ctx())
        assert not e.value.landed
        absent = Ansible(tmp_path, ("ansible-playbook-that-is-not-there",))
        with pytest.raises(
            StepFailed, match="ansible-playbook-that-is-not-there is not on the PATH"
        ) as e:
            AnsibleRun(absent, "x", "x", DELIVER).run(Ctx())
        assert not e.value.landed


class TestTheUndo:
    def test_without_a_counter_run_there_is_no_undo(self, tmp_path):
        plain = step(tmp_path)
        assert plain.mutates and not plain.activator and plain.undo is None
        assert plain.no_undo == "deliver the backup secret_id: the playbook has no counter-run"
        told = step(tmp_path, no_undo="the old secret_id is not known")
        assert told.no_undo == "the old secret_id is not known"

    def test_the_counter_run_is_the_undo(self, tmp_path):
        counter = Playbook("playbooks/site-openbao.yml", tags=("remove",), extra={"key": "new"})
        run = step(tmp_path, counter=counter)
        assert run.undo(Ctx()) == "3 hosts ok, 2 changed"
        assert report(tmp_path)["argv"][:3] == ["playbooks/site-openbao.yml", "--tags", "remove"]
        assert report(tmp_path)["extra_vars"] == {"key": "new"}


def test_recap_reads_the_last_play_recap_per_host():
    output = "\n".join(
        [
            "PLAY RECAP ***",
            "old : ok=1 changed=0 unreachable=0 failed=1",
            "TASK [x] ***",
            "PLAY RECAP ***",
            "srvvault1                  : ok=3    changed=1    unreachable=0    failed=0  ",
            "",
            "x : ---- 1.23s",
        ]
    )
    assert recap(output) == {"srvvault1": {"ok": 3, "changed": 1, "unreachable": 0, "failed": 0}}
    assert recap("ERROR! nothing") is None


def test_the_factory_builds_a_playbook_run_with_its_counter_run(tmp_path):
    factory = StepFactory(None, ansible=ansible(tmp_path))
    [run] = factory.playbook("deliver", "deliver it", DELIVER, no_undo="never")
    assert (run.id, run.title, run.book, run.no_undo) == (
        "ansible.run:deliver",
        "deliver it",
        DELIVER,
        "never",
    )
    assert run.ansible is factory.ansible
