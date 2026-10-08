"""The keycloak-client kind (design §6) over the seed's rows: its args and each row's realm; its
plans; the regenerate as the realm's counterpart client, read back, re-run without regenerating
again, and the failures that did not land; OpenBao's own client written into auth/oidc/config with
every other field kept; and a counterpart that rotates its own secret."""

import urllib.error
from pathlib import Path

import pytest
from fake_cluster import FakeCluster, compliant_objects
from fake_keycloak import COUNTERPART, FakeKeycloak, FakeResponse
from fake_openbao import oidc_config
from fixtures import compliant_store
from plans import Recorder, client, fake_of, lock, run_state, state_of
from test_activation import ticking

from secret_rotator import annotate as ann
from secret_rotator.audit import audit
from secret_rotator.cluster import Cluster
from secret_rotator.executor import AbortRefused, Executor, Outcome
from secret_rotator.kinds.keycloak_client import REALMS, KeycloakClient, counterpart
from secret_rotator.kinds.keycloak_client.keycloak import Keycloak, KeycloakError
from secret_rotator.plan import PlanError, make

SEED = ann.load_seed(ann.DEFAULT_SEED)
KIND = "keycloak-client"
OIDC_AUTH = "rotator/oidc-auth-client"  # client openbao, target auth/oidc/config
HOMELAB, HOMELAB_DEV = counterpart("homelab"), counterpart("homelab-dev")
DEV = "eso/dev/electronics-inventory/dev/oidc"  # homelab-dev, its client_id, activate none
PIPELINE = "jenkins/iotsupport-pipeline-oidc"  # homelab, its client_id, activate none
APP = "eso/prd/app/prd/oidc"  # the fixture's: homelab, auto, copied into eso/prd/kc/prd/catalog
CATALOG = "eso/prd/kc/prd/catalog"
DATA = {
    HOMELAB: {"client_id": COUNTERPART, "client_secret": "SECRET-homelab-counterpart"},
    HOMELAB_DEV: {"client_id": COUNTERPART, "client_secret": "SECRET-homelab-dev-counterpart"},
    OIDC_AUTH: {"client_secret": "SECRET-old-openbao"},
    DEV: {"client_id": "electronics-inventory", "client_secret": "SECRET-old-ei-dev"},
    PIPELINE: {"client_id": "iotsupport-pipeline", "client_secret": "SECRET-old-pipeline"},
    APP: {"client_id": "app", "client_secret": "SECRET-old-app"},
    CATALOG: {"client-id": "app", "client-secret": "SECRET-old-app", "jenkins-user": "admin"},
}
CLIENTS = [
    ("homelab", "openbao", "SECRET-old-openbao"),
    ("homelab-dev", "electronics-inventory", "SECRET-old-ei-dev"),
    ("homelab", "iotsupport-pipeline", "SECRET-old-pipeline"),
    ("homelab", "app", "SECRET-old-app"),
]
TOKEN_PATH = "/realms/homelab/protocol/openid-connect/token"
SECRET_PATH = "/admin/realms/homelab/clients/uuid-of-homelab-{}/client-secret"


def seed_store():
    return ann.offline_store(Path(str(ann.DEFAULT_KEYS)), SEED, lambda line: None)


def rows():
    """Every keycloak-client row of the seed, by leaf."""
    found = SEED.leaves.items()
    return {path: leaf.default for path, leaf in found if leaf.default.get("kind") == KIND}


class World:
    """A store on the fake OpenBao, the counterparts' leaves and OpenBao's OIDC config with it, and
    the Keycloak and cluster a plan reaches."""

    def __init__(self, store=None):
        self.store = store or seed_store()
        self.keycloak = FakeKeycloak()
        for realm, client_id, secret in CLIENTS:
            self.keycloak.add(realm, client_id, secret)
        self.bao = fake_of(self.store, DATA)
        for leaf in (HOMELAB, HOMELAB_DEV):
            self.bao.leaves.setdefault(leaf, {"data": dict(DATA[leaf]), "meta": {}})
        self.bao.oidc = oidc_config()
        self.cluster = FakeCluster(compliant_objects())
        self.kind = KeycloakClient(opener=self.keycloak)

    def plan(self, leaf, *, cluster=False):
        return make(
            {KIND: self.kind},
            leaf,
            KIND,
            ["client_secret"],
            audit(self.store),
            Cluster(self.cluster.kube()) if cluster else None,
        )

    def executor(self, leaf, *, cluster=False):
        self.recorder = Recorder()
        return Executor(
            client(self.bao),
            self.plan(leaf, cluster=cluster),
            self.recorder,
            lock(self.bao),
            state=run_state(self.bao),
            dry_run=False,
            clock=ticking(),
        )

    def run(self, leaf, *, cluster=False):
        return self.executor(leaf, cluster=cluster).run()

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


