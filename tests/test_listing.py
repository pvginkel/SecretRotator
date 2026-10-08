"""What `secret-rotator ui` lists (design §7.3, §7.5): every plan with an operator step — manual,
approle's manual deliveries, external, a key without a due date — with where it stands, in flight
since when, failed or rolling back, and the texts its box shows; no plan without an operator step,
in flight or failed, and none that cannot be built."""

import dataclasses
import datetime
from pathlib import Path

from fake_cluster import FakeCluster
from fake_openbao import WRITTEN_AT
from fixtures import annotated
from plans import COPY, LEAF, client, fake_of, put_flight, put_state
from test_kinds import KINDS, store_of

from secret_rotator import annotate as ann
from secret_rotator.audit import Leaf, audit, live_store
from secret_rotator.cluster import Cluster
from secret_rotator.executor import Stand
from secret_rotator.listing import listed
from secret_rotator.staging import ROLLBACK, staging_leaf

PAT = "eso/prd/gh/prd/pat"  # manual, a GitHub personal access token, yearly
BOT = "eso/prd/bot/prd/config"  # manual without a credential type, yearly
WIFI = "shared/wifi"  # manual at never without an expiry: no due date
SHOP = "eso/prd/shop/prd/api"  # manual at never, with an expiry
HOOK = "eso/prd/hook/prd/hook"  # random, activated by a confirm
TOKEN = "eso/prd/app/prd/token"  # random: no operator step
MIXED = "eso/prd/mixed/prd/creds"  # a random key and two manual ones
AUTO = "eso/prd/auto/prd/key"  # manual, activated from the cluster: no plan offline
IAC_AGENT = "rotator/approle/iac-agent"  # approle, delivered by hand, 90 days
OPENBAO_ADMIN = "rotator/approle/openbao-admin"  # approle, delivered by hand, 90 days
ROTATOR = "iac/rotator-approle"  # approle, delivered to its own leaf: no operator step
SEAL = "rotator/bootstrap/seal-key"  # external, yearly

YEARLY = {"interval": "365d", "activate": "none"}
NEVER = {"interval": "never", "activate": "none", "notes": "rotated by hand"}


def seed_store():
    return ann.offline_store(
        Path(str(ann.DEFAULT_KEYS)), ann.load_seed(ann.DEFAULT_SEED), lambda line: None
    )


def leaf(path, **entries):
    return Leaf(path, set(entries), annotated(entries))


def store():
    seed = seed_store()
    own = [
        leaf(PAT, token={"kind": "manual", "args": {"type": "github-pat"}, **YEARLY}),
        leaf(BOT, **{"telegram-bot-token": {"kind": "manual", **YEARLY}}),
        leaf(WIFI, password={"kind": "manual", **NEVER}),
        leaf(SHOP, **{"api-key": {"kind": "manual", "expires_at": "2027-03-01", **NEVER}}),
        leaf(HOOK, secret={"kind": "random", "interval": "30d", "activate": "manual:tell Bob"}),
        leaf(TOKEN, token={"kind": "random", **YEARLY}),
        leaf(
            MIXED,
            password={"kind": "manual", **YEARLY},
            pin={"kind": "manual", **YEARLY},
            token={"kind": "random", **YEARLY},
        ),
        leaf(AUTO, key={"kind": "manual", "interval": "365d", "activate": "auto"}),
    ]
    return {x.path: x for x in own} | {
        p: seed[p] for p in (IAC_AGENT, OPENBAO_ADMIN, ROTATOR, SEAL)
    }


def listing(bao, cluster=None):
    return listed(live_store(client(bao), runs=True), KINDS, cluster)


def by_plan(rotations):
    return {(r.plan.target.leaf, r.plan.target.keys): r for r in rotations}


def plans_in(rotations):
    return [(r.plan.target.leaf, r.plan.target.keys) for r in rotations]


class TestWhatIsListed:
    def test_every_plan_with_an_operator_step_a_manual_key_s_per_key_and_no_other(self):
        assert set(plans_in(listing(fake_of(store())))) == {
            (PAT, ("token",)),
            (BOT, ("telegram-bot-token",)),
            (WIFI, ("password",)),
            (SHOP, ("api-key",)),
            (HOOK, ("secret",)),
            (MIXED, ("password",)),
            (MIXED, ("pin",)),
            (IAC_AGENT, ("secret_id",)),
            (OPENBAO_ADMIN, ("secret_id",)),
            (SEAL, ("seal-key",)),
        }

    def test_the_seed_lists_approle_s_two_manual_deliveries_and_every_external_key(self):
        seed = seed_store()
        rotations = listed(seed, KINDS)
        approle = {r.plan.target.leaf for r in rotations if r.plan.target.kind == "approle"}
        assert approle == {IAC_AGENT, OPENBAO_ADMIN}
        external = {
            (path, (key,))
            for path, entries in audit(seed, plugins=KINDS).entries.items()
            for key, entry in entries.items()
            if entry.kind == "external"
        }
        assert external and {p for p in plans_in(rotations) if p in external} == external


