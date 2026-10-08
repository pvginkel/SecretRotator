"""The nightly run, `secret-rotator run` without a path (design §8), against doubles of OpenBao,
the cluster, YouTrack and Telegram: the lock, the dry run, the due set oldest first under the cap
and kinds_enabled, admission and the manual warnings, the health gate, a failed plan rolled back by
the run and the backoff after three nights, and the standing card."""

import datetime
import itertools
import json

import pytest
from fake_cluster import FakeCluster
from fake_openbao import ROLE_ID, SECRET_ID, TOKEN
from fake_telegram import CHAT, FakeTelegram
from fake_telegram import TOKEN as BOT
from fake_youtrack import TAG, FakeYouTrack
from fake_youtrack import TOKEN as JEEVES
from fixtures import annotated, edit
from plans import (
    COPY,
    LEAF,
    NOW,
    Journal,
    RandomLike,
    Tool,
    fake,
    flight_of,
    put_state,
    state_of,
)
from test_kinds import KINDS

from secret_rotator import card, nightly
from secret_rotator.cluster import Cluster
from secret_rotator.contract import LOCK_LEAF
from secret_rotator.openbao import OpenBao
from secret_rotator.switches import Switches
from secret_rotator.telegram import Telegram
from secret_rotator.youtrack import YouTrack

TODAY = NOW.date()
TRELLO = "eso/prd/trello/prd/trello"  # random bearer-token, auto: ExternalSecret trello
BOT_LEAF = "eso/prd/bot/prd/config"  # manual telegram-bot-token, 365 d
WEBHOOK = "eso/prd/yt/prd/webhook"  # random, activated by a Jenkins job
STRAY = "shared/stray"
DAY = datetime.timedelta(days=1)
NOT_ANNOTATED = {}


def world(*, due=(LEAF, TRELLO)):
    """The compliant store with the rotator's own leaves; of the random leaves only those in due
    are due. WEBHOOK is never due here: its Jenkins job is test_jenkinssteps'."""
    bao = fake()
    own = {
        "rotator/telegram": {"token": BOT},
        "rotator/youtrack": {"token": JEEVES},
        "iac/rotator-approle": {"role_id": ROLE_ID, "secret_id": SECRET_ID},
    }
    for leaf, data in own.items():
        bao.leaves[leaf] = {
            "data": data,
            "meta": annotated({key: {"kind": "none"} for key in data}),
        }
    for leaf, key in ((LEAF, "token"), (TRELLO, "bearer-token"), (WEBHOOK, "token")):
        if leaf not in due:
            put_state(bao, leaf, stamps={key: TODAY.isoformat()})
    put_state(bao, BOT_LEAF, stamps={"telegram-bot-token": TODAY.isoformat()})
    return bao


def manual_due_in(bao, days):
    """The bot leaf's manual telegram-bot-token falls due that many days from today."""
    stamp = TODAY + datetime.timedelta(days=days - 365)
    put_state(bao, BOT_LEAF, stamps={"telegram-bot-token": stamp.isoformat()})


class Night:
    """Nightly runs against one world's doubles."""

    def __init__(self, bao=None, *, kinds=KINDS, opener=None):
        self.bao = bao or world()
        self.cluster = FakeCluster()
        self.youtrack = FakeYouTrack()
        self.telegram = FakeTelegram()
        self.kinds = kinds
        self.opener = opener or self.bao
        self.lines = []
        # A step that runs again writes another mark on its target only with another time.
        self.ticks = itertools.count()

    def __call__(self, now=NOW, **switches):
        self.lines.clear()
        settings = {
            "dry_run": False,
            "paused": False,
            "kinds_enabled": frozenset({"random"}),
            "max_rotations_per_run": 10,
            "card_tag": TAG,
            "telegram_chat_id": CHAT,
        } | switches
        return nightly.run(
            OpenBao(opener=self.opener, token=TOKEN),
            Cluster(self.cluster.kube()),
            self.kinds,
            Switches(**settings),
            youtrack=self.card_client,
            telegram=self.bot,
            out=self.lines.append,
            holder="run on srviac, pid 7",
            now=lambda: now + datetime.timedelta(microseconds=next(self.ticks)),
        )

    def card_client(self, token):
        assert token == JEEVES
        return YouTrack(token, opener=self.youtrack)

    def bot(self, token, chat):
        assert token == BOT
        return Telegram(token, chat, opener=self.telegram)

    def log(self):
        return "\n".join(self.lines)

    def card(self):
        (issue,) = self.youtrack.open()
        return issue


