"""Design §3.4's metrics, pushed to a fake Pushgateway behind the fake apiserver's service proxy:
the state group's per-key and per-leaf series over the whole store, the edge keys among them; the
findings; the nightly run's health; which process pushes which group; and a failed push, which is
a line of output and changes nothing else."""

import datetime
import io

import pytest
from fake_cluster import TOKEN, FakeCluster
from fake_openbao import ROLE_ID, SECRET_ID
from fake_pushgateway import GROUPS, SERVICE, parse
from fixtures import edit
from plans import COPY, LEAF, NOW, client, fake, fake_of, put_state, state_of
from test_kinds import ACTIVATE_NONE, KINDS, WIFI, store_of
from test_nightly import BOT_LEAF, DAY, STRAY, TODAY, TRELLO, Night, world

from secret_rotator import cli, metrics
from secret_rotator.audit import Audit, Finding
from secret_rotator.console import Console
from secret_rotator.contract import LOCK_LEAF
from secret_rotator.openbao import OpenBaoError

ES = "eso/prd/es/prd/creds"  # elastic-user password, 14 d
YOUTRACK = "jenkins/youtrack"  # youtrack-token admin-token, 14 d, with an expiry
OIDC = "eso/prd/app/prd/oidc"  # none client_id, keycloak-client client_secret
ENV = {cli.ROLE_ID_ENV: ROLE_ID, cli.SECRET_ID_ENV: SECRET_ID, cli.K8S_TOKEN_ENV: TOKEN}
HEALTH = (
    "secret_rotator_run_timestamp",
    "secret_rotator_run_success",
    "secret_rotator_run_rotations",
    "secret_rotator_run_deferred",
    "secret_rotator_dry_run",
    "secret_rotator_paused",
)


def day(text):
    return metrics.day(datetime.date.fromisoformat(text))


def pushed_state(bao):
    """The Pushgateway after a push of the state group over the fake's store."""
    cluster = FakeCluster()
    metrics.push_state(client(bao), KINDS, cluster.kube(), pytest.fail)
    assert cluster.pushes() == ["state"]
    return cluster.pushgateway


def key_of(leaf, key, kind):
    return {"leaf": leaf, "key": key, "kind": kind}


def keys_in(gateway, name):
    return {(labels["leaf"], labels["key"]) for labels, _ in gateway.of("state", name)}


def health(gateway):
    return {name: gateway.value("nightly", name) for name in HEALTH}


def test_a_date_is_unix_seconds_at_00_00_utc():
    midnight = datetime.datetime(2026, 10, 1, tzinfo=datetime.UTC)
    assert day("2026-10-01") == midnight.timestamp() == 1790812800
    assert day("1970-01-02") == 86400


