"""The ssh-key kind (design §6, §4.6; Ansible slice 060) over the seed's iac/ansible-ssh-key, the
ansible user's SSH key, and its copy in the KubeCoder catalog: the hosts are the Ansible
inventory's two classes; the plan starts each VM that may be off, checks every host reached over
SSH answers, generates the new keypair, authorises and proves it on every host while the rotator
logs in with the old key, commits its public half to the Ansible repo, writes the leaf and its
copy, switches srviac's copy, rolls the KubeCoder controller and takes the old key off every host
logging in with the new one, and shuts the VMs it started down; a failed plan rolls back in
reverse; an always-up host that does not answer skips the plan at night, as does a VM that does not
come up, and the nightly run rotates it with no operator. ansible-playbook plays the rotation
playbook on an estate of hosts (fake_ansible_playbook.py rotate), ssh the hosts' answers
(fake_ssh.py)."""

import datetime
import itertools
import json
import sys
from pathlib import Path

import pytest
from fake_cluster import FakeCluster
from fake_github import CONTENTS_TOKEN, FakeGitHub
from fake_pve import FakePve, FakeVm
from fake_telegram import CHAT, FakeTelegram
from fake_telegram import TOKEN as BOT
from fake_youtrack import TAG, FakeYouTrack
from fake_youtrack import TOKEN as JEEVES
from fixtures import edit
from plans import Recorder, client, fake_of, flight_of, lock, run_state, state_of
from test_activation import ticking
from test_pve_root_password import catalog_objects
from test_sshsteps import FAKE as FAKE_SSH
from test_sshsteps import calls
from test_vmsteps import Clock

from secret_rotator import annotate as ann
from secret_rotator import nightly, registry
from secret_rotator.ansiblekey import AnsibleKey
from secret_rotator.ansiblesteps import ANSIBLE_DIR, Ansible
from secret_rotator.audit import audit
from secret_rotator.cluster import Cluster
from secret_rotator.executor import Executor, Outcome
from secret_rotator.kinds.ssh_key import (
    COMMENT,
    CREDENTIALS,
    MESSAGE,
    PLAYBOOK,
    PUBLIC,
    REPO,
    SshKey,
)
from secret_rotator.kinds.ssh_key.hosts import INVENTORY, Hosts, hosts
from secret_rotator.kinds.ssh_key.steps import Reach
from secret_rotator.listing import credential
from secret_rotator.model import Action, NoAnswer
from secret_rotator.plan import PlanError, make, of_leaf
from secret_rotator.sshsteps import Ssh, generate, public_line
from secret_rotator.switches import Switches
from secret_rotator.telegram import Telegram
from secret_rotator.vmsteps import Pve
from secret_rotator.youtrack import YouTrack