class TestTheLock:
    def test_held_at_the_start_it_is_told_once_and_the_night_ends_there(self):
        bao = world()
        since = "2026-10-05T04:12:09+00:00"
        bao.leaves[LOCK_LEAF] = {
            "data": {
                "holder": "run x/y on c1, pid 9",
                "since": since,
                "plan": "random plan of x/y",
            },
            "meta": {},
        }
        night = Night(bao)
        assert night() == 0
        (message,) = night.telegram.messages
        assert "kv/rotator/lock held by run x/y on c1, pid 9 since 2026-10-05T04:12:09" in message
        assert "it ran nothing tonight" in message
        assert bao.writes() == [] and bao.data(LOCK_LEAF)["holder"] == "run x/y on c1, pid 9"
        assert night.youtrack.requests == []
        # The cluster: no read, the run health alone pushed.
        assert [r[0] for r in night.cluster.requests] == ["PUT"]
        assert night.cluster.pushes() == ["nightly"]

    def test_taken_mid_run_no_further_plan_starts_and_the_rest_is_reported(self):
        bao = world()

        def operator_takes_it_after_the_first_plan(req):
            answer = bao(req)
            body = json.loads(req.data) if req.data else {}
            if req.full_url.endswith(f"/kv/data/{LOCK_LEAF}") and body.get("data") == {}:
                entry = bao.leaves[LOCK_LEAF]
                entry["data"] = {"holder": "run a/b", "since": "now", "plan": "manual plan of a/b"}
                entry["version"] += 1
            return answer

        night = Night(bao, opener=operator_takes_it_after_the_first_plan)
        assert night() == 0
        assert state_of(bao, LEAF).stamps["token"] == TODAY.isoformat()
        assert "bearer-token" not in state_of(bao, TRELLO).stamps
        lock_message, digest = night.telegram.messages
        assert "held by run a/b" in lock_message and "started no plan after it" in lock_message
        assert "Rotated 1:" in digest


class TestDryRun:
    def test_it_plans_every_due_plan_of_the_enabled_kinds_and_writes_nothing(self):
        bao = world()
        manual_due_in(bao, 0)
        night = Night(bao)
        assert night(dry_run=True) == 0
        assert bao.writes() == [] and night.cluster.patches() == []
        log = night.log()
        assert "── random plan of eso/prd/app/prd/token (token) · due: never rotated" in log
        assert (
            "kv.write" in log
            and "k8s.rollout                   roll out app-prd/deployment/app" in log
        )
        assert BOT_LEAF not in log  # manual is not in kinds_enabled
        (digest,) = night.telegram.messages
        assert digest.startswith("Dry run. Secret rotation, 2026-10-05\nWould rotate 2:\n")
        assert f"• random plan of {TRELLO} (bearer-token)" in digest

    def test_it_is_not_capped_it_plans_every_due_plan(self):
        night = Night()
        night(dry_run=True, max_rotations_per_run=1)
        log = night.log()
        for leaf in (LEAF, TRELLO):
            plan = log.split(f"── random plan of {leaf}")[1].split("──")[0]
            assert "kv.write" in plan and "k8s.rollout" in plan
        (digest,) = night.telegram.messages
        assert digest.startswith("Dry run. Secret rotation, 2026-10-05\nWould rotate 2:\n")
        assert "past the cap" not in log + digest

    def test_its_card_is_marked_as_a_dry_run(self):
        bao = world()
        bao.leaves[STRAY] = {"data": {"x": "SECRET-stray"}, "meta": dict(NOT_ANNOTATED)}
        night = Night(bao)
        night(dry_run=True)
        issue = night.card()
        assert issue["description"].startswith("Open as of 2026-10-05 (dry run).")
        assert f"- `{STRAY}`: rotation_x: missing" in issue["description"]
        assert bao.writes() == []


