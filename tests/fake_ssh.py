"""A stand-in for ssh running `sudo -n true` or `sudo -n chpasswd`, run as
`python fake_ssh.py <dir> <ssh argv…>`: it appends how it was called to calls.jsonl in <dir>, then
plays the scenario scenarios.json in <dir> names for the host, `ok` by default: true exits 0, and
chpasswd records each user's password per host in passwords.json. `echo` and `drop` play ok for
true. The remote command is what the login shell on the host makes of it: ssh joins its words
with spaces into one string, which that shell splits again."""

import json
import shlex
import sys
import time
from pathlib import Path

VALUED = ("-F", "-o", "-l")  # the options the rotator passes that take a value


def parse(argv):
    """(options, login, host, remote) of an ssh command line as the rotator builds it; remote: the
    words the host's shell splits the joined command into."""
    options, login, at = {}, None, 0
    while argv[at].startswith("-"):
        flag = argv[at]
        if flag in VALUED:
            value = argv[at + 1]
            if flag == "-o":
                name, _, setting = value.partition("=")
                options[name] = setting
            elif flag == "-l":
                login = value
            else:
                options[flag] = value
            at += 2
        else:
            options[flag] = True
            at += 1
    return options, login, argv[at], shlex.split(" ".join(argv[at + 1 :]))


def main():
    where, *argv = sys.argv[1:]
    where = Path(where)
    stdin = sys.stdin.read()
    options, login, host, remote = parse(argv)
    call = {
        "argv": argv,
        "options": options,
        "login": login,
        "host": host,
        "remote": remote,
        "stdin": stdin,
    }
    with (where / "calls.jsonl").open("a") as f:
        f.write(json.dumps(call) + "\n")
    scenarios = where / "scenarios.json"
    scenario = json.loads(scenarios.read_text()).get(host, "ok") if scenarios.exists() else "ok"
    if remote == ["sudo", "-n", "true"] and scenario in ("echo", "drop"):
        scenario = "ok"
    if scenario in ("ok", "drop") and remote == ["sudo", "-n", "chpasswd"]:
        user, _, password = stdin.removesuffix("\n").partition(":")
        held = where / "passwords.json"
        passwords = json.loads(held.read_text()) if held.exists() else {}
        passwords.setdefault(host, {})[user] = password
        held.write_text(json.dumps(passwords))
    if scenario == "drop":
        # chpasswd ran, then the connection went before its exit status came back.
        print(f"Connection to {host} closed by remote host.", file=sys.stderr)
        sys.exit(255)
    elif scenario == "unreachable":
        print(f"ssh: connect to host {host} port 22: No route to host", file=sys.stderr)
        sys.exit(255)
    elif scenario == "hostkey":
        print("Host key verification failed.", file=sys.stderr)
        sys.exit(255)
    elif scenario == "sudo":
        print("sudo: a password is required", file=sys.stderr)
        sys.exit(1)
    elif scenario == "echo":
        # A chpasswd that says what it was given, password and all.
        print(f"chpasswd: line 1: cannot set {stdin.strip()}", file=sys.stderr)
        print("chpasswd: error detected, changes ignored", file=sys.stderr)
        sys.exit(1)
    elif scenario == "hang":
        time.sleep(60)


main()