def ids(plan):
    return [s.id for s in plan.steps]


class TestTheArgs:
    def test_every_row_of_the_seed_is_accepted_and_names_its_realm(self):
        found = rows()
        assert len(found) == 16
        for leaf, row in found.items():
            assert KeycloakClient().args_problems(row["args"]) == [], leaf
        realms = {leaf: row["args"]["realm"] for leaf, row in found.items()}
        dev = {DEV, HOMELAB_DEV, "jenkins/keycloak-da-admin", "jenkins/keycloak-iotsupport-admin"}
        assert {leaf for leaf, realm in realms.items() if realm == "homelab-dev"} == dev
        assert set(realms.values()) == {"homelab", "homelab-dev"}

    def test_the_realms_reached_by_their_issuers_url(self):
        # Their consumers' issuer is https://auth.ginbov.nl/realms/homelab (2026-10-08).
        for leaf in (
            "eso/prd/dnsmasq/prd/oidc",
            "eso/prd/electronics-inventory/prd/oidc",
            "eso/prd/fieldnotes/prd/oidc",
            "eso/prd/grafana/prd/oidc",
            "eso/prd/iot/prd/oidc",
            "eso/prd/pgadmin/prd/oidc",
            "eso/prd/zigbee2mqtt/prd/oidc",
            PIPELINE,
        ):
            assert rows()[leaf]["args"] == {"realm": "homelab"}, leaf

    def test_the_counterparts_and_openbao_s_own_client(self):
        found = rows()
        for realm in REALMS:
            row = found[counterpart(realm)]
            assert row["args"] == {"realm": realm}
            assert (row["interval"], row["activate"]) == ("365d", "none")
        assert found[OIDC_AUTH]["args"] == {
            "realm": "homelab",
            "client": "openbao",
            "target": "auth/oidc/config",
        }
        assert (found[OIDC_AUTH]["interval"], found[OIDC_AUTH]["activate"]) == ("14d", "none")
        assert SEED.markers.get(OIDC_AUTH) is None

    def test_each_realm_is_reached_at_one_base_url(self):
        assert REALMS == {
            "homelab": "https://auth.ginbov.nl",
            "homelab-dev": "http://keycloak-dev.home",
        }

    @pytest.mark.parametrize(
        ("args", "problem"),
        [
            ({}, "realm: not homelab or homelab-dev"),
            ({"realm": "master"}, "realm: not homelab or homelab-dev"),
            ({"realm": ["homelab"]}, "realm: not homelab or homelab-dev"),
            ({"realm": "homelab", "client": " app"}, "client: not a client id"),
            ({"realm": "homelab", "client": ["app"]}, "client: not a client id"),
            ({"realm": "homelab", "target": "sys/auth"}, "target: not auth/oidc/config"),
            ({"realm": "homelab", "url": "x"}, "url: not one of keycloak-client's"),
        ],
    )
    def test_args_it_cannot_use_are_named(self, args, problem):
        problems = KeycloakClient().args_problems(args)
        assert any(p.startswith(problem) for p in problems), problems


