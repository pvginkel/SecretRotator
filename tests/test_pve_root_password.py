"""The pve-root-password kind (design §6, §4.6) over the seed's iac/proxmox, the PVE root@pam
password, and its copy in the KubeCoder catalog: the plan generates the password, sets it as root's
on pve, pve1 and pve2, one ssh.set_password each, writes it and the copy, rolls the KubeCoder
controller the copy's activate names and hands it to the operator for RoboForm (052 D3); the runs,
which leave every node, the leaf and the copy on the new password; and the failures and Aborts
before the show, whose rollback leaves every node on the password the leaf held."""

import json
import sys
from pathlib import Path

import pytest
from fake_cluster import FakeCluster, externalsecret, pod_spec, workload
from fixtures import edit
from plans import Recorder, client, fake_of, lock, run_state, state_of
from test_activation import ticking
from test_sshsteps import FAKE, calls, passwords, scenario

from secret_rotator import annotate as ann
from secret_rotator import registry
from secret_rotator.audit import audit
from secret_rotator.cluster import Cluster
from secret_rotator.executor import Abandon, Executor, Outcome
from secret_rotator.kinds.pve_root_password import NODES, STORE, PveRootPassword
from secret_rotator.listing import credential
from secret_rotator.opsteps import ShowRequest
from secret_rotator.plan import PlanError, make, of_leaf
from secret_rotator.sshsteps import Ssh

KINDS = registry.load()
SEED = ann.load_seed(ann.DEFAULT_SEED)
KIND = "pve-root-password"
LEAF = "iac/proxmox"
KEY = "password"
CATALOG = "eso/prd/kubecoder/prd/catalog"
COPY = "proxmox-password"
ES = "kubecoder-prd/kubecoder-secret-catalog"
CONTROLLER = "kubecoder-prd/deployment/kubecoder-controller"
OLD = "SECRET-old-root-password"


def seed_store():
    return ann.offline_store(Path(str(ann.DEFAULT_KEYS)), SEED, lambda line: None)


def catalog_objects():
    """(resource, object) of the catalog's readers on prd: the controller mounts the Secret its
    ExternalSecret extracts from the whole leaf."""
    return [
        (
            "externalsecrets",
            externalsecret(
                "kubecoder-prd",
                "kubecoder-secret-catalog",
                extract=[CATALOG],
                target="kubecoder-secret-catalog",
            ),
        ),
        (
            "deployments",
            workload(
                "Deployment",
                "kubecoder-prd",
                "kubecoder-controller",
                pod_spec(volume=["kubecoder-secret-catalog"]),
                app="kubecoder-prd",
            ),
        ),
        (
            "applications",
            {
                "metadata": {"namespace": "argocd-prd", "name": "kubecoder-prd"},
                "status": {"health": {"status": "Healthy"}},
            },
        ),
    ]


class World:
    """The seed's store on the fake OpenBao, the leaf and its copy holding the old password; the
    nodes, which take it as root's; and the catalog's readers on the fake cluster."""

    def __init__(self, tmp_path):
        self.tmp_path = tmp_path
        self.store = seed_store()
        self.bao = fake_of(self.store, {LEAF: {KEY: OLD}})
        catalog = self.bao.leaves[CATALOG]["data"]
        catalog[COPY] = OLD
        self.catalog = dict(catalog)
        known_hosts = tmp_path / "homelab"
        known_hosts.write_text("@cert-authority * ssh-ed25519 AAAAC3Nza homelab-ssh-host-ca\n")
        self.ssh = Ssh(known_hosts, (sys.executable, FAKE, str(tmp_path)))
        held = {node: {"root": OLD} for node in NODES}
        (tmp_path / "passwords.json").write_text(json.dumps(held))
        self.cluster = FakeCluster(catalog_objects())

    def password(self):
        return self.bao.data(LEAF)[KEY]

    def nodes(self):
        """root's password on each node."""
        return {node: held["root"] for node, held in passwords(self.tmp_path).items()}

    def plan(self, *, store=None):
        return make(
            KINDS,
            LEAF,
            KIND,
            [KEY],
            audit(store or self.store),
            Cluster(self.cluster.kube()),
            ssh=self.ssh,
        )

    def executor(self, *answers):
        self.recorder = Recorder(*answers)
        return Executor(
            client(self.bao),
            self.plan(),
            self.recorder,
            lock(self.bao),
            state=run_state(self.bao),
            dry_run=False,
            clock=ticking(),
        )

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

    def generation(self):
        return self.cluster.get("deployments", "kubecoder-prd", "kubecoder-controller")["metadata"][
            "generation"
        ]


