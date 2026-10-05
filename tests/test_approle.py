"""The approle kind (design §6) over the seed's rows (catalog § rotator/): its args; its plans, one
per delivery; the mint with its expiry recorded as rotator_expires_at; the proving login; each
delivery run, failed and rolled back; and the destroy of the accessor the consumer held before,
told apart or refused. OpenBao's AppRole answers are those witnessed on OpenBao 2.5.4."""

import base64
import json
import sys
from pathlib import Path

import pytest
from fake_cluster import FakeCluster, compliant_objects, pod_spec, secret, workload
from fake_jenkins import APPROLE as JENKINS_CREDENTIAL
from fake_jenkins import CREDENTIALS, FakeJenkins
from fake_openbao import approle
from plans import NOW, Recorder, client, fake_of, lock
from test_activation import ticking
from test_kinds import KINDS

from secret_rotator import annotate as ann
from secret_rotator.ansiblesteps import Ansible
from secret_rotator.audit import audit
from secret_rotator.cluster import Cluster
from secret_rotator.contract import EXPIRES_AT, MARKER_VALUE, parse_args
from secret_rotator.executor import Abandon, AbortRefused, Executor, Outcome
from secret_rotator.kinds.approle import AppRole
from secret_rotator.kinds.approle.steps import NEW, OLD, SECRET, DestroyOldAccessor, Mint
from secret_rotator.model import StepFailed, value_name
from secret_rotator.opsteps import ShowRequest
from secret_rotator.plan import PlanError, make

FAKE_PLAYBOOK = str(Path(__file__).with_name("fake_ansible_playbook.py"))
SEED = ann.load_seed(ann.DEFAULT_SEED)
ROTATOR = "iac/rotator-approle"  # kv
ESO = "rotator/approle/eso"  # k8s_secret=external-secrets-prd/openbao-eso-approle
ESO_DEV = "rotator/approle/eso-dev"  # never
JENKINS = "rotator/approle/jenkins"  # jenkins_credential=jenkins-vault-approle
BACKUP = "rotator/approle/backup"  # playbook
IAC_AGENT = "rotator/approle/iac-agent"  # manual, 90d
ADMIN = "rotator/approle/openbao-admin"  # manual, 90d
ESO_SECRET = ("external-secrets-prd", "openbao-eso-approle")
ESO_DEPLOYMENT = "external-secrets-prd/deployment/external-secrets-prd"
MARKED = f"{MARKER_VALUE}; rotated 2026-10-05T04:30:00+00:00"
# NOW plus the ttl, as a UTC date: OpenBao reports it as the previous evening in its host's zone.
IN_90_DAYS, IN_360_DAYS = "2027-01-03", "2027-09-30"


def seed_store():
    return ann.offline_store(Path(str(ann.DEFAULT_KEYS)), SEED, lambda line: None)


def roles():
    """Each AppRole with the secret_id its consumer holds, old-<role>; rotator and eso also with
    a stray one no consumer holds."""
    found = {
        r: approle(f"role-id-of-{r}", **{f"SECRET-old-{r}": f"accessor-old-{r}"})
        for r in ("backup", "eso", "iac-agent", "jenkins", "openbao-admin")
    }
    for r in ("rotator", "eso"):
        found[r] = approle(
            f"role-id-of-{r}",
            **{f"SECRET-old-{r}": f"accessor-old-{r}", f"SECRET-stray-{r}": f"accessor-stray-{r}"},
        )
    return found


def bao_of(store):
    data = {path: {"secret_id": MARKER_VALUE} for path in SEED.markers if path in store}
    data[ROTATOR] = {"role_id": "role-id-of-rotator", "secret_id": "SECRET-old-rotator"}
    data["rotator/jenkins"] = dict(CREDENTIALS)
    bao = fake_of(store, data)
    bao.approles = roles()
    return bao


def cluster_fake():
    return FakeCluster(
        [
            *compliant_objects(),
            ("secrets", secret(*ESO_SECRET, role_id="role-id-of-eso", secret_id="SECRET-old-eso")),
            (
                "deployments",
                workload("Deployment", "external-secrets-prd", "external-secrets-prd", pod_spec()),
            ),
        ]
    )


