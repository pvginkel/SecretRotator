"""The cephx kind (design §6; slice 061 rulings D1, D2, D3, plan review r1 F1 and F2) over the
seed's two leaves, whose user_id and user_key it rotates together: two entities, client.k8s and
client.k8s-b, take turns. Its plans, which sync every ExternalSecret that reads the leaf before the
proof whatever the leaf's activate, on dev also dev's own CSI readers through the dev write token,
and on dev start srvk8sdev first when it is off; the runs, which re-key the entity the leaf does not
hold with the caps of the one it holds and never touch the one it holds, through the guest agent of
the first Ceph VM that answers, no key on a command line or in a text; a rotation the monitors'
sessions block, which fails before any change naming the hosts; the failures and rollbacks; the
night; and the keys, keyrings and hosts it makes and reads."""

import base64
import copy
import dataclasses
import datetime
import itertools
import json
import struct
from pathlib import Path

import pytest
from fake_ceph import CAPS, FSID, MADE, FakeCeph, aes, session
from fake_cluster import FakeCluster, externalsecret, snapshot, token_in, token_secret
from fake_pve import FakePve, FakeVm
from fake_telegram import CHAT, FakeTelegram
from fake_telegram import TOKEN as BOT
from fake_youtrack import TAG, FakeYouTrack
from fake_youtrack import TOKEN as JEEVES
from plans import NOW, Recorder, client, fake_of, lock, put_state, run_state, state_of
from test_activation import ticking
from test_k8s_sa_token import CATALOG, DEV_ADDR, DEV_WRITE_KEY, NS, RW, RW_SECRET, kubeconfig

from secret_rotator import annotate as ann
from secret_rotator import nightly
from secret_rotator.audit import audit
from secret_rotator.cluster import Cluster, Ref
from secret_rotator.executor import Executor, Outcome
from secret_rotator.kinds.cephx import SITES, Cephx
from secret_rotator.kinds.cephx.ceph import (
    DEV,
    DEV_READERS,
    PAIR,
    PRD,
    Ceph,
    hosts,
    keyring,
    new_key,
    redact,
)
from secret_rotator.kinds.cephx.steps import KEY, USER, Mint
from secret_rotator.kube import Kube
from secret_rotator.model import Action, Finished, StepFailed, value_name
from secret_rotator.plan import PlanError, make
from secret_rotator.switches import Switches
from secret_rotator.telegram import Telegram
from secret_rotator.vmsteps import DEV_VM, PVE_NODES, Pve
from secret_rotator.youtrack import YouTrack

SEED = ann.load_seed(ann.DEFAULT_SEED)
KIND = "cephx"
PRD_LEAF, DEV_LEAF = "shared/prd/ceph-csi", "shared/dev/ceph-csi"
RGW = "shared/prd/ceph-rgw/s3"
KEYS = [USER, KEY]
MINT, PROVE = "cephx.mint", "cephx.prove"
DEV_START = f"vm.start:{DEV_VM}"
CSI = (
    "ceph-csi-cephfs-prd/csi-cephfs-secret",
    "ceph-csi-cephfs-prd/csi-cephfs-secret-user",
    "ceph-csi-rbd-prd/csi-rbd-secret",
    "ceph-csi-rbd-prd/csi-rbd-secret-user",
)
HOOK = "argocd-hooks/argocd-hook-credentials"
PRD_SYNCS = [f"eso.sync:{es}" for es in (HOOK, *CSI)]
DEV_SYNCS = [f"eso.sync:dev:{es}" for es in CSI]
CEPH_VMS = (("srvceph1", 113, "pve1"), ("srvceph2", 114, "pve2"), ("srvceph3", 115, "pve"))
BOOT = 180  # seconds srvk8sdev's apiserver and monitors take to answer once it is started
NOWHERE = Path(__file__).parent / "no-such-host-vars"
IMPORT = ("auth", "import", "-i", "-")


def seed_store():
    return ann.offline_store(Path(str(ann.DEFAULT_KEYS)), SEED, lambda line: None)