class TestThePlans:
    @pytest.mark.parametrize("leaf", [HOMELAB, HOMELAB_DEV, DEV, PIPELINE])
    def test_regenerate_write_and_stamp_where_nothing_is_activated(self, leaf):
        assert ids(World().plan(leaf)) == ["keycloak.regenerate", "kv.write", "kv.stamp"]

    def test_openbao_s_own_client_is_also_put_into_its_oidc_config(self):
        assert ids(World().plan(OIDC_AUTH)) == [
            "keycloak.regenerate",
            "kv.write",
            "keycloak.openbao_oidc_config",
            "kv.stamp",
        ]

    def test_the_copies_and_the_activation_come_after_the_regenerate(self):
        world = World(compliant_store())
        assert ids(world.plan(APP, cluster=True)) == [
            "keycloak.regenerate",
            "kv.write",
            f"kv.copy:{CATALOG}#client-secret",
            "eso.sync:app-prd/app-oidc",
            "eso.sync:kubecoder-prd/kubecoder-secret-catalog",
            "k8s.rollout:app-prd/daemonset/app-agent",
            "k8s.rollout:app-prd/deployment/app",
            "k8s.rollout:kubecoder-prd/deployment/kubecoder-controller",
            "kv.stamp",
        ]

    def test_no_plan_needs_the_operator_and_no_regenerate_has_an_undo(self):
        world = World()
        for leaf in (HOMELAB, DEV, OIDC_AUTH):
            plan = world.plan(leaf)
            assert not plan.needs_operator and plan.ask == ""
            regenerate, *_ = plan.steps
            assert regenerate.mutates and regenerate.undo is None and regenerate.no_undo
        config = world.plan(OIDC_AUTH).steps[2]
        assert config.mutates and config.undo is None
        assert config.no_undo == (
            "the client secret OpenBao's auth/oidc/config held is the one Keycloak ended"
        )

    def test_the_titles_and_the_description_name_the_client_and_its_realm(self):
        world = World()
        own, dev = world.plan(OIDC_AUTH), world.plan(DEV)
        assert own.steps[0].title == (
            "regenerate the secret of client openbao in Keycloak realm homelab"
        )
        assert own.description == (
            "Keycloak regenerates the secret of client openbao in realm homelab, which ends the "
            "old one. The tool writes it to the leaf and puts it into OpenBao's auth/oidc/config."
        )
        assert dev.steps[0].title == (
            "regenerate the secret of the leaf's client in Keycloak realm homelab-dev"
        )
        assert dev.description == (
            "Keycloak regenerates the secret of the client the leaf's client_id names in realm "
            "homelab-dev, which ends the old one. The tool writes it to the leaf."
        )

    def test_offline_without_a_snapshot_an_activated_leaf_has_no_plan(self):
        with pytest.raises(PlanError, match="offline plan without a snapshot does not reach"):
            World(compliant_store()).plan(APP)