FAKE_PLAYBOOK = str(Path(__file__).with_name("fake_ansible_playbook.py"))
SEED = ann.load_seed(ann.DEFAULT_SEED)
KIND = "ssh-key"
LEAF = "iac/ansible-ssh-key"
KEY = "private"
CATALOG = "eso/prd/kubecoder/prd/catalog"
COPY = "ssh-key-ansible"
ES = "kubecoder-prd/kubecoder-secret-catalog"
CONTROLLER = "kubecoder-prd/deployment/kubecoder-controller"
HEAD = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"
NOW = datetime.datetime(2026, 10, 5, 4, 30, tzinfo=datetime.UTC)
# The shape of Ansible's inventories/prd/hosts.yml: the classes' children defined elsewhere, a VM
# that may be off in two groups, hosts that hold no key.
INVENTORY_TEXT = """\
all:
  children:
    proxmox:
      hosts:
        pve:
        pve1:
    k8s_prd:
      hosts:
        srvk8s1:
    k8s_dev:
      hosts:
        srvk8sdev:
    ceph_prd:
      hosts:
        srvceph1:
    iac_agent:
      hosts:
        srviac:
    workstations:
      hosts:
        wrkdev:
    pve_vms_unmanaged:
      hosts:
        wrkscratchk8s1:
    ansible_key:
      children:
        ansible_key_always_up:
          children:
            proxmox:
            k8s_prd:
            iac_agent:
            workstations:
        ansible_key_may_be_off:
          hosts:
            srvk8sdev:
            wrkscratchk8s1:
    k8s:
      children:
        k8s_prd:
        k8s_dev:
"""
ALWAYS_UP = ("pve", "pve1", "srviac", "srvk8s1", "wrkdev")
MAY_BE_OFF = ("srvk8sdev", "wrkscratchk8s1")
DEV_VM, SCRATCH = MAY_BE_OFF
OTHER = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOtherKeyAuthorisedForAnsible operator"
ADD, REMOVE = "ansible.run:add-new-key", "ansible.run:remove-old-key"
COMMIT = f"github.commit:{REPO}/{PUBLIC}"
STEPS = [
    f"vm.start:{DEV_VM}",
    f"vm.start:{SCRATCH}",
    "ssh_key.reach",
    f"ssh.keygen:{KEY}",
    ADD,
    COMMIT,
    "kv.write",
    f"kv.copy:{CATALOG}#{COPY}",
    f"ssh.key_copy:{KEY}",
    f"eso.sync:{ES}",
    f"k8s.rollout:{CONTROLLER}",
    REMOVE,
    "kv.stamp",
]


def seed_store():
    return ann.offline_store(Path(str(ann.DEFAULT_KEYS)), SEED, lambda line: None)


def key_of(line):
    return " ".join(line.split()[:2])