def csi_objects(leaf):
    """(resource, object) of the CSI drivers' four ExternalSecrets that read the leaf (prd live
    2026-10-10; dev as HelmCharts last deployed them)."""
    data = [(leaf, USER), (leaf, KEY)]
    return [
        ("externalsecrets", externalsecret(*es.split("/"), data=data, target=es.split("/")[1]))
        for es in CSI
    ]


def prd_objects():
    """The prd leaf's readers on prd: the CSI drivers' and the Argo CD PreSync hook's."""
    data = [(PRD_LEAF, USER), (PRD_LEAF, KEY), (RGW, "access_key_id"), (RGW, "secret_access_key")]
    hook = externalsecret(*HOOK.split("/"), data=data, target="creds")
    return [*csi_objects(PRD_LEAF), ("externalsecrets", hook)]


def host_vars(directory):
    """Host_vars of two k8s nodes, as Ansible's inventory names their backplane addresses, and of
    a host without network devices."""
    for name, n in (("srvk8s1", 27), ("srvk8s3", 29)):
        devices = [
            {"bridge": "vmbr0", "addresses": [f"10.1.0.{n}/16"]},
            {"bridge": "vmbr1", "addresses": [f"192.168.188.{n}/24", f"fdd0:6a51:35de::{n}/64"]},
        ]
        (directory / f"{name}.yml").write_text(json.dumps({"network_devices": devices}))
    (directory / "pve.yml").write_text("pve_node: pve\n")
    return directory


class World:
    """The seed's store on the fake OpenBao, the leaf holding client.k8s and its key; the cluster's
    Ceph, which holds client.k8s with prd's caps; prd's readers on the fake prd cluster; on prd the
    Ceph VMs on their PVE nodes, each with its guest agent; on dev srvk8sdev on pve, running unless
    off, whose monitors and apiserver answer BOOT seconds after a start; dev's CSI readers on the
    fake dev cluster, reached with the dev write token the KubeCoder catalog holds."""

    def __init__(self, leaf=PRD_LEAF, *, off=False, inventory=NOWHERE):
        self.leaf = leaf
        self.store = seed_store()
        self.cluster = FakeCluster(prd_objects())
        rw = token_secret(NS, RW_SECRET, RW)
        self.dev = FakeCluster([*csi_objects(DEV_LEAF), ("secrets", rw)], addr=DEV_ADDR)
        self.dev.static = set()
        self.dev_token = token_in(rw)
        if leaf == DEV_LEAF:
            self.ceph = FakeCeph((DEV_VM,))
            self.vm = FakeVm(DEV_VM, 919, "pve", "running", self.boot, self.halt, agent=self.ceph)
            vms = [self.vm]
        else:
            self.ceph = FakeCeph()
            vms = [FakeVm(n, i, node, "running", agent=self.ceph) for n, i, node in CEPH_VMS]
        self.key = self.ceph.entity("client.k8s")
        catalog = {k: f"SECRET-{CATALOG}-{k}" for k in sorted(self.store[CATALOG].keys)}
        catalog[DEV_WRITE_KEY] = kubeconfig({"dev": self.dev_token})
        data = {leaf: {USER: "k8s", KEY: self.key}, CATALOG: catalog}
        self.bao = fake_of(self.store, data)
        self.pve_cluster = FakePve(vms)
        self.pve = Pve(self.pve_cluster, sleep=self.dev.sleep, clock=self.dev.clock)
        self.kind = Cephx(self.connect, inventory=inventory)
        if off:
            self.vm.status = "stopped"
            self.halt()

    def boot(self):
        self.dev.later(BOOT, self.answer)

    def answer(self):
        self.dev.off = False
        self.ceph.up = True

    def halt(self):
        self.dev.off = True
        self.ceph.up = False

    def connect(self, token, server, ca):
        """dev's client, as reach.connect makes one."""
        return Kube(token, server, opener=self.dev, sleep=self.dev.sleep, clock=self.dev.clock)

    def held(self):
        data = self.bao.data(self.leaf)
        return data[USER], data[KEY]

    def plan(self, *, cluster=None, store=None):
        return make(
            {KIND: self.kind},
            self.leaf,
            KIND,
            KEYS,
            audit(store or self.store),
            cluster or Cluster(self.cluster.kube()),
            pve=self.pve,
        )

    def executor(self, *, day=0):
        self.recorder = Recorder()
        tick = ticking()
        return Executor(
            client(self.bao),
            self.plan(),
            self.recorder,
            lock(self.bao),
            state=run_state(self.bao),
            dry_run=False,
            clock=lambda: tick() + datetime.timedelta(days=day),
        )

    def run(self, *, day=0):
        return self.executor(day=day).run()

    def failure(self):
        (failure,) = self.recorder.failures()
        return failure

    def texts(self):
        """Every detail, error and technical text the run reported."""
        return [
            getattr(e, name, "")
            for e in self.recorder.events
            for name in ("detail", "error", "technical")
        ]

    def secrets(self):
        """Every key Ceph has held in the run, and the leaf's."""
        return {e["key"] for e in self.ceph.entities.values()} | {self.key, self.held()[1]}

    def argvs(self):
        """Every command line the rotator ran on a PVE node, which sudo logs there."""
        return [" ".join(remote) for _, remote in self.pve_cluster.calls]

    def finished(self, step, action=Action.RUN):
        (*_, last) = (
            e.detail
            for e in self.recorder.events
            if isinstance(e, Finished) and e.ok and e.step.id == step and e.action is action
        )
        return last

    def imports(self):
        return self.ceph.commands.count(IMPORT)