def ids(plan):
    return [s.id for s in plan.steps]


class TestTheSeed:
    def test_the_root_password_rotates_yearly_its_leaf_activating_nothing(self):
        entries = audit(seed_store()).entries
        entry = entries[LEAF][KEY]
        assert (entry.kind, entry.args, entry.interval, entry.activate) == (KIND, {}, 365, ())
        assert PveRootPassword().args_problems(entry.args) == []

    def test_its_copy_in_the_catalog_rolls_the_kubecoder_controller(self):
        copy = audit(seed_store()).entries[CATALOG][COPY]
        assert copy.kind == f"copy:{LEAF}#{KEY}"
        assert [str(spec) for spec in copy.activate] == [f"k8s-rollout:{CONTROLLER}"]

    def test_the_nodes_are_the_inventory_s_proxmox_group(self):
        # Ansible ansible/inventories/prd/hosts.yml; pve3 is decommissioned.
        assert NODES == ("pve", "pve1", "pve2")

    def test_an_arg_is_named(self):
        assert PveRootPassword().args_problems({"nodes": ["pve"]}) == [
            "nodes: pve-root-password takes no args"
        ]


class TestThePlan:
    def test_it_sets_each_node_before_it_writes_then_activates_and_shows(self, tmp_path):
        assert ids(World(tmp_path).plan()) == [
            f"random.generate:{KEY}",
            "ssh.set_password:root@pve",
            "ssh.set_password:root@pve1",
            "ssh.set_password:root@pve2",
            "kv.write",
            f"kv.copy:{CATALOG}#{COPY}",
            f"eso.sync:{ES}",
            f"k8s.rollout:{CONTROLLER}",
            f"operator.show:value:{KEY}",
            "kv.stamp",
        ]

    def test_the_steps_the_ask_and_the_description(self, tmp_path):
        world = World(tmp_path)
        plan = world.plan()
        generate, show = plan.steps[0], plan.steps[8]
        assert generate.length == 43
        assert all(step.ssh is world.ssh for step in plan.steps[1:4])
        assert show.title == STORE == "Store it in RoboForm (PVE root@pam)"
        assert show.instruction == (
            "The new root@pam password of the PVE nodes pve, pve1 and pve2: replace the one "
            "RoboForm holds for PVE root@pam with it."
        )
        assert not show.mutates
        assert plan.needs_operator  # so the nightly run never starts it
        assert plan.ask == "store the new password in RoboForm"
        assert credential(KINDS[KIND], plan.target) == "PVE root@pam password"
        assert plan.description == (
            "The tool generates a new root@pam password, sets it as root's on the PVE nodes pve, "
            "pve1 and pve2 over SSH and writes it to the leaf and its 1 copy and activates what "
            "reads it. You store it in RoboForm."
        )

    def test_secret_rotator_plan_builds_it_for_the_leaf(self, tmp_path):
        world = World(tmp_path)
        cluster = Cluster(world.cluster.kube())
        (planned,), unplanned = of_leaf(LEAF, world.store, audit(world.store), KINDS, cluster)
        assert (planned.kind, planned.keys, planned.error, unplanned) == (KIND, (KEY,), "", {})
        assert ids(planned.plan) == ids(world.plan())

    def test_offline_without_a_snapshot_it_has_no_plan(self):
        store = seed_store()
        with pytest.raises(PlanError, match="offline plan without a snapshot does not reach"):
            make(KINDS, LEAF, KIND, [KEY], audit(store))

    def test_a_leaf_with_two_keys_of_the_kind_has_no_plan(self, tmp_path):
        world = World(tmp_path)
        world.store[LEAF].keys.add("other")
        edit(world.store[LEAF].meta, "other", kind=KIND, interval="365d", activate="none")
        with pytest.raises(PlanError, match="a pve-root-password plan rotates one key, not 2"):
            make(
                KINDS,
                LEAF,
                KIND,
                [KEY, "other"],
                audit(world.store),
                Cluster(world.cluster.kube()),
            )