class World:
    """The seed's store on the fake OpenBao, the leaf and its copy holding the old key and the
    commit's token leaf the token the fake GitHub takes; the Ansible repo on GitHub with the old
    public half; the estate, whose hosts each authorise the old key and another, the controller's
    key file and srviac's copy holding the old key; srvk8sdev stopped on pve, wrkscratchk8s1
    absent; the catalog's readers on the fake cluster."""

    def __init__(self, tmp_path):
        self.tmp_path = tmp_path
        self.store = seed_store()
        self.old = generate()
        self.old_public = public_line(self.old, COMMENT)
        rotator = {"rotator/youtrack": {"token": JEEVES}, "rotator/telegram": {"token": BOT}}
        held = {LEAF: {KEY: self.old}, CREDENTIALS: {"token": CONTENTS_TOKEN}}
        self.bao = fake_of(self.store, held | rotator)
        self.bao.leaves[CATALOG]["data"][COPY] = self.old
        self.catalog = dict(self.bao.leaves[CATALOG]["data"])
        self.github = FakeGitHub()
        self.github.repo(REPO, {PUBLIC: self.old_public + "\n", "README.md": "readme\n"}, sha=HEAD)
        self.inventory = tmp_path / "hosts.yml"
        self.inventory.write_text(INVENTORY_TEXT)
        self.kind = SshKey(self.inventory)
        (tmp_path / "login").mkdir()
        (tmp_path / "srviac").mkdir()
        self.key = AnsibleKey(tmp_path / "login" / "id_ed25519_ansible", tmp_path / "srviac" / "k")
        self.key.login.write_text(self.old)
        self.key.copy.write_text(self.old)
        self.checkout = tmp_path / "ansible"
        self.checkout.mkdir()
        self.ansible = Ansible(self.checkout, (sys.executable, FAKE_PLAYBOOK, "rotate"))
        authorized = {host: [self.old_public, OTHER] for host in (*ALWAYS_UP, *MAY_BE_OFF)}
        self.put_estate(login=str(self.key.login), authorized=authorized)
        (tmp_path / "ssh").mkdir()
        known_hosts = tmp_path / "homelab"
        known_hosts.write_text("@cert-authority * ssh-ed25519 AAAAC3Nza homelab-ssh-host-ca\n")
        self.ssh = Ssh(known_hosts, (sys.executable, FAKE_SSH, str(tmp_path / "ssh")))
        self.clock = Clock()
        self.vm = FakeVm(DEV_VM, 919, "pve")
        self.pve_cluster = FakePve([self.vm])
        self.pve = Pve(self.pve_cluster, sleep=self.clock.sleep, clock=self.clock.clock)
        self.cluster = FakeCluster(catalog_objects())

    def estate(self):
        return json.loads((self.checkout / "estate.json").read_text())

    def put_estate(self, **fields):
        path = self.checkout / "estate.json"
        estate = json.loads(path.read_text()) if path.exists() else {}
        path.write_text(json.dumps(estate | fields))

    def scenario(self, **hosts):
        """What ssh to each host plays (fake_ssh.py)."""
        (self.tmp_path / "ssh" / "scenarios.json").write_text(json.dumps(hosts))

    def runs(self):
        path = self.checkout / "runs.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def probes(self):
        """The hosts the reach logged in to, in order."""
        return [c["host"] for c in calls(self.tmp_path / "ssh")]

    def authorized(self):
        """The keys each host but the absent VM authorises for ansible, without comments."""
        held = self.estate()["authorized"]
        return {host: {key_of(k) for k in keys} for host, keys in held.items() if host != SCRATCH}

    def everywhere(self, *lines):
        return {host: {key_of(k) for k in lines} for host in (*ALWAYS_UP, DEV_VM)}

    def plan(self, *, store=None, kind=None):
        return make(
            {KIND: kind or self.kind},
            LEAF,
            KIND,
            [KEY],
            audit(store or self.store),
            Cluster(self.cluster.kube()),
            ansible=self.ansible,
            ssh=self.ssh,
            github=self.github.github(),
            pve=self.pve,
            ansible_key=self.key,
        )

    def executor(self):
        self.recorder = Recorder()
        return Executor(
            client(self.bao),
            self.plan(),
            self.recorder,
            lock(self.bao),
            state=run_state(self.bao),
            dry_run=False,
            clock=ticking(),
            ansible_key=self.key,
        )

    def held(self):
        return self.bao.data(LEAF)[KEY]

    def text(self):
        """ansible.pub on the Ansible repo's main."""
        return self.github.head(REPO)["files"][PUBLIC]

    def messages(self):
        """The messages of the commits on main after the first."""
        return [c["message"] for c in self.github.repos[REPO][1:]]

    def generation(self):
        deployment = self.cluster.get("deployments", "kubecoder-prd", "kubecoder-controller")
        return deployment["metadata"]["generation"]

    def failure(self):
        return self.recorder.failures()[-1]

    def texts(self):
        """Every detail, error and technical text the run reported."""
        return [
            getattr(e, name, "")
            for e in self.recorder.events
            for name in ("detail", "error", "technical")
        ]

    def undone(self):
        """(action, step id) of every rollback line, in order."""
        return [
            (action, step)
            for kind, step, action in self.recorder.lines()
            if kind == "started" and action is not Action.RUN
        ]


def ids(plan):
    return [s.id for s in plan.steps]