class TestTheRuns:
    def test_it_regenerates_as_the_realm_s_counterpart_and_activates_the_consumers(self):
        world = World(compliant_store())
        assert world.run(APP, cluster=True) is Outcome.DONE
        new = world.keycloak.secret("homelab", "app")
        assert new == "SECRET-homelab-regenerated-1"
        assert world.bao.data(APP) == {"client_id": "app", "client_secret": new}
        assert world.bao.data(CATALOG)["client-secret"] == new
        assert world.keycloak.logins() == [("https://auth.ginbov.nl", COUNTERPART)]
        rolled = world.cluster.get("deployments", "app-prd", "app")
        assert rolled["metadata"]["generation"] == 2
        assert state_of(world.bao, APP).stamps == {"client_secret": "2026-10-05"}
        assert not any(new in text for text in world.texts())

    def test_homelab_dev_is_reached_over_http_at_keycloak_dev_home(self):
        world = World()
        assert world.run(DEV) is Outcome.DONE
        new = world.keycloak.secret("homelab-dev", "electronics-inventory")
        assert world.bao.data(DEV)["client_secret"] == new != "SECRET-old-ei-dev"
        assert {base for _, base, *_ in world.keycloak.requests} == {"http://keycloak-dev.home"}
        assert world.keycloak.logins() == [("http://keycloak-dev.home", COUNTERPART)]

    def test_openbao_s_oidc_config_takes_the_new_secret_and_keeps_every_other_field(self):
        world = World()
        assert world.run(OIDC_AUTH) is Outcome.DONE
        new = world.keycloak.secret("homelab", "openbao")
        assert world.bao.data(OIDC_AUTH) == {"client_secret": new}
        assert world.bao.oidc == oidc_config(oidc_client_secret=new)
        assert not any(new in text for text in world.texts())

    def test_a_field_the_config_write_loses_fails_the_step(self):
        world = World()
        world.bao.oidc = oidc_config(provider_config={"provider": "keycloak"}, extra="x")
        original = world.bao.oidc_config

        def drops_extra(method, body):
            return original(method, {k: v for k, v in (body or {}).items() if k != "extra"})

        world.bao.oidc_config = drops_extra
        executor = world.executor(OIDC_AUTH)
        assert executor.run() is Outcome.FAILED
        failure = world.failure()
        assert failure.step.id == "keycloak.openbao_oidc_config"
        assert failure.error == "the re-read of auth/oidc/config changed extra"
        assert world.bao.oidc["provider_config"] == {"provider": "keycloak"}

    def test_without_an_oidc_config_it_fails_after_the_regenerate_and_retry_finishes(self):
        world = World()
        world.bao.oidc = None
        executor = world.executor(OIDC_AUTH)
        assert executor.run() is Outcome.FAILED
        assert world.failure().error == (
            "OpenBao has no auth/oidc/config: its OIDC login is not set up"
        )
        with pytest.raises(AbortRefused, match="Keycloak ended the secret client openbao held"):
            executor.abort()
        world.bao.oidc = oidc_config()
        assert world.executor(OIDC_AUTH).run() is Outcome.DONE
        assert world.keycloak.regenerated == 1
        assert world.bao.oidc["oidc_client_secret"] == world.keycloak.secret("homelab", "openbao")

    def test_a_counterpart_regenerates_its_own_secret_and_the_next_plan_logs_in_with_it(self):
        world = World()
        assert world.run(HOMELAB) is Outcome.DONE
        new = world.keycloak.secret("homelab", COUNTERPART)
        assert new != "SECRET-homelab-counterpart"
        assert world.bao.data(HOMELAB) == {"client_id": COUNTERPART, "client_secret": new}
        assert world.run(OIDC_AUTH) is Outcome.DONE
        assert (
            world.keycloak.secret("homelab", "openbao")
            == world.bao.data(OIDC_AUTH)["client_secret"]
        )

    def test_a_counterpart_s_re_run_logs_in_with_the_secret_it_staged(self):
        world = World()
        own = SECRET_PATH.format(COUNTERPART)
        world.keycloak.broken["GET", own] = ConnectionResetError("reset")
        assert world.run(HOMELAB) is Outcome.FAILED
        assert world.failure().step.id == "keycloak.regenerate"
        new = world.keycloak.secret("homelab", COUNTERPART)
        # The leaf holds the secret the regenerate ended.
        assert world.bao.data(HOMELAB)["client_secret"] == "SECRET-homelab-counterpart"
        del world.keycloak.broken["GET", own]
        assert world.run(HOMELAB) is Outcome.DONE
        assert world.keycloak.regenerated == 1
        assert world.bao.data(HOMELAB)["client_secret"] == new
        requests = world.keycloak.requests
        logins = [form["client_secret"] for _, _, path, _, form in requests if path == TOKEN_PATH]
        assert logins == ["SECRET-homelab-counterpart", new]

    def test_a_re_run_re_reads_the_staged_secret_and_fails_when_keycloak_holds_another(self):
        world = World()
        path = SECRET_PATH.format("iotsupport-pipeline")
        world.keycloak.broken["GET", path] = ConnectionResetError("reset")
        assert world.run(PIPELINE) is Outcome.FAILED
        del world.keycloak.broken["GET", path]
        world.keycloak.realms["homelab"]["iotsupport-pipeline"]["secret"] = "SECRET-someone-else"
        assert world.run(PIPELINE) is Outcome.FAILED
        assert world.failure().error == (
            "the re-read of client iotsupport-pipeline's secret is not the one Keycloak regenerated"
        )
        assert world.keycloak.regenerated == 1

    @pytest.mark.parametrize(
        ("setup", "error"),
        [
            (
                lambda w: w.bao.leaves.pop(HOMELAB),
                f"{HOMELAB} cannot be read: no such leaf, or its current version is deleted",
            ),
            (
                lambda w: w.bao.leaves[HOMELAB]["data"].pop("client_id"),
                f"{HOMELAB} has no client_id",
            ),
            (
                lambda w: w.bao.leaves[HOMELAB]["data"].update(client_secret="SECRET-wrong"),
                f"Keycloak realm homelab refuses the login of {HOMELAB}'s client {COUNTERPART}: "
                f"POST {TOKEN_PATH}: HTTP 401: Invalid client or Invalid client credentials",
            ),
            (
                lambda w: w.keycloak.realms["homelab"][COUNTERPART].update(manage=False),
                "GET /admin/realms/homelab/clients?clientId=iotsupport-pipeline: HTTP 403: "
                "HTTP 403 Forbidden",
            ),
            (
                lambda w: w.keycloak.realms["homelab"].pop("iotsupport-pipeline"),
                "Keycloak realm homelab has no client iotsupport-pipeline",
            ),
            (
                lambda w: w.bao.leaves[PIPELINE]["data"].pop("client_id"),
                f"{PIPELINE} holds no client_id, and its args name no client",
            ),
            (
                lambda w: w.keycloak.refused.update(
                    {("POST", SECRET_PATH.format("iotsupport-pipeline")): 403}
                ),
                f"POST {SECRET_PATH.format('iotsupport-pipeline')}: HTTP 403: refused",
            ),
            (
                lambda w: w.keycloak.broken.update({("POST", TOKEN_PATH): OSError("unreachable")}),
                f"POST {TOKEN_PATH}: transport error: OSError('unreachable')",
            ),
        ],
    )
    def test_a_failure_before_keycloak_regenerated_leaves_abort_a_cancel(self, setup, error):
        world = World()
        setup(world)
        executor = world.executor(PIPELINE)
        assert executor.run() is Outcome.FAILED
        assert world.failure().error == error
        assert executor.abort_blocker() is None
        assert executor.abort() is Outcome.CANCELLED
        assert world.keycloak.regenerated == 0
        assert world.bao.data(PIPELINE)["client_secret"] == "SECRET-old-pipeline"

    def test_the_args_client_and_the_leaf_s_client_id_must_agree(self):
        world = World()
        world.bao.leaves[OIDC_AUTH]["data"]["client_id"] = "vault"
        executor = world.executor(OIDC_AUTH)
        assert executor.run() is Outcome.FAILED
        assert world.failure().error == (
            f"{OIDC_AUTH} holds client_id vault, not openbao, the client its args name"
        )
        assert executor.abort() is Outcome.CANCELLED
        assert world.keycloak.regenerated == 0

    @pytest.mark.parametrize(
        ("hook", "failure", "regenerated"),
        [
            # Keycloak regenerated, and its answer is a 5xx
            ("lost", 502, 2),
            # a transport error: whether Keycloak regenerated is not known
            ("broken", OSError("connection reset"), 1),
        ],
    )
    def test_a_regenerate_whose_answer_is_lost_cannot_be_aborted_and_retry_regenerates(
        self, hook, failure, regenerated
    ):
        world = World()
        getattr(world.keycloak, hook)["POST", SECRET_PATH.format("iotsupport-pipeline")] = failure
        executor = world.executor(PIPELINE)
        assert executor.run() is Outcome.FAILED
        with pytest.raises(AbortRefused, match="Keycloak ended the secret the leaf's client held"):
            executor.abort()
        getattr(world.keycloak, hook).clear()
        assert world.executor(PIPELINE).run() is Outcome.DONE
        assert world.keycloak.regenerated == regenerated
        assert world.bao.data(PIPELINE)["client_secret"] == world.keycloak.secret(
            "homelab", "iotsupport-pipeline"
        )

    def test_the_kind_logs_in_only_as_its_counterparts(self):
        world = World()
        for leaf in (DEV, PIPELINE, OIDC_AUTH, HOMELAB, HOMELAB_DEV):
            assert world.run(leaf) is Outcome.DONE, leaf
        assert {client_id for _, client_id in world.keycloak.logins()} == {COUNTERPART}


