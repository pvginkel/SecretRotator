"""github.webhook (design §4.2): GitHub reached with the token from rotator/github; a hook's secret
set from KV with the rest of its config kept and re-read; the ping that proves it, read from the
hook's deliveries; the redelivery of what failed since the leaf's version was written; and the
github-webhook: activator (design §4.3) built into plans, run, failed and rolled back."""

import datetime

import pytest
from fake_github import (
    CONFIG,
    CREDENTIALS,
    HOOK,
    OLD,
    REPO,
    SPEC,
    START,
    TOKEN,
    FakeGitHub,
)
from fake_openbao import FakeOpenBao
from plans import COPY, LEAF, NOW, Recorder, client, fake_of, flight_of, lock, run_state, state_of
from test_activation import ticking
from test_kinds import KINDS, store_of

from secret_rotator.audit import audit
from secret_rotator.executor import Executor, Outcome
from secret_rotator.github import GitHubError
from secret_rotator.githubsteps import PING_BOUND, GitHubWebhook, parse_hook
from secret_rotator.model import Action, StepFailed
from secret_rotator.plan import PlanError, make

KEY_LEAF = "eso/prd/app/prd/hook"
NEW = "SECRET-new-hook-secret"
STEP_ID = f"github.webhook:{SPEC}"
CONFIG_PATH = f"/repos/{REPO}/hooks/{HOOK}/config"


def bao_with(value=NEW, created=START, **leaves):
    found = {
        "rotator/github": {"data": dict(CREDENTIALS), "meta": {}},
        KEY_LEAF: {"data": {"secret": value}, "meta": {}, "created": {1: created}},
    }
    return FakeOpenBao(found | leaves)


class Ctx:
    def __init__(self, bao=None):
        self.fake = bao or bao_with()
        self.bao = client(self.fake)
        self.now = NOW
        self.values = {}
        self.progressed = []

    def progress(self, detail):
        self.progressed.append(detail)

    def stage(self, name, value):
        self.values[name] = value

    def staged(self, name):
        return self.values.get(name)


def step(fake):
    return GitHubWebhook(fake.github(), SPEC, KEY_LEAF, "secret")


def holding_new(fake):
    """The receiver holds the new secret, as once its consumers rolled onto it."""
    fake.held = lambda: NEW
    return fake


class TestTheClient:
    def test_a_refusal_names_the_request_and_github_s_message_never_the_token(self):
        fake = FakeGitHub()
        fake.refused["GET", CONFIG_PATH] = 404
        github = fake.github()
        github.authenticate(TOKEN)
        with pytest.raises(GitHubError) as e:
            github.hook_config(REPO, HOOK)
        assert (str(e.value), e.value.status) == (f"GET {CONFIG_PATH}: HTTP 404: Refused", 404)
        assert TOKEN not in str(e.value)

    def test_a_transport_error_has_no_status(self):
        fake = FakeGitHub()
        fake.broken["GET", CONFIG_PATH] = ConnectionResetError("reset by peer")
        github = fake.github()
        with pytest.raises(GitHubError) as e:
            github.hook_config(REPO, HOOK)
        assert e.value.status is None
        assert str(e.value).startswith(f"GET {CONFIG_PATH}: transport error: ")

    def test_it_sends_the_token_as_bearer_with_the_api_version(self):
        fake = FakeGitHub()
        github = fake.github()
        with pytest.raises(GitHubError, match="HTTP 401: Bad credentials"):
            github.hook_config(REPO, HOOK)
        github.authenticate(TOKEN)
        assert github.hook_config(REPO, HOOK) == CONFIG | {"secret": "********"}

    def test_without_since_it_lists_the_newest_page(self):
        fake = FakeGitHub(page=2)
        made = [fake.deliver("push", at=START + datetime.timedelta(minutes=n)) for n in range(5)]
        github = fake.github()
        github.authenticate(TOKEN)
        assert [d["id"] for d in github.deliveries(REPO, HOOK)] == [made[4]["id"], made[3]["id"]]

    def test_with_since_it_pages_back_to_the_first_page_reaching_before_it(self):
        fake = FakeGitHub(page=2)
        made = [fake.deliver("push", at=START + datetime.timedelta(minutes=n)) for n in range(7)]
        github = fake.github()
        github.authenticate(TOKEN)
        found = github.deliveries(REPO, HOOK, START + datetime.timedelta(minutes=3))
        assert [d["id"] for d in found] == [d["id"] for d in reversed(made[3:])]
        pages = [q.get("cursor") for m, p, q, _ in fake.requests if p.endswith("/deliveries")]
        assert pages == [None, "v1_2", "v1_4"]


