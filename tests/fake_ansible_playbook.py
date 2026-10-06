"""A stand-in for ansible-playbook, run as `python fake_ansible_playbook.py <scenario> <argv…>`: it
records how it was called in report.json in its working directory, then plays the scenario."""

import json
import os
import stat
import subprocess
import sys
import time
from pathlib import Path

HOSTS = ("srvvault1", "srvvault2", "srvvault3")


def recap(**bad):
    """bad: host -> its count that is not 0, as `failed` or `unreachable`."""
    print("PLAY RECAP *********************************************************************")
    for host in HOSTS:
        failed = 1 if bad.get(host) == "failed" else 0
        unreachable = 1 if bad.get(host) == "unreachable" else 0
        changed = 0 if host == "srvvault3" else 1
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
        "env": {k: os.environ.get(k) for k in ("ANSIBLE_NOCOLOR", "ANSIBLE_FORCE_COLOR")},
    }
    Path("report.json").write_text(json.dumps(report))
    if scenario == "hang":
        # A child that holds the output open, as ssh does, started before any output so that the
        # rotator never reads a task line without it; its pid tells a test whether it was killed.
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        Path("child.pid").write_text(str(child.pid))
    tasks()
    if scenario == "ok":
        recap()
    elif scenario == "failed":
        print('fatal: [srvvault2]: FAILED! => {"msg": "the login was refused"}')
        recap(srvvault2="failed")
        sys.exit(2)
    elif scenario == "unreachable":
        recap(srvvault3="unreachable")
        sys.exit(4)
    elif scenario == "echo":
        secret = values["openbao_backup_secret_id"]
        print(f'fatal: [srvvault1]: FAILED! => {{"msg": "refused {secret} for {secret}"}}')
        recap(srvvault1="failed")
        sys.exit(2)
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
