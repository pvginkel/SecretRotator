"""The generic step ssh.set_password of design §4.2: one user's password on one host, set by
`sudo -n chpasswd` over SSH. The rotator logs in the way Ansible does from the iac container: as
ansible, with the Ansible key, the host key checked against the homelab SSH host CA. The password
goes to chpasswd on its stdin, never on a command line, and no detail or error carries it."""

import shlex
import subprocess
from collections.abc import Sequence
from pathlib import Path

from secret_rotator.ansiblesteps import ANSIBLE_DIR, Ran, redact
from secret_rotator.model import Context, Step, StepFailed, not_landed, value_name

# Ansible's connection settings (Ansible ansible/ansible.cfg ssh_args, group_vars/all
# ansible_user): the known_hosts file whose one @cert-authority line is the homelab SSH host CA,
# and the key iac-impl writes into the iac container. ansible has passwordless sudo on every
# managed host (the bootstrap role).
KNOWN_HOSTS = ANSIBLE_DIR / "files" / "known_hosts.d" / "homelab"
IDENTITY = "~/.ssh/id_ed25519_ansible"
LOGIN = "ansible"
COMMAND = ("ssh",)
CHPASSWD = ("sudo", "-n", "chpasswd")
REACH = ("sudo", "-n", "true")
CONNECT_TIMEOUT = 15  # seconds
RUN_BOUND = 60  # seconds


class Ssh:
    """Runs a command on a host over SSH as ansible, with no config file but its options: no
    prompt, the Ansible key only, and a host key the homelab SSH host CA signed."""

    def __init__(
        self,
        known_hosts: Path = KNOWN_HOSTS,
        command: tuple[str, ...] = COMMAND,
        *,
        bound: int = RUN_BOUND,
    ):
        self.known_hosts = known_hosts
        self.command = command
        self.bound = bound

    def argv(self, host: str, remote: Sequence[str]) -> list[str]:
        options = {
            "BatchMode": "yes",
            "ConnectTimeout": str(CONNECT_TIMEOUT),
            "PreferredAuthentications": "publickey",
            "IdentityFile": IDENTITY,
            "IdentitiesOnly": "yes",
            "StrictHostKeyChecking": "yes",
            "UserKnownHostsFile": str(self.known_hosts),
            "GlobalKnownHostsFile": "/dev/null",
            "HostKeyAlgorithms": "ssh-ed25519-cert-v01@openssh.com,ssh-ed25519",
        }
        flags = [arg for name, value in options.items() for arg in ("-o", f"{name}={value}")]
        # ssh joins the remote words with spaces into one string the host's login shell splits
        # again: each is quoted for that shell.
        quoted = [shlex.quote(word) for word in remote]
        return [*self.command, "-F", "none", "-T", *flags, "-l", LOGIN, host, *quoted]

    def run(self, host: str, remote: Sequence[str], stdin: str) -> Ran:
        """The remote command's exit code and output, stdout and stderr in one stream; code None:
        killed at the bound. ssh's own failures (unreachable, host key, login) exit 255."""
        if not self.known_hosts.is_file():
            raise StepFailed(f"there is no homelab SSH host CA file at {self.known_hosts}")
        try:
            done = subprocess.run(
                self.argv(host, remote),
                input=stdin,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=self.bound,
                start_new_session=True,
            )
        except FileNotFoundError:
            raise StepFailed(f"{self.command[0]} is not on the PATH") from None
        except subprocess.TimeoutExpired:
            return Ran(None, "")
        return Ran(done.returncode, done.stdout)


class SshSetPassword(Step):
    """Sets one user's password on one host to the new value staged for the plan's key, by
    `sudo -n chpasswd` over SSH, the user and password on its stdin. Nothing verifies it (design
    §4.2); a re-run sets it again.

    Its run first reaches the host as the set does, by `sudo -n true`: a failure there (ssh, the
    CA file, the connection, the host key, the login, sudo) ran no chpasswd, so it reports the set
    not landed and a rollback leaves the host alone. Any failure after that counts as landed.

    Its undo sets back the password the leaf's key holds, read from KV when it runs: the plan puts
    the step before kv.write, whose undo a rollback runs first, so KV holds the password from
    before the plan again."""

    type = "ssh.set_password"
    mutates = True

    def __init__(self, ssh: Ssh, host: str, user: str, leaf: str, key: str):
        super().__init__(f"ssh.set_password:{user}@{host}", f"set {user}'s password on {host}")
        self.ssh = ssh
        self.host = host
        self.user = user
        self.leaf = leaf
        self.key = key  # the plan's data key whose new value it sets

    def run(self, ctx: Context) -> str:
        new = ctx.staged(value_name(self.key))
        if new is None:
            raise StepFailed("no new password is staged", landed=False)
        _refuse(new, "the new password")
        try:
            self._call(REACH, "", "")
        except StepFailed as e:
            raise not_landed(e) from e
        self._call(CHPASSWD, f"{self.user}:{new}\n", new)
        return f"{self.user}'s password set on {self.host}"

    def undo(self, ctx: Context) -> str:
        version = ctx.bao.read(self.leaf)
        old = None if version is None else version.data.get(self.key)
        if old is None:
            raise StepFailed(f"{self.leaf} holds no {self.key} to set back")
        _refuse(old, f"the password {self.leaf} holds")
        self._call(CHPASSWD, f"{self.user}:{old}\n", old)
        return f"{self.user}'s password on {self.host} set back to the one {self.leaf} holds"

    def _call(self, remote: tuple[str, ...], stdin: str, password: str) -> None:
        ran = self.ssh.run(self.host, remote, stdin)
        output = redact(ran.output, {"password": password}).strip()
        said = f": {output.splitlines()[-1]}" if output else ""
        if ran.code is None:
            raise StepFailed(f"ssh to {self.host} did not finish within {self.ssh.bound} s")
        if ran.code == 255:
            raise StepFailed(f"ssh to {self.host} as {LOGIN} failed{said}", output)
        if ran.code != 0:
            raise StepFailed(f"{' '.join(remote)} on {self.host} exited {ran.code}{said}", output)


def _refuse(password: str, what: str) -> None:
    if not password or "\n" in password:
        raise StepFailed(
            f"{what} is empty or more than one line: refused for chpasswd", landed=False
        )
