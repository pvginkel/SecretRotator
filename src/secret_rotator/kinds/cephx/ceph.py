"""Ceph as the cephx kind reaches it (slice 061 ruling D2): over SSH as ansible to the PVE node one
of the cluster's Ceph VMs runs on (vmsteps.Pve), then `sudo -n qm guest exec` into that VM, whose
guest agent runs microceph's ceph CLI as root with the cluster's admin keyring; the rotator has no
Ceph identity of its own. sudo logs each command line on the PVE node, so a key never rides one: it
goes in on the command's standard input and comes back in its output, and no text this module
reports carries a key or a command's output."""

import base64
import datetime
import ipaddress
import json
import re
import secrets
import struct
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from secret_rotator.ansiblesteps import ANSIBLE_DIR, Ran
from secret_rotator.cluster import Ref
from secret_rotator.model import StepFailed
from secret_rotator.vmsteps import DEV_VM, RUNNING, Pve, said

CEPH = ("microceph.ceph",)
QM_BOUND = 60  # seconds qm waits for the command in the VM
GUEST_BOUND = 50  # seconds timeout(1) lets the command run in the VM, within QM_BOUND
TIMED_OUT = 124  # timeout(1)'s exit code
# A run of base64 as long as a cephx key: cut out of every text this module reports.
KEY_LIKE = re.compile(r"[A-Za-z0-9+/]{38,}={0,2}")
AES = 1  # CEPH_CRYPTO_AES: kernel clients before Linux 7.0 take no other cipher
# The proof: the key read from standard input reaches the CLI in CEPH_ARGS, never in an argv.
PROOF = 'read -r key && CEPH_ARGS="--key $key" exec "$@"'
# The host_vars whose network_devices name each host's addresses (Ansible terraform/prd/vms.tf
# reads the same lists).
HOST_VARS = ANSIBLE_DIR / "inventories" / "prd" / "host_vars"

# The two entities that take turns in a cluster's leaf: client.k8s, every reader's since the
# cluster was built, and the one the first rotation creates. The dev microceph role declares both
# (Ansible group_vars/ceph_dev.yml).
PAIR = ("k8s", "k8s-b")
# The dev CSI drivers' ExternalSecrets on dev, which read shared/dev/ceph-csi (HelmCharts
# configs/dev/ceph-csi-rbd/prd/manifests.yaml, configs/dev/ceph-csi-cephfs/prd/manifests.yaml).
DEV_READERS = (
    Ref("ceph-csi-cephfs-prd", "csi-cephfs-secret"),
    Ref("ceph-csi-cephfs-prd", "csi-cephfs-secret-user"),
    Ref("ceph-csi-rbd-prd", "csi-rbd-secret"),
    Ref("ceph-csi-rbd-prd", "csi-rbd-secret-user"),
)


@dataclass(frozen=True)
class Site:
    """A cluster's Ceph: its VMs by PVE name, the first that answers running the CLI; the two
    entities that take turns in its leaf; the VM that may be off it runs in (Step.vm); and the
    ExternalSecrets on that VM's own cluster that read the leaf, which the plan names because no
    read of that cluster finds them while it is off."""

    cluster: str
    guests: tuple[str, ...]
    pair: tuple[str, str] = PAIR
    vm: str | None = None
    readers: tuple[Ref, ...] = ()

    def other(self, user: str, leaf: str) -> str:
        """The entity of the pair the leaf's user is not."""
        if user not in self.pair:
            raise StepFailed(f"{leaf} holds user_id {user}, not one of {' and '.join(self.pair)}")
        (other,) = [u for u in self.pair if u != user]
        return other


PRD = Site("prd", ("srvceph1", "srvceph2", "srvceph3"))
DEV = Site("dev", (DEV_VM,), vm=DEV_VM, readers=DEV_READERS)


@dataclass(frozen=True)
class Entity:
    key: str = field(repr=False)
    caps: Mapping[str, str]


def redact(text: str) -> str:
    return KEY_LIKE.sub("<redacted>", text)


def new_key(now: datetime.datetime) -> str:
    """A new AES cephx key as ceph-authtool --gen-print-key encodes one: its cipher, when it was
    made, its length and 16 random bytes, in base64."""
    secret = secrets.token_bytes(16)
    created = struct.pack("<II", int(now.timestamp()), now.microsecond * 1000)
    raw = struct.pack("<H", AES) + created + struct.pack("<H", len(secret)) + secret
    return base64.b64encode(raw).decode()


def keyring(entity: str, key: str, caps: Mapping[str, str]) -> str:
    """The keyring `ceph auth import` takes: the entity, its key and its caps, which replace an
    existing entity's."""
    lines = [f"[{entity}]", f"\tkey = {key}"]
    lines += [f'\tcaps {service} = "{cap}"' for service, cap in sorted(caps.items())]
    return "\n".join(lines) + "\n"


def hosts(inventory: Path) -> dict[str, str]:
    """Each address a host's network_devices name, to the host's name."""
    found = {}
    for path in sorted(inventory.glob("*.yml")):
        doc = yaml.safe_load(path.read_text()) or {}
        for device in doc.get("network_devices") or []:
            for address in device.get("addresses") or []:
                found[str(ipaddress.ip_interface(address).ip)] = path.stem
    return found