class World:
    """The seed's store on the fake OpenBao, and the cluster, Jenkins and Ansible a plan reaches."""

    def __init__(self, tmp_path, scenario="ok", store=None):
        self.store = store or seed_store()
        self.bao = bao_of(self.store)
        self.cluster = cluster_fake()
        self.jenkins = FakeJenkins()
        self.tmp_path = tmp_path
        self.ansible = Ansible(tmp_path, (sys.executable, FAKE_PLAYBOOK, scenario))

    def plan(self, leaf, *, cluster=True):
        return make(
            KINDS,
            leaf,
            "approle",
            ["secret_id"],
            self.store,
            audit(self.store),
            Cluster(self.cluster.kube()) if cluster else None,
            jenkins=self.jenkins.jenkins(),
            ansible=self.ansible,
        )

    def executor(self, leaf, *answers):
        self.recorder = Recorder(*answers)
        return Executor(
            client(self.bao),
            self.plan(leaf),
            self.recorder,
            lock(self.bao),
            dry_run=False,
            clock=ticking(),
        )

    def run(self, leaf, *answers):
        return self.executor(leaf, *answers).run()

    def live(self, role):
        return self.bao.live(role)

    def minted(self, role):
        """The ttls the mints asked for."""
        return [
            body["ttl"]
            for method, path, _, body, _ in self.bao.requests
            if (method, path) == ("POST", f"auth/approle/role/{role}/secret-id")
        ]

    def secret_value(self):
        obj = self.cluster.get("secrets", *ESO_SECRET)
        return base64.b64decode(obj["data"]["secret_id"]).decode()

    def texts(self):
        """Every detail, error and technical text the run reported."""
        return [
            text
            for e in self.recorder.events
            for text in (
                getattr(e, "detail", ""),
                getattr(e, "error", ""),
                getattr(e, "technical", ""),
            )
        ]


def ids(plan):
    return [s.id for s in plan.steps]


class TestTheArgs:
    def test_every_approle_row_of_the_seed_is_accepted(self):
        rows = [m for m in SEED.annotations.values() if m.get("rotation_mechanism") == "approle"]
        assert len(rows) == 7
        for meta in rows:
            assert AppRole().args_problems(parse_args(meta["rotation_args"])) == [], meta

    @pytest.mark.parametrize(
        ("args", "problem"),
        [
            ({}, "role: not an AppRole name"),
            ({"role": "eso"}, "delivery: not kv, k8s_secret=<namespace>/<name>"),
            ({"role": "a/b", "delivery": "kv"}, "role: not an AppRole name"),
            ({"role": "eso", "delivery": "vault"}, "delivery: not kv"),
            ({"role": "eso", "delivery": "k8s_secret=Bad/Name"}, "delivery: not kv"),
            ({"role": "eso", "delivery": "k8s_secret=only-a-name"}, "delivery: not kv"),
            ({"role": "jenkins", "delivery": "jenkins_credential= "}, "delivery: not kv"),
            ({"role": "iac-agent", "delivery": "manual="}, "delivery: not kv"),
            ({"role": "eso", "delivery": "playbook"}, "playbook delivers the backup role's"),
            ({"role": "eso", "delivery": "kv", "ttl": "1h"}, "ttl: not one of approle's"),
        ],
    )
    def test_args_it_cannot_use_are_named(self, args, problem):
        problems = AppRole().args_problems(args)
        assert any(p.startswith(problem) or problem in p for p in problems), problems


