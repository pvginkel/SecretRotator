"""The PVE cluster's nodes as the rotator calls them over SSH (Ssh.run): `sudo -n pvesh get
/cluster/resources --type vm --output-format json`, `sudo -n qm start <id>` and `sudo -n qm shutdown
<id> --timeout 300 --forceStop 1`, played on the VMs it holds. A VM's started and stopped hooks are
what its start and its shutdown do to what runs in it."""

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


@dataclass
class FakePve:
    vms: list[FakeVm]
    bound: int = PVE_BOUND
    calls: list[tuple[str, tuple[str, ...]]] = field(default_factory=list)  # (node, remote)
    down: set[str] = field(default_factory=set)  # nodes ssh does not reach
    refused: set[str] = field(default_factory=set)  # qm commands that fail: "start", "shutdown"

    def run(self, host, remote, stdin):
        remote = tuple(remote)
        assert stdin == "", stdin
        self.calls.append((host, remote))
        if host in self.down:
            return Ran(255, f"ssh: connect to host {host} port 22: No route to host\n")
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

    def commands(self):
        """(node, the qm verb) of every qm call, in order."""
        return [(node, remote[3]) for node, remote in self.calls if remote[2] == "qm"]