def where(addresses: Iterable[str], inventory: Path) -> list[str]:
    """The hosts at the addresses, by name where the inventory names one, else by address."""
    known = hosts(inventory)
    return sorted({known.get(address, address) for address in addresses})


def _ip(addr: str) -> str:
    """The address of a Ceph `ip:port` or `[ip]:port`, normalized."""
    host = addr.rpartition(":")[0].strip("[]")
    try:
        return str(ipaddress.ip_address(host))
    except ValueError:
        return addr


def _answer(output: str) -> dict | None:
    """qm's JSON answer, which it prints pretty from a line of its own; ssh merges in what sudo
    and qm warn on stderr."""
    start = re.search(r"^\{", output, re.MULTILINE)
    if start is None:
        return None
    try:
        answer, _ = json.JSONDecoder().raw_decode(output, start.start())
    except ValueError:
        return None
    return answer if isinstance(answer, dict) else None


class Ceph:
    """A Site's Ceph, through the guest agent of the first of its VMs that runs and answers. A VM
    that is stopped, or that qm does not reach, is passed over; a command that ran and failed is
    the cluster's answer."""

    def __init__(self, site: Site, pve: Pve):
        self.site = site
        self.pve = pve

    def run(self, args: Sequence[str], stdin: str = "") -> str:
        """What `ceph <args>` prints on standard output; stdin: its standard input."""
        return self._exec((*CEPH, *args), stdin, " ".join(("ceph", *args)))

    def read(self, args: Sequence[str]):
        """What `ceph <args> --format json` prints, parsed."""
        what = " ".join(("ceph", *args))
        try:
            return json.loads(self.run((*args, "--format", "json")))
        except ValueError:
            raise StepFailed(f"{what} printed no JSON") from None

    def entity(self, entity: str) -> Entity:
        """The entity's key and caps, which Ceph must hold."""
        found = self.read(("auth", "get", entity))
        if not (isinstance(found, list) and len(found) == 1 and found[0].get("entity") == entity):
            raise StepFailed(f"ceph auth get {entity} printed no one entity {entity}")
        return Entity(found[0]["key"], found[0].get("caps") or {})

    def clients(self, entity: str) -> list[str]:
        """The addresses every monitor lists a client session of the entity from: one kernel
        client per node and entity, never a pod. Each monitor must answer."""
        addresses = set()
        for mon in self.read(("mon", "dump"))["mons"]:
            sessions = self.read(("tell", f"mon.{mon['name']}", "sessions"))
            if not isinstance(sessions, list):
                raise StepFailed(f"mon.{mon['name']} printed no list of sessions")
            addresses |= {
                _ip(s["socket_addr"]["addr"]) for s in sessions if s.get("entity_name") == entity
            }
        return sorted(addresses)

    def authenticates(self, entity: str, key: str) -> str:
        """The cluster's fsid, as the monitors answer a CLI that authenticated as the entity with
        the key, which goes in on standard input."""
        command = ("sh", "-c", PROOF, "sh", *CEPH, "--name", entity, "fsid")
        return self._exec(command, f"{key}\n", f"ceph --name {entity} fsid").strip()

    def unanswered(self) -> str | None:
        """Why the Ceph of a Site on a VM that may be off does not answer: no VM's guest agent runs
        the CLI, or the CLI does not reach the monitors. A Site on no such VM always answers."""
        if self.site.vm is None:
            return None
        try:
            self.run(("fsid",))
        except StepFailed as e:
            return e.error
        return None

    def _exec(self, command: tuple[str, ...], stdin: str, what: str) -> str:
        failures = []
        for name in self.site.guests:
            guest = self.pve.find(name)
            if guest.status != RUNNING:
                failures.append(f"{name} is {guest.status}")
                continue
            passing = ("--pass-stdin", "1") if stdin else ()
            remote = (
                *("sudo", "-n", "qm", "guest", "exec", str(guest.vmid)),
                *("--timeout", str(QM_BOUND), *passing, "--"),
                *("timeout", str(GUEST_BOUND), *command),
            )
            ran = self.pve.ssh.run(guest.node, remote, stdin)
            if ran.code != 0:
                quiet = Ran(ran.code, redact(ran.output))
                failures.append(
                    f"qm guest exec {guest.vmid} on {guest.node} {said(quiet, self.pve.ssh.bound)}"
                )
                continue
            return self._out(ran.output, f"{what} in {name}")
        raise StepFailed(f"no Ceph VM of {self.site.cluster} answers: {'; '.join(failures)}")

    @staticmethod
    def _out(output: str, what: str) -> str:
        """The command's standard output from qm's answer; its failure raised with its standard
        error alone."""
        answer = _answer(output)
        if answer is None:
            raise StepFailed(f"{what}: qm printed no answer of the guest agent", redact(output))
        if not answer.get("exited"):
            raise StepFailed(f"{what} did not finish within {QM_BOUND} s")
        err = redact(answer.get("err-data") or "").strip()
        code = answer.get("exitcode")
        if code == TIMED_OUT:
            raise StepFailed(f"{what} did not finish within {GUEST_BOUND} s", err)
        if code != 0:
            last = f": {err.splitlines()[-1]}" if err else ""
            raise StepFailed(f"{what} exited {code}{last}", err)
        if answer.get("out-truncated"):
            raise StepFailed(f"{what} printed more than the guest agent passes on")
        return answer.get("out-data") or ""
