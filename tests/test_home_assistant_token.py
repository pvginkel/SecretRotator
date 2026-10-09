"""The home-assistant-token kind (design §6) over the seed's two rows, which take no args; its
plans, which sync every ExternalSecret that reads the leaf before the proof and the delete, whatever
the leaf's activate; the runs, in which each token mints its successor for its own user, named anew
and staged with its expiry, and the plan deletes the token the leaf held and no other; their
failures and rollbacks; Home Assistant's client; and the websocket it speaks over."""

import datetime
import json
import re
import socket
import threading
from pathlib import Path

import pytest
from fake_cluster import FakeCluster, externalsecret, pod_spec, snapshot, workload
from fake_homeassistant import INVALID, LONG_LIVED, FakeHomeAssistant
from fixtures import edit
from plans import NOW, Recorder, client, fake_of, lock, run_state, state_of
from test_activation import ticking

from secret_rotator import annotate as ann
from secret_rotator.audit import audit
from secret_rotator.cluster import Cluster
from secret_rotator.contract import entry_name, load_entry
from secret_rotator.executor import AbortRefused, Executor, Outcome
from secret_rotator.kinds.home_assistant_token import HomeAssistantToken, homeassistant
from secret_rotator.kinds.home_assistant_token.homeassistant import (
    URL,
    HomeAssistantError,
    connect,
)
from secret_rotator.kinds.home_assistant_token.steps import NAME, NEW, OLD, Delete, Mint, Prove
from secret_rotator.kinds.home_assistant_token.websocket import WebSocketError, accept, masked
from secret_rotator.kinds.home_assistant_token.websocket import connect as ws_connect
from secret_rotator.model import Action, Finished, StepFailed, expiry_name, value_name
from secret_rotator.plan import PlanError, make

SEED = ann.load_seed(ann.DEFAULT_SEED)
KIND = "home-assistant-token"
MCP = "eso/prd/homeassistant-mcp/prd/homeassistant"
FLEET = "jenkins/home-automation-fleet"
LEAVES = {MCP: "token", FLEET: "ha_token"}  # each leaf's key of the kind
ES = "homeassistant-mcp-prd/homeassistant-mcp-token"
ROLLED = "homeassistant-mcp-prd/deployment/homeassistant-mcp"
OPERATOR, OTHER = "u-operator", "u-other"  # Home Assistant users
MINT, PROVE, DELETE = (
    "home_assistant_token.mint",
    "home_assistant_token.prove",
    "home_assistant_token.delete",
)
LIFESPAN = 90  # a 14-day key's successor's, in days
EXPIRES = "2027-01-03"  # NOW's date and LIFESPAN days
LOST = ConnectionResetError(104, "Connection reset by peer")


def seed_store():
    return ann.offline_store(Path(str(ann.DEFAULT_KEYS)), SEED, lambda line: None)


def ha_objects():
    """(resource, object) of the readers of the kind's leaves, as prd holds them (2026-10-09):
    the homeassistant-mcp Deployment reads homeassistant-mcp-token's token into its env. Jenkins
    reads jenkins/home-automation-fleet at build time; no ExternalSecret does."""
    return [
        (
            "externalsecrets",
            externalsecret(
                "homeassistant-mcp-prd",
                "homeassistant-mcp-token",
                data=[(MCP, "token")],
                target="homeassistant-mcp-token",
            ),
        ),
        (
            "deployments",
            workload(
                "Deployment",
                "homeassistant-mcp-prd",
                "homeassistant-mcp",
                pod_spec(env=["homeassistant-mcp-token"]),
            ),
        ),
    ]