def ids(plan):
    return [s.id for s in plan.steps]


class TestTheSeed:
    def test_each_leaf_rotates_its_user_id_and_user_key_together_with_no_args(self):
        entries = audit(seed_store()).entries
        found = {
            (leaf, key): (entry.args, entry.interval, [str(a) for a in entry.activate])
            for leaf, by_key in entries.items()
            for key, entry in by_key.items()
            if entry.kind == KIND
        }
        assert found == {
            (leaf, key): ({}, 365, []) for leaf in (DEV_LEAF, PRD_LEAF) for key in KEYS
        }

    def test_args_it_cannot_use_are_named(self):
        assert Cephx().args_problems({"pair": "k8s"}) == ["pair: cephx takes no args"]

    def test_each_cluster_s_pair_vms_and_dev_s_own_readers(self):
        assert SITES == {PRD_LEAF: PRD, DEV_LEAF: DEV}
        assert PAIR == ("k8s", "k8s-b") and PRD.pair == DEV.pair == PAIR
        assert (PRD.guests, PRD.vm, PRD.readers) == (("srvceph1", "srvceph2", "srvceph3"), None, ())
        assert (DEV.guests, DEV.vm) == ((DEV_VM,), DEV_VM)
        assert DEV.readers == DEV_READERS == tuple(Ref(*es.split("/")) for es in CSI)


class TestTheKeys:
    def test_a_new_key_is_an_aes_cephx_key_made_now_of_16_random_bytes(self):
        at = datetime.datetime(2026, 10, 5, 4, 30, 1, 250000, tzinfo=datetime.UTC)
        key = new_key(at)
        raw = base64.b64decode(key)
        assert struct.unpack("<HIIH", raw[:12]) == (1, int(at.timestamp()), 250000000, 16)
        assert len(raw) == 28 and len(key) == 40 and key.startswith("AQ")
        assert new_key(at) != key and aes(key)

    def test_a_keyring_holds_the_entity_its_key_and_its_caps_quoted(self):
        assert keyring("client.k8s-b", "AQKEY==", {"osd": "profile rbd", "mon": "allow r"}) == (
            '[client.k8s-b]\n\tkey = AQKEY==\n\tcaps mon = "allow r"\n\tcaps osd = "profile rbd"\n'
        )

    def test_a_text_s_keys_are_cut_out(self):
        key = new_key(MADE)
        assert redact(f'"key": "{key}", fsid {FSID}') == f'"key": "<redacted>", fsid {FSID}'

    def test_the_inventory_names_each_host_s_addresses(self, tmp_path):
        found = hosts(host_vars(tmp_path))
        assert found["192.168.188.27"] == found["fdd0:6a51:35de::27"] == "srvk8s1"
        assert found["10.1.0.29"] == "srvk8s3" and len(found) == 6
        assert hosts(NOWHERE) == {}