class TestTheStateGroup:
    @staticmethod
    def store():
        bao = fake()
        put_state(bao, LEAF, stamps={"token": "2026-10-01"})
        put_state(bao, WIFI, stamps={"password": "2026-09-01"})
        put_state(bao, YOUTRACK, stamps={"admin-token": "2026-10-01"})
        edit(bao.leaves[YOUTRACK]["meta"], "admin-token", expires_at="2026-10-10")
        edit(bao.leaves[TRELLO]["meta"], "token", expires_at="2026-12-01")
        return bao

    def test_a_stamped_key_carries_its_stamp_and_the_day_it_falls_due(self):
        gateway = pushed_state(self.store())
        key = key_of(LEAF, "token", "random")
        info = gateway.value(
            "state", "secret_rotation_key_info", **key, interval="14", stamped="true"
        )
        assert info == 1
        assert gateway.value("state", "secret_rotation_last_rotated_timestamp", **key) == day(
            "2026-10-01"
        )
        assert gateway.value("state", "secret_rotation_due_timestamp", **key) == day("2026-10-15")

    def test_an_expiry_brings_the_due_day_forward_to_7_days_before_it(self):
        gateway = pushed_state(self.store())
        key = key_of(YOUTRACK, "admin-token", "youtrack-token")
        assert gateway.value("state", "secret_rotation_due_timestamp", **key) == day("2026-10-03")

    def test_a_key_never_stamped_is_due_at_once_and_carries_no_date(self):
        gateway = pushed_state(self.store())
        key = key_of(TRELLO, "bearer-token", "random")
        info = gateway.value(
            "state", "secret_rotation_key_info", **key, interval="14", stamped="false"
        )
        assert info == 1
        assert gateway.value("state", "secret_rotation_last_rotated_timestamp", **key) is None
        assert gateway.value("state", "secret_rotation_due_timestamp", **key) is None

    def test_a_never_key_without_an_expiry_never_falls_due(self):
        gateway = pushed_state(self.store())
        rotated_by_hand = key_of(WIFI, "password", "manual")
        info = "secret_rotation_key_info"
        assert gateway.value("state", info, **rotated_by_hand, interval="never", stamped="true")
        assert gateway.value(
            "state", "secret_rotation_last_rotated_timestamp", **rotated_by_hand
        ) == day("2026-09-01")
        assert gateway.value("state", "secret_rotation_due_timestamp", **rotated_by_hand) is None
        never_stamped = key_of(TRELLO, "api-key", "manual")
        assert gateway.value("state", info, **never_stamped, interval="never", stamped="false")
        assert gateway.value("state", "secret_rotation_due_timestamp", **never_stamped) is None

    def test_a_never_key_with_an_expiry_falls_due_7_days_before_it_like_any_other(self):
        gateway = pushed_state(self.store())
        key = key_of(TRELLO, "token", "manual")
        info = "secret_rotation_key_info"
        assert gateway.value("state", info, **key, interval="never", stamped="false") == 1
        assert gateway.value("state", "secret_rotation_due_timestamp", **key) == day("2026-11-24")

    def test_no_date_is_one_of_year_1(self):
        gateway = pushed_state(self.store())
        for name in ("secret_rotation_last_rotated_timestamp", "secret_rotation_due_timestamp"):
            assert all(value >= day("2026-01-01") for _, value in gateway.of("state", name))

    def test_only_scheduled_keys_whose_entry_the_audit_accepts_have_series(self):
        bao = self.store()
        edit(bao.leaves[ES]["meta"], "password", interval="fortnightly")
        # The manual kind's own args check, as the nightly run's audit makes it.
        edit(bao.leaves[WIFI]["meta"], "password", args={"type": "carrier-pigeon"})
        keys = keys_in(pushed_state(bao), "secret_rotation_key_info")
        assert (LEAF, "token") in keys and (OIDC, "client_secret") in keys
        assert (OIDC, "client_id") not in keys and (COPY, "token") not in keys  # none, a copy
        assert (ES, "password") not in keys and (WIFI, "password") not in keys

    def test_a_leaf_s_status_is_one_series_per_status_1_for_the_one_it_holds(self):
        bao = self.store()
        put_state(bao, BOT_LEAF, status="failed-activation")
        gateway = pushed_state(bao)
        statuses = {
            labels["status"]: value
            for labels, value in gateway.of("state", "secret_rotation_status")
            if labels["leaf"] == BOT_LEAF
        }
        assert statuses == {
            "ok": 0,
            "failed": 0,
            "failed-activation": 1,
            "manual-due": 0,
            "skipped": 0,
        }
        assert LEAF not in {
            labels["leaf"] for labels, _ in gateway.of("state", "secret_rotation_status")
        }


def test_a_finding_reaches_the_pushgateway_as_its_text_reads():
    message = 'stale: the leaf has no key "a\\b"\nand a second line'
    series = metrics.findings(Audit([Finding("x/y", "rotation_k", message)], {}))
    labels = (("key", "rotation_k"), ("leaf", "x/y"), ("message", message))
    assert parse(series.text()) == {("secret_rotation_finding", labels): 1}


def test_a_group_that_cannot_be_made_is_a_line_and_the_next_is_still_pushed():
    cluster, lines = FakeCluster(), []

    def unreadable():
        raise OpenBaoError("GET kv/metadata/x: HTTP 503: Vault is sealed", 503)

    groups = {"state": unreadable, "nightly": metrics.Series}
    assert metrics.push(cluster.kube(), groups, lines.append) == ["nightly"]
    assert lines == [
        "metrics: the state group is not pushed: GET kv/metadata/x: HTTP 503: Vault is sealed"
    ]
    assert cluster.pushes() == ["nightly"]