class World:
    """The seed's store on the fake OpenBao; Home Assistant, where each leaf holds a long-lived
    token of the operator's, who has one more and a browser login, and another user has a token
    of the same name as the MCP's; and the leaves' readers on the fake cluster."""

    def __init__(self, store=None):
        self.store = store or seed_store()
        self.ha = FakeHomeAssistant()
        self.made = {
            MCP: self.ha.token(OPERATOR, "Home Assistant MCP"),
            FLEET: self.ha.token(OPERATOR, "Home Automation Fleet"),
        }
        self.laptop = self.ha.token(OPERATOR, "Laptop")
        self.browser = self.ha.token(OPERATOR, None, type="normal")
        self.other = self.ha.token(OTHER, "Home Assistant MCP")
        self.initial = set(self.ha.tokens)
        data = {leaf: {key: self.ha.value(self.made[leaf])} for leaf, key in LEAVES.items()}
        self.bao = fake_of(self.store, data)
        self.cluster = FakeCluster(ha_objects())
        self.kind = HomeAssistantToken(self.ha)

    def held(self, leaf):
        """The token the leaf holds."""
        return self.bao.data(leaf)[LEAVES[leaf]]

    def holding(self, leaf):
        """The id of the token the leaf holds."""
        return self.ha.by_value(self.held(leaf))

    def plan(self, leaf, *, cluster=None, store=None):
        return make(
            {KIND: self.kind},
            leaf,
            KIND,
            [LEAVES[leaf]],
            audit(store or self.store),
            cluster or Cluster(self.cluster.kube()),
        )

    def executor(self, leaf, *, day=0, store=None):
        self.recorder = Recorder()
        tick = ticking()
        return Executor(
            client(self.bao),
            self.plan(leaf, store=store),
            self.recorder,
            lock(self.bao),
            state=run_state(self.bao),
            dry_run=False,
            clock=lambda: tick() + datetime.timedelta(days=day),
        )

    def run(self, leaf, *, day=0, store=None):
        return self.executor(leaf, day=day, store=store).run()

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

    def finished(self, step, action=Action.RUN):
        """The detail of the step's last line of the action that finished ok."""
        (*_, last) = (
            e.detail
            for e in self.recorder.events
            if isinstance(e, Finished) and e.ok and e.step.id == step and e.action is action
        )
        return last

    def generation(self):
        ns, _, name = ROLLED.split("/")
        return self.cluster.get("deployments", ns, name)["metadata"]["generation"]

    def entry(self, leaf):
        return load_entry(self.bao.leaves[leaf]["meta"][entry_name(LEAVES[leaf])])


def ids(plan):
    return [s.id for s in plan.steps]


def name_of(leaf, day=0):
    """What a successor of the leaf's token is named: its leaf, key and the time it is minted."""
    date = (NOW + datetime.timedelta(days=day)).date().isoformat()
    return re.compile(rf"{re.escape(leaf)}#{LEAVES[leaf]} {date}T04:30:\d\dZ")


class TestTheSeed:
    def test_each_leaf_of_the_kind_has_one_key_of_it_and_no_args(self):
        entries = audit(seed_store()).entries
        found = {
            leaf: (key, entry.args, entry.interval, [str(a) for a in entry.activate])
            for leaf, by_key in entries.items()
            for key, entry in by_key.items()
            if entry.kind == KIND
        }
        assert found == {
            MCP: ("token", {}, 14, ["eso", "k8s-rollout"]),
            FLEET: ("ha_token", {}, 14, []),
        }

    def test_args_it_cannot_use_are_named(self):
        assert HomeAssistantToken().args_problems({"user": "pieter"}) == [
            "user: home-assistant-token takes no args"
        ]

    def test_it_reaches_home_assistant_at_the_address_its_mcp_server_does(self):
        # HomeassistantMcpDeploy config/prd/values.yaml: https://homeassistant.webathome.org
        assert homeassistant.URL == "wss://homeassistant.webathome.org/api/websocket"


