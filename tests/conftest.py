"""No test reaches a live host: the dev environment's iac container holds the Ansible SSH key and
the homelab host CA, so the real ssh of an Ssh a test leaves unfaked logs in to the PVE cluster
(Pve's default). A test plays ssh with fake_ssh.py or an in-process fake, or reads options back
with `ssh -G`, which connects nowhere."""

import subprocess

import pytest

REAL_RUN = subprocess.run


def guarded(argv, *args, **kwargs):
    if argv and argv[0] == "ssh" and "-G" not in argv:
        raise AssertionError(f"a test ran the real ssh: {' '.join(argv)}")
    return REAL_RUN(argv, *args, **kwargs)


@pytest.fixture(autouse=True)
def no_live_ssh(monkeypatch):
    monkeypatch.setattr(subprocess, "run", guarded)