class TestTheSeed:
    def test_the_key_rotates_every_14_days_its_leaf_activating_nothing(self):
        entry = audit(seed_store()).entries[LEAF][KEY]
        assert (entry.kind, entry.args, entry.interval, entry.activate) == (KIND, {}, 14, ())
        assert SshKey().args_problems(entry.args) == []

    def test_its_copy_in_the_catalog_rolls_the_kubecoder_controller(self):
        copy = audit(seed_store()).entries[CATALOG][COPY]
        assert copy.kind == f"copy:{LEAF}#{KEY}"
        assert [str(spec) for spec in copy.activate] == [f"k8s-rollout:{CONTROLLER}"]

    def test_the_commit_s_token_is_a_github_pat_the_operator_renews_yearly(self):
        entry = audit(seed_store()).entries[CREDENTIALS]["token"]
        assert (entry.kind, entry.args, entry.interval, entry.activate) == (
            "manual",
            {"type": "github-pat"},
            365,
            (),
        )
        assert "Contents read and write" in entry.notes
        assert "repository access only pvginkel/Ansible" in entry.notes
        assert (CREDENTIALS, REPO) == ("rotator/ssh-key/credentials", "pvginkel/Ansible")

    def test_an_arg_is_named(self):
        assert SshKey().args_problems({"hosts": ["pve"]}) == ["hosts: ssh-key takes no args"]

    def test_the_registered_kind_reads_the_prd_inventory_of_the_checkout_playbooks_run_from(self):
        assert isinstance(registry.load()[KIND], SshKey)
        assert SshKey().inventory == INVENTORY == ANSIBLE_DIR / "inventories" / "prd" / "hosts.yml"
        assert PUBLIC == "ansible/roles/bootstrap/files/ansible.pub"
        assert PLAYBOOK == "playbooks/rotate-ansible-key.yml"


class TestTheHosts:
    def test_the_classes_are_the_two_groups_with_their_children_s_hosts_sorted(self, tmp_path):
        path = tmp_path / "hosts.yml"
        path.write_text(INVENTORY_TEXT)
        assert hosts(path) == Hosts(ALWAYS_UP, MAY_BE_OFF)

    def test_an_inventory_without_one_of_the_groups_gives_no_plan(self, tmp_path):
        world = World(tmp_path)
        world.inventory.write_text(INVENTORY_TEXT.split("        ansible_key_may_be_off:")[0])
        with pytest.raises(PlanError, match=f"^{LEAF}: the Ansible inventory .* has no group "):
            world.plan()

    def test_an_inventory_it_cannot_read_gives_no_plan(self, tmp_path):
        world = World(tmp_path)
        world.inventory.unlink()
        with pytest.raises(PlanError, match="cannot be read: No such file or directory"):
            world.plan()

    @pytest.mark.parametrize(
        ("text", "why"),
        [("{}\n", "it has no group all"), ("all: []\n", "group all is not a mapping")],
    )
    def test_an_inventory_of_another_shape_gives_no_plan(self, tmp_path, text, why):
        world = World(tmp_path)
        world.inventory.write_text(text)
        with pytest.raises(PlanError, match=f"^{LEAF}: the Ansible inventory .*: {why}$"):
            world.plan()

    def test_an_inventory_that_is_no_yaml_gives_no_plan(self, tmp_path):
        world = World(tmp_path)
        world.inventory.write_text("all: [unclosed\n")
        with pytest.raises(PlanError, match="is no YAML"):
            world.plan()