class TestThePlans:
    def test_the_mcp_s_leaf_syncs_and_rolls_out_before_the_proof_and_the_delete(self):
        assert ids(World().plan(MCP)) == [
            MINT,
            "kv.write",
            f"eso.sync:{ES}",
            f"k8s.rollout:{ROLLED}",
            PROVE,
            DELETE,
            "kv.stamp",
        ]

    def test_the_mcp_s_externalsecret_syncs_though_its_activate_is_none(self):
        store = seed_store()
        edit(store[MCP].meta, "token", activate="none")
        assert ids(World().plan(MCP, store=store)) == [
            MINT,
            "kv.write",
            f"eso.sync:{ES}",
            PROVE,
            DELETE,
            "kv.stamp",
        ]

    def test_no_externalsecret_reads_the_jenkins_leaf(self):
        assert ids(World().plan(FLEET)) == [MINT, "kv.write", PROVE, DELETE, "kv.stamp"]

    def test_offline_without_a_snapshot_no_leaf_has_a_plan(self):
        world = World()
        for leaf, key in LEAVES.items():
            with pytest.raises(PlanError, match="offline plan without a snapshot does not reach"):
                make({KIND: world.kind}, leaf, KIND, [key], audit(world.store), None)

    def test_against_a_snapshot_it_plans_without_reaching_home_assistant(self, tmp_path):
        path = tmp_path / "snapshot.json"
        path.write_text(json.dumps(snapshot(ha_objects())))
        world = World()
        for leaf in LEAVES:
            plan = world.plan(leaf, cluster=Cluster.of_snapshot(path))
            assert ids(plan) == ids(world.plan(leaf))
        assert world.ha.opened == 0

    def test_the_successor_lasts_four_intervals_and_never_under_90_days(self):
        store = seed_store()
        edit(store[MCP].meta, "token", interval="30d")
        world = World(store)
        assert world.plan(MCP).steps[0].days == 120
        assert World().plan(MCP).steps[0].days == LIFESPAN

    def test_a_key_that_rotates_never_has_no_plan(self):
        store = seed_store()
        edit(store[FLEET].meta, "ha_token", interval="never", notes="by hand")
        with pytest.raises(PlanError, match="ha_token rotates never"):
            World(store).plan(FLEET)

    def test_a_leaf_with_two_keys_of_the_kind_has_no_plan(self):
        store = seed_store()
        store[FLEET].keys.add("other")
        edit(store[FLEET].meta, "other", kind=KIND, interval="14d", activate="none")
        with pytest.raises(PlanError, match="a home-assistant-token plan rotates one key, not 2"):
            make(
                {KIND: HomeAssistantToken()},
                FLEET,
                KIND,
                ["ha_token", "other"],
                audit(store),
                Cluster(FakeCluster(ha_objects()).kube()),
            )

    def test_the_steps_and_the_description(self):
        plan = World().plan(MCP)
        mint, prove, delete = plan.steps[0], plan.steps[4], plan.steps[5]
        assert (mint.mutates, mint.activator, mint.undo is not None) == (True, False, True)
        assert (prove.mutates, prove.silent) == (False, True)
        assert (delete.mutates, delete.undo) == (True, None)
        assert delete.no_undo == "a deleted Home Assistant token cannot be restored"
        assert mint.title == (
            "mint a new Home Assistant long-lived access token that expires in 90 days"
        )
        assert prove.title == "log in with the new token"
        assert delete.title == "delete the token the leaf held"
        assert not plan.needs_operator and plan.ask == ""
        assert plan.description == (
            "The tool mints a new Home Assistant long-lived access token that expires in 90 days "
            "with the one the leaf holds, and writes it to the leaf and activates what reads it. "
            "Once every ExternalSecret that reads the leaf has synced, it logs in with the new "
            "token and deletes the one the leaf held."
        )
        assert HomeAssistantToken().credential(plan.target) == (
            "Home Assistant long-lived access token"
        )


