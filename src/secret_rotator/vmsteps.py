"""The core step vm.start, and what the executor does with a VM that may be off (design ruling D3 of
slice 061, D2 of Ansible slice 060): a step on such VMs names them (Step.vms), every plan with one
starts with a vm.start of each (plan.build), the executor has the VM up whenever such a step or its
undo runs, and it shuts the VM down again once the plan's run or abort ends if the plan started it.
A VM is reached through the PVE cluster, never over SSH to itself, whose host certificate lapses
while it is off: SSH as ansible to a PVE node, `sudo -n pvesh` there to find which node runs the VM
and whether it runs, and `sudo -n qm` on that node to start it, to ping its guest agent and to shut
it down. The node is looked up at each use: a VM moves between nodes. A VM the cluster does not
list is absent, as a scratch VM is while the scratch stack does not exist."""

import json
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from secret_rotator.ansiblesteps import Ran
from secret_rotator.model import Context, NoAnswer, Step, StepFailed, wait
from secret_rotator.openbao import OpenBao
from secret_rotator.sshsteps import Ssh

# The dev cluster's one VM, off by default (Ansible terraform/prd/vms.tf: on_boot = false).
DEV_VM = "srvk8sdev"
# The PVE cluster's nodes: Ansible ansible/inventories/prd/hosts.yml's proxmox group.
PVE_NODES = ("pve", "pve1", "pve2")
RESOURCES = (
    "sudo",
    "-n",
    "pvesh",
    "get",
    "/cluster/resources",
    "--type",
    "vm",
    "--output-format",
    "json",
)
# Past it qm forces the guest off, as Ansible update-k8s.yml's cold cycle does.
SHUTDOWN_TIMEOUT = 300  # seconds
PVE_BOUND = SHUTDOWN_TIMEOUT + 60  # seconds: an ssh call to a node, a qm shutdown included
BOOT_BOUND, BOOT_POLL = 900, 10  # seconds: a boot of microk8s and microceph
RUNNING, STOPPED = "running", "stopped"
ABSENT = "absent"  # where a VM the cluster does not list runs (location_name)


def started_name(guest: str) -> str:
    """The staging name that records that the plan started the VM: staged before its qm start,
    dropped once the executor has shut it down again."""
    return f"vm.start:{guest}:started"


def location_name(guest: str) -> str:
    """The staging name of where the VM runs, `<node>/<vmid>` (`pve/919`), or ABSENT: staged by
    its vm.start, in each run or abort before the first step on the VM, for the steps on it
    (Ansible slice 060's ruling A1: the key rotation's playbook takes it as an extra var)."""
    return f"vm.start:{guest}:location"


@dataclass(frozen=True)
class Guest:
    """A VM as the PVE cluster lists it."""

    name: str
    node: str  # the PVE node it is on
    vmid: int
    status: str  # running, stopped


def said(ran: Ran, bound: int) -> str:
    """How a command ended that did not exit 0, with its output's last line."""
    if ran.code is None:
        return f"did not finish within {bound} s"
    output = ran.output.strip()
    return f"exited {ran.code}" + (f": {output.splitlines()[-1]}" if output else "")


