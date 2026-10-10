"""The ssh-key kind's own step (design §4.2: custom steps are the plugin's): ssh_key.reach, which
asks each host the plan reaches over SSH whether it answers (Ansible slice 060's ruling D2: an
always-up host that does not answer skips the plan for the night)."""

from collections.abc import Sequence

from secret_rotator.model import Context, NoAnswer, Step
from secret_rotator.openbao import OpenBao
from secret_rotator.sshsteps import Ssh

PROBE = ("true",)
# What ssh prints, exiting 255, when it reaches no sshd on the host: no address, no connection, or
# none that got as far as the key exchange. Any other 255 is the host refusing the login or its
# host key: an answer.
UNREACHED = (
    "ssh: connect to host ",
    "ssh: Could not resolve hostname ",
    "Connection timed out during banner exchange",
    "kex_exchange_identification: ",
)


class Reach(Step):
    """Logs in to each host over SSH as ansible and runs `true`, to learn whether it answers: one
    whose ssh does not finish within its bound, or reaches no sshd, does not. The nightly run asks
    it before it starts the plan (Step.unanswered); its run asks again and raises NoAnswer, which
    skips an unattended run where nothing has mutated yet and fails an attended one. A host that
    refuses the login or the host key answers: the add run fails on it."""

    type = "ssh_key.reach"
    logs_in = True

    def __init__(self, ssh: Ssh, hosts: Sequence[str]):
        super().__init__(self.type, f"check that the {len(hosts)} hosts reached over SSH answer")
        self.ssh = ssh
        self.hosts = tuple(hosts)

    def unanswered(self, bao: OpenBao) -> str | None:
        whys = [f"{host} {why}" for host in self.hosts if (why := self._unreached(host))]
        return "; ".join(whys) or None

    def run(self, ctx: Context) -> str:
        if why := self.unanswered(ctx.bao):
            raise NoAnswer(why)
        return f"{len(self.hosts)} hosts answer"

    def _unreached(self, host: str) -> str | None:
        """Why the host does not answer; None when it does."""
        ran = self.ssh.run(host, PROBE, "")
        if ran.code is None:
            return f"does not answer: ssh did not finish within {self.ssh.bound} s"
        said = ran.output.strip().splitlines()
        if ran.code == 255 and (lines := [ln for ln in said if ln.startswith(UNREACHED)]):
            return f"does not answer: {lines[0]}"
        return None