class TestTheRuns:
    @pytest.mark.parametrize("leaf", [MCP, FLEET])
    def test_a_rotation_mints_the_successor_for_the_same_user_and_deletes_only_the_old(self, leaf):
        world = World()
        old, old_value = world.made[leaf], world.held(leaf)
        assert world.run(leaf) is Outcome.DONE
        new = world.holding(leaf)
        assert new is not None and new not in world.initial
        token = world.ha.tokens[new]
        assert (token["user"], token["type"], token["lifespan"]) == (OPERATOR, LONG_LIVED, 90)
        assert name_of(leaf).fullmatch(token["name"])
        assert set(world.ha.tokens) == (world.initial - {old}) | {new}
        assert state_of(world.bao, leaf).stamps == {LEAVES[leaf]: "2026-10-05"}
        assert world.entry(leaf)["expires_at"] == EXPIRES
        values = (old_value, *(t["value"] for t in world.ha.tokens.values()))
        assert not any(v in text for text in world.texts() for v in values)
        assert world.ha.done("auth/delete_refresh_token") == [("auth/delete_refresh_token", new)]

    def test_the_old_token_is_deleted_only_once_the_secret_holds_the_new_and_the_mcp_rolled(
        self,
    ):
        world = World()
        at_delete = []
        world.ha.before["auth/delete_refresh_token"] = lambda msg: at_delete.append(
            (msg["refresh_token_id"], world.holding(MCP), world.cluster.syncs, world.generation())
        )
        assert world.run(MCP) is Outcome.DONE
        assert at_delete == [(world.made[MCP], world.holding(MCP), 1, 2)]

    def test_the_next_rotation_mints_with_the_first_s_successor_under_a_name_of_its_own(self):
        world = World()
        assert world.run(FLEET) is Outcome.DONE
        first = world.holding(FLEET)
        assert world.run(FLEET, day=1) is Outcome.DONE
        second = world.holding(FLEET)
        assert first not in world.ha.tokens
        assert name_of(FLEET, day=1).fullmatch(world.ha.tokens[second]["name"])
        assert set(world.ha.tokens) == (world.initial - {world.made[FLEET]}) | {second}

    def test_each_leaf_s_rotation_leaves_the_other_s_token(self):
        world = World()
        assert world.run(MCP) is Outcome.DONE
        assert world.run(FLEET) is Outcome.DONE
        assert world.holding(MCP) is not None and world.holding(FLEET) is not None
        assert set(world.ha.tokens) - {world.holding(MCP), world.holding(FLEET)} == {
            world.laptop,
            world.browser,
            world.other,
        }

    def test_an_unreachable_home_assistant_stops_the_plan_before_it_writes(self):
        world = World()
        world.ha.down = ConnectionRefusedError(111, "Connection refused")
        executor = world.executor(MCP)
        assert executor.run() is Outcome.FAILED
        assert world.failure().step.id == MINT
        assert world.failure().error == (
            f"{URL}: transport error: ConnectionRefusedError(111, 'Connection refused')"
        )
        assert executor.abort() is Outcome.CANCELLED
        assert world.holding(MCP) == world.made[MCP]

    @pytest.mark.parametrize(
        ("value", "error"),
        [
            (
                "SECRET-no-token-of-home-assistant-s",
                f"Home Assistant refuses the token {FLEET}#ha_token holds: {URL}: login refused: "
                f"{INVALID}",
            ),
            ("", f"{FLEET}#ha_token holds no token"),
        ],
    )
    def test_a_token_home_assistant_does_not_take_stops_the_plan_before_it_writes(
        self, value, error
    ):
        world = World()
        world.bao.new_version(FLEET, {"ha_token": value})
        executor = world.executor(FLEET)
        assert executor.run() is Outcome.FAILED
        assert world.failure().step.id == MINT
        assert world.failure().error == error
        assert executor.abort() is Outcome.CANCELLED
        assert world.held(FLEET) == value and set(world.ha.tokens) == world.initial

    def test_a_refused_mint_did_not_land(self):
        world = World()
        world.ha.refused["auth/long_lived_access_token"] = "unauthorized"
        executor = world.executor(MCP)
        assert executor.run() is Outcome.FAILED
        assert world.failure().error == "auth/long_lived_access_token: unauthorized: refused"
        assert executor.abort() is Outcome.CANCELLED
        assert set(world.ha.tokens) == world.initial

    def test_a_mint_home_assistant_failed_mid_way_is_undone(self):
        world = World()
        world.ha.refused["auth/long_lived_access_token"] = "unknown_error"
        executor = world.executor(MCP)
        assert executor.run() is Outcome.FAILED
        assert executor.abort() is Outcome.ROLLED_BACK
        assert world.finished(MINT, Action.UNDO).startswith("Home Assistant lists no token named ")
        assert set(world.ha.tokens) == world.initial

    def test_a_mint_whose_answer_is_lost_is_retried_under_its_name_and_leaves_no_token(self):
        world = World()
        world.ha.lost["auth/long_lived_access_token"] = LOST
        assert world.run(MCP) is Outcome.FAILED
        assert world.failure().error == (f"auth/long_lived_access_token: transport error: {LOST!r}")
        (lost,) = set(world.ha.tokens) - world.initial
        name = world.ha.tokens[lost]["name"]
        world.ha.lost.clear()
        assert world.run(MCP) is Outcome.DONE
        new = world.holding(MCP)
        assert lost not in world.ha.tokens and world.ha.tokens[new]["name"] == name
        assert set(world.ha.tokens) == (world.initial - {world.made[MCP]}) | {new}
        assert world.ha.done("auth/delete_refresh_token") == [
            ("auth/delete_refresh_token", world.made[MCP]),
            ("auth/delete_refresh_token", new),
        ]

    def test_a_mint_whose_answer_is_lost_is_undone_by_its_name(self):
        world = World()
        world.ha.lost["auth/long_lived_access_token"] = LOST
        executor = world.executor(MCP)
        assert executor.run() is Outcome.FAILED
        assert len(world.ha.tokens) == len(world.initial) + 1
        world.ha.lost.clear()
        assert executor.abort() is Outcome.ROLLED_BACK
        assert set(world.ha.tokens) == world.initial
        assert world.holding(MCP) == world.made[MCP]

    def test_a_new_token_home_assistant_does_not_take_rolls_back_to_the_old(self):
        world = World()
        world.ha.refuse = lambda type, token: None if token in world.initial else "unauthorized"
        executor = world.executor(MCP)
        assert executor.run() is Outcome.FAILED
        assert world.failure().step.id == PROVE
        assert world.failure().error == "auth/refresh_tokens: unauthorized: refused"
        assert executor.abort() is Outcome.ROLLED_BACK
        assert world.holding(MCP) == world.made[MCP]
        assert set(world.ha.tokens) == world.initial
        assert world.cluster.syncs == 2 and world.generation() == 3

    def test_a_refused_delete_did_not_land_and_rolls_back_to_the_old_token(self):
        world = World()
        world.ha.refuse = lambda type, token: (
            "unauthorized"
            if type == "auth/delete_refresh_token" and token not in world.initial
            else None
        )
        executor = world.executor(FLEET)
        assert executor.run() is Outcome.FAILED
        assert world.failure().step.id == DELETE
        assert executor.abort_blocker() is None
        assert executor.abort() is Outcome.ROLLED_BACK
        assert world.holding(FLEET) == world.made[FLEET]
        assert set(world.ha.tokens) == world.initial

    def test_a_delete_whose_answer_is_lost_cannot_be_aborted_and_a_retry_finishes(self):
        world = World()
        world.ha.lost["auth/delete_refresh_token"] = LOST
        executor = world.executor(FLEET)
        assert executor.run() is Outcome.FAILED
        assert world.made[FLEET] not in world.ha.tokens
        assert executor.abort_blocker() == "a deleted Home Assistant token cannot be restored"
        with pytest.raises(AbortRefused):
            executor.abort()
        world.ha.lost.clear()
        assert executor.run() is Outcome.DONE
        assert world.finished(DELETE) == f"token {world.made[FLEET]} was deleted already"
        assert state_of(world.bao, FLEET).stamps == {"ha_token": "2026-10-05"}


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
    def test_a_mint_stages_the_old_id_the_name_the_expiry_the_value_and_the_new_id(self):
        world = World()
        ctx = Ctx(world.bao)
        detail = Mint(world.ha, FLEET, "ha_token", 90).run(ctx)
        name = f"{FLEET}#ha_token 2026-10-05T04:30:00Z"
        new = world.ha.by_value(ctx.values[value_name("ha_token")])
        assert ctx.values == {
            OLD: world.made[FLEET],
            NAME: name,
            expiry_name("ha_token"): EXPIRES,
            value_name("ha_token"): world.ha.value(new),
            NEW: new,
        }
        assert detail == f"{name}, which expires {EXPIRES}"

    def test_a_re_run_with_a_token_minted_mints_none(self):
        world = World()
        ctx = Ctx(world.bao)
        mint = Mint(world.ha, FLEET, "ha_token", 90)
        mint.run(ctx)
        before = dict(ctx.values)
        ctx.values.pop(NEW)
        mint.run(ctx)
        assert ctx.values == before
        assert len(world.ha.done("auth/long_lived_access_token")) == 1

    def test_a_mint_home_assistant_lists_no_more_fails(self):
        world = World()
        ctx = Ctx(world.bao)
        mint = Mint(world.ha, FLEET, "ha_token", 90)
        mint.run(ctx)
        del world.ha.tokens[ctx.values[NEW]]
        with pytest.raises(StepFailed, match="^Home Assistant lists no long-lived access token"):
            mint.run(ctx)

    def test_an_undo_with_nothing_minted_reaches_nothing(self):
        world = World()
        assert Mint(world.ha, FLEET, "ha_token", 90).undo(Ctx(world.bao)) == "nothing was minted"
        assert world.ha.opened == 0

    def test_an_undo_deletes_the_token_named_so_with_the_token_the_leaf_holds(self):
        world = World()
        ctx = Ctx(world.bao)
        mint = Mint(world.ha, FLEET, "ha_token", 90)
        mint.run(ctx)
        assert mint.undo(ctx) == f"deleted {ctx.values[NAME]}"
        assert set(world.ha.tokens) == world.initial
        assert world.ha.done("auth/delete_refresh_token") == [
            ("auth/delete_refresh_token", world.made[FLEET])
        ]

    def test_a_proof_needs_the_leaf_to_hold_the_token_the_plan_minted(self):
        world = World()
        ctx = Ctx(world.bao)
        Mint(world.ha, FLEET, "ha_token", 90).run(ctx)
        prove = Prove(world.ha, FLEET, "ha_token")
        with pytest.raises(StepFailed, match="does not hold the token the plan minted$"):
            prove.run(ctx)
        world.bao.new_version(FLEET, {"ha_token": ctx.values[value_name("ha_token")]})
        assert prove.run(ctx) == f"logged in as {ctx.values[NAME]}"
        minted, ctx.values[NEW] = ctx.values[NEW], world.laptop
        with pytest.raises(StepFailed, match=f"as token {minted}, not {world.laptop}$"):
            prove.run(ctx)

    @pytest.mark.parametrize("step", [Prove(None, FLEET, "ha_token"), Delete(None, "ha_token")])
    def test_a_proof_and_a_delete_need_the_new_token_staged(self, step):
        world = World()
        with pytest.raises(StepFailed, match="^no new token is staged$") as e:
            step.run(Ctx(world.bao, {OLD: world.made[FLEET]}))
        assert e.value.landed is (not isinstance(step, Delete))

    def test_a_delete_of_a_token_deleted_already_sends_none(self):
        world = World()
        gone = world.ha.token(OPERATOR, "Gone")
        world.ha.delete(gone)
        ctx = Ctx(world.bao, {OLD: gone, value_name("ha_token"): world.ha.value(world.laptop)})
        assert Delete(world.ha, "ha_token").run(ctx) == f"token {gone} was deleted already"
        assert world.ha.done("auth/delete_refresh_token") == []

    def test_a_delete_home_assistant_answers_without_deleting_fails(self):
        world = World()
        world.ha.delete = lambda id: None
        ctx = Ctx(
            world.bao,
            {OLD: world.made[FLEET], value_name("ha_token"): world.ha.value(world.laptop)},
        )
        with pytest.raises(StepFailed, match="still lists token .* after its delete$") as e:
            Delete(world.ha, "ha_token").run(ctx)
        assert e.value.landed