class TestThePlan:
    def test_it_proves_commits_writes_switches_activates_then_takes_the_old_key_off(self, tmp_path):
        plan = World(tmp_path).plan()
        assert ids(plan) == STEPS
        assert not plan.needs_operator  # so the nightly run starts it
        assert plan.logs_in

    def test_both_runs_play_the_rotation_playbook_on_every_host_each_undoing_the_other(
        self, tmp_path
    ):
        plan = World(tmp_path).plan()
        steps = {s.id: s for s in plan.steps}
        add, remove = steps[ADD], steps[REMOVE]
        where = {
            f"key_rotation_vm_{DEV_VM}": f"vm.start:{DEV_VM}:location",
            f"key_rotation_vm_{SCRATCH}": f"vm.start:{SCRATCH}:location",
        }
        runs = {
            "add": add.book,
            "add's undo": add.counter,
            "remove": remove.book,
            "remove's undo": remove.counter,
        }
        assert {name: (b.path, b.tags, dict(b.extra)) for name, b in runs.items()} == {
            "add": (PLAYBOOK, (), {"key_rotation_state": "present"}),
            "add's undo": (PLAYBOOK, (), {"key_rotation_state": "absent"}),
            "remove": (PLAYBOOK, (), {"key_rotation_state": "absent"}),
            "remove's undo": (PLAYBOOK, (), {"key_rotation_state": "present"}),
        }
        assert {name: dict(b.staged) for name, b in runs.items()} == {
            "add": {
                "key_rotation_public": f"public:{KEY}",
                "key_rotation_private": f"value:{KEY}",
                **where,
            },
            "add's undo": {"key_rotation_public": f"public:{KEY}", **where},
            "remove": {"key_rotation_public": f"old-public:{KEY}", **where},
            "remove's undo": {
                "key_rotation_public": f"old-public:{KEY}",
                "key_rotation_private": f"old:{KEY}",
                **where,
            },
        }
        assert add.vms == remove.vms == MAY_BE_OFF
        assert [s.id for s in plan.steps if s.vms] == [ADD, REMOVE]
        reach = steps["ssh_key.reach"]
        assert isinstance(reach, Reach) and reach.hosts == ALWAYS_UP and not reach.mutates

    def test_the_ask_the_title_and_the_description(self, tmp_path):
        plan = World(tmp_path).plan()
        assert plan.ask == ""
        assert credential(SshKey(), plan.target) == "Ansible SSH key"
        assert plan.description == (
            "The tool generates a new SSH key for the ansible user, authorises it on every host "
            "that holds the key and logs in to each with it, commits its public half to "
            "pvginkel/Ansible, writes it to the leaf and its 1 copy and activates what reads it, "
            "switches srviac's copy of the key to it, and takes the old key off every host. A VM "
            "that may be off is started for it and shut down again."
        )

    def test_secret_rotator_plan_builds_it_for_the_leaf(self, tmp_path):
        world = World(tmp_path)
        cluster = Cluster(world.cluster.kube())
        kinds = {KIND: world.kind}
        (planned,), unplanned = of_leaf(LEAF, world.store, audit(world.store), kinds, cluster)
        assert (planned.kind, planned.keys, planned.error, unplanned) == (KIND, (KEY,), "", {})
        assert ids(planned.plan) == STEPS

    def test_a_leaf_with_two_keys_of_the_kind_has_no_plan(self, tmp_path):
        world = World(tmp_path)
        world.store[LEAF].keys.add("other")
        edit(world.store[LEAF].meta, "other", kind=KIND, interval="14d", activate="none")
        with pytest.raises(PlanError, match="an ssh-key plan rotates one key, not 2"):
            make(
                {KIND: world.kind},
                LEAF,
                KIND,
                [KEY, "other"],
                audit(world.store),
                Cluster(world.cluster.kube()),
            )


