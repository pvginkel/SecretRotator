"""A stand-in for ansible-playbook, run as `python fake_ansible_playbook.py <scenario> <argv…>`: it
records how it was called in report.json in its working directory, then plays the scenario. rotate
plays Ansible's rotate-ansible-key.yml on the estate estate.json there holds (rotate)."""

import json
import os
import stat
import subprocess
import sys
import time
from pathlib import Path

from secret_rotator.sshsteps import public_line

HOSTS = ("srvvault1", "srvvault2", "srvvault3")


def recap(changes=True, **bad):
    """bad: host -> its count that is not 0, as `failed` or `unreachable`; changes: whether
    srvvault1 and srvvault2 changed."""
    print("PLAY RECAP *********************************************************************")
    for host in HOSTS:
        failed = 1 if bad.get(host) == "failed" else 0
        unreachable = 1 if bad.get(host) == "unreachable" else 0
        changed = 1 if changes and host != "srvvault3" else 0
        print(
            f"{host:<27}: ok=3    changed={changed}    unreachable={unreachable}    "
            f"failed={failed}    skipped=1    rescued=0    ignored=0   "
        )
    print()
    print("Monday 05 October 2026  04:31:02 +0000 (0:00:01.234)       0:00:09.876 *****")
    print("===============================================================================")
    print("openbao : Write the secret_id ------------------------------------------- 1.23s")


def tasks():
    print("PLAY [Deliver the backup secret_id] ********************************************")
    print("TASK [openbao : Prove the secret_id] *******************************************")
    print("ok: [srvvault1]")
    print("TASK [openbao : Write the secret_id] *******************************************")


def key_of(line):
    """A public half's line without its comment: what authorized_keys matches a key by."""
    return " ".join(line.split()[:2])


def rotate(values):
    """rotate-ansible-key.yml with these extra vars on the estate estate.json holds: login, the
    controller's key file; authorized, each host's authorized_keys lines for ansible; refuse, the
    hosts that refuse the proof's login; fail, per state, the hosts whose run of it fails and
    changes nothing; unreachable, the hosts no run reaches. A host reached over SSH takes the
    controller's login only with the key the login file holds authorised; one with
    key_rotation_vm_<host> is a VM reached through its PVE node's guest agent, and left alone when
    that is absent. A present run's proof fails on every host when its private half is not the
    public half's. Each run is appended to runs.jsonl: its state, public and private halves, where
    the VMs run and the login file's key."""
    estate = json.loads(Path("estate.json").read_text())
    login = Path(estate["login"]).read_text()
    state, public = values["key_rotation_state"], values["key_rotation_public"]
    private = values.get("key_rotation_private")
    prefix = "key_rotation_vm_"
    vms = {k.removeprefix(prefix): v for k, v in values.items() if k.startswith(prefix)}
    run = {"state": state, "public": public, "private": private, "vms": vms, "login": login}
    with Path("runs.jsonl").open("a") as f:
        f.write(json.dumps(run) + "\n")
    proved = private is not None and key_of(public_line(private, "")) == key_of(public)
    controller = key_of(public_line(login, ""))
    counts = {}
    for host, keys in estate["authorized"].items():
        if vms.get(host) == "absent":
            counts[host] = {"ok": 1, "skipped": 1}
            continue
        reached = host in vms or controller in map(key_of, keys)
        if host in estate.get("unreachable", ()) or not reached:
            counts[host] = {"unreachable": 1}
            continue
        if host in estate.get("fail", {}).get(state, ()):
            counts[host] = {"ok": 1, "failed": 1}
            continue
        held = [k for k in keys if key_of(k) == key_of(public)]
        if state == "present":
            changed = not held
            keys += [public] if changed else []
            failed = host in estate.get("refuse", ()) or not proved
        else:
            changed = bool(held)
            keys[:] = [k for k in keys if key_of(k) != key_of(public)]
            failed = False
        counts[host] = {"ok": 3, "changed": int(changed), "failed": int(failed)}
    Path("estate.json").write_text(json.dumps(estate))
    print("PLAY RECAP *********************************************************************")
    for host, n in counts.items():
        print(
            f"{host:<27}: ok={n.get('ok', 0)}    changed={n.get('changed', 0)}    "
            f"unreachable={n.get('unreachable', 0)}    failed={n.get('failed', 0)}    "
            f"skipped={n.get('skipped', 0)}    rescued=0    ignored=0   "
        )
    if any(n.get("failed") for n in counts.values()):
        sys.exit(2)
    if any(n.get("unreachable") for n in counts.values()):
        sys.exit(4)


def main():
    scenario, *argv = sys.argv[1:]
    extra = argv[argv.index("--extra-vars") + 1].removeprefix("@")
    values = json.loads(Path(extra).read_text())
    report = {
        "pid": os.getpid(),
        "argv": argv,
        "cwd": os.getcwd(),
        "extra_path": extra,
        "extra_vars": values,
        "mode": stat.S_IMODE(os.stat(extra).st_mode),
        "dir_mode": stat.S_IMODE(os.stat(Path(extra).parent).st_mode),
        "env": {
            k: os.environ.get(k)
            for k in ("ANSIBLE_NOCOLOR", "ANSIBLE_FORCE_COLOR", "ANSIBLE_SSH_CONTROL_PATH_DIR")
        },
    }
    Path("report.json").write_text(json.dumps(report))
    if scenario == "hang":
        # A child that holds the output open, as ssh does, started before any output so that the
        # rotator never reads a task line without it; its pid tells a test whether it was killed.
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        Path("child.pid").write_text(str(child.pid))
    tasks()
    if scenario == "rotate":
        rotate(values)
    elif scenario == "ok":
        recap()
    elif scenario == "failed":
        print('fatal: [srvvault2]: FAILED! => {"msg": "the login was refused"}')
        recap(srvvault2="failed")
        sys.exit(2)
    elif scenario == "unchanged":
        # The proof failed on one node before any node wrote (any_errors_fatal).
        print('fatal: [srvvault2]: FAILED! => {"msg": "the login was refused"}')
        recap(False, srvvault2="failed")
        sys.exit(2)
    elif scenario == "unreachable":
        recap(srvvault3="unreachable")
        sys.exit(4)
    elif scenario == "echo":
        secret = values["openbao_backup_secret_id"]
        print(f'fatal: [srvvault1]: FAILED! => {{"msg": "refused {secret} for {secret}"}}')
        recap(srvvault1="failed")
        sys.exit(2)
    elif scenario == "named":
        # A task named with the staged var: Ansible renders it in the banner.
        secret = values["openbao_backup_secret_id"]
        print(f"TASK [openbao : Write {secret}] *******************************************")
        recap()
    elif scenario == "norecap":
        print("ERROR! the playbook: playbooks/x.yml could not be found", file=sys.stderr)
        sys.exit(1)
    elif scenario == "nohosts":
        print("PLAY RECAP *********************************************************************")
    elif scenario == "exit2":
        recap()
        sys.exit(2)
    elif scenario == "hang":
        sys.stdout.flush()
        time.sleep(60)


main()
