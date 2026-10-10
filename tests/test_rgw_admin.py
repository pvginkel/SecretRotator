"""The rgw-admin kind (design §6; slice 061 rulings D2, Q1) over the seed's two leaves, whose
access_key_id and secret_access_key it rotates together; its plans, which sync every ExternalSecret
that reads the leaf before the proof and the removal, whatever the leaf's activate, and on dev
start srvk8sdev first when it is off; the runs, in which the admin user's key adds its successor
through RGW's admin API, and the plan removes the key the leaf held and no other; their failures
and rollbacks; and the SigV4 signature every request carries."""

import copy
import dataclasses
import datetime
import json
from pathlib import Path

import pytest
from fake_cluster import FakeCluster, externalsecret, snapshot
from fake_pve import FakePve, FakeVm
from fake_rgw import FakeRgw
from fixtures import edit
from plans import NOW, Recorder, client, fake_of, lock, run_state, state_of
from test_activation import ticking

from secret_rotator import annotate as ann
from secret_rotator.audit import audit
from secret_rotator.cluster import Cluster
from secret_rotator.executor import AbortRefused, Executor, Outcome
from secret_rotator.kinds.rgw_admin import SITES, RgwAdmin
from secret_rotator.kinds.rgw_admin.rgw import (
    CACHE_POLL,
    DEV,
    PRD,
    Gateway,
    Key,
    authorization,
    canonical_query,
)
from secret_rotator.kinds.rgw_admin.steps import BEFORE, OLD, Delete, Mint, Prove
from secret_rotator.model import Action, Finished, StepFailed, value_name
from secret_rotator.plan import PlanError, make
from secret_rotator.vmsteps import DEV_VM, Pve

SEED = ann.load_seed(ann.DEFAULT_SEED)
KIND = "rgw-admin"
PRD_LEAF, DEV_LEAF = "shared/prd/ceph-rgw/s3", "shared/dev/ceph-rgw/s3"
KEYS = ["access_key_id", "secret_access_key"]
CSI = "shared/prd/ceph-csi"
ES = "argocd-hooks/argocd-hook-credentials"
MINT, PROVE, DELETE = "rgw_admin.mint", "rgw_admin.prove", "rgw_admin.delete"
DEV_START = f"vm.start:{DEV_VM}"
UID = "k8s"
LOST = ConnectionResetError(104, "Connection reset by peer")
ADD, REMOVE, INFO = ("PUT", "user?key"), ("DELETE", "user?key"), ("GET", "user")


def seed_store():
    return ann.offline_store(Path(str(ann.DEFAULT_KEYS)), SEED, lambda line: None)


def hook_objects():
    """(resource, object) of the readers of the prd leaf, as prd holds them (2026-10-10): the
    Argo CD PreSync hook's credentials, which its Job reads at each run. Nothing reads the dev
    leaf."""
    data = [(CSI, "user_id"), (CSI, "user_key"), (PRD_LEAF, KEYS[0]), (PRD_LEAF, KEYS[1])]
    hook = externalsecret("argocd-hooks", "argocd-hook-credentials", data=data, target="creds")
    return [("externalsecrets", hook)]


class World:
    """The seed's store on the fake OpenBao; the cluster's RGW, where the leaf holds a key of the
    admin user k8s, which has one more key, and another user has a key of its own; the leaf's
    readers on the fake cluster; and on dev srvk8sdev, running unless off, on pve."""

    def __init__(self, leaf=PRD_LEAF, *, off=False):
        self.leaf = leaf
        self.site = SITES[leaf]
        self.store = seed_store()
        self.rgw = FakeRgw(self.site.endpoints)
        self.made = self.rgw.user(UID)
        self.spare = self.rgw.key(UID)
        self.other = self.rgw.user("app", caps="")
        self.initial = self.rgw.keys(UID)
        data = {leaf: {KEYS[0]: self.made.access, KEYS[1]: self.made.secret}}
        self.bao = fake_of(self.store, data)
        self.cluster = FakeCluster(hook_objects())
        self.kind = RgwAdmin(self.rgw, sleep=self.rgw.sleep, clock=self.rgw.clock)
        status = "stopped" if off else "running"
        self.vm = FakeVm(
            DEV_VM, 919, "pve", status, started=self.rgw.started, stopped=self.rgw.stopped
        )
        self.rgw.off = off
        self.pve_cluster = FakePve([self.vm])
        self.pve = Pve(self.pve_cluster, sleep=self.rgw.sleep, clock=self.rgw.clock)

    def held(self):
        """The key the leaf holds."""
        data = self.bao.data(self.leaf)
        return Key(data[KEYS[0]], data[KEYS[1]])

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
        """Every secret key RGW has held in the run, and the leaf's."""
        held = {s for u in self.rgw.users.values() for s in u["keys"].values()}
        return held | {self.made.secret, self.spare.secret, self.held().secret}

    def finished(self, step, action=Action.RUN):
        """The detail of the step's last line of the action that finished ok."""
        (*_, last) = (
            e.detail
            for e in self.recorder.events
            if isinstance(e, Finished) and e.ok and e.step.id == step and e.action is action
        )
        return last

    def new(self):
        """The access key the user has now that it did not have at the start."""
        (new,) = self.rgw.keys(UID) - self.initial
        return new