class TestThePlans:
    @pytest.mark.parametrize(
        ("leaf", "delivery"),
        [
            (ROTATOR, ["kv.write"]),
            (
                ESO,
                [
                    "approle.k8s_secret:external-secrets-prd/openbao-eso-approle",
                    "kv.write",
                    f"k8s.rollout:{ESO_DEPLOYMENT}",
                ],
            ),
            (JENKINS, [f"jenkins.credential:{JENKINS_CREDENTIAL}", "kv.write"]),
            (BACKUP, ["ansible.run:openbao-backup-secret-id", "kv.write"]),
            (IAC_AGENT, [f"operator.show:{SECRET}", "kv.write"]),
            (ADMIN, [f"operator.show:{SECRET}", "kv.write"]),
        ],
    )
    def test_mint_login_delivery_write_activation_then_the_destroy_and_the_stamp(
        self, tmp_path, leaf, delivery
    ):
        plan = World(tmp_path).plan(leaf)
        marker = [] if leaf == ROTATOR else ["approle.marker:secret_id"]
        assert ids(plan) == [
            *marker,
            "approle.mint",
            "approle.login",
            *delivery,
            "approle.destroy_old_accessor",
            "kv.stamp",
        ]

    def test_only_a_manual_delivery_needs_the_operator(self, tmp_path):
        world = World(tmp_path)
        needs = {leaf: world.plan(leaf).needs_operator for leaf in (ROTATOR, ESO, JENKINS, BACKUP)}
        assert needs == dict.fromkeys(needs, False)
        assert world.plan(IAC_AGENT).needs_operator and world.plan(ADMIN).needs_operator

    @pytest.mark.parametrize(
        ("leaf", "days"), [(ROTATOR, 90), (ESO, 90), (JENKINS, 90), (BACKUP, 90), (ADMIN, 360)]
    )
    def test_the_expiry_is_four_times_the_interval_and_never_less_than_90_days(
        self, tmp_path, leaf, days
    ):
        mint = World(tmp_path).plan(leaf).steps[0 if leaf == ROTATOR else 1]
        assert mint.id == "approle.mint" and mint.days == days

    def test_a_key_that_rotates_never_has_no_plan(self, tmp_path):
        with pytest.raises(PlanError, match="secret_id rotates never"):
            World(tmp_path).plan(ESO_DEV)

    def test_offline_the_k8s_secret_delivery_has_no_plan(self, tmp_path):
        with pytest.raises(PlanError, match="writes Secret external-secrets-prd/openbao-eso"):
            World(tmp_path).plan(ESO, cluster=False)

    def test_args_the_kind_refuses_refuse_the_plan(self, tmp_path):
        world = World(tmp_path)
        world.store[ESO].meta["rotation_args"] = '{"role":"eso","delivery":"playbook"}'
        with pytest.raises(PlanError, match="rotation_args: delivery: playbook delivers"):
            world.plan(ESO)

    def test_the_ask_and_the_description_say_who_does_what(self, tmp_path):
        world = World(tmp_path)
        agent, eso = world.plan(IAC_AGENT), world.plan(ESO)
        assert agent.ask == "put the new iac-agent secret_id in place"
        assert agent.description == (
            "The tool mints a new iac-agent secret_id that expires in 360 days and logs in with "
            "it. You put it in place: Paste it as OPENBAO_SECRET_ID in srviac "
            "/etc/iac/secrets.yaml. The tool records the rotation on this marker leaf, then "
            "destroys the one its consumer held before."
        )
        assert eso.ask == ""
        assert eso.description == (
            "The tool mints a new eso secret_id that expires in 90 days, logs in with it, writes "
            "it into Secret external-secrets-prd/openbao-eso-approle and activates what reads it, "
            "then destroys the one Secret external-secrets-prd/openbao-eso-approle held before."
        )