class TestTheDueSet:
    def test_due_plans_rotate_and_are_stamped_and_a_quiet_night_after_touches_nothing(self):
        night = Night()
        assert night() == 0
        bao = night.bao
        assert state_of(bao, LEAF).stamps["token"] == TODAY.isoformat()
        assert state_of(bao, TRELLO).stamps["bearer-token"] == TODAY.isoformat()
        assert bao.data(COPY)["token"] == bao.data(LEAF)["token"] != f"SECRET-{LEAF}-token"
        assert bao.data(LOCK_LEAF) == {}
        (digest,) = night.telegram.messages
        assert "Rotated 2:" in digest and "SECRET" not in digest
        assert night.youtrack.writes() == []  # nothing open: no card
        writes = len(bao.writes())
        assert night(now=NOW + DAY) == 0
        assert len(bao.writes()) == writes and len(night.telegram.messages) == 1
        assert night.youtrack.writes() == []
        assert "telegram: a quiet night, nothing to send" in night.lines

    def test_the_cap_takes_the_oldest_first_and_the_next_night_the_rest(self):
        bao = world()
        put_state(bao, LEAF, stamps={"token": (TODAY - 15 * DAY).isoformat()})
        put_state(bao, TRELLO, stamps={"bearer-token": (TODAY - 30 * DAY).isoformat()})
        night = Night(bao)
        night(max_rotations_per_run=1)
        assert state_of(bao, TRELLO).stamps["bearer-token"] == TODAY.isoformat()
        assert state_of(bao, LEAF).stamps["token"] != TODAY.isoformat()
        assert "1 more due past the cap of 1" in night.telegram.messages[-1]
        night(max_rotations_per_run=1)
        assert state_of(bao, LEAF).stamps["token"] == TODAY.isoformat()

    def test_the_first_pass_drains_every_unstamped_key_under_the_cap(self):
        bao = world()
        night = Night(bao)
        night(max_rotations_per_run=1)
        assert "token" in state_of(bao, LEAF).stamps
        assert "bearer-token" not in state_of(bao, TRELLO).stamps
        night(now=NOW + DAY, max_rotations_per_run=1)
        assert state_of(bao, TRELLO).stamps["bearer-token"] == (TODAY + DAY).isoformat()


