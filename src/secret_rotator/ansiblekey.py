"""The key the rotator logs in to a host as ansible with (Ansible slice 060's settled items): the
file every such login names holds the key the leaf iac/ansible-ssh-key holds, rewritten from the
leaf before each step of a plan that logs in (executor). A plan's kv.write of a new key switches
the running rotator to it, the rollback's kv.write undo switches it back, and a rotator that
outlives a rotation, as the UI does, logs in with the key the leaf holds when its plan runs.
srviac's own copy of the key is never written from the leaf: only the plan's ssh.key_copy writes
it (sshsteps)."""

import contextlib
import os
import tempfile
from pathlib import Path

from secret_rotator.openbao import OpenBao

# The leaf whose key iac-impl writes to IDENTITY when the iac container starts (Ansible
# support/iac-agent/etc/iac/secrets.example.yaml).
LEAF, KEY = "iac/ansible-ssh-key", "private"
# The file every login as ansible names: Ansible's ansible.cfg (private_key_file and ssh_args), the
# playbooks that set their own ansible_ssh_args, and the rotator's own ssh (sshsteps).
IDENTITY = "~/.ssh/id_ed25519_ansible"
# srviac's own copy of the key, for a run of Ansible while OpenBao is down: its directory is
# bind-mounted read-write into every iac container on srviac (Ansible support/iac-agent/bin/iac.j2)
# and is absent in any other container.
COPY = Path("/var/lib/iac/ansible-ssh-key/id_ed25519_ansible")


def read(path: Path) -> str | None:
    """The file's content; None when there is no such file."""
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None


def replace(path: Path, content: str) -> None:
    """The file holds content, mode 0600: a new file in its directory renamed over it, so a reader
    finds the old content or the new, never part of either. An OSError leaves the file as it was."""
    fd, temp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temp)
        raise


class AnsibleKey:
    """The rotator's Ansible key on disk: the file its logins as ansible read the key from, and
    srviac's own copy."""

    def __init__(self, login: Path | None = None, copy: Path = COPY):
        self.login = login or Path(IDENTITY).expanduser()
        self.copy = copy

    def follow(self, bao: OpenBao) -> None:
        """The login file holds the key the leaf holds, rewritten only when it holds another."""
        held = bao.value(LEAF, KEY)
        if read(self.login) != held:
            replace(self.login, held)
