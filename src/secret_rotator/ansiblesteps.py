"""The generic step ansible.run of design §4.2: a playbook run from the Ansible checkout that
iac-impl clones into the iac container, which succeeds on a PLAY RECAP with no failed or
unreachable host. Its undo is a counter-run where the plan names one."""

import contextlib
import json
import os
import re
import signal
import subprocess
import tempfile
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from secret_rotator.model import Context, Step, StepFailed

# iac-impl's clone of pvginkel/Ansible; its ansible.cfg names the inventory and the SSH key file
# that iac-impl writes into the container, and iac-impl exports ANSIBLE_VAULT_PASSWORD_FILE.
ANSIBLE_DIR = Path("/work/Ansible/ansible")
COMMAND = ("ansible-playbook",)
RUN_BOUND = 1200  # seconds
TAIL = 200  # the lines of a failed run's output kept as its technical detail
RECAP_LINE = re.compile(r"(?P<host>\S+)\s+:\s+(?P<counts>(?:[a-z]+=\d+\s*)+)")
COUNT = re.compile(r"([a-z]+)=(\d+)")


@dataclass(frozen=True)
class Playbook:
    """One run of a playbook. Every extra var goes to it in a file, never on its command line."""

    path: str  # relative to ANSIBLE_DIR
    tags: tuple[str, ...] = ()
    staged: Mapping[str, str] = field(default_factory=dict)  # extra var -> its staging name
    extra: Mapping[str, str] = field(default_factory=dict)  # extra var -> its value, no secret

    def __str__(self) -> str:
        return self.path + (f" --tags {','.join(self.tags)}" if self.tags else "")


@dataclass(frozen=True)
class Ran:
    code: int | None  # None: killed at the bound
    output: str  # stdout and stderr, one stream