class TestTheClient:
    @pytest.mark.parametrize(
        ("code", "refused"),
        [
            ("unauthorized", True),
            ("invalid_token_id", True),
            ("unknown_error", False),
            (None, False),
        ],
    )
    def test_an_error_but_a_handler_s_failure_or_the_transport_s_changed_nothing(
        self, code, refused
    ):
        assert HomeAssistantError("x", code).refused is refused

    def test_an_error_answered_names_the_command_its_code_and_its_message(self):
        world = World()
        with (
            connect(world.ha.value(world.laptop), world.ha) as ha,
            pytest.raises(HomeAssistantError) as e,
        ):
            ha.delete(world.other)
        assert str(e.value) == "auth/delete_refresh_token: invalid_token_id: Received invalid token"
        assert e.value.code == "invalid_token_id"
        assert world.other in world.ha.tokens

    def test_a_login_refused_closes_the_connection(self):
        world = World()
        with pytest.raises(HomeAssistantError) as e:
            connect("SECRET-wrong", world.ha)
        assert (str(e.value), e.value.code) == (f"{URL}: login refused: {INVALID}", "auth_invalid")
        assert world.ha.sockets[0].closed_by_client

    def test_a_connection_that_deletes_its_own_token_loses_the_answer(self):
        world = World()
        with (
            connect(world.ha.value(world.laptop), world.ha) as ha,
            pytest.raises(HomeAssistantError) as e,
        ):
            ha.delete(world.laptop)
        assert str(e.value) == (
            "auth/delete_refresh_token: transport error: "
            "WebSocketError('the server closed the connection')"
        )
        assert e.value.code is None and world.laptop not in world.ha.tokens

    def test_a_connection_lists_its_users_tokens_its_own_current(self):
        world = World()
        with connect(world.ha.value(world.laptop), world.ha) as ha:
            tokens = {t.id: (t.name, t.type, t.current) for t in ha.tokens()}
        assert tokens == {
            world.made[MCP]: ("Home Assistant MCP", LONG_LIVED, False),
            world.made[FLEET]: ("Home Automation Fleet", LONG_LIVED, False),
            world.laptop: ("Laptop", LONG_LIVED, True),
            world.browser: ("", "normal", False),
        }