class TestTheNightlyRun:
    def test_it_pushes_the_state_it_left_its_findings_and_its_health_last(self):
        bao = world()
        bao.leaves[STRAY] = {"data": {"x": "SECRET-stray"}, "meta": {}}
        night = Night(bao)
        assert night() == 0
        assert night.cluster.pushes() == ["state", "audit", "nightly"]
        assert night.lines[-1] == "metrics: pushed state, audit, nightly"
        gateway = night.cluster.pushgateway
        key = key_of(LEAF, "token", "random")
        rotated = gateway.value("state", "secret_rotation_last_rotated_timestamp", **key)
        assert rotated == metrics.day(TODAY)
        due = gateway.value("state", "secret_rotation_due_timestamp", **key)
        assert due == metrics.day(TODAY + 14 * DAY)
        finding = {"leaf": STRAY, "key": "rotation_x", "message": "missing"}
        assert gateway.value("audit", "secret_rotation_finding", **finding) == 1
        assert health(gateway) == {
            "secret_rotator_run_timestamp": round(NOW.timestamp()),
            "secret_rotator_run_success": 1,
            "secret_rotator_run_rotations": 2,
            "secret_rotator_run_deferred": 0,
            "secret_rotator_dry_run": 0,
            "secret_rotator_paused": 0,
        }
        assert gateway.value("nightly", "secret_rotator_run_duration_seconds") >= 0

    def test_a_dry_run_says_so_and_counts_the_plans_it_would_have_run(self):
        night = Night()
        assert night(dry_run=True) == 0
        gateway = night.cluster.pushgateway
        assert health(gateway)["secret_rotator_dry_run"] == 1
        assert health(gateway)["secret_rotator_run_rotations"] == 2
        key = key_of(LEAF, "token", "random")
        info = "secret_rotation_key_info"
        assert gateway.value("state", info, **key, interval="14", stamped="false") == 1

    def test_the_plans_past_the_cap_are_deferred(self):
        night = Night()
        night(max_rotations_per_run=1)
        gateway = night.cluster.pushgateway
        assert health(gateway)["secret_rotator_run_rotations"] == 1
        assert health(gateway)["secret_rotator_run_deferred"] == 1

    def test_a_night_that_found_the_lock_held_pushes_its_health_alone(self):
        bao = world()
        holder = {"holder": "run x/y on c1, pid 9", "since": "2026-10-05T04:12:09+00:00"}
        bao.leaves[LOCK_LEAF] = {"data": holder | {"plan": "random plan of x/y"}, "meta": {}}
        night = Night(bao)
        assert night() == 0
        assert night.cluster.pushes() == ["nightly"]
        assert health(night.cluster.pushgateway)["secret_rotator_run_rotations"] == 0
        assert health(night.cluster.pushgateway)["secret_rotator_run_success"] == 1

    def test_a_run_that_broke_pushes_success_0(self):
        night = Night(world(due=(LEAF,)))
        night.telegram.down = True
        assert night() == 1
        assert health(night.cluster.pushgateway)["secret_rotator_run_success"] == 0

    def test_a_failed_push_is_a_line_each_and_changes_nothing_else(self):
        nights = []
        for down in (False, True):
            bao = world()
            bao.leaves[STRAY] = {"data": {"x": "SECRET-stray"}, "meta": {}}
            night = Night(bao)
            night.cluster.pushgateway.down = down
            assert night() == 0
            nights.append(night)
        up, down = nights
        assert down.telegram.messages == up.telegram.messages
        assert down.card()["description"] == up.card()["description"]
        assert state_of(down.bao, LEAF) == state_of(up.bao, LEAF)
        assert [line for line in down.lines if line.startswith("metrics:")] == [
            f"metrics: the {group} group is not pushed: PUT {GROUPS}{group}: HTTP 503: no "
            f'endpoints available for service "{SERVICE}"'
            for group in ("state", "audit", "nightly")
        ]
        assert down.cluster.pushgateway.groups == {}

    def test_a_group_not_pushed_does_not_stop_the_next(self):
        night = Night()
        night.cluster.broken["PUT", GROUPS + "state"] = ConnectionResetError("reset by peer")
        assert night() == 0
        assert set(night.cluster.pushgateway.groups) == {"audit", "nightly"}
        assert night.lines[-2:] == [
            f"metrics: the state group is not pushed: PUT {GROUPS}state: transport error: "
            "ConnectionResetError('reset by peer')",
            "metrics: pushed audit, nightly",
        ]