class TestTheStep:
    def test_it_parses_its_hook_from_the_activator(self):
        assert parse_hook(SPEC) == (REPO, HOOK)
        s = step(FakeGitHub())
        assert s.id == STEP_ID
        assert s.title == f"set the secret of GitHub hook {SPEC} from {KEY_LEAF}#secret"
        assert s.mutates and s.activator and s.undo is None

    def test_it_sets_the_secret_keeping_the_config_and_a_ping_proves_it(self):
        fake = holding_new(FakeGitHub())
        detail = step(fake).run(Ctx())
        assert fake.secret() == NEW
        assert fake.hooks[REPO, HOOK]["config"] == CONFIG
        (patch,) = [body for m, p, _, body in fake.requests if m == "PATCH"]
        assert patch == CONFIG | {"secret": NEW}
        assert detail == "secret set; ping answered HTTP 200; no failed delivery to redeliver"
        (ping,) = fake.hooks[REPO, HOOK]["deliveries"]
        assert (ping["event"], ping["status_code"]) == ("ping", 200)

    def test_a_ping_the_receiver_refuses_fails_the_step(self):
        fake = FakeGitHub()  # the receiver still holds the old secret
        with pytest.raises(StepFailed) as e:
            step(fake).run(Ctx())
        (ping,) = fake.hooks[REPO, HOOK]["deliveries"]
        assert e.value.error == (
            f"the ping of hook {SPEC} failed: Invalid HTTP Response: 401 (delivery {ping['id']})"
        )
        assert fake.secret() == NEW

    def test_a_ping_github_does_not_deliver_in_time_fails_the_step(self):
        fake = holding_new(FakeGitHub(ping_lag=PING_BOUND + 60))
        ctx = Ctx()
        with pytest.raises(StepFailed) as e:
            step(fake).run(ctx)
        assert e.value.error == (
            f"no ping of hook {SPEC} within 2 min: waiting for GitHub to deliver the ping"
        )
        assert ctx.progressed[0] == "waiting for GitHub to deliver the ping"

    def test_an_earlier_ping_is_not_taken_for_its_own(self):
        fake = holding_new(FakeGitHub())
        fake.deliver("ping", signed=OLD)  # refused, before the step
        step(fake).run(Ctx())
        assert [d["status_code"] for d in fake.hooks[REPO, HOOK]["deliveries"]] == [401, 200]

    def test_a_config_github_re_reads_otherwise_fails_the_step(self):
        fake = holding_new(FakeGitHub())
        fake.mangle = True
        with pytest.raises(StepFailed, match=f"the re-read config of hook {SPEC} is not the"):
            step(fake).run(Ctx())

    def test_without_its_token_or_its_value_it_fails_before_github_hears_of_it(self):
        fake = FakeGitHub()
        bao = bao_with()
        del bao.leaves["rotator/github"]
        with pytest.raises(StepFailed, match="rotator/github cannot be read"):
            step(fake).run(Ctx(bao))
        bao = bao_with()
        bao.leaves["rotator/github"]["data"] = {}
        with pytest.raises(StepFailed, match="rotator/github has no token"):
            step(fake).run(Ctx(bao))
        bao = bao_with()
        bao.leaves[KEY_LEAF]["data"] = {}
        with pytest.raises(StepFailed, match=f"{KEY_LEAF}#secret cannot be read"):
            step(fake).run(Ctx(bao))
        assert fake.requests == []


class TestTheRedelivery:
    def world(self):
        """A hook whose deliveries straddle START, when the leaf's new version was written: the
        guids of the ones the step redelivers, oldest first."""
        fake = holding_new(FakeGitHub())
        minute = datetime.timedelta(minutes=1)
        fake.deliver("push", at=START - minute, signed=OLD)  # failed before the window
        b = fake.deliver("push", at=START + minute, signed=OLD)
        c = fake.deliver("push", at=START + 2 * minute, signed=OLD)
        fake.deliver("push", at=START + 3 * minute, signed=NEW)  # answered
        fake.deliver("ping", at=START + 4 * minute, signed=OLD)  # a ping is never redelivered
        f = fake.deliver("push", at=START + 5 * minute, signed=OLD)
        fake.deliver("push", at=START + 6 * minute, guid=f["guid"], signed=NEW)  # redelivered
        fake.now = 600
        return fake, [b["guid"], c["guid"]]

    def test_it_redelivers_oldest_first_what_failed_since_the_version_and_was_never_answered(self):
        fake, want = self.world()
        detail = step(fake).run(Ctx())
        assert fake.redelivered()[1:] == want
        assert [fake.attempts(guid) for guid in want] == [[401, 200], [401, 200]]
        assert detail.endswith(f"; redelivered 2: {', '.join(want)}")

    def test_a_redelivery_github_signs_as_the_original_fails_and_the_step_does_not(self):
        fake, want = self.world()
        fake.redelivery_signed = "original"
        step(fake).run(Ctx())
        assert [fake.attempts(guid) for guid in want] == [[401, 401], [401, 401]]

    def test_its_first_run_s_version_bounds_every_later_run(self):
        fake, want = self.world()
        ctx = Ctx()
        s = step(fake)
        s.run(ctx)
        # A rollback's KV undo writes a later version; a delivery failed between the two.
        late = fake.deliver("push", signed="SECRET-something-else")
        ctx.fake.leaves[KEY_LEAF]["created"][1] = fake.at() + datetime.timedelta(hours=1)
        s.run(ctx)
        assert fake.redelivered()[-1] == late["guid"]
        assert ctx.staged(f"{STEP_ID}:since") == START.isoformat()

    def test_a_refused_redelivery_fails_the_step(self):
        fake, want = self.world()
        (b,) = [d for d in fake.hooks[REPO, HOOK]["deliveries"] if d["guid"] == want[0]]
        fake.refused["POST", f"/repos/{REPO}/hooks/{HOOK}/deliveries/{b['id']}/attempts"] = 422
        with pytest.raises(GitHubError, match="HTTP 422"):
            step(fake).run(Ctx())

    def test_nothing_it_reports_carries_a_secret_or_the_token(self):
        fake, _ = self.world()
        ctx = Ctx()
        texts = [step(fake).run(ctx), *ctx.progressed]
        fake.mangle = True
        with pytest.raises(StepFailed) as e:
            step(fake).run(ctx)
        texts += [e.value.error, e.value.technical]
        for text in texts:
            assert NEW not in text and OLD not in text and TOKEN not in text, text