class TestAdmission:
    def test_a_plan_with_an_operator_step_is_not_started_and_its_leaf_is_manual_due(self):
        bao = world(due=())
        manual_due_in(bao, 0)
        before = dict(bao.data(BOT_LEAF))
        night = Night(bao)
        assert night(kinds_enabled=frozenset({"random", "manual"})) == 0
        assert state_of(bao, BOT_LEAF).status == "manual-due" and flight_of(bao, BOT_LEAF) is None
        assert bao.data(BOT_LEAF) == before and night.cluster.patches() == []
        line = f"Manual rotation of telegram-bot-token at `{BOT_LEAF}` is due"
        assert night.telegram.messages == [f"Secret rotation, 2026-10-05\n{line}"]
        assert f"### Manual rotations due\n- {line}" in night.card()["description"]

    def test_an_external_key_is_warned_of_and_marked_due_as_a_manual_one(self):
        bao = world(due=())
        edit(
            bao.leaves[BOT_LEAF]["meta"],
            "telegram-bot-token",
            kind="external",
            activate="none",
            notes="Rotated at the vendor.",
        )
        before = dict(bao.data(BOT_LEAF))
        night = Night(bao)
        line = f"Manual rotation of telegram-bot-token at `{BOT_LEAF}` is due"
        manual_due_in(bao, 28)
        night(kinds_enabled=frozenset({"random", "external"}))
        on = TODAY + 28 * DAY
        assert night.telegram.messages == [
            f"Secret rotation, 2026-10-05\n{line} in 28 days, on {on}"
        ]
        manual_due_in(bao, 0)
        assert night(now=NOW + DAY, kinds_enabled=frozenset({"random", "external"})) == 0
        assert state_of(bao, BOT_LEAF).status == "manual-due" and flight_of(bao, BOT_LEAF) is None
        assert bao.data(BOT_LEAF) == before
        assert state_of(bao, BOT_LEAF).stamps["telegram-bot-token"] == str(TODAY - 365 * DAY)
        assert f"### Manual rotations due\n- {line}" in night.card()["description"]

    @pytest.mark.parametrize("status", ["failed", "failed-activation"])
    def test_a_manual_due_leaf_keeps_the_failed_status_of_its_other_plan(self, status):
        bao = world(due=())
        manual_due_in(bao, 0)
        put_state(bao, BOT_LEAF, status=status, last_error="the job failed")
        night = Night(bao)
        assert night(kinds_enabled=frozenset({"random", "manual"})) == 0
        assert state_of(bao, BOT_LEAF).status == status
        assert f"`{BOT_LEAF}`: {status}, rolled back" in night.card()["description"]

    @pytest.mark.parametrize("days", [28, 21, 14, 13, 7, 1])
    def test_a_manual_rotation_is_warned_28_21_and_14_days_ahead_then_nightly(self, days):
        bao = world(due=())
        manual_due_in(bao, days)
        night = Night(bao)
        night(kinds_enabled=frozenset({"random", "manual"}))
        on = TODAY + datetime.timedelta(days=days)
        unit = "day" if days == 1 else "days"
        line = f"Manual rotation of telegram-bot-token at `{BOT_LEAF}` is due in {days} {unit}"
        assert night.telegram.messages == [f"Secret rotation, 2026-10-05\n{line}, on {on}"]
        assert state_of(bao, BOT_LEAF).status is None and night.youtrack.writes() == []

    @pytest.mark.parametrize("days", [29, 27, 22, 20, 15, 300])
    def test_between_those_days_it_is_quiet(self, days):
        bao = world(due=())
        manual_due_in(bao, days)
        night = Night(bao)
        night(kinds_enabled=frozenset({"random", "manual"}))
        assert night.telegram.messages == []

    def test_the_warning_days(self):
        warned = [d for d in range(-3, 40) if nightly.warns(TODAY + d * DAY, TODAY)]
        assert warned == [*range(-3, 14), 14, 21, 28]
        assert nightly.warns(datetime.date.min, TODAY)

    def test_one_line_per_leaf_its_keys_grouped_by_when_they_fall_due(self):
        keys = [("b", TODAY), ("a", datetime.date.min), ("c", TODAY + 14 * DAY)]
        assert nightly.manual_line("eso/x", keys, TODAY) == (
            "Manual rotation of a, b at `eso/x` is due; of c due in 14 days, on 2026-10-19"
        )


class TestHealthFirst:
    def test_a_plan_whose_target_is_not_healthy_is_skipped_onto_the_card_without_telegram(self):
        night = Night(world(due=(LEAF,)))
        app = night.cluster.get("applications", "argocd-prd", "app-prd")
        app["status"]["health"]["status"] = "Degraded"
        assert night() == 0
        bao = night.bao
        assert "token" not in state_of(bao, LEAF).stamps and bao.data(LEAF)["token"].startswith(
            "SECRET-"
        )
        assert night.telegram.messages == [] and night.cluster.patches() == []
        assert (
            f"- `{LEAF}`: its random plan of token: app-prd/deployment/app: its Argo Application "
            "app-prd is Degraded"
        ) in night.card()["description"]
        app["status"]["health"]["status"] = "Healthy"
        night(now=NOW + DAY)
        assert state_of(bao, LEAF).stamps["token"] == (TODAY + DAY).isoformat()
        assert "1 resolved" in night.card()["comments"][-1]