class TestTheTexts:
    def test_a_manual_key_names_its_credential_type_and_is_filtered_by_it(self):
        pat = by_plan(listing(fake_of(store())))[PAT, ("token",)]
        assert pat.credential == "GitHub personal access token" and pat.type == "github-pat"
        assert pat.plan.ask == "paste a new GitHub personal access token"
        assert pat.plan.description.startswith("You mint a new GitHub personal access token")

    def test_a_manual_key_without_a_credential_type_shows_and_is_filtered_by_its_kind(self):
        bot = by_plan(listing(fake_of(store())))[BOT, ("telegram-bot-token",)]
        assert (bot.credential, bot.type) == ("manual", "manual")

    def test_an_approle_secret_id_is_named_so_and_filtered_by_its_kind(self):
        found = by_plan(listing(fake_of(store())))
        for path in (IAC_AGENT, OPENBAO_ADMIN):
            r = found[path, ("secret_id",)]
            assert (r.credential, r.type) == ("AppRole secret_id", "approle")
            assert r.plan.ask == f"put the new {path.rsplit('/', 1)[1]} secret_id in place"

    def test_a_kind_without_the_hook_shows_and_is_filtered_by_its_name(self):
        found = by_plan(listing(fake_of(store())))
        seal, hook = found[SEAL, ("seal-key",)], found[HOOK, ("secret",)]
        assert (seal.credential, seal.type) == ("external", "external")
        assert (hook.credential, hook.type) == ("random", "random")

    def test_the_estimate_is_of_what_remains(self):
        pat = by_plan(listing(fake_of(store())))[PAT, ("token",)]
        for step, seconds in zip(pat.plan.steps, (120, 30, 5), strict=True):
            step.estimate = seconds
        assert pat.estimate == 155
        assert dataclasses.replace(pat, stand=Stand.IN_FLIGHT, at=1).estimate == 35


class TestWhenEachIsDue:
    def test_by_its_keys_schedule_a_key_at_never_without_an_expiry_having_none(self):
        bao = fake_of(store())
        put_state(bao, PAT, stamps={"token": "2025-10-01"})
        put_state(bao, IAC_AGENT, stamps={"secret_id": "2026-09-01"})
        found = by_plan(listing(bao))
        assert found[PAT, ("token",)].due_at == datetime.date(2026, 10, 1)
        assert found[IAC_AGENT, ("secret_id",)].due_at == datetime.date(2026, 11, 30)
        assert found[BOT, ("telegram-bot-token",)].due_at == datetime.date.min  # never stamped
        assert found[SHOP, ("api-key",)].due_at == datetime.date(2027, 2, 22)  # its expiry less 7d
        assert found[WIFI, ("password",)].due_at is None

    def test_the_schedule_decides_not_the_nightly_run_s_manual_due(self):
        bao = fake_of(store())
        put_state(bao, PAT, stamps={"token": "2026-10-01"}, status="manual-due")
        pat = by_plan(listing(bao))[PAT, ("token",)]
        assert pat.due_at == datetime.date(2027, 10, 1) and pat.stand is Stand.FRESH

    def test_in_flight_and_failed_first_then_the_earliest_due_then_those_without_a_due_date(self):
        bao = fake_of(store())
        for path, key in [(HOOK, "secret"), (IAC_AGENT, "secret_id"), (OPENBAO_ADMIN, "secret_id")]:
            put_state(bao, path, stamps={key: "2026-09-01"})
        put_state(bao, PAT, stamps={"token": "2026-01-01"}, status="failed")
        put_state(bao, MIXED, stamps={"password": "2026-06-01", "pin": "2026-03-01"})
        put_state(bao, SEAL, stamps={"seal-key": "2026-05-01"})
        put_flight(bao, "manual", PAT, ["token"], "kv.write")
        put_flight(bao, "approle", OPENBAO_ADMIN, ["secret_id"], "approle.mint")
        assert plans_in(listing(bao)) == [
            (OPENBAO_ADMIN, ("secret_id",)),  # in flight, due 2026-11-30
            (PAT, ("token",)),  # failed, due 2027-01-01
            (BOT, ("telegram-bot-token",)),  # never stamped
            (HOOK, ("secret",)),  # 2026-10-01
            (IAC_AGENT, ("secret_id",)),  # 2026-11-30
            (SHOP, ("api-key",)),  # 2027-02-22
            (MIXED, ("pin",)),  # 2027-03-01
            (SEAL, ("seal-key",)),  # 2027-05-01
            (MIXED, ("password",)),  # 2027-06-01
            (WIFI, ("password",)),  # no due date
        ]


