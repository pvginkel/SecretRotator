"""The generic SSH steps of design §4.2. ssh.set_password: one user's password on one host, set by
`sudo -n chpasswd` over SSH. The rotator logs in the way Ansible does from the iac container: as
ansible, with the Ansible key (ansiblekey), the host key checked against the homelab SSH host CA.
The password goes to chpasswd on its stdin, never on a command line, and no detail or error carries
it. ssh.keygen: a new ed25519 keypair for a plan's key. ssh.key_copy: srviac's own copy of the
Ansible key switched to the new private half, and back by its undo. No detail or error of theirs
carries a private half."""

import base64
import hashlib
import shlex
import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path

from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from secret_rotator.ansiblekey import IDENTITY, AnsibleKey, read, replace
from secret_rotator.ansiblesteps import ANSIBLE_DIR, Ran, redact
from secret_rotator.model import Context, Step, StepFailed, not_landed, value_name

# Ansible's connection settings (Ansible ansible/ansible.cfg ssh_args, group_vars/all
# ansible_user): the known_hosts file whose one @cert-authority line is the homelab SSH host CA.
# ansible has passwordless sudo on every managed host (the bootstrap role).
KNOWN_HOSTS = ANSIBLE_DIR / "files" / "known_hosts.d" / "homelab"
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
    logs_in = True

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


def public_name(key: str) -> str:
    """The staging name of the public half of the new keypair of a plan's data key."""
    return f"public:{key}"


def old_name(key: str) -> str:
    """The staging name of the private half the leaf's key held before the plan."""
    return f"old:{key}"


def old_public_name(key: str) -> str:
    """The staging name of the public half of the key the leaf's key held before the plan."""
    return f"old-public:{key}"


def public_line(private: str, comment: str) -> str:
    """The `<type> <base64> <comment>` line of an unencrypted OpenSSH private key's public half."""
    loaded = serialization.load_ssh_private_key(private.encode(), password=None)
    public = loaded.public_key().public_bytes(
        serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH
    )
    return f"{public.decode()} {comment}"


def fingerprint(line: str) -> str:
    """The SHA256 fingerprint of a public half's line, as ssh-keygen -l prints it."""
    digest = hashlib.sha256(base64.b64decode(line.split()[1])).digest()
    return "SHA256:" + base64.b64encode(digest).decode().rstrip("=")


def _keep(ctx: Context, name: str, make: Callable[[], str]) -> str:
    """The value staged under name, staged first from make() when there is none."""
    if ctx.staged(name) is None:
        ctx.stage(name, make())
    return ctx.staged(name)


class SshKeygen(Step):
    """A new ed25519 keypair for one of the plan's keys, staged before anything uses it: the private
    half as the key's new value, in the OpenSSH form ssh reads from a file, and the public half as
    one `ssh-ed25519 <base64> <comment>` line (public_name). With it, the pair the leaf's key holds
    when the step first runs: its private half (old_name) and its public half with the same comment
    (old_public_name), for the steps that take the old half off and the undos that put it back. The
    plan puts the step before kv.write. A re-run keeps what is staged. A leaf whose key holds no
    OpenSSH private key fails it."""

    type = "ssh.keygen"
    silent = True

    def __init__(self, leaf: str, key: str, comment: str):
        if comment.split() != [comment]:
            raise ValueError(f"comment {comment!r}: not one word")
        super().__init__(f"ssh.keygen:{key}", f"generate a new {key} keypair")
        self.leaf = leaf
        self.key = key
        self.comment = comment

    def run(self, ctx: Context) -> str:
        old = _keep(ctx, old_name(self.key), lambda: self._held(ctx))
        was = _keep(ctx, old_public_name(self.key), lambda: public_line(old, self.comment))
        new = _keep(ctx, value_name(self.key), generate)
        line = _keep(ctx, public_name(self.key), lambda: public_line(new, self.comment))
        return f"ed25519 {fingerprint(line)}, replacing {fingerprint(was)}"

    def _held(self, ctx: Context) -> str:
        """The private half the leaf's key holds, which must be an unencrypted OpenSSH key."""
        version = ctx.bao.read(self.leaf)
        held = None if version is None else version.data.get(self.key)
        if not held:
            raise StepFailed(f"{self.leaf} holds no {self.key}: there is no old key to replace")
        try:
            public_line(held, self.comment)
        except (ValueError, TypeError, UnsupportedAlgorithm):
            raise StepFailed(
                f"{self.leaf}#{self.key} holds no unencrypted OpenSSH private key"
            ) from None
        return held


def generate() -> str:
    """A new ed25519 private key in the OpenSSH form, ending in a newline as ssh needs."""
    return (
        Ed25519PrivateKey.generate()
        .private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.OpenSSH,
            serialization.NoEncryption(),
        )
        .decode()
    )


class SshKeyCopy(Step):
    """srviac's own copy of the Ansible key (AnsibleKey.copy), which a run of Ansible takes while
    OpenBao is down: the whole file replaced with the new private half of one of the plan's keys,
    mode 0600, and read back. Its undo puts back the private half the plan's ssh.keygen staged as
    the old one. Nothing else writes the copy, and nothing writes it from the leaf: a store
    restored from an older snapshot does not reach it. The plan puts it after the add run that
    proves the new key on every host and before the run that takes the old one off, so the copy
    holds a key every host takes at each point of the plan and of its rollback.

    A copy it cannot write, as in a container without srviac's directory, fails it reporting that
    it did not land: the file is as it was, and the plan stops there."""

    type = "ssh.key_copy"
    mutates = True

    def __init__(self, files: AnsibleKey, key: str):
        super().__init__(f"ssh.key_copy:{key}", "switch srviac's copy of the key to the new one")
        self.path = files.copy
        self.key = key

    def run(self, ctx: Context) -> str:
        new = ctx.staged(value_name(self.key))
        if new is None:
            raise StepFailed("no new key is staged", landed=False)
        return f"{self.path} holds the new key, {self._write(new)}"

    def undo(self, ctx: Context) -> str:
        old = ctx.staged(old_name(self.key))
        if old is None:
            raise StepFailed("no old key is staged: the plan's ssh.keygen stages it")
        return f"{self.path} holds the old key again, {self._write(old)}"

    def _write(self, private: str) -> str:
        """Writes the key and reads it back; its fingerprint."""
        try:
            replace(self.path, private)
        except OSError as e:
            raise StepFailed(f"{self.path} cannot be written: {e.strerror}", landed=False) from e
        if read(self.path) != private:
            raise StepFailed(f"the read-back of {self.path} does not hold the key written")
        return fingerprint(public_line(private, ""))