class Peer:
    """The server's end of a connection: whole reads, whole writes."""

    def __init__(self, conn):
        self.conn = conn
        self.reader = conn.makefile("rb")

    def readline(self):
        return self.reader.readline()

    def read(self, n):
        data = self.reader.read(n)
        assert len(data) == n
        return data

    def write(self, data):
        self.conn.sendall(data)


class Server:
    """A websocket server on localhost for one connection, which a test plays by hand on a thread
    of its own: play(peer, key) with its end of the connection and the handshake's key."""

    def __init__(self, play):
        self.listener = socket.create_server(("127.0.0.1", 0))
        self.url = f"ws://127.0.0.1:{self.listener.getsockname()[1]}/api/websocket"
        self.request = []
        self.error = None
        self.thread = threading.Thread(target=self._serve, args=(play,), daemon=True)
        self.thread.start()

    def _serve(self, play):
        try:
            conn, _ = self.listener.accept()
            with conn:
                peer = Peer(conn)
                while line := peer.readline().decode().strip():
                    self.request.append(line)
                key = next(
                    h.split(": ")[1] for h in self.request if h.startswith("Sec-WebSocket-Key")
                )
                play(peer, key)
                peer.reader.close()
        except Exception as e:
            self.error = e
        finally:
            self.listener.close()

    def join(self):
        self.thread.join(10)
        assert not self.thread.is_alive()
        if self.error is not None:
            raise self.error