class TestWhereEachStands:
    def test_a_plan_not_in_flight_is_fresh_at_its_first_step(self):
        for r in listing(fake_of(store())):
            assert (r.stand, r.at, r.flight, r.waits) == (Stand.FRESH, 0, None, False)

    def test_a_plan_in_flight_is_since_its_staging_leaf_s_latest_version(self):
        bao = fake_of(store())
        flight = put_flight(bao, "manual", PAT, ["token"], "operator.credential:token")
        later = WRITTEN_AT + datetime.timedelta(hours=3)
        bao.now = later
        path = staging_leaf("manual", PAT)
        client(bao).write(path, bao.data(path) | {"memo": "x"})
        pat = by_plan(listing(bao))[PAT, ("token",)]
        assert (pat.stand, pat.at) == (Stand.IN_FLIGHT, 0)
        assert pat.flight == dataclasses.replace(flight, since=later)
        assert not pat.waits

    def test_a_failed_plan_stands_failed_at_its_tool_step(self):
        bao = fake_of(store())
        put_flight(bao, "manual", PAT, ["token"], "kv.write")
        put_state(bao, PAT, status="failed", last_error="kv.write: HTTP 403")
        pat = by_plan(listing(bao))[PAT, ("token",)]
        assert (pat.stand, pat.plan.steps[pat.at].id) == (Stand.FAILED, "kv.write")
        assert pat.flight.since == WRITTEN_AT

    def test_a_rollback_stopped_part_way_stands_rolling_back(self):
        bao = fake_of(store())
        put_flight(bao, "manual", PAT, ["token"], "kv.write", **{ROLLBACK: "1"})
        put_state(bao, PAT, status="failed")
        assert by_plan(listing(bao))[PAT, ("token",)].stand is Stand.ROLLING_BACK

    def test_a_plan_without_an_operator_step_is_not_listed_in_flight_or_failed(self):
        bao = fake_of(store())
        put_flight(bao, "random", MIXED, ["token"], "kv.write")
        put_state(bao, MIXED, status="failed")
        put_flight(bao, "random", TOKEN, ["token"], "kv.write")
        rotations = listing(bao)
        assert (MIXED, ("token",)) not in plans_in(rotations)
        assert TOKEN not in {r.plan.target.leaf for r in rotations}

    def test_the_leaf_s_other_plans_wait_on_its_plan_in_flight(self):
        bao = fake_of(store())
        flight = put_flight(bao, "random", MIXED, ["token"], "kv.write")
        found = by_plan(listing(bao))
        for keys in [("password",), ("pin",)]:
            r = found[MIXED, keys]
            assert (r.stand, r.flight, r.waits) == (Stand.FRESH, flight, True)

    def test_a_plan_in_flight_at_a_step_it_no_longer_has_is_not_listed(self):
        bao = fake_of(store())
        put_flight(bao, "manual", MIXED, ["password"], "gone")
        found = by_plan(listing(bao))
        assert (MIXED, ("password",)) not in found and found[MIXED, ("pin",)].waits

    def test_a_plan_that_cannot_be_built_is_not_listed_in_flight_or_not(self):
        bao = fake_of(store())
        assert AUTO not in {r.plan.target.leaf for r in listing(bao)}
        put_flight(bao, "manual", AUTO, ["key"], "kv.write")
        assert AUTO not in {r.plan.target.leaf for r in listing(bao)}

    def test_a_plan_in_flight_is_listed_though_its_leaf_became_an_orphan(self):
        """As `run <path>` takes it up (design §3.3): an orphan finding blocks the leaf's plans,
        not its plan in flight."""
        store = store_of(eso__prd__app__prd__token="manual:restart the app by hand")
        del store[COPY]
        fake = FakeCluster()
        del fake.objects["externalsecrets", "app-prd", "app-token"]
        bao = fake_of(store)
        assert LEAF not in {r.plan.target.leaf for r in listing(bao, Cluster(fake.kube()))}
        put_flight(bao, "random", LEAF, ["token"], f"operator.confirm:{LEAF}:1")
        (r,) = [r for r in listing(bao, Cluster(fake.kube())) if r.plan.target.leaf == LEAF]
        assert r.stand is Stand.IN_FLIGHT
        assert r.plan.steps[r.at].id == f"operator.confirm:{LEAF}:1"