class TestThePlans:
    def test_prd_syncs_the_csi_drivers_and_the_hook_before_the_proof_and_ends_nothing(self):
        assert ids(World().plan()) == [MINT, "kv.write", *PRD_SYNCS, PROVE, "kv.stamp"]

    def test_the_readers_sync_though_the_leaf_s_activate_is_none(self):
        world = World()
        for key in KEYS:
            assert [str(a) for a in audit(world.store).entries[PRD_LEAF][key].activate] == []

    def test_dev_starts_srvk8sdev_first_and_syncs_dev_s_own_readers(self):
        plan = World(DEV_LEAF).plan()
        assert ids(plan) == [DEV_START, MINT, "kv.write", *DEV_SYNCS, PROVE, "kv.stamp"]
        assert [s.vm for s in plan.steps] == [None, DEV_VM, None, *[DEV_VM] * 4, DEV_VM, None]

    def test_prd_s_steps_name_no_vm_and_always_answer(self):
        world = World()
        world.pve_cluster.vms = []
        plan = world.plan()
        assert all(s.vm is None for s in plan.steps)
        assert not any(s.unanswered(client(world.bao)) for s in plan.steps)
        assert world.pve_cluster.calls == []

    def test_against_a_snapshot_it_plans_without_reaching_ceph_pve_or_dev(self, tmp_path):
        path = tmp_path / "snapshot.json"
        path.write_text(json.dumps(snapshot(prd_objects())))
        for leaf in (PRD_LEAF, DEV_LEAF):
            world = World(leaf, off=True) if leaf == DEV_LEAF else World(leaf)
            assert ids(world.plan(cluster=Cluster.of_snapshot(path))) == ids(world.plan())
            assert world.pve_cluster.calls == [] and world.ceph.commands == []
            assert world.dev.requests == []

    def test_a_plan_of_one_of_the_two_keys_is_refused(self):
        world = World()
        with pytest.raises(PlanError, match="rotates user_id and user_key together"):
            make({KIND: world.kind}, PRD_LEAF, KIND, [KEY], audit(world.store))

    def test_a_leaf_of_no_cluster_it_reaches_has_no_plan(self):
        world = World()
        leaf = "shared/test/ceph-csi"
        world.store[leaf] = dataclasses.replace(copy.deepcopy(world.store[PRD_LEAF]), path=leaf)
        with pytest.raises(PlanError, match=f"^{leaf}: not the leaf of a Ceph cluster"):
            make({KIND: world.kind}, leaf, KIND, KEYS, audit(world.store))

    def test_the_steps_and_the_description(self):
        plan = World().plan()
        mint, prove = plan.steps[0], plan.steps[-2]
        assert (mint.mutates, mint.activator, mint.undo is not None) == (True, False, True)
        assert (prove.mutates, prove.silent) == (False, True)
        assert mint.title == "give the idle Ceph client on prd a new key"
        assert prove.title == "prove the new key against Ceph"
        assert not plan.needs_operator and plan.ask == ""
        assert plan.description == (
            "The leaf's Ceph client takes turns between client.k8s and client.k8s-b: the tool "
            "gives the one on prd the leaf does not hold a new key and the caps of the one it "
            "holds, once no client uses it, through the Ceph VMs' guest agent, and writes it to "
            "the leaf. Once every ExternalSecret that reads the leaf has synced, it proves the new "
            "key; it ends no key."
        )
        dev = World(DEV_LEAF).plan()
        assert dev.steps[3].title == f"sync ExternalSecret {CSI[0]} on dev"
        assert dev.steps[3].activator and dev.steps[3].mutates
        assert dev.description.endswith(
            "it ends no key. It syncs dev's own readers with the dev write token the catalog "
            "holds. It starts srvk8sdev first when it is off, and shuts it down again after."
        )
        assert Cephx().credential(plan.target) == "Ceph client key"