class TestTheRuns:
    def test_kv_writes_the_leaf_and_destroys_the_secret_id_it_held(self, tmp_path):
        world = World(tmp_path)
        assert world.run(ROTATOR) is Outcome.DONE
        (new,) = set(world.live("rotator")) - {"SECRET-old-rotator", "SECRET-stray-rotator"}
        assert world.bao.data(ROTATOR) == {"role_id": "role-id-of-rotator", "secret_id": new}
        # The leaf told which one the consumer held: the stray is no business of this plan.
        assert set(world.live("rotator")) == {new, "SECRET-stray-rotator"}
        assert world.minted("rotator") == ["2160h"]
        meta = world.bao.meta(ROTATOR)
        assert meta[EXPIRES_AT] == IN_90_DAYS and meta["rotated_at_secret_id"] == "2026-10-05"
        assert not any(new in text for text in world.texts())

    def test_k8s_secret_writes_the_secret_rolls_eso_and_destroys_the_one_it_held(self, tmp_path):
        world = World(tmp_path)
        assert world.run(ESO) is Outcome.DONE
        new = world.secret_value()
        assert set(world.live("eso")) == {new, "SECRET-stray-eso"}
        assert world.cluster.get("secrets", *ESO_SECRET)["data"]["role_id"] == (
            base64.b64encode(b"role-id-of-eso").decode()
        )
        rolled = world.cluster.get("deployments", "external-secrets-prd", "external-secrets-prd")
        assert rolled["metadata"]["generation"] == 2
        assert world.bao.data(ESO) == {"secret_id": MARKED}
        assert world.bao.meta(ESO)[EXPIRES_AT] == IN_90_DAYS

    def test_jenkins_credential_updates_it_and_destroys_the_role_s_only_other_one(self, tmp_path):
        world = World(tmp_path)
        assert world.run(JENKINS) is Outcome.DONE
        (new,) = world.live("jenkins")
        assert (
            new.startswith("SECRET-jenkins-new") and world.jenkins.secret(JENKINS_CREDENTIAL) == new
        )
        assert world.bao.data(JENKINS) == {"secret_id": MARKED}

    def test_a_role_whose_consumer_cannot_be_told_apart_fails_before_anything_is_minted(
        self, tmp_path
    ):
        world = World(tmp_path)
        world.bao.approles["jenkins"]["secret_ids"]["SECRET-stray"] = {"accessor": "a-2", "ttl": 0}
        executor = world.executor(JENKINS)
        assert executor.run() is Outcome.FAILED
        (failure,) = world.recorder.failures()
        assert failure.step.id == "approle.mint"
        assert failure.error == (
            "AppRole jenkins has 2 secret_ids, and which one Jenkins credential "
            "jenkins-vault-approle holds cannot be read: destroy the ones no consumer holds first"
        )
        assert world.minted("jenkins") == []
        assert executor.abort() is Outcome.ROLLED_BACK
        assert len(world.live("jenkins")) == 2 and EXPIRES_AT not in world.bao.meta(JENKINS)
        assert world.bao.data(JENKINS) == {"secret_id": MARKER_VALUE}

    def test_playbook_hands_the_new_secret_id_over_in_its_extra_vars_file(self, tmp_path):
        world = World(tmp_path)
        assert world.run(BACKUP) is Outcome.DONE
        (new,) = world.live("backup")
        report = json.loads((tmp_path / "report.json").read_text())
        assert report["extra_vars"] == {"openbao_backup_secret_id": new}
        assert report["argv"][:3] == [
            "playbooks/openbao-backup-secret-id.yml",
            "--tags",
            "openbao_backup_secret_id",
        ]
        assert new not in " ".join(report["argv"])

    def test_a_failed_playbook_cannot_be_rolled_back(self, tmp_path):
        world = World(tmp_path, "failed")
        executor = world.executor(BACKUP)
        assert executor.run() is Outcome.FAILED
        with pytest.raises(AbortRefused, match="previous backup secret_id is not known"):
            executor.abort()
        assert "SECRET-old-backup" in world.live("backup")

    def test_manual_shows_the_new_secret_id_and_records_the_rotation(self, tmp_path):
        world = World(tmp_path)
        assert world.run(IAC_AGENT, {}) is Outcome.DONE
        (new,) = world.live("iac-agent")
        ((step, request),) = world.recorder.asked
        assert step == f"operator.show:{SECRET}" and isinstance(request, ShowRequest)
        assert request.value == new
        assert (
            request.instruction == "Paste it as OPENBAO_SECRET_ID in srviac /etc/iac/secrets.yaml"
        )
        assert world.minted("iac-agent") == ["8640h"]
        assert world.bao.meta(IAC_AGENT)[EXPIRES_AT] == IN_360_DAYS
        assert world.bao.data(IAC_AGENT) == {"secret_id": MARKED}

    def test_aborted_at_the_show_the_new_secret_id_is_destroyed_and_the_expiry_put_back(
        self, tmp_path
    ):
        world = World(tmp_path)
        world.bao.leaves[IAC_AGENT]["meta"][EXPIRES_AT] = "2026-12-01"
        assert world.run(IAC_AGENT, Abandon.ABORT) is Outcome.ROLLED_BACK
        assert world.live("iac-agent") == {"SECRET-old-iac-agent": "accessor-old-iac-agent"}
        assert world.bao.meta(IAC_AGENT)[EXPIRES_AT] == "2026-12-01"
        assert world.bao.data(IAC_AGENT) == {"secret_id": MARKER_VALUE}

    def test_once_put_in_place_a_manual_delivery_cannot_be_aborted(self, tmp_path):
        world = World(tmp_path)
        world.bao.refuse["PATCH", f"kv/data/{ADMIN}"] = 403
        executor = world.executor(ADMIN, {})
        assert executor.run() is Outcome.FAILED
        with pytest.raises(AbortRefused, match="new openbao-admin secret_id is in place"):
            executor.abort()

    def test_a_refused_login_stops_before_the_delivery_and_rolls_back(self, tmp_path):
        world = World(tmp_path)
        world.bao.refused_logins.add("rotator")
        executor = world.executor(ROTATOR)
        assert executor.run() is Outcome.FAILED
        (failure,) = world.recorder.failures()
        assert failure.step.id == "approle.login"
        assert failure.error.startswith("the login as rotator with the new secret_id is refused")
        assert world.bao.data(ROTATOR)["secret_id"] == "SECRET-old-rotator"
        assert executor.abort() is Outcome.ROLLED_BACK
        assert set(world.live("rotator")) == {"SECRET-old-rotator", "SECRET-stray-rotator"}
        assert EXPIRES_AT not in world.bao.meta(ROTATOR)

    def test_a_rollback_puts_the_secret_back_and_rolls_eso_again(self, tmp_path):
        world = World(tmp_path)
        world.cluster.stuck.add("external-secrets-prd/external-secrets-prd")
        executor = world.executor(ESO)
        assert executor.run() is Outcome.FAILED
        assert world.recorder.failures()[0].step.id == f"k8s.rollout:{ESO_DEPLOYMENT}"
        world.cluster.stuck.clear()
        assert executor.abort() is Outcome.ROLLED_BACK
        assert world.secret_value() == "SECRET-old-eso"
        assert set(world.live("eso")) == {"SECRET-old-eso", "SECRET-stray-eso"}
        rolled = world.cluster.get("deployments", "external-secrets-prd", "external-secrets-prd")
        assert rolled["metadata"]["generation"] == 3  # the rollout, then its re-run
        assert world.bao.data(ESO) == {"secret_id": MARKER_VALUE}

    def test_the_mount_s_cap_is_the_expiry_recorded(self, tmp_path):
        world = World(tmp_path)
        world.store[IAC_AGENT].meta["rotation_interval"] = "120d"
        assert world.run(IAC_AGENT, {}) is Outcome.DONE
        assert world.minted("iac-agent") == ["11520h"]
        assert world.bao.meta(IAC_AGENT)[EXPIRES_AT] == IN_360_DAYS

    def test_a_marker_leaf_that_holds_a_credential_is_not_overwritten(self, tmp_path):
        world = World(tmp_path)
        world.bao.leaves[JENKINS]["data"] = {"secret_id": "SECRET-a-real-secret-id"}
        assert world.run(JENKINS) is Outcome.FAILED
        assert world.recorder.failures()[0].step.id == "approle.marker:secret_id"
        assert world.minted("jenkins") == []