class TestAnOperatorsProcess:
    """`run <path>` and `stamp` push the state group alone, over the whole store."""

    def test_run_path_pushes_the_state_its_plan_left(self):
        bao = fake_of(store_of(**ACTIVATE_NONE))
        cluster = FakeCluster()
        code = cli.main(
            ["run", WIFI],
            opener=bao,
            out=lambda line: None,
            environ=ENV,
            console=lambda: Console(io.StringIO("y\nSECRET-psk\nc\n"), io.StringIO()),
            kube=cluster.kube,
            source=lambda: "commit c0ffee",
        )
        assert code == 0
        assert cluster.pushes() == ["state"]
        stamp = state_of(bao, WIFI).stamps["password"]
        key = key_of(WIFI, "password", "manual")
        assert cluster.pushgateway.value(
            "state", "secret_rotation_last_rotated_timestamp", **key
        ) == day(stamp)
        assert (LEAF, "token") in keys_in(cluster.pushgateway, "secret_rotation_key_info")

    def stamp(self, bao, *argv, env=ENV, cluster=None):
        cluster = cluster or FakeCluster()
        lines = []
        code = cli.main(
            ["stamp", *argv], opener=bao, out=lines.append, environ=env, kube=cluster.kube
        )
        return code, lines, cluster

    def test_stamp_pushes_the_state_with_its_new_stamp(self):
        bao = fake_of(store_of(**ACTIVATE_NONE))
        code, lines, cluster = self.stamp(bao, LEAF, "token", "--rotated-at", "2026-10-01")
        assert code == 0 and lines == [f"{LEAF}#token: rotation stamp 2026-10-01, was none"]
        assert cluster.pushes() == ["state"]
        key = key_of(LEAF, "token", "random")
        assert cluster.pushgateway.value("state", "secret_rotation_due_timestamp", **key) == day(
            "2026-10-15"
        )

    def test_stamp_without_the_cluster_token_stamps_and_its_push_is_the_one_that_fails(self):
        bao = fake_of(store_of(**ACTIVATE_NONE))
        env = {k: v for k, v in ENV.items() if k != cli.K8S_TOKEN_ENV}
        lines = []
        code = cli.main(
            ["stamp", LEAF, "token", "--rotated-at", "2026-10-01"],
            opener=bao,
            out=lines.append,
            environ=env,
            kube=lambda token: pytest.fail("a cluster client without a token"),
        )
        assert code == 0
        assert state_of(bao, LEAF).stamps == {"token": "2026-10-01"}
        assert lines == [
            f"{LEAF}#token: rotation stamp 2026-10-01, was none",
            "metrics: the state group is not pushed: SECRET_ROTATOR_K8S_TOKEN is not set",
        ]

    def test_a_stamp_that_writes_nothing_pushes_nothing(self):
        bao = fake_of(store_of(**ACTIVATE_NONE))
        code, _, cluster = self.stamp(bao, "no/such", "token", "--rotated-at", "2026-10-01")
        assert code == 1 and cluster.requests == []

    def test_a_failed_push_leaves_the_stamp_and_its_exit_status(self):
        bao = fake_of(store_of(**ACTIVATE_NONE))
        cluster = FakeCluster()
        cluster.pushgateway.down = True
        code, lines, _ = self.stamp(
            bao, LEAF, "token", "--rotated-at", "2026-10-01", cluster=cluster
        )
        assert code == 0 and state_of(bao, LEAF).stamps == {"token": "2026-10-01"}
        assert (
            lines[1].startswith("metrics: the state group is not pushed: PUT ") and len(lines) == 2
        )

    def test_they_leave_the_findings_and_the_run_health_as_the_night_pushed_them(self):
        bao = world()
        bao.leaves[STRAY] = {"data": {"x": "SECRET-stray"}, "meta": {}}
        night = Night(bao)
        night()
        gateway = night.cluster.pushgateway
        before = {group: gateway.bodies[group] for group in ("audit", "nightly")}
        code, _, _ = self.stamp(
            bao, LEAF, "token", "--rotated-at", "2026-10-01", cluster=night.cluster
        )
        assert code == 0
        assert night.cluster.pushes()[-1] == "state"
        assert {group: gateway.bodies[group] for group in ("audit", "nightly")} == before
        key = key_of(LEAF, "token", "random")
        assert gateway.value("state", "secret_rotation_last_rotated_timestamp", **key) == day(
            "2026-10-01"
        )


def test_the_push_is_a_put_of_the_exposition_text_through_the_apiserver_s_proxy():
    cluster = FakeCluster()
    series = metrics.Series()
    series.add("secret_rotator_paused", 1)
    assert metrics.push(cluster.kube(), {"nightly": lambda: series}, pytest.fail) == ["nightly"]
    ((method, path, body),) = cluster.requests
    assert method == "PUT"
    assert path == (
        "/api/v1/namespaces/prometheus-prd/services/prometheus-prd-prometheus-pushgateway:9091"
        "/proxy/metrics/job/secret-rotator/instance/nightly"
    )
    assert body == (
        "# HELP secret_rotator_paused 1 while the switches pause the nightly run.\n"
        "# TYPE secret_rotator_paused gauge\n"
        "secret_rotator_paused 1\n"
    )