class TestTheRuns:
    def test_a_rotation_leaves_every_node_the_leaf_and_its_copy_on_the_new_password(self, tmp_path):
        world = World(tmp_path)
        assert world.executor({}).run() is Outcome.DONE
        new = world.password()
        assert new != OLD and len(new) == 43
        assert world.nodes() == dict.fromkeys(NODES, new)
        assert world.bao.data(CATALOG)[COPY] == new
        assert world.generation() == 2
        ((step, request),) = world.recorder.asked
        assert step == f"operator.show:value:{KEY}"
        assert isinstance(request, ShowRequest) and request.value == new
        assert state_of(world.bao, LEAF).stamps == {KEY: "2026-10-05"}
        assert [c["host"] for c in calls(tmp_path)] == list(NODES)
        assert [c["stdin"] for c in calls(tmp_path)] == [f"root:{new}\n"] * 3
        assert not any(new in " ".join(c["argv"]) for c in calls(tmp_path))
        assert not any(OLD in text or new in text for text in world.texts())

    def test_an_abort_at_the_show_sets_every_node_back_with_the_leaf_and_its_copy(self, tmp_path):
        world = World(tmp_path)
        assert world.executor(Abandon.ABORT).run() is Outcome.ROLLED_BACK
        new = calls(tmp_path)[0]["stdin"].removeprefix("root:").removesuffix("\n")
        assert world.nodes() == dict.fromkeys(NODES, OLD)
        assert world.password() == OLD and world.bao.data(CATALOG) == world.catalog
        assert [(c["host"], c["stdin"]) for c in calls(tmp_path)] == [
            *((node, f"root:{new}\n") for node in NODES),
            *((node, f"root:{OLD}\n") for node in reversed(NODES)),
        ]
        assert world.generation() == 3  # rolled again after the KV rollback
        assert not any(OLD in text or new in text for text in world.texts())

    def test_a_node_it_cannot_reach_stops_the_plan_before_the_write(self, tmp_path):
        world = World(tmp_path)
        scenario(tmp_path, pve1="unreachable")
        executor = world.executor()
        assert executor.run() is Outcome.FAILED
        failure = world.failure()
        assert failure.step.id == "ssh.set_password:root@pve1"
        assert failure.error == (
            "ssh to pve1 as ansible failed: ssh: connect to host pve1 port 22: No route to host"
        )
        assert world.password() == OLD and world.nodes()["pve2"] == OLD
        assert world.nodes()["pve"] != OLD
        assert executor.abort_blocker() is None
        scenario(tmp_path)
        assert executor.abort() is Outcome.ROLLED_BACK
        assert world.nodes() == dict.fromkeys(NODES, OLD)
        assert [c["host"] for c in calls(tmp_path)] == ["pve", "pve1", "pve1", "pve"]

    def test_a_rollback_that_cannot_reach_a_node_fails_and_its_retry_finishes_it(self, tmp_path):
        world = World(tmp_path)
        scenario(tmp_path, pve2="hostkey")
        executor = world.executor()
        assert executor.run() is Outcome.FAILED
        assert executor.abort() is Outcome.ROLLBACK_FAILED
        assert world.recorder.failures()[-1].error == (
            "ssh to pve2 as ansible failed: Host key verification failed."
        )
        assert world.nodes()["pve"] != OLD
        scenario(tmp_path)
        assert executor.run() is Outcome.ROLLED_BACK
        assert world.nodes() == dict.fromkeys(NODES, OLD) and world.password() == OLD

    def test_a_retry_once_the_node_is_reachable_finishes_the_rotation(self, tmp_path):
        world = World(tmp_path)
        scenario(tmp_path, pve1="unreachable")
        assert world.executor().run() is Outcome.FAILED
        scenario(tmp_path)
        assert world.executor({}).run() is Outcome.DONE
        new = world.password()
        assert new != OLD and world.nodes() == dict.fromkeys(NODES, new)
        assert world.bao.data(CATALOG)[COPY] == new