class TestTheRuns:
    def test_the_first_rotation_creates_client_k8s_b_with_k8s_s_caps_and_moves_the_leaf(self):
        world = World()
        assert world.run() is Outcome.DONE
        user, key = world.held()
        assert user == "k8s-b" and key == world.ceph.key("client.k8s-b") and aes(key)
        assert world.ceph.caps("client.k8s-b") == CAPS
        assert world.ceph.key("client.k8s") == world.key
        assert world.cluster.syncs == 5
        assert state_of(world.bao, PRD_LEAF).stamps == {k: "2026-10-05" for k in KEYS}
        assert world.finished(MINT) == "client.k8s-b re-keyed with client.k8s's caps"
        assert world.finished(PROVE) == "prd's monitors take client.k8s-b's new key"

    def test_no_key_rides_a_command_line_or_a_text(self):
        world = World()
        assert world.run() is Outcome.DONE
        assert not any(k in t for t in world.texts() for k in world.secrets())
        assert not any(k in argv for argv in world.argvs() for k in world.secrets())
        new = world.held()[1]
        assert [s for s in world.pve_cluster.stdins if new in s] == [
            keyring("client.k8s-b", new, CAPS),
            f"{new}\n",
        ]

    def test_the_next_rotation_re_keys_client_k8s_and_never_the_entity_the_leaf_holds(self):
        world = World()
        assert world.run() is Outcome.DONE
        first = world.held()[1]
        assert world.run(day=365) is Outcome.DONE
        user, key = world.held()
        assert user == "k8s" and key == world.ceph.key("client.k8s") != world.key
        assert world.ceph.key("client.k8s-b") == first
        assert world.finished(MINT) == "client.k8s re-keyed with client.k8s-b's caps"

    def test_the_idle_entity_takes_the_caps_the_active_one_has_now(self):
        world = World()
        world.ceph.entity("client.k8s-b", caps={"mon": "allow r"})
        world.ceph.caps("client.k8s")["mgr"] = "allow r"
        assert world.run() is Outcome.DONE
        assert world.ceph.caps("client.k8s-b") == CAPS | {"mgr": "allow r"}

    def test_clients_of_the_entity_the_leaf_holds_do_not_block(self):
        world = World()
        for mon in world.ceph.mons:
            world.ceph.sessions[mon] = [session("client.k8s", f"192.168.188.{n}") for n in (27, 28)]
        assert world.run() is Outcome.DONE

    def test_clients_of_the_idle_entity_block_it_before_any_change_naming_their_hosts(
        self, tmp_path
    ):
        world = World(inventory=host_vars(tmp_path))
        assert world.run() is Outcome.DONE
        world.ceph.sessions["srvceph2"] = [
            session("client.k8s", "192.168.188.27"),
            session("client.k8s", "fdd0:6a51:35de::29"),
            session("client.k8s-b", "192.168.188.28"),
        ]
        world.ceph.sessions["srvceph3"] = [session("client.k8s", "192.168.188.30")]
        held, keys = world.held(), copy.deepcopy(world.ceph.entities)
        executor = world.executor(day=365)
        assert executor.run() is Outcome.FAILED
        assert world.failure().step.id == MINT
        assert world.failure().error == (
            "the monitors list Ceph clients of client.k8s, which the plan would re-key, on "
            "192.168.188.30, srvk8s1, srvk8s3: restart the Ceph-backed pods there, or let the next "
            "update round that reboots them move them"
        )
        assert executor.abort() is Outcome.CANCELLED
        assert world.held() == held and world.ceph.entities == keys
        assert world.imports() == 1

    def test_a_monitor_that_does_not_answer_stops_it_before_any_change(self):
        world = World()
        world.ceph.fail[("tell", "mon.srvceph3", "sessions", "--format", "json")] = (
            110,
            "Error ETIMEDOUT: mon.srvceph3 does not answer",
        )
        executor = world.executor()
        assert executor.run() is Outcome.FAILED
        assert world.failure().error == (
            "ceph tell mon.srvceph3 sessions --format json in srvceph1 exited 110: Error "
            "ETIMEDOUT: mon.srvceph3 does not answer"
        )
        assert executor.abort() is Outcome.CANCELLED
        assert "client.k8s-b" not in world.ceph.entities and world.held() == ("k8s", world.key)

    @pytest.mark.parametrize(
        ("data", "error"),
        [
            (
                {USER: "k8s", KEY: "AQ" + "A" * 38},
                f"client.k8s on prd does not hold the key {PRD_LEAF} holds",
            ),
            (
                {USER: "admin", KEY: "KEY"},
                f"{PRD_LEAF} holds user_id admin, not one of k8s and k8s-b",
            ),
            ({USER: "k8s", KEY: ""}, f"{PRD_LEAF} holds no user_key"),
        ],
    )
    def test_a_leaf_ceph_does_not_hold_for_the_pair_stops_it_before_any_change(self, data, error):
        world = World()
        world.bao.new_version(PRD_LEAF, data)
        executor = world.executor()
        assert executor.run() is Outcome.FAILED
        assert world.failure().error == error
        assert executor.abort() is Outcome.CANCELLED
        assert world.imports() == 0

    def test_caps_a_keyring_cannot_carry_stop_it_before_any_change(self):
        world = World()
        world.ceph.caps("client.k8s")["mon"] = 'allow command "osd blocklist"'
        executor = world.executor()
        assert executor.run() is Outcome.FAILED
        assert world.failure().error == (
            "client.k8s's mon caps hold a quote or a line break, which a keyring cannot carry"
        )
        assert executor.abort() is Outcome.CANCELLED

    def test_a_ceph_vm_that_does_not_answer_is_passed_over(self):
        world = World()
        srvceph1, srvceph2, _ = world.pve_cluster.vms
        srvceph1.status = "stopped"
        srvceph2.agent = None
        assert world.run() is Outcome.DONE
        execs = {node for node, remote in world.pve_cluster.calls if remote[2:4] == ("qm", "guest")}
        assert execs == {"pve2", "pve"}  # srvceph1 stopped is not tried; srvceph2 is, in vain

    def test_no_ceph_vm_answering_stops_it_before_any_change(self):
        world = World()
        srvceph1, srvceph2, srvceph3 = world.pve_cluster.vms
        srvceph1.status = "stopped"
        srvceph2.agent = None
        world.pve_cluster.down = {"pve"}
        executor = world.executor()
        assert executor.run() is Outcome.FAILED
        assert world.failure().error == (
            "no Ceph VM of prd answers: srvceph1 is stopped; qm guest exec 114 on pve2 exited "
            "255: QEMU guest agent is not running; qm guest exec 115 on pve exited 255: ssh: "
            "connect to host pve port 22: No route to host"
        )
        assert executor.abort() is Outcome.CANCELLED

    @pytest.mark.parametrize(
        ("failed", "error"),
        [
            (None, "ceph auth import -i - in srvceph1 did not finish within 60 s"),
            ((124, ""), "ceph auth import -i - in srvceph1 did not finish within 50 s"),
            (
                (22, "Error EINVAL: bad"),
                "ceph auth import -i - in srvceph1 exited 22: Error EINVAL: bad",
            ),
        ],
    )
    def test_an_import_that_fails_landed_and_rolls_back_to_the_entity_the_leaf_held(
        self, failed, error
    ):
        world = World()
        world.ceph.fail[IMPORT] = failed
        executor = world.executor()
        assert executor.run() is Outcome.FAILED
        assert world.failure().error == error
        assert executor.abort() is Outcome.ROLLED_BACK
        assert world.held() == ("k8s", world.key)
        assert world.finished(MINT, Action.UNDO) == (
            "client.k8s-b keeps its new key, which no reader holds once the leaf is put back"
        )

    def test_a_new_key_ceph_does_not_take_rolls_back_to_the_entity_the_leaf_held(self):
        world = World()
        world.ceph.fail[("--name", "client.k8s-b", "fsid")] = (
            13,
            "[errno 13] RADOS permission denied (error connecting to the cluster)",
        )
        executor = world.executor()
        assert executor.run() is Outcome.FAILED
        assert world.failure().step.id == PROVE
        assert world.failure().error == (
            "ceph --name client.k8s-b fsid in srvceph1 exited 13: [errno 13] RADOS permission "
            "denied (error connecting to the cluster)"
        )
        synced = world.cluster.syncs
        assert executor.abort() is Outcome.ROLLED_BACK
        assert world.held() == ("k8s", world.key)
        assert world.cluster.syncs == synced + 5
        assert world.ceph.key("client.k8s") == world.key

    def test_an_error_carrying_a_key_is_cut(self):
        world = World()
        world.ceph.fail[("auth", "get", "client.k8s", "--format", "json")] = (
            1,
            f"unexpected keyring entry key = {world.key}",
        )
        executor = world.executor()
        assert executor.run() is Outcome.FAILED
        assert world.failure().error.endswith("exited 1: unexpected keyring entry key = <redacted>")
        assert not any(k in t for t in world.texts() for k in world.secrets())