class Ansible:
    """Runs ansible-playbook in the Ansible checkout with the container's environment, so with its
    SSH key and vault password file."""

    def __init__(
        self,
        directory: Path = ANSIBLE_DIR,
        command: tuple[str, ...] = COMMAND,
        *,
        bound: int = RUN_BOUND,
    ):
        self.directory = directory
        self.command = command
        self.bound = bound

    def run(
        self, book: Playbook, values: Mapping[str, str], progress: Callable[[str], None]
    ) -> Ran:
        """Runs the playbook with its extra vars, values those of its staged ones; each task it
        starts is a progress detail."""
        if not self.directory.is_dir():
            raise StepFailed(f"there is no Ansible checkout at {self.directory}")
        env = {k: v for k, v in os.environ.items() if k != "ANSIBLE_FORCE_COLOR"}
        env["ANSIBLE_NOCOLOR"] = "1"
        # A file a killed rotator leaves goes with the iac container, removed when its command ends.
        with tempfile.TemporaryDirectory(prefix="secret-rotator-") as private:
            extra = Path(private) / "extra-vars.json"
            fd = os.open(extra, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as f:
                json.dump({**book.extra, **values}, f)
            argv = [*self.command, book.path]
            if book.tags:
                argv += ["--tags", ",".join(book.tags)]
            argv += ["--extra-vars", f"@{extra}"]
            try:
                proc = subprocess.Popen(
                    argv,
                    cwd=self.directory,
                    env=env,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    start_new_session=True,
                )
            except FileNotFoundError:
                raise StepFailed(f"{self.command[0]} is not on the PATH") from None
            killed = threading.Event()

            def kill() -> None:
                # The whole session: ssh children that hold the output open go with it, and a
                # session of its own is out of reach of the terminal's Ctrl-C.
                with contextlib.suppress(ProcessLookupError):  # it ended meanwhile
                    os.killpg(proc.pid, signal.SIGKILL)

            def expire() -> None:
                killed.set()
                kill()

            timer = threading.Timer(self.bound, expire)
            timer.start()
            lines = []
            try:
                for line in proc.stdout:
                    lines.append(line.rstrip("\n"))
                    if line.startswith("TASK ["):
                        progress(line[len("TASK [") :].split("]", 1)[0])
                code = proc.wait()
            finally:
                timer.cancel()
                if proc.poll() is None:  # the rotator itself is interrupted
                    kill()
                    proc.wait()
        return Ran(None if killed.is_set() else code, "\n".join(lines))


def recap(output: str) -> dict[str, dict[str, int]] | None:
    """The last PLAY RECAP's counts per host; None when the output has none."""
    lines = output.splitlines()
    starts = [n for n, line in enumerate(lines) if line.startswith("PLAY RECAP")]
    if not starts:
        return None
    hosts = {}
    for line in lines[starts[-1] + 1 :]:
        if found := RECAP_LINE.fullmatch(line.strip()):
            hosts[found["host"]] = {k: int(v) for k, v in COUNT.findall(found["counts"])}
    return hosts


def redact(text: str, values: Mapping[str, str]) -> str:
    for value in sorted(values.values(), key=len, reverse=True):
        if value:
            text = text.replace(value, "<redacted>")
    return text


class AnsibleRun(Step):
    """Runs a playbook (tags, extra vars) and verifies its PLAY RECAP: at least one host, none
    failed or unreachable, and the run exited 0. A secret it hands the playbook is staged, read
    when it runs, and goes in the extra-vars file; its output is kept with every such value
    redacted.

    counter: the run that takes this one's change out again, its undo; a playbook run is
    idempotent, so the counter-run also leaves a run that did not land as it is. Without one the
    step has no undo, and no_undo says why; a run that never started, or whose PLAY RECAP shows no
    host changed, then reports that the step did not land; one killed at its bound, or that
    ended without a recap, counts as landed (Step.no_undo)."""

    type = "ansible.run"
    mutates = True

    def __init__(
        self,
        ansible: Ansible,
        name: str,
        title: str,
        book: Playbook,
        counter: Playbook | None = None,
        *,
        no_undo: str = "",
    ):
        super().__init__(f"ansible.run:{name}", title)
        self.ansible = ansible
        self.book = book
        self.counter = counter
        if counter is None:
            self.undo = None
            self.no_undo = no_undo or f"{title}: the playbook has no counter-run"

    def run(self, ctx: Context) -> str:
        return self._play(ctx, self.book)

    def undo(self, ctx: Context) -> str:
        return self._play(ctx, self.counter)

    def _failed(self, error: str, tail: str = "", *, changed: bool) -> StepFailed:
        """The run's failure, known to have changed a host or not."""
        return StepFailed(error, tail, landed=changed or self.counter is not None)

    def _play(self, ctx: Context, book: Playbook) -> str:
        values = {var: ctx.staged(name) for var, name in book.staged.items()}
        if missing := sorted(var for var, value in values.items() if value is None):
            raise self._failed(f"no value is staged for {', '.join(missing)}", changed=False)
        try:
            ran = self.ansible.run(book, values, lambda task: ctx.progress(redact(task, values)))
        except StepFailed as e:  # Ansible.run raises it only before the playbook starts
            raise self._failed(e.error, e.technical, changed=False) from e
        output = redact(ran.output, values)
        tail = "\n".join(output.splitlines()[-TAIL:])
        hosts = recap(output)
        if ran.code is None:
            raise StepFailed(f"{book} did not finish within {self.ansible.bound // 60} min", tail)
        if hosts is None:
            raise StepFailed(f"{book} ended without a PLAY RECAP (exit {ran.code})", tail)
        changed = sum(1 for counts in hosts.values() if counts.get("changed"))
        bad = [
            f"{host} {what}"
            for host, counts in sorted(hosts.items())
            for what in ("failed", "unreachable")
            if counts.get(what)
        ]
        if bad:
            raise self._failed(f"{book}: {', '.join(bad)}", tail, changed=bool(changed))
        if not hosts:
            raise self._failed(f"{book} ran on no host", tail, changed=False)
        if ran.code != 0:
            raise self._failed(f"{book} exited {ran.code}", tail, changed=bool(changed))
        return f"{len(hosts)} host{'s' if len(hosts) != 1 else ''} ok, {changed} changed"