# --- the github-webhook: activator in plans --------------------------------------------------


def store_and_bao(**activate):
    store = store_of(**activate)
    bao = fake_of(store)
    bao.leaves["rotator/github"] = {"data": dict(CREDENTIALS), "meta": {}}
    bao.now = START
    return store, bao


def plan_of(store, fake, leaf=LEAF):
    return make(KINDS, leaf, "random", ["token"], audit(store), github=fake.github())


def run(bao, plan, recorder=None):
    executor = Executor(
        client(bao),
        plan,
        recorder or Recorder(),
        lock(bao),
        state=run_state(bao),
        dry_run=False,
        clock=ticking(),
    )
    return executor, executor.run()


HOOKED = {"eso__prd__app__prd__token": f"github-webhook:{SPEC}"}


class TestTheActivator:
    def test_it_builds_the_step_with_the_key_whose_entry_names_the_hook(self):
        store, _ = store_and_bao(
            eso__prd__app__prd__token=f"github-webhook:{SPEC}",
            iac__copy="github-webhook:pvginkel/Other/7",
        )
        steps = plan_of(store, FakeGitHub()).steps
        hooks = [s for s in steps if isinstance(s, GitHubWebhook)]
        assert [(s.id, s.leaf, s.key) for s in hooks] == [
            (STEP_ID, LEAF, "token"),
            ("github.webhook:pvginkel/Other/7", COPY, "token"),
        ]

    def test_a_hook_two_entries_name_refuses_the_plan(self):
        store, _ = store_and_bao(
            eso__prd__app__prd__token=f"github-webhook:{SPEC}",
            iac__copy=f"github-webhook:{SPEC}",
        )
        with pytest.raises(PlanError) as e:
            plan_of(store, FakeGitHub())
        assert str(e.value) == (
            f"{LEAF}: {COPY}'s rotation_token activate github-webhook:{SPEC}: rotation_token "
            f"names the hook too, and it takes one secret"
        )

    def test_it_runs_to_done(self):
        fake = FakeGitHub()
        store, bao = store_and_bao(**HOOKED)
        fake.held = lambda: bao.data(LEAF)["token"]
        _, outcome = run(bao, plan_of(store, fake))
        assert outcome is Outcome.DONE
        assert fake.secret() == bao.data(LEAF)["token"] != OLD
        assert state_of(bao, LEAF).status == "ok"

    def test_a_refused_ping_stops_the_plan_and_abort_puts_the_old_secret_back(self):
        fake = FakeGitHub()
        store, bao = store_and_bao(**HOOKED)
        old = bao.data(LEAF)["token"]
        fake.hooks[REPO, HOOK]["secret"] = old
        fake.held = lambda: old  # the consumers never took the new secret
        recorder = Recorder()
        executor, outcome = run(bao, plan_of(store, fake), recorder)
        assert outcome is Outcome.FAILED
        assert state_of(bao, LEAF).status == "failed-activation"
        assert flight_of(bao, LEAF).step == STEP_ID
        assert fake.secret() != old
        recorder.events.clear()
        assert executor.abort() is Outcome.ROLLED_BACK
        assert [(line[1], line[2]) for line in recorder.lines() if line[0] == "ok"] == [
            (f"kv.copy:{COPY}#token", Action.UNDO),
            ("kv.write", Action.UNDO),
            (STEP_ID, Action.RERUN),
        ]
        assert fake.secret() == old == bao.data(LEAF)["token"]
        pings = [d["status_code"] for d in fake.hooks[REPO, HOOK]["deliveries"]]
        assert pings == [401, 200]