class TestDev:
    def test_with_srvk8sdev_off_the_plan_starts_it_rotates_syncs_dev_s_readers_and_stops_it(self):
        world = World(DEV_LEAF, off=True)
        assert world.run() is Outcome.DONE
        assert world.pve_cluster.commands() == [("pve", "start"), ("pve", "shutdown")]
        assert world.vm.status == "stopped" and world.dev.off
        user, key = world.held()
        assert (user, key) == ("k8s-b", world.ceph.key("client.k8s-b")) and aes(key)
        assert world.ceph.caps("client.k8s-b") == CAPS
        assert world.dev.syncs == 4 and world.cluster.syncs == 0
        patched = [path for path, _ in world.dev.patches()]
        assert patched == [Ref(*es.split("/")).path for es in CSI]
        assert set(world.dev.bearers) == {world.dev_token}
        assert world.dev.now >= BOOT
        assert not any(k in t for t in world.texts() for k in world.secrets())

    def test_srvk8sdev_is_reached_through_pve_never_over_ssh_to_itself(self):
        world = World(DEV_LEAF, off=True)
        assert world.run() is Outcome.DONE
        assert {node for node, _ in world.pve_cluster.calls} <= set(PVE_NODES)
        assert not any(k in argv for argv in world.argvs() for k in world.secrets())

    def test_a_running_srvk8sdev_is_left_running(self):
        world = World(DEV_LEAF)
        assert world.run() is Outcome.DONE
        assert world.pve_cluster.commands() == [] and world.vm.status == "running"

    def test_a_dev_plan_rolled_back_has_srvk8sdev_up_to_sync_dev_s_readers_again(self):
        world = World(DEV_LEAF, off=True)
        world.ceph.fail[("--name", "client.k8s-b", "fsid")] = (13, "[errno 13] denied")
        executor = world.executor()
        assert executor.run() is Outcome.FAILED
        assert world.vm.status == "stopped"
        assert executor.abort() is Outcome.ROLLED_BACK
        assert world.held() == ("k8s", world.key)
        assert world.dev.syncs == 8
        assert world.pve_cluster.commands() == [("pve", "start"), ("pve", "shutdown")] * 2
        assert world.vm.status == "stopped"

    def test_dev_s_ceph_does_not_answer_while_off_or_booting_and_answers_once_up(self):
        world = World(DEV_LEAF, off=True)
        ceph = Ceph(DEV, world.pve)
        assert ceph.unanswered() == ("no Ceph VM of dev answers: srvk8sdev is stopped")
        world.vm.status = "running"
        assert ceph.unanswered() == "ceph fsid in srvk8sdev did not finish within 50 s"
        world.answer()
        assert ceph.unanswered() is None
        assert Ceph(PRD, world.pve).unanswered() is None