class Ctx:
    def __init__(self, bao, staged=None):
        self.bao = client(bao)
        self.now = NOW
        self.values = dict(staged or {})

    def stage(self, name, value):
        self.values[name] = value

    def staged(self, name):
        return self.values.get(name)


class TestTheSteps:
    def bao(self):
        return bao_of(seed_store())

    def test_a_mint_resumed_after_its_secret_id_was_staged_mints_no_other(self):
        bao = self.bao()
        mint = Mint("jenkins", JENKINS, SECRET, 90, "Jenkins", None)
        ctx = Ctx(bao)
        mint.run(ctx)
        del ctx.values[NEW]
        assert mint.run(ctx) == f"expires {IN_90_DAYS}"
        assert bao.minted == 1 and ctx.staged(NEW) == "accessor-jenkins-new-1"

    def test_a_mint_retried_keeps_the_accessor_it_staged_as_the_consumer_s(self):
        bao = self.bao()
        # A first attempt staged the old accessor, minted, and lost the answer.
        bao.approles["jenkins"]["secret_ids"]["SECRET-lost"] = {"accessor": "lost", "ttl": 7776000}
        ctx = Ctx(bao, {OLD: "accessor-old-jenkins"})
        Mint("jenkins", JENKINS, SECRET, 90, "Jenkins", None).run(ctx)
        assert ctx.staged(OLD) == "accessor-old-jenkins"

    def test_a_consumer_holding_a_secret_id_that_is_not_live_leaves_nothing_to_destroy(self):
        bao = self.bao()
        ctx = Ctx(bao)
        Mint("eso", ESO, SECRET, 90, "Secret", lambda ctx: "SECRET-long-gone").run(ctx)
        assert ctx.staged(OLD) == ""
        assert DestroyOldAccessor("eso", "Secret").run(ctx) == (
            "nothing to destroy: it held no live secret_id"
        )
        assert set(bao.live("eso")) == {"SECRET-old-eso", "SECRET-stray-eso", ctx.staged(SECRET)}

    def test_a_destroy_run_again_finds_the_accessor_gone(self):
        bao = self.bao()
        ctx = Ctx(bao, {OLD: "accessor-old-jenkins"})
        destroy = DestroyOldAccessor("jenkins", "Jenkins")
        assert destroy.run(ctx) == "accessor accessor-old-jenkins destroyed"
        assert destroy.run(ctx) == "accessor accessor-old-jenkins is gone already"
        assert destroy.undo is None and destroy.mutates

    def test_a_role_openbao_does_not_have_fails_the_mint(self):
        bao = self.bao()
        del bao.approles["jenkins"]
        with pytest.raises(StepFailed, match="OpenBao has no AppRole jenkins"):
            Mint("jenkins", JENKINS, SECRET, 90, "Jenkins", None).run(Ctx(bao))

    def test_the_kv_delivery_stages_the_secret_id_for_its_kv_write(self, tmp_path):
        plan = World(tmp_path).plan(ROTATOR)
        assert plan.steps[0].secret == value_name("secret_id")
