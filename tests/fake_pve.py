"""The PVE cluster's nodes as the rotator calls them over SSH (Ssh.run): `sudo -n pvesh get
/cluster/resources --type vm --output-format json`, `sudo -n qm start <id>`, `sudo -n qm shutdown
<id> --timeout 300 --forceStop 1` and `sudo -n qm guest exec <id> --timeout 60 [--pass-stdin 1] --
<command…>`, played on the VMs it holds. A VM's started and stopped hooks are what its start and its
shutdown do to what runs in it; its agent, the guest agent, runs a command with its stdin and
answers (exit code, stdout, stderr), or None for a command that does not finish; a VM without one
has no guest agent running."""

import json
from collections.abc import Callable
from dataclasses import dataclass, field

from secret_rotator.ansiblesteps import Ran
from secret_rotator.vmsteps import PVE_BOUND, RESOURCES


@dataclass
class FakeVm:
    name: str
    vmid: int
    node: str
    status: str = "stopped"
    started: Callable[[], None] = lambda: None
    stopped: Callable[[], None] = lambda: None
    agent: Callable[[tuple[str, ...], str], tuple[int, str, str] | None] | None = None


@dataclass
class FakePve:
    vms: list[FakeVm]
    bound: int = PVE_BOUND
    calls: list[tuple[str, tuple[str, ...]]] = field(default_factory=list)  # (node, remote)
    down: set[str] = field(default_factory=set)  # nodes ssh does not reach
    refused: set[str] = field(default_factory=set)  # qm commands that fail: "start", "shutdown"
    stdins: list[str] = field(default_factory=list)  # each call's stdin, in order

    def run(self, host, remote, stdin):
        remote = tuple(remote)
        self.calls.append((host, remote))
        self.stdins.append(stdin)
        if host in self.down:
            return Ran(255, f"ssh: connect to host {host} port 22: No route to host\n")
        if remote[:4] == ("sudo", "-n", "qm", "guest"):
            return self.guest_exec(host, remote[4:], stdin)
        assert stdin == "", stdin
        if remote == RESOURCES:
            listed = [
                {
                    "id": f"qemu/{vm.vmid}",
                    "type": "qemu",
                    "vmid": vm.vmid,
                    "name": vm.name,
                    "node": vm.node,
                    "status": vm.status,
                    "template": 0,
                }
                for vm in self.vms
            ]
            return Ran(0, json.dumps(listed))
        sudo, qm, verb, vmid, *rest = remote[:2], *remote[2:]
        assert (sudo, qm) == (("sudo", "-n"), "qm"), remote
        (vm,) = [vm for vm in self.vms if str(vm.vmid) == vmid]
        if vm.node != host:
            return Ran(255, f"Configuration file 'nodes/{host}/qemu-server/{vmid}.conf' absent\n")
        if verb in self.refused:
            return Ran(255, f"{verb} failed: refused\n")
        if verb == "start":
            assert rest == [], remote
            if vm.status == "running":
                return Ran(255, f"VM {vmid} already running\n")
            vm.status = "running"
            vm.started()
            return Ran(0, "")
        assert verb == "shutdown" and rest == ["--timeout", "300", "--forceStop", "1"], remote
        vm.status = "stopped"
        vm.stopped()
        return Ran(0, "")

    def guest_exec(self, host, args, stdin):
        """qm's answer, its result printed pretty after any warning, as `qm guest exec` prints it
        on the node the VM is on."""
        at = args.index("--")
        (exec_, vmid, *options), command = args[:at], args[at + 1 :]
        assert exec_ == "exec" and options[:2] == ["--timeout", "60"], args
        assert options[2:] == (["--pass-stdin", "1"] if stdin else []), args
        (vm,) = [vm for vm in self.vms if str(vm.vmid) == vmid]
        if vm.node != host:
            return Ran(255, f"Configuration file 'nodes/{host}/qemu-server/{vmid}.conf' absent\n")
        if vm.status != "running":
            return Ran(255, f"VM {vmid} not running\n")
        if vm.agent is None:
            return Ran(255, "QEMU guest agent is not running\n")
        answered = vm.agent(command, stdin)
        if answered is None:
            return Ran(0, 'timeout reached, returning pid\n{\n   "pid" : 4242\n}\n')
        code, out, err = answered
        result = {"exitcode": code, "exited": 1}
        result |= {"out-data": out} if out else {}
        result |= {"err-data": err} if err else {}
        return Ran(0, json.dumps(result, indent=3, sort_keys=True) + "\n")

    def commands(self):
        """(node, the qm verb) of every qm call but a guest exec, in order."""
        return [
            (node, remote[3])
            for node, remote in self.calls
            if remote[2] == "qm" and remote[3] != "guest"
        ]