def ids(plan):
    return [s.id for s in plan.steps]


class TestTheSeed:
    def test_each_leaf_rotates_its_access_key_id_and_secret_together_with_no_args(self):
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
        assert RgwAdmin().args_problems({"uid": "k8s"}) == ["uid: rgw-admin takes no args"]

    def test_it_reaches_each_cluster_s_rgw_on_the_storage_backplane_as_the_admin_user_k8s(self):
        # prd: srvceph1-3's backplane addresses, read live 2026-10-10; dev: Ansible
        # host_vars/srvk8sdev.yml's vmbr1 address and group_vars/ceph_dev.yml's microceph_rgw_port.
        assert SITES == {PRD_LEAF: PRD, DEV_LEAF: DEV}
        assert PRD.endpoints == tuple(f"http://192.168.188.{n}:7480" for n in (24, 25, 26))
        assert DEV.endpoints == ("http://192.168.188.17",)
        assert (PRD.uid, PRD.vm, DEV.uid, DEV.vm) == (UID, None, UID, DEV_VM)


class TestTheSignature:
    """AWS Signature Version 4, checked against the examples AWS publishes: S3's (us-east-1, the
    secret wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY, 2013-05-24) and IAM's."""

    S3_KEY = Key("AKIAIOSFODNN7EXAMPLE", "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY")
    S3_AT = datetime.datetime(2013, 5, 24, tzinfo=datetime.UTC)
    EMPTY = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    HOST = "examplebucket.s3.amazonaws.com"

    def s3(self, path, query, **headers):
        signed = {
            "Host": self.HOST,
            **headers,
            "x-amz-content-sha256": self.EMPTY,
            "x-amz-date": "20130524T000000Z",
        }
        return authorization(
            "GET", path, query, signed, self.EMPTY, self.S3_KEY, self.S3_AT, region="us-east-1"
        )

    @pytest.mark.parametrize(
        ("path", "query", "headers", "signed", "signature"),
        [
            (
                "/test.txt",
                [],
                {"Range": "bytes=0-9"},
                "host;range;x-amz-content-sha256;x-amz-date",
                "f0e8bdb87c964420e857bd35b5d6ed310bd44f0170aba48dd91039c6036bdb41",
            ),
            (
                "/",
                [("lifecycle", "")],
                {},
                "host;x-amz-content-sha256;x-amz-date",
                "fea454ca298b7da1c68078a5d1bdbfbbe0d65c699e0f91ac7a200a0136783543",
            ),
            (
                "/",
                [("prefix", "J"), ("max-keys", "2")],
                {},
                "host;x-amz-content-sha256;x-amz-date",
                "34b48302e7b5fa45bde8084f4b7868a86f0a534bc59db6670ed5711ef69dc6f7",
            ),
        ],
    )
    def test_it_signs_aws_s3_examples_as_aws_does(self, path, query, headers, signed, signature):
        assert self.s3(path, query, **headers) == (
            f"AWS4-HMAC-SHA256 Credential=AKIAIOSFODNN7EXAMPLE/20130524/us-east-1/s3/"
            f"aws4_request, SignedHeaders={signed}, Signature={signature}"
        )

    def test_it_signs_aws_s_iam_example_as_aws_does(self):
        key = Key("AKIDEXAMPLE", "wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY")
        headers = {
            "Content-Type": "application/x-www-form-urlencoded; charset=utf-8",
            "Host": "iam.amazonaws.com",
            "X-Amz-Date": "20150830T123600Z",
        }
        at = datetime.datetime(2015, 8, 30, 12, 36, tzinfo=datetime.UTC)
        query = [("Action", "ListUsers"), ("Version", "2010-05-08")]
        assert authorization(
            "GET", "/", query, headers, self.EMPTY, key, at, region="us-east-1", service="iam"
        ).endswith("Signature=5d672d79c15b13162d9279b0855cfba6789a8edb4c82c400e06b5924a6f2b5d7")

    def test_the_query_is_sent_as_it_is_signed_sorted_and_encoded(self):
        query = [("uid", "k8s"), ("key", ""), ("access-key", "A B/C"), ("format", "json")]
        assert canonical_query(query) == "access-key=A%20B%2FC&format=json&key=&uid=k8s"