class TestTheKeycloakClient:
    def keycloak(self, fake):
        return Keycloak(REALMS["homelab"], "homelab", fake)

    def test_a_transport_failure_has_no_status(self):
        fake = FakeKeycloak()
        fake.broken["POST", TOKEN_PATH] = urllib.error.URLError("refused")
        with pytest.raises(KeycloakError) as e:
            self.keycloak(fake).login(COUNTERPART, "SECRET-homelab-counterpart")
        assert e.value.status is None
        assert e.value.error == f"POST {TOKEN_PATH}: transport error: refused"

    def test_an_answer_that_is_no_json_is_refused(self):
        def opener(req):
            return FakeResponse(200, b"<html>")

        with pytest.raises(KeycloakError, match="HTTP 200, not a JSON answer"):
            self.keycloak(opener).login(COUNTERPART, "x")

    def test_an_error_answer_without_keycloak_s_error_is_its_status(self):
        fake = FakeKeycloak()
        fake.lost["POST", TOKEN_PATH] = 502
        with pytest.raises(KeycloakError) as e:
            self.keycloak(fake).login(COUNTERPART, "SECRET-homelab-counterpart")
        assert (e.value.error, e.value.status) == (f"POST {TOKEN_PATH}: HTTP 502", 502)

    def test_a_client_it_does_not_have_has_no_id(self):
        keycloak = self.keycloak(FakeKeycloak())
        keycloak.login(COUNTERPART, "SECRET-homelab-counterpart")
        assert keycloak.client_uuid("nobody") is None
        assert keycloak.client_uuid(COUNTERPART) == f"uuid-of-homelab-{COUNTERPART}"