class Ctx:
    def __init__(self, bao, staged=None):
        self.bao = client(bao)
        self.now = NOW
        self.values = dict(staged or {})

    def progress(self, detail):
        pass

    def stage(self, name, value):
        self.values[name] = value

    def staged(self, name):
        return self.values.get(name)


class TestTheSteps:
    def mint(self, world):
        return Mint(Ceph(PRD, world.pve), PRD_LEAF, NOWHERE)

    def test_a_mint_stages_the_new_key_then_the_idle_entity(self):
        world = World()
        ctx = Ctx(world.bao)
        self.mint(world).run(ctx)
        assert ctx.values == {
            value_name(KEY): world.ceph.key("client.k8s-b"),
            value_name(USER): "k8s-b",
        }
        assert list(ctx.values)[-1] == value_name(USER)

    def test_a_re_run_imports_the_key_staged_again(self):
        world = World()
        ctx = Ctx(world.bao)
        mint = self.mint(world)
        mint.run(ctx)
        before = dict(ctx.values)
        mint.run(ctx)
        assert ctx.values == before and world.imports() == 2
        assert world.ceph.key("client.k8s-b") == before[value_name(KEY)]

    def test_an_import_ceph_did_not_take_fails(self):
        world = World()
        world.ceph.fail[IMPORT] = (0, "")
        with pytest.raises(
            StepFailed,
            match="^ceph auth get client.k8s-b --format json in srvceph1 exited 2: Error ENOENT",
        ):
            self.mint(world).run(Ctx(world.bao))
        world.ceph.entity("client.k8s-b", caps={"mon": "allow r"})
        with pytest.raises(StepFailed, match="^client.k8s-b on prd does not hold the new key$"):
            self.mint(world).run(Ctx(world.bao))

    def test_a_mint_whose_leaf_changed_under_the_plan_fails(self):
        world = World()
        ctx = Ctx(world.bao, {value_name(USER): "k8s"})
        with pytest.raises(StepFailed, match=f"^the plan re-keys client.k8s, which {PRD_LEAF}"):
            self.mint(world).run(ctx)