class TestThePlans:
    def test_prd_syncs_the_hook_s_credentials_before_the_proof_and_the_removal(self):
        assert ids(World().plan()) == [
            MINT,
            "kv.write",
            f"eso.sync:{ES}",
            PROVE,
            DELETE,
            "kv.stamp",
        ]

    def test_the_hook_s_credentials_sync_though_the_leaf_s_activate_is_none(self):
        store = seed_store()
        for key in KEYS:
            edit(store[PRD_LEAF].meta, key, activate="none")
        assert f"eso.sync:{ES}" in ids(World().plan(store=store))

    def test_dev_has_no_reader_and_starts_srvk8sdev_first(self):
        plan = World(DEV_LEAF).plan()
        assert ids(plan) == [DEV_START, MINT, "kv.write", PROVE, DELETE, "kv.stamp"]
        assert [s.id for s in plan.steps if s.vm == DEV_VM] == [MINT, PROVE, DELETE]
        assert [s.id for s in plan.steps[0].steps] == [MINT, PROVE, DELETE]
        assert not any(s.vm for s in World().plan().steps)

    def test_against_a_snapshot_it_plans_without_reaching_rgw(self, tmp_path):
        path = tmp_path / "snapshot.json"
        path.write_text(json.dumps(snapshot(hook_objects())))
        for leaf in (PRD_LEAF, DEV_LEAF):
            world = World(leaf)
            assert ids(world.plan(cluster=Cluster.of_snapshot(path))) == ids(world.plan())
            assert world.rgw.requests == []

    def test_a_plan_of_one_of_the_two_keys_is_refused(self):
        world = World()
        with pytest.raises(PlanError, match="rotates access_key_id and secret_access_key"):
            make({KIND: world.kind}, PRD_LEAF, KIND, ["secret_access_key"], audit(world.store))

    def test_a_leaf_of_no_rgw_admin_user_it_reaches_has_no_plan(self):
        world = World()
        leaf = "shared/test/ceph-rgw/s3"
        world.store[leaf] = dataclasses.replace(copy.deepcopy(world.store[PRD_LEAF]), path=leaf)
        with pytest.raises(PlanError, match=f"^{leaf}: not the leaf of an RGW admin user"):
            make({KIND: world.kind}, leaf, KIND, KEYS, audit(world.store))

    def test_the_steps_and_the_description(self):
        plan = World().plan()
        mint, prove, delete = plan.steps[0], plan.steps[3], plan.steps[4]
        assert (mint.mutates, mint.activator, mint.undo is not None) == (True, False, True)
        assert (prove.mutates, prove.silent) == (False, True)
        assert (delete.mutates, delete.undo) == (True, None)
        assert delete.no_undo == "a removed RGW key cannot be restored"
        assert mint.title == "add a new S3 key to the RGW admin user k8s on prd"
        assert prove.title == "prove the new key against RGW"
        assert delete.title == "remove the key the leaf held"
        assert not plan.needs_operator and plan.ask == ""
        assert plan.description == (
            "The tool adds a new S3 key to the RGW admin user on prd with the one the leaf holds, "
            "through RGW's admin API, and writes it to the leaf. Once every ExternalSecret that "
            "reads the leaf has synced, it proves the new key and removes the one the leaf held."
        )
        dev = World(DEV_LEAF).plan().description
        assert dev.startswith("The tool adds a new S3 key to the RGW admin user on dev ")
        assert dev.endswith(
            "the one the leaf held. It starts srvk8sdev first when it is off, and shuts it "
            "down again after."
        )
        assert RgwAdmin().credential(plan.target) == "Ceph RGW admin S3 key"