class Pve:
    """The PVE cluster as the rotator reaches it: over SSH as ansible to a node, sudo -n there. Its
    sleep and clock pace the wait for a VM it started."""

    def __init__(
        self,
        ssh: Ssh | None = None,
        *,
        nodes: Sequence[str] = PVE_NODES,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.ssh = ssh or Ssh(bound=PVE_BOUND)
        self.nodes = tuple(nodes)
        self.sleep = sleep
        self.clock = clock

    def find(self, name: str) -> Guest | None:
        """The VM of that name, as the first node that answers lists the cluster's VMs; None when
        it lists none."""
        failures = []
        for node in self.nodes:
            ran = self.ssh.run(node, RESOURCES, "")
            if ran.code != 0:
                failures.append(f"on {node} it {said(ran, self.ssh.bound)}")
                continue
            try:
                listed = json.loads(ran.output)
            except json.JSONDecodeError:
                raise StepFailed(f"pvesh on {node} printed no list of VMs", ran.output) from None
            found = [
                r
                for r in listed
                if r.get("type") == "qemu" and r.get("name") == name and not r.get("template")
            ]
            if not found:
                return None
            if len(found) > 1:
                raise StepFailed(f"the PVE cluster has {len(found)} VMs named {name}, not one")
            (vm,) = found
            return Guest(name, vm["node"], int(vm["vmid"]), vm["status"])
        raise StepFailed(f"no PVE node lists the cluster's VMs: {'; '.join(failures)}")

    def start(self, guest: Guest) -> None:
        self._call(guest, ("qm", "start", str(guest.vmid)))

    def shutdown(self, guest: Guest) -> None:
        """A clean shutdown, forced past SHUTDOWN_TIMEOUT; done once the VM is stopped."""
        timeout = ("--timeout", str(SHUTDOWN_TIMEOUT), "--forceStop", "1")
        self._call(guest, ("qm", "shutdown", str(guest.vmid), *timeout))

    def agent(self, guest: Guest) -> str | None:
        """Why the VM's guest agent does not answer a ping; None when it answers."""
        try:
            self._call(guest, ("qm", "guest", "cmd", str(guest.vmid), "ping"))
        except StepFailed as e:
            return f"its guest agent does not answer: {e.error}"
        return None

    def _call(self, guest: Guest, command: tuple[str, ...]) -> None:
        ran = self.ssh.run(guest.node, ("sudo", "-n", *command), "")
        if ran.code != 0:
            raise StepFailed(
                f"{' '.join(command)} on {guest.node} {said(ran, self.ssh.bound)}", ran.output
            )


class VmStart(Step):
    """Has the VM up for the plan's steps on it, where it runs staged for them (location_name), and
    waits until its guest agent and what each of those steps acts on answer (Step.unanswered),
    within BOOT_BOUND: past it, NoAnswer. A VM that runs is left as it is; one that does not is
    started, the start staged first (started_name), so whichever run of the plan ends it shuts the
    VM down again (executor); one the cluster does not list is nothing to do. PVE refusing the
    start fails it. It mutates nothing a rollback undoes: the shutdown is the executor's, whatever
    the plan's outcome."""

    type = "vm.start"
    estimate = 180  # a boot of microk8s and microceph
    logs_in = True

    def __init__(self, pve: Pve, guest: str, steps: Sequence[Step]):
        super().__init__(f"vm.start:{guest}", f"start {guest} if it is off")
        self.pve = pve
        self.guest = guest
        self.steps = tuple(steps)  # the plan's steps on the VM

    def run(self, ctx: Context) -> str:
        return self.up(ctx)

    def up(self, ctx: Context) -> str:
        """What it found and did, once the VM's guest agent and every step on the VM answer."""
        found = self.pve.find(self.guest)
        if found is None:
            ctx.stage(location_name(self.guest), ABSENT)
            return f"{self.guest} does not exist: nothing to start"
        ctx.stage(location_name(self.guest), f"{found.node}/{found.vmid}")
        if found.status == RUNNING:
            ours = ctx.staged(started_name(self.guest)) is not None
            done = f"{self.guest} runs on {found.node}: " + (
                "the plan started it" if ours else "left running"
            )
        else:
            ctx.progress(f"{self.guest} is {found.status} on {found.node}: starting it")
            ctx.stage(started_name(self.guest), found.node)
            self.pve.start(found)
            done = f"{self.guest} started on {found.node}: it is shut down again after the plan"
        wait(
            self.pve,
            ctx,
            BOOT_BOUND,
            BOOT_POLL,
            lambda: self.pve.agent(found) or self.unanswered_on(ctx.bao),
            f"{self.guest} did not answer",
            failed=NoAnswer,
        )
        return done

    def unanswered_on(self, bao: OpenBao) -> str | None:
        """Why a step on the VM does not answer; None when every one does."""
        return next((why for step in self.steps if (why := step.unanswered(bao))), None)

    def down(self) -> str:
        """Shuts the VM down, and says what it did or why it did not: a shutdown fails no plan."""
        try:
            found = self.pve.find(self.guest)
            if found is None:
                return f"{self.guest} does not exist"
            if found.status == STOPPED:
                return f"{self.guest} is stopped already"
            self.pve.shutdown(found)
        except StepFailed as e:
            return f"{self.guest} is not shut down: {e.error}"
        return f"{self.guest} shut down on {found.node}"


def starts(pve: Pve, steps: Sequence[Step]) -> list[Step]:
    """A vm.start of each VM a step names (Step.vms), in the order the steps first name them."""
    guests = dict.fromkeys(guest for step in steps for guest in step.vms)
    return [VmStart(pve, guest, [s for s in steps if guest in s.vms]) for guest in guests]