class TestFailure:
    def test_a_failed_plan_is_rolled_back_told_due_again_and_the_run_goes_on(self):
        bao = world()
        bao.refuse["PATCH", f"kv/data/{COPY}"] = 403
        night = Night(bao)
        assert night() == 0
        state = state_of(bao, LEAF)
        assert bao.data(LEAF)["token"] == f"SECRET-{LEAF}-token"
        assert state.status == "failed" and state.failed_nights == 1
        assert flight_of(bao, LEAF) is None
        assert "rotator/staging/random/" + LEAF not in bao.leaves
        assert state_of(bao, TRELLO).stamps["bearer-token"] == TODAY.isoformat()
        failure, digest = night.telegram.messages
        assert failure.startswith(
            f"The random plan of {LEAF} (token) failed at copy to {COPY}#token: PATCH "
            f"kv/data/{COPY}: HTTP 403"
        )
        assert failure.endswith("The run rolls it back; the leaf is due again.")
        assert f"• random plan of {LEAF} (token): rolled back" in digest
        assert (
            f"- `{LEAF}`: failed, rolled back, due again: PATCH kv/data/{COPY}"
            in (night.card()["description"])
        )
        del bao.refuse["PATCH", f"kv/data/{COPY}"]
        night(now=NOW + DAY)
        state = state_of(bao, LEAF)
        assert state.status == "ok" and state.failed_nights == 0
        assert night.card()["description"].endswith("Nothing is open.")

    def test_a_failed_rollback_is_told_too_and_left_for_run(self):
        night = Night(world(due=(LEAF,)))
        night.cluster.stuck.add("app-prd/app")
        night()
        failure, rollback, _ = night.telegram.messages
        assert "failed at roll out app-prd/deployment/app" in failure
        assert rollback.startswith(
            f"The rollback of the random plan of {LEAF} (token) failed at again: roll out "
            "app-prd/deployment/app"
        )
        assert rollback.endswith(f"`secret-rotator run {LEAF}` continues it.")
        assert flight_of(night.bao, LEAF).step == "k8s.rollout:app-prd/deployment/app"
        night.cluster.stuck.clear()
        night(now=NOW + DAY)
        assert "not started: the leaf has its random plan in flight" in night.log()
        assert f"`secret-rotator run {LEAF}` takes it up" in night.card()["description"]

    def test_a_plan_whose_abort_is_refused_stays_stopped_for_run(self):
        journal = Journal()
        kinds = {"random": RandomLike(Tool("deliver", journal, fail=1, undoable=False))}
        night = Night(world(), kinds=kinds)
        assert night() == 0
        failure, digest = night.telegram.messages
        assert "It is not rolled back: deliver cannot be taken back. It stays stopped" in failure
        assert flight_of(night.bao, LEAF).step == "deliver"
        assert state_of(night.bao, TRELLO).stamps["bearer-token"] == TODAY.isoformat()
        assert f"random plan of {LEAF} (token): stopped for `secret-rotator run {LEAF}`" in digest

    def test_a_plan_whose_step_without_an_undo_did_not_land_is_rolled_back(self):
        deliver = Tool("deliver", Journal(), fail=1, undoable=False, landed=False)
        night = Night(world(), kinds={"random": RandomLike(deliver)})
        assert night() == 0
        failure, digest = night.telegram.messages
        assert failure.endswith("The run rolls it back; the leaf is due again.")
        assert f"• random plan of {LEAF} (token): rolled back" in digest
        assert flight_of(night.bao, LEAF) is None
        assert night.bao.data(LEAF)["token"] == f"SECRET-{LEAF}-token"

    def test_a_plan_that_breaks_off_outside_its_steps_does_not_end_the_run(self):
        bao = world()
        bao.refuse["DELETE", f"kv/metadata/rotator/staging/random/{LEAF}"] = 403
        night = Night(bao)
        assert night() == 0
        failure, digest = night.telegram.messages
        assert failure.startswith(f"The random plan of {LEAF} (token) broke off: DELETE")
        assert state_of(bao, TRELLO).stamps["bearer-token"] == TODAY.isoformat()
        assert "Failed 1:" in digest and bao.data(LOCK_LEAF) == {}

    def test_three_failed_nights_hold_the_leaf_until_its_card_is_closed(self):
        journal = Journal()
        kinds = {"random": RandomLike(Tool("deliver", journal, fail=99))}
        night = Night(world(due=(LEAF,)), kinds=kinds)
        for n in range(3):
            night(now=NOW + n * DAY)
        state = state_of(night.bao, LEAF)
        assert state.failed_nights == 3 and state.held_by == "ANS-101"
        assert "not retried while this card is open" in night.card()["description"]
        runs, messages = len(journal), len(night.telegram.messages)
        night(now=NOW + 3 * DAY)
        assert len(journal) == runs and len(night.telegram.messages) == messages
        night.card()["resolved"] = True
        night(now=NOW + 4 * DAY)
        assert len(journal) > runs and state_of(night.bao, LEAF).failed_nights == 1
        assert state_of(night.bao, LEAF).held_by is None
        assert night.card()["idReadable"] == "ANS-102"

    def test_a_plan_that_cannot_be_built_goes_on_the_card(self):
        bao = world(due=(LEAF,))
        edit(bao.meta(LEAF), "token", activate="argocd-sync:app-prd")
        night = Night(bao)
        assert night() == 0
        assert night.telegram.messages == []
        assert (
            f"- `{LEAF}`: its random plan of token: rotation_token activate argocd-sync:app-prd: "
            "no step is built for it yet"
        ) in night.card()["description"]


