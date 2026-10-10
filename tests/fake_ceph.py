"""A Ceph cluster as microceph's ceph CLI answers in one of its VMs, run as root by the guest agent
(FakeVm.agent) under timeout(1): `fsid`, `mon dump`, `tell mon.<name> sessions` and `auth get` with
--format json where the cephx kind asks for it, `auth import -i -` of a keyring on stdin, which
replaces an entity's key and caps or creates it; and the proof, `sh -c` reading a key from stdin
into CEPH_ARGS for a CLI that authenticates as an entity. Its keys are cephx AES keys as new_key
makes them; each monitor's sessions are what a test puts there."""

import base64
import datetime
import json
import struct

from secret_rotator.kinds.cephx.ceph import CEPH, GUEST_BOUND, PROOF, new_key

FSID = "6f0b3c1e-2d4a-4b8e-9c71-0a5d3e2f1b44"
TIMEOUT = ("timeout", str(GUEST_BOUND))
TIMED_OUT = (124, "", "")  # what timeout(1) answers for a CLI that does not reach the monitors
# client.k8s's caps on prd (live auth ls, 2026-10-10).
CAPS = {
    "mds": "allow rw",
    "mgr": "allow rw",
    "mon": "profile rbd, allow r",
    "osd": "profile rbd pool=k8s, allow rw tag cephfs data=cephfs",
}
MADE = datetime.datetime(2025, 3, 1, tzinfo=datetime.UTC)  # when the cluster's keys were made


def session(entity, address, port=51000):
    """A monitor's session of a client at the address, as `tell mon.<name> sessions` dumps one."""
    host = f"[{address}]" if ":" in address else address
    return {
        "name": "client.4242",
        "entity_name": entity,
        "addrs": {"addrvec": [{"type": "v1", "addr": f"{host}:0", "nonce": 7}]},
        "socket_addr": {"type": "v1", "addr": f"{host}:{port}", "nonce": 7},
        "con_type": "client",
        "open": True,
        "authenticated": True,
        "global_id": 4242,
        "remote_host": "",
    }


def aes(key):
    """Whether the key is a cephx AES key: cipher 1 and 16 bytes of secret."""
    raw = base64.b64decode(key)
    cipher, _, _, length = struct.unpack("<HIIH", raw[:12])
    return cipher == 1 and length == 16 and len(raw) == 28


def parse_keyring(text):
    """(entity, key, caps) of a keyring of one entity, its caps quoted."""
    entity, key, caps = None, None, {}
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("["):
            assert entity is None and line.endswith("]"), text
            entity = line[1:-1]
        elif line.startswith("key = "):
            key = line.removeprefix("key = ")
        else:
            service, sep, cap = line.removeprefix("caps ").partition(" = ")
            assert line.startswith("caps ") and sep and cap[0] == cap[-1] == '"', line
            caps[service] = cap[1:-1]
    return entity, key, caps


class FakeCeph:
    def __init__(self, mons=("srvceph1", "srvceph2", "srvceph3")):
        self.mons = list(mons)
        self.entities = {"client.admin": {"key": new_key(MADE), "caps": {"mon": "allow *"}}}
        self.sessions = {mon: [] for mon in self.mons}
        self.up = True  # whether the CLI reaches the monitors
        self.commands = []  # each command's ceph args, the proof's included
        # A ceph command's args -> the (exit code, stderr) it fails with, or None: it does not
        # finish within qm's timeout.
        self.fail = {}

    def entity(self, name, caps=None):
        """Creates the entity with a new key; its key."""
        self.entities[name] = {"key": new_key(MADE), "caps": dict(caps or CAPS)}
        return self.entities[name]["key"]

    def key(self, name):
        return self.entities[name]["key"]

    def caps(self, name):
        return self.entities[name]["caps"]

    def __call__(self, command, stdin):
        assert command[: len(TIMEOUT)] == TIMEOUT, command
        command = command[len(TIMEOUT) :]
        if command[0] == "sh":
            assert command[1:4] == ("-c", PROOF, "sh") and command[4 : 4 + len(CEPH)] == CEPH
            return self._prove(command[4 + len(CEPH) :], stdin)
        assert command[: len(CEPH)] == CEPH, command
        args = command[len(CEPH) :]
        self.commands.append(args)
        if args in self.fail:
            failed = self.fail[args]
            return None if failed is None else (failed[0], "", failed[1])
        if not self.up:
            return TIMED_OUT
        json_ = args[-2:] == ("--format", "json")
        args = args[:-2] if json_ else args
        if args == ("fsid",):
            return 0, f"{FSID}\n", ""
        if args == ("mon", "dump") and json_:
            mons = [{"rank": rank, "name": name} for rank, name in enumerate(self.mons)]
            return (
                0,
                json.dumps({"epoch": 3, "fsid": FSID, "mons": mons}),
                "dumped monmap epoch 3\n",
            )
        if args[0] == "tell" and args[2:] == ("sessions",) and json_:
            mon = args[1].removeprefix("mon.")
            assert mon in self.mons, args
            return 0, json.dumps(self.sessions[mon]), ""
        if args[:2] == ("auth", "get") and json_:
            (name,) = args[2:]
            if name not in self.entities:
                return 2, "", f"Error ENOENT: failed to find {name} in keyring\n"
            found = self.entities[name]
            return 0, json.dumps([{"entity": name, "key": found["key"], "caps": found["caps"]}]), ""
        assert args == ("auth", "import", "-i", "-"), args
        name, key, caps = parse_keyring(stdin)
        if not aes(key):
            return 22, "", "Error EINVAL: error decoding keyring\n"
        self.entities[name] = {"key": key, "caps": caps}
        return 0, "", "imported keyring\n"

    def _prove(self, args, stdin):
        assert args[0] == "--name" and args[2:] == ("fsid",), args
        self.commands.append(args)
        if args in self.fail:
            failed = self.fail[args]
            return None if failed is None else (failed[0], "", failed[1])
        if not self.up:
            return TIMED_OUT
        key = stdin.split("\n")[0]
        if self.entities.get(args[1], {}).get("key") != key:
            return 13, "", "[errno 13] RADOS permission denied (error connecting to the cluster)\n"
        return 0, f"{FSID}\n", ""