class TestTheRuns:
    def test_a_rotation_adds_a_key_writes_both_halves_and_removes_only_the_old(self):
        world = World()
        assert world.run() is Outcome.DONE
        new = world.new()
        assert world.held() == Key(new, world.rgw.users[UID]["keys"][new])
        assert world.rgw.keys(UID) == {world.spare.access, new}
        assert world.rgw.keys("app") == {world.other.access}
        assert state_of(world.bao, PRD_LEAF).stamps == {k: "2026-10-05" for k in KEYS}
        assert not any(s in t for t in world.texts() for s in world.secrets())
        assert world.finished(DELETE) == f"removed key {world.made.access} of k8s"

    def test_the_old_key_signs_the_add_and_the_new_one_the_removal_of_the_old_alone(self):
        world = World()
        assert world.run() is Outcome.DONE
        assert world.rgw.done(*ADD) == [world.made.access]
        assert world.rgw.done(*REMOVE) == [world.new()]

    def test_the_old_key_is_removed_only_once_the_leaf_holds_the_new_and_the_hook_synced(self):
        world = World()
        at_removal = []
        world.rgw.before[REMOVE] = lambda params: at_removal.append(
            (params["access-key"], world.held().access, world.cluster.syncs)
        )
        assert world.run() is Outcome.DONE
        assert at_removal == [(world.made.access, world.new(), 1)]

    def test_the_next_rotation_replaces_the_key_the_first_added(self):
        world = World()
        assert world.run() is Outcome.DONE
        first = world.held().access
        assert world.run(day=365) is Outcome.DONE
        second = world.held().access
        assert second != first and world.rgw.keys(UID) == {world.spare.access, second}

    def test_an_instance_that_does_not_answer_is_passed_over(self):
        world = World()
        world.rgw.down = {PRD.endpoints[0]}
        assert world.run() is Outcome.DONE
        assert {r[0] for r in world.rgw.requests} == set(PRD.endpoints[1:])
        assert world.finished(PROVE) == f"2 RGW instance(s) take key {world.new()} of k8s"

    def test_the_proof_asks_every_instance_that_answers(self):
        world = World()
        assert world.run() is Outcome.DONE
        proof = [r[0] for r in world.rgw.requests if r[3] == world.new() and r[1:3] == INFO]
        assert set(PRD.endpoints) <= set(proof)
        assert world.finished(PROVE) == f"3 RGW instance(s) take key {world.new()} of k8s"

    def test_an_rgw_no_instance_of_which_answers_stops_the_plan_before_it_writes(self):
        world = World()
        world.rgw.down = set(PRD.endpoints)
        executor = world.executor()
        assert executor.run() is Outcome.FAILED
        assert world.failure().step.id == MINT
        assert world.failure().error == "no RGW instance of prd answers: " + "; ".join(
            f"GET {e}/admin/user: transport error: [Errno 113] No route to host"
            for e in PRD.endpoints
        )
        assert executor.abort() is Outcome.CANCELLED
        assert world.held() == world.made

    @pytest.mark.parametrize(
        ("data", "error"),
        [
            (
                {KEYS[0]: "RGWNOSUCHKEY", KEYS[1]: "SECRET-no-key"},
                f"GET {PRD.endpoints[0]}/admin/user: HTTP 403: InvalidAccessKeyId",
            ),
            (
                {KEYS[0]: "MADE", KEYS[1]: "SECRET-wrong"},
                f"GET {PRD.endpoints[0]}/admin/user: HTTP 403: SignatureDoesNotMatch",
            ),
            ({KEYS[0]: "RGWNOSUCHKEY", KEYS[1]: ""}, f"{PRD_LEAF} holds no secret_access_key"),
        ],
    )
    def test_a_key_rgw_does_not_take_stops_the_plan_before_it_writes(self, data, error):
        world = World()
        data = {k: world.made.access if v == "MADE" else v for k, v in data.items()}
        world.bao.new_version(PRD_LEAF, data)
        executor = world.executor()
        assert executor.run() is Outcome.FAILED
        assert world.failure().step.id == MINT
        assert world.failure().error == error
        assert executor.abort() is Outcome.CANCELLED
        assert world.rgw.keys(UID) == world.initial

    def test_a_key_of_another_admin_user_stops_the_plan_before_it_adds(self):
        world = World()
        ops = world.rgw.user("ops")
        world.bao.new_version(PRD_LEAF, {KEYS[0]: ops.access, KEYS[1]: ops.secret})
        executor = world.executor()
        assert executor.run() is Outcome.FAILED
        assert world.failure().error == (
            f"k8s on prd has no key {ops.access}, which {PRD_LEAF} holds"
        )
        assert executor.abort() is Outcome.CANCELLED
        assert world.rgw.done(*ADD) == []

    def test_a_refused_add_did_not_land(self):
        world = World()
        world.rgw.refused[ADD] = (403, "AccessDenied")
        executor = world.executor()
        assert executor.run() is Outcome.FAILED
        assert world.failure().error == (
            f"PUT {PRD.endpoints[0]}/admin/user?key: HTTP 403: AccessDenied"
        )
        assert executor.abort() is Outcome.CANCELLED
        assert world.rgw.keys(UID) == world.initial

    def test_an_add_rgw_failed_mid_way_is_undone(self):
        world = World()
        world.rgw.refused[ADD] = (500, "InternalError")
        executor = world.executor()
        assert executor.run() is Outcome.FAILED
        assert executor.abort() is Outcome.ROLLED_BACK
        assert world.finished(MINT, Action.UNDO) == (
            "k8s has no key the plan added: nothing to remove"
        )
        assert world.rgw.keys(UID) == world.initial

    def test_an_add_whose_answer_is_lost_is_removed_and_added_again_on_the_retry(self):
        world = World()
        world.rgw.lost[ADD] = LOST
        assert world.run() is Outcome.FAILED
        assert world.failure().error == (
            f"PUT {PRD.endpoints[0]}/admin/user?key: transport error: {LOST!r}"
        )
        (lost,) = world.rgw.keys(UID) - world.initial
        world.rgw.lost.clear()
        assert world.run() is Outcome.DONE
        new = world.held().access
        assert new != lost and world.rgw.keys(UID) == {world.spare.access, new}
        removed = [r for r in world.rgw.requests if r[1:3] == REMOVE]
        assert [r[3] for r in removed] == [world.made.access, new]

    def test_an_add_whose_answer_is_lost_is_undone(self):
        world = World()
        world.rgw.lost[ADD] = LOST
        executor = world.executor()
        assert executor.run() is Outcome.FAILED
        assert len(world.rgw.keys(UID)) == len(world.initial) + 1
        world.rgw.lost.clear()
        assert executor.abort() is Outcome.ROLLED_BACK
        assert world.rgw.keys(UID) == world.initial
        assert world.held() == world.made

    def test_a_new_key_rgw_does_not_take_rolls_back_to_the_old(self):
        world = World()
        world.rgw.refuse = lambda method, op, access: (
            None if access in world.initial else (403, "InvalidAccessKeyId")
        )
        executor = world.executor()
        assert executor.run() is Outcome.FAILED
        assert world.failure().step.id == PROVE
        assert world.failure().error == (
            f"{PRD.endpoints[0]} did not take the key within 1 min: GET {PRD.endpoints[0]}"
            f"/admin/user: HTTP 403: InvalidAccessKeyId"
        )
        assert executor.abort() is Outcome.ROLLED_BACK
        assert world.held() == world.made
        assert world.rgw.keys(UID) == world.initial
        assert world.cluster.syncs == 2

    def test_instances_serving_the_user_from_their_cache_are_asked_again(self):
        world = World()
        world.rgw.lag = 3
        assert world.run() is Outcome.DONE
        assert world.rgw.time > 0
        assert world.rgw.keys(UID) == {world.spare.access, world.new()}

    def test_a_refused_removal_did_not_land_and_rolls_back_to_the_old_key(self):
        world = World()
        world.rgw.refuse = lambda method, op, access: (
            (403, "AccessDenied")
            if (method, op) == REMOVE and access not in world.initial
            else None
        )
        executor = world.executor()
        assert executor.run() is Outcome.FAILED
        assert world.failure().step.id == DELETE
        assert executor.abort_blocker() is None
        assert executor.abort() is Outcome.ROLLED_BACK
        assert world.held() == world.made
        assert world.rgw.keys(UID) == world.initial

    def test_an_admin_user_rgw_lets_remove_no_key_of_its_own_keeps_both_on_the_old(self):
        world = World()
        world.rgw.refused[REMOVE] = (403, "AccessDenied")
        executor = world.executor()
        assert executor.run() is Outcome.FAILED
        assert world.failure().step.id == DELETE
        assert executor.abort() is Outcome.ROLLBACK_FAILED
        assert world.held() == world.made
        assert world.rgw.keys(UID) == world.initial | {world.new()}

    def test_a_removal_whose_answer_is_lost_cannot_be_aborted_and_a_retry_finishes(self):
        world = World()
        world.rgw.lost[REMOVE] = LOST
        executor = world.executor()
        assert executor.run() is Outcome.FAILED
        assert world.made.access not in world.rgw.keys(UID)
        assert executor.abort_blocker() == "a removed RGW key cannot be restored"
        with pytest.raises(AbortRefused):
            executor.abort()
        world.rgw.lost.clear()
        assert executor.run() is Outcome.DONE
        assert world.finished(DELETE) == f"key {world.made.access} was removed already"
        assert state_of(world.bao, PRD_LEAF).stamps == {k: "2026-10-05" for k in KEYS}