class Nights:
    """Nightly runs over a world's store, the kind alone enabled, the other cluster's leaf rotated
    today."""

    def __init__(self, world):
        self.world = world
        self.youtrack = FakeYouTrack()
        self.telegram = FakeTelegram()
        self.lines = []
        self.ticks = itertools.count()
        other = DEV_LEAF if world.leaf == PRD_LEAF else PRD_LEAF
        put_state(world.bao, other, stamps={k: NOW.date().isoformat() for k in KEYS})
        world.bao.new_version("rotator/youtrack", {"token": JEEVES})
        world.bao.new_version("rotator/telegram", {"token": BOT})

    def __call__(self, days=0):
        self.lines.clear()
        now = NOW + datetime.timedelta(days=days)
        switches = Switches(
            dry_run=False,
            paused=False,
            kinds_enabled=frozenset({KIND}),
            max_rotations_per_run=10,
            card_tag=TAG,
            telegram_chat_id=CHAT,
        )
        return nightly.run(
            client(self.world.bao),
            Cluster(self.world.cluster.kube()),
            {KIND: self.world.kind},
            switches,
            youtrack=lambda token: YouTrack(token, opener=self.youtrack),
            telegram=lambda token, chat: Telegram(token, chat, opener=self.telegram),
            out=self.lines.append,
            holder="run on srviac, pid 7",
            now=lambda: now + datetime.timedelta(microseconds=next(self.ticks)),
            pve=self.world.pve,
        )


class TestTheNight:
    def test_a_blocked_rotation_fails_the_night_and_telegram_names_the_hosts(self, tmp_path):
        world = World(inventory=host_vars(tmp_path))
        world.ceph.sessions["srvceph1"] = [session("client.k8s-b", "192.168.188.27")]
        nights = Nights(world)
        assert nights() == 0
        failed = [m for m in nights.telegram.messages if "client.k8s-b" in m]
        assert len(failed) == 1 and "on srvk8s1: restart the Ceph-backed pods there" in failed[0]
        assert "Failed 1:" in nights.telegram.messages[-1]
        assert world.held() == ("k8s", world.key) and world.imports() == 0
        assert state_of(world.bao, PRD_LEAF).failed_nights == 1

    def test_while_srvk8sdev_is_off_the_night_starts_it_rotates_dev_and_shuts_it_down(self):
        world = World(DEV_LEAF, off=True)
        nights = Nights(world)
        assert nights() == 0
        assert world.held()[0] == "k8s-b" and world.dev.syncs == 4
        assert world.pve_cluster.commands() == [("pve", "start"), ("pve", "shutdown")]
        assert world.vm.status == "stopped"
        assert "Rotated 1:" in nights.telegram.messages[-1]