class TestTheStandingCard:
    def test_it_is_created_as_a_task_with_the_tag(self):
        bao = world(due=())
        bao.leaves[STRAY] = {"data": {"x": "SECRET-stray"}, "meta": dict(NOT_ANNOTATED)}
        night = Night(bao)
        night()
        issue = night.card()
        assert issue["customFields"] == {"Type": "Task", "State": "New"} and issue["tags"] == [TAG]
        assert issue["summary"] == card.SUMMARY and "SECRET" not in issue["description"]
        assert issue["description"].startswith("Open as of 2026-10-05.\n")

    def test_an_open_card_is_commented_with_what_changed_and_rewritten(self):
        bao = world(due=())
        bao.leaves[STRAY] = {"data": {"x": "SECRET-stray"}, "meta": dict(NOT_ANNOTATED)}
        night = Night(bao)
        old = card.render(
            [card.Section("Findings", ("`gone/leaf`: rotation_token: missing",))],
            TODAY - DAY,
            dry_run=False,
        )
        night.youtrack.card(old)
        night()
        issue = night.card()
        assert f"- `{STRAY}`: rotation_x: missing" in issue["description"]
        (comment,) = issue["comments"]
        assert comment.startswith("2026-10-05: 1 new, 1 resolved.\n\nNew:\n")
        assert "Resolved:\n- `gone/leaf`: rotation_token: missing" in comment
        before = len(night.youtrack.writes())
        night(now=NOW + DAY)
        assert len(night.youtrack.writes()) == before  # nothing changed: nothing touched

    def test_args_the_kinds_plugin_cannot_use_are_a_finding_on_it(self):
        bao = world(due=())
        edit(bao.leaves[TRELLO]["meta"], "bearer-token", args={"length": 0})
        night = Night(bao)
        night()
        finding = f"`{TRELLO}`: rotation_bearer-token: args: length: not a whole number from 1"
        assert f"- {finding}" in night.card()["description"]

    def test_youtrack_down_the_rotations_still_run_and_the_run_exits_non_zero(self):
        night = Night(world(due=(LEAF,)))
        night.youtrack.down = True
        assert night() == 1
        assert state_of(night.bao, LEAF).stamps["token"] == TODAY.isoformat()
        assert "error: the open card cannot be looked up" in night.log()


class TestTelegram:
    def test_without_a_chat_id_nothing_is_sent_and_the_log_says_what_was_not(self):
        night = Night(world(due=(LEAF,)))
        assert night(telegram_chat_id=None) == 0
        assert night.telegram.messages == []
        assert "telegram: no telegram_chat_id is committed, so this is not sent:" in night.lines

    def test_telegram_down_the_run_exits_non_zero(self):
        night = Night(world(due=(LEAF,)))
        night.telegram.down = True
        assert night() == 1
        assert state_of(night.bao, LEAF).stamps["token"] == TODAY.isoformat()
        assert "error: a Telegram message was not sent: sendMessage: HTTP 502" in night.log()
        assert BOT not in night.log()


def test_the_log_carries_no_secret_value():
    night = Night()
    night()
    assert "SECRET" not in night.log()