class TestTheRuns:
    def test_a_rotation_leaves_every_host_the_leaf_its_copies_and_the_repo_on_the_new_key(
        self, tmp_path
    ):
        world = World(tmp_path)
        assert world.executor().run() is Outcome.DONE
        new = world.held()
        public = public_line(new, COMMENT)
        assert new != world.old and public.endswith(" ansible")
        assert world.authorized() == world.everywhere(public, OTHER)
        assert world.bao.data(CATALOG)[COPY] == new
        assert world.key.copy.read_text() == new and world.key.login.read_text() == new
        assert world.text() == public + "\n"
        assert world.messages() == [f"{MESSAGE} (secret-rotator)"]
        assert world.generation() == 2
        assert state_of(world.bao, LEAF).stamps == {KEY: "2026-10-05"}
        where = {DEV_VM: "pve/919", SCRATCH: "absent"}
        ran = [(r["state"], r["public"], r["private"], r["vms"], r["login"]) for r in world.runs()]
        assert ran == [
            ("present", public, new, where, world.old),  # the old key logs in, the new one proves
            ("absent", world.old_public, None, where, new),  # the new key logs in
        ]
        assert world.probes() == list(ALWAYS_UP)
        assert world.pve_cluster.commands() == [("pve", "start"), ("pve", "shutdown")]
        assert world.vm.status == "stopped"
        assert not any(world.old in t or new in t for t in world.texts())
        assert flight_of(world.bao, LEAF) is None

    def test_a_running_vm_is_left_running_and_an_existing_scratch_vm_is_rotated(self, tmp_path):
        world = World(tmp_path)
        world.vm.status = "running"
        world.pve_cluster.vms.append(FakeVm(SCRATCH, 931, "pve1", "running"))
        assert world.executor().run() is Outcome.DONE
        public = public_line(world.held(), COMMENT)
        held = world.estate()["authorized"][SCRATCH]
        assert {key_of(k) for k in held} == {key_of(public), key_of(OTHER)}
        assert [r["vms"] for r in world.runs()] == [{DEV_VM: "pve/919", SCRATCH: "pve1/931"}] * 2
        assert world.pve_cluster.commands() == []
        assert {vm.status for vm in world.pve_cluster.vms} == {"running"}

    def test_a_host_that_refuses_the_new_key_fails_the_plan_before_the_commit_and_the_write(
        self, tmp_path
    ):
        world = World(tmp_path)
        world.put_estate(refuse=["srvk8s1"])
        executor = world.executor()
        assert executor.run() is Outcome.FAILED
        assert world.failure().step.id == ADD
        assert world.failure().error == f"{PLAYBOOK}: srvk8s1 failed"
        assert world.held() == world.old and world.text() == world.old_public + "\n"
        assert world.key.copy.read_text() == world.old
        assert executor.abort_blocker() is None
        assert executor.abort() is Outcome.ROLLED_BACK
        assert world.undone() == [(Action.UNDO, ADD)]
        assert world.authorized() == world.everywhere(world.old_public, OTHER)
        new_public = world.runs()[0]["public"]
        assert [(r["state"], r["public"], r["login"]) for r in world.runs()] == [
            ("present", new_public, world.old),
            ("absent", new_public, world.old),
        ]
        assert world.messages() == [] and world.generation() == 1
        assert world.vm.status == "stopped"

    def test_a_failed_removal_rolls_back_in_reverse_each_run_logging_in_with_the_leaf_s_key(
        self, tmp_path
    ):
        world = World(tmp_path)
        world.put_estate(fail={"absent": ["pve1"]})
        executor = world.executor()
        assert executor.run() is Outcome.FAILED
        assert world.failure().step.id == REMOVE
        new = world.held()
        assert world.key.login.read_text() == new
        world.put_estate(fail={})  # pve1 takes runs again
        assert executor.abort() is Outcome.ROLLED_BACK
        assert world.undone() == [
            (Action.UNDO, REMOVE),
            (Action.UNDO, f"ssh.key_copy:{KEY}"),
            (Action.UNDO, f"kv.copy:{CATALOG}#{COPY}"),
            (Action.UNDO, "kv.write"),
            (Action.UNDO, COMMIT),
            (Action.UNDO, ADD),
            (Action.RERUN, f"eso.sync:{ES}"),
            (Action.RERUN, f"k8s.rollout:{CONTROLLER}"),
        ]
        public = public_line(new, COMMENT)
        assert [(r["state"], r["public"], r["private"], r["login"]) for r in world.runs()] == [
            ("present", public, new, world.old),
            ("absent", world.old_public, None, new),
            ("present", world.old_public, world.old, new),  # the removal's undo
            ("absent", public, None, world.old),  # the add's undo, after the write's
        ]
        assert world.authorized() == world.everywhere(world.old_public, OTHER)
        assert world.held() == world.old and world.bao.data(CATALOG) == world.catalog
        assert world.key.copy.read_text() == world.old
        assert world.key.login.read_text() == world.old
        assert world.text() == world.old_public + "\n"
        assert world.messages() == [
            f"{MESSAGE} (secret-rotator)",
            f'revert "{MESSAGE}" (secret-rotator)',
        ]
        assert world.generation() == 3  # rolled again after the KV rollback
        assert world.vm.status == "stopped"

    def test_a_commit_github_refuses_fails_before_the_write_and_the_new_key_comes_off(
        self, tmp_path
    ):
        world = World(tmp_path)
        world.bao.leaves[CREDENTIALS]["data"]["token"] = "SECRET-a-token-github-refuses"
        executor = world.executor()
        assert executor.run() is Outcome.FAILED
        assert world.failure().step.id == COMMIT
        assert world.held() == world.old and world.messages() == []
        assert executor.abort() is Outcome.ROLLED_BACK
        assert world.undone() == [(Action.UNDO, ADD)]
        assert world.authorized() == world.everywhere(world.old_public, OTHER)

    def test_a_copy_it_cannot_write_fails_the_switch_and_the_rollback_restores_the_leaf(
        self, tmp_path
    ):
        world = World(tmp_path)
        world.key.copy.unlink()
        world.key.copy.parent.rmdir()  # as in a container without srviac's mount
        executor = world.executor()
        assert executor.run() is Outcome.FAILED
        assert world.failure().step.id == f"ssh.key_copy:{KEY}"
        assert executor.abort() is Outcome.ROLLED_BACK
        assert world.held() == world.old and world.key.login.read_text() == world.old
        assert world.authorized() == world.everywhere(world.old_public, OTHER)
        assert world.text() == world.old_public + "\n"