def upgrade(f, key):
    f.write(
        b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
        + f"Sec-WebSocket-Accept: {accept(key)}\r\n\r\n".encode()
    )


def frame(opcode, payload, *, fin=True):
    """A server's frame, unmasked."""
    n = len(payload)
    length = bytes([n]) if n < 126 else bytes([126]) + n.to_bytes(2, "big")
    return bytes([(0x80 if fin else 0) | opcode]) + length + payload


def client_frame(f):
    """A client's frame, which must be masked: its first byte and its payload."""
    first, second = f.read(2)
    assert second & 0x80, "unmasked"
    n = second & 0x7F
    if n == 126:
        n = int.from_bytes(f.read(2), "big")
    elif n == 127:
        n = int.from_bytes(f.read(8), "big")
    mask = f.read(4)
    return first, masked(f.read(n), mask)


class TestTheWebSocket:
    def test_it_sends_masked_text_joins_fragments_answers_pings_and_ends_at_a_close(self):
        got = []

        def play(f, key):
            upgrade(f, key)
            got.append(client_frame(f))
            got.append(client_frame(f))
            f.write(frame(0x1, b"hel", fin=False) + frame(0x9, b"p") + frame(0x0, b"lo"))
            got.append(client_frame(f))
            f.write(frame(0x1, b"x" * 300) + frame(0x8, (1001).to_bytes(2, "big")))
            got.append(client_frame(f))

        server = Server(play)
        ws = ws_connect(server.url)
        ws.send("hi")
        ws.send("y" * 70000)
        assert ws.recv() == "hello"
        assert ws.recv() == "x" * 300
        with pytest.raises(WebSocketError, match="^the server closed the connection with status"):
            ws.recv()
        ws.close()
        server.join()
        assert server.request[0] == "GET /api/websocket HTTP/1.1"
        assert "Upgrade: websocket" in server.request
        assert got == [
            (0x81, b"hi"),
            (0x81, b"y" * 70000),
            (0x8A, b"p"),
            (0x88, (1000).to_bytes(2, "big")),
        ]

    def test_a_connection_the_server_drops_mid_frame_is_closed(self):
        def play(f, key):
            upgrade(f, key)
            f.write(b"\x81\x05he")

        server = Server(play)
        ws = ws_connect(server.url)
        server.join()
        with pytest.raises(WebSocketError, match="^the server closed the connection$"):
            ws.recv()
        ws.close()

    @pytest.mark.parametrize(
        ("answer", "error"),
        [
            (
                lambda key: b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n",
                "the handshake was answered HTTP/1.1 403 Forbidden",
            ),
            (
                lambda key: (
                    b"HTTP/1.1 101 Switching Protocols\r\nSec-WebSocket-Accept: bm90IGl0\r\n\r\n"
                ),
                "the handshake's answer does not accept its key",
            ),
            (lambda key: b"", "the handshake was answered with nothing"),
        ],
    )
    def test_a_handshake_not_answered_as_a_websocket_fails(self, answer, error):
        server = Server(lambda peer, key: peer.write(answer(key)))
        with pytest.raises(WebSocketError, match=f"^{re.escape(error)}$"):
            ws_connect(server.url)
        server.join()