class TestDev:
    def test_with_srvk8sdev_off_the_plan_starts_it_rotates_and_shuts_it_down(self):
        world = World(DEV_LEAF, off=True)
        assert world.run() is Outcome.DONE
        assert world.pve_cluster.commands() == [("pve", "start"), ("pve", "shutdown")]
        assert world.vm.status == "stopped"
        assert world.rgw.keys(UID) == {world.spare.access, world.new()}
        assert world.held().access == world.new()
        assert world.rgw.time >= world.rgw.boot
        assert {r[0] for r in world.rgw.requests} == {DEV.endpoints[0]}
        assert not any(s in t for t in world.texts() for s in world.secrets())

    def test_a_running_srvk8sdev_is_left_running(self):
        world = World(DEV_LEAF)
        assert world.run() is Outcome.DONE
        assert world.pve_cluster.commands() == []
        assert world.vm.status == "running"

    def test_a_dev_plan_rolled_back_has_srvk8sdev_up_again_for_the_mint_s_undo(self):
        world = World(DEV_LEAF, off=True)
        world.rgw.refuse = lambda method, op, access: (
            (403, "AccessDenied")
            if (method, op) == REMOVE and access not in world.initial
            else None
        )
        executor = world.executor()
        assert executor.run() is Outcome.FAILED
        assert world.vm.status == "stopped"
        assert executor.abort() is Outcome.ROLLED_BACK
        assert world.rgw.keys(UID) == world.initial and world.held() == world.made
        assert world.pve_cluster.commands() == [("pve", "start"), ("pve", "shutdown")] * 2
        assert world.vm.status == "stopped"

    def test_dev_off_does_not_answer_and_dev_up_answers_though_it_refuses(self):
        world = World(DEV_LEAF, off=True)
        dev = Gateway(DEV, world.rgw)
        assert dev.unanswered() == (
            "dev's RGW does not answer: http://192.168.188.17: transport error: [Errno 113] No "
            "route to host"
        )
        world.rgw.off = False
        assert dev.unanswered() is None
        assert world.rgw.requests[-1] == (DEV.endpoints[0], "GET", "/", None)

    def test_prd_always_answers_and_its_steps_ask_nothing(self):
        world = World()
        world.rgw.down = set(PRD.endpoints)
        assert Gateway(PRD, world.rgw).unanswered() is None
        assert world.rgw.requests == []
        assert not any(s.unanswered(client(world.bao)) for s in world.plan().steps)


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
        return Mint(Gateway(PRD, world.rgw, sleep=world.rgw.sleep, clock=world.rgw.clock), PRD_LEAF)

    def test_a_mint_stages_the_old_key_the_user_s_keys_and_the_new_pair(self):
        world = World()
        ctx = Ctx(world.bao)
        detail = self.mint(world).run(ctx)
        new = world.new()
        assert ctx.values == {
            OLD: world.made.access,
            BEFORE: json.dumps(sorted(world.initial)),
            value_name(KEYS[1]): world.rgw.users[UID]["keys"][new],
            value_name(KEYS[0]): new,
        }
        assert list(ctx.values)[-1] == value_name(KEYS[0])
        assert detail == f"added key {new} to k8s"

    def test_a_re_run_with_a_key_added_adds_none(self):
        world = World()
        ctx = Ctx(world.bao)
        mint = self.mint(world)
        mint.run(ctx)
        before = dict(ctx.values)
        mint.run(ctx)
        assert ctx.values == before
        assert len(world.rgw.done(*ADD)) == 1

    def test_a_mint_whose_key_rgw_lists_no_more_fails(self):
        world = World()
        ctx = Ctx(world.bao)
        mint = self.mint(world)
        mint.run(ctx)
        del world.rgw.users[UID]["keys"][world.new()]
        with pytest.raises(StepFailed, match="^RGW lists no key RGWACCESSKEY"):
            mint.run(ctx)

    def test_an_undo_never_removes_the_key_the_leaf_holds(self):
        world = World()
        ctx = Ctx(world.bao)
        mint = self.mint(world)
        mint.run(ctx)
        new = world.new()
        world.bao.new_version(PRD_LEAF, {KEYS[0]: new, KEYS[1]: ctx.values[value_name(KEYS[1])]})
        with pytest.raises(StepFailed, match=f"^{PRD_LEAF} holds the key the plan added, {new}$"):
            mint.undo(ctx)
        assert new in world.rgw.keys(UID)

    def test_a_transport_failure_after_the_request_is_no_unanswered_instance(self):
        world = World()
        world.rgw.lost[INFO] = LOST
        with pytest.raises(StepFailed, match="transport error: ConnectionResetError"):
            self.mint(world).run(Ctx(world.bao))
        assert {r[0] for r in world.rgw.requests} == {PRD.endpoints[0]}

    def moved(self, world, *, on=None):
        """A key added to k8s, as on the instance on when given (every other one answers from its
        cache for its next world.rgw.lag requests), the leaf moved onto it, and a Ctx staged as the
        mint and kv.write leave it."""
        before = copy.deepcopy(world.rgw.users)
        new = world.rgw.key(UID)
        if on:
            world.rgw.changed(on, before)
        world.bao.new_version(PRD_LEAF, {KEYS[0]: new.access, KEYS[1]: new.secret})
        staged = {OLD: world.made.access, value_name(KEYS[0]): new.access}
        return new, Ctx(world.bao, staged | {value_name(KEYS[1]): new.secret})

    def gateway(self, world):
        return Gateway(PRD, world.rgw, sleep=world.rgw.sleep, clock=world.rgw.clock)

    def test_a_delete_asks_again_an_instance_that_does_not_take_the_new_key_yet(self):
        world = World()
        world.rgw.lag = 2
        new, ctx = self.moved(world, on=PRD.endpoints[1])
        detail = Delete(self.gateway(world), PRD_LEAF).run(ctx)
        assert detail == f"removed key {world.made.access} of k8s"
        assert world.rgw.keys(UID) == {world.spare.access, new.access}
        assert world.rgw.done(*REMOVE) == [new.access] and world.rgw.time == 2 * CACHE_POLL

    def test_a_delete_lists_the_keys_again_until_the_removed_one_is_gone(self):
        world = World()
        new, ctx = self.moved(world)
        endpoint = PRD.endpoints[0]

        def listed_a_moment_longer(params):
            world.rgw.views[endpoint] = [copy.deepcopy(world.rgw.users), 1]

        world.rgw.before[REMOVE] = listed_a_moment_longer
        detail = Delete(self.gateway(world), PRD_LEAF).run(ctx)
        assert detail == f"removed key {world.made.access} of k8s"
        assert [r[1:3] for r in world.rgw.requests] == [INFO, REMOVE, INFO, INFO]
        assert world.rgw.time == CACHE_POLL
        assert world.rgw.keys(UID) == {world.spare.access, new.access}

    def test_a_proof_while_the_leaf_holds_another_key_than_the_one_added_asks_rgw_nothing(self):
        world = World()
        _, ctx = self.moved(world)
        world.bao.new_version(PRD_LEAF, {KEYS[0]: world.made.access, KEYS[1]: world.made.secret})
        with pytest.raises(StepFailed, match=f"^{PRD_LEAF} does not hold the key the plan added$"):
            Prove(self.gateway(world), PRD_LEAF).run(ctx)
        assert world.rgw.requests == []

    def test_a_delete_while_the_leaf_holds_another_key_than_the_one_added_removes_nothing(self):
        world = World()
        _, ctx = self.moved(world)
        world.bao.new_version(PRD_LEAF, {KEYS[0]: world.made.access, KEYS[1]: world.made.secret})
        not_held = f"^{PRD_LEAF} does not hold the key the plan added$"
        with pytest.raises(StepFailed, match=not_held) as e:
            Delete(self.gateway(world), PRD_LEAF).run(ctx)
        assert e.value.landed is False
        assert world.made.access in world.rgw.keys(UID) and world.rgw.requests == []