class TestTheReach:
    def test_a_host_that_refuses_the_login_or_its_host_key_answers(self, tmp_path):
        world = World(tmp_path)
        world.scenario(pve1="hostkey", srvk8s1="sudo")
        assert Reach(world.ssh, ALWAYS_UP).unanswered(client(world.bao)) is None

    def test_each_host_that_does_not_answer_is_named(self, tmp_path):
        world = World(tmp_path)
        world.scenario(pve1="unreachable", wrkdev="unreachable")
        assert Reach(world.ssh, ALWAYS_UP).unanswered(client(world.bao)) == (
            "pve1 does not answer: ssh: connect to host pve1 port 22: No route to host; "
            "wrkdev does not answer: ssh: connect to host wrkdev port 22: No route to host"
        )

    def test_a_host_whose_ssh_does_not_finish_does_not_answer(self, tmp_path):
        world = World(tmp_path)
        world.scenario(pve="hang")
        ssh = Ssh(world.ssh.known_hosts, world.ssh.command, bound=1)
        assert Reach(ssh, ("pve",)).unanswered(client(world.bao)) == (
            "pve does not answer: ssh did not finish within 1 s"
        )

    def test_an_attended_run_fails_there_before_anything_changed_and_aborts_clean(self, tmp_path):
        world = World(tmp_path)
        world.scenario(srvk8s1="unreachable")
        executor = world.executor()
        assert executor.run() is Outcome.FAILED
        assert world.failure().step.id == "ssh_key.reach"
        assert executor.abort() is Outcome.CANCELLED
        assert world.runs() == [] and world.held() == world.old
        assert world.vm.status == "stopped"

    def test_unattended_it_skips_the_plan(self, tmp_path):
        world = World(tmp_path)
        world.scenario(srvk8s1="unreachable")
        executor = world.executor()
        assert executor.run(unattended=True) is Outcome.SKIPPED
        assert executor.skip.step.id == "ssh_key.reach"
        assert world.runs() == [] and flight_of(world.bao, LEAF) is None
        assert world.vm.status == "stopped"

    def test_its_run_raises_no_answer(self, tmp_path):
        world = World(tmp_path)
        world.scenario(pve="unreachable")

        class Ctx:
            bao = client(world.bao)

        with pytest.raises(NoAnswer, match="^pve does not answer: ssh: connect to host pve"):
            Reach(world.ssh, ("pve",)).run(Ctx())


class Nights:
    """Nightly runs over a world's store, the kind alone enabled; the seed's leaf is due."""

    def __init__(self, world):
        self.world = world
        self.youtrack = FakeYouTrack()
        self.telegram = FakeTelegram()
        self.lines = []
        self.ticks = itertools.count()

    def __call__(self):
        self.lines.clear()
        switches = Switches(
            dry_run=False,
            paused=False,
            kinds_enabled=frozenset({KIND}),
            max_rotations_per_run=10,
            card_tag=TAG,
            telegram_chat_id=CHAT,
        )
        world = self.world
        return nightly.run(
            client(world.bao),
            Cluster(world.cluster.kube()),
            {KIND: world.kind},
            switches,
            youtrack=lambda token: YouTrack(token, opener=self.youtrack),
            telegram=lambda token, chat: Telegram(token, chat, opener=self.telegram),
            out=self.lines.append,
            holder="run on srviac, pid 7",
            now=lambda: NOW + datetime.timedelta(microseconds=next(self.ticks)),
            pve=world.pve,
            ansible=world.ansible,
            ssh=world.ssh,
            github=world.github.github(),
            ansible_key=world.key,
        )

    def card(self):
        (issue,) = self.youtrack.open()
        return issue["description"]


class TestTheNight:
    def test_the_night_rotates_it_starting_srvk8sdev_and_shutting_it_down(self, tmp_path):
        world = World(tmp_path)
        nights = Nights(world)
        assert nights() == 0
        new = world.held()
        assert new != world.old
        assert world.authorized() == world.everywhere(public_line(new, COMMENT), OTHER)
        assert world.key.copy.read_text() == new
        assert world.text() == public_line(new, COMMENT) + "\n"
        assert world.pve_cluster.commands() == [("pve", "start"), ("pve", "shutdown")]
        assert f"    {DEV_VM} shut down on pve" in nights.lines
        assert "Rotated 1:" in nights.telegram.messages[-1]

    def test_an_always_up_host_that_does_not_answer_skips_the_plan_onto_the_card(self, tmp_path):
        world = World(tmp_path)
        world.scenario(srvk8s1="unreachable")
        nights = Nights(world)
        assert nights() == 0
        why = "srvk8s1 does not answer: ssh: connect to host srvk8s1 port 22: No route to host"
        assert f"    skipped: {why}" in nights.lines
        assert f"- `{LEAF}`: its {KIND} plan of {KEY}: {why}" in nights.card()
        assert not any("Failed" in m or "rolls it back" in m for m in nights.telegram.messages)
        state = state_of(world.bao, LEAF)
        assert state.stamps == {} and state.failed_nights == 0
        assert world.held() == world.old and flight_of(world.bao, LEAF) is None
        assert world.runs() == [] and world.messages() == []
        assert world.pve_cluster.commands() == []  # skipped before its VMs were started

    def test_a_vm_that_does_not_come_up_skips_the_plan_and_is_shut_down_again(self, tmp_path):
        world = World(tmp_path)
        world.vm.agent = None  # its guest agent never answers
        nights = Nights(world)
        assert nights() == 0
        why = (
            f"{DEV_VM} did not answer within 15 min: its guest agent does not answer: qm guest "
            f"cmd 919 ping on pve exited 255: QEMU guest agent is not running"
        )
        assert f"    skipped: {why}" in nights.lines
        assert f"- `{LEAF}`: its {KIND} plan of {KEY}: {why}" in nights.card()
        assert not any("Failed" in m or "rolls it back" in m for m in nights.telegram.messages)
        assert state_of(world.bao, LEAF).failed_nights == 0
        assert world.held() == world.old and flight_of(world.bao, LEAF) is None
        assert world.runs() == [] and world.messages() == []
        assert world.pve_cluster.commands() == [("pve", "start"), ("pve", "shutdown")]
        assert world.vm.status == "stopped"
