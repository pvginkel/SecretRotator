"""The command line: its usage errors, and the offline audit over a key file."""

import io
import json
import subprocess
import sys
import tempfile
import urllib.error
from contextlib import redirect_stderr
from pathlib import Path

import pytest
import yaml
from fake_cluster import TOKEN, FakeCluster
from fake_openbao import ROLE_ID, SECRET_ID, FakeOpenBao, approle
from fake_openbao import TOKEN as BAO_TOKEN
from fake_telegram import CHAT, FakeTelegram
from fake_telegram import TOKEN as BOT
from fake_youtrack import TAG, FakeYouTrack
from fixtures import COMPLIANT
from plans import LEAF, fake_of
from test_kinds import ACTIVATE_NONE, WIFI, store_of
from test_nightly import TRELLO, WEBHOOK
from test_nightly import world as nightly_world

from secret_rotator import cli
from secret_rotator.console import Console
from secret_rotator.contract import LOCK_LEAF
from secret_rotator.openbao import OpenBao
from secret_rotator.switches import Switches, SwitchesError
from secret_rotator.telegram import Telegram
from secret_rotator.youtrack import YouTrack

SOURCE = "commit 0394711c7d5e4b0f8a1d2c3b4a5968778695a4b3"


def usage(*argv, env=None):
    err = io.StringIO()
    with redirect_stderr(err), pytest.raises(SystemExit) as e:
        cli.main(
            list(argv),
            opener=lambda req: pytest.fail("no request expected"),
            out=lambda line: None,
            environ=env or {},
        )
    return e.value.code, err.getvalue()


def test_a_command_is_required():
    assert usage()[0] == 2


def test_the_live_audit_takes_no_seed():
    assert (
        usage(
            "audit",
            "--seed=s.yaml",
            env={"SECRET_ROTATOR_ROLE_ID": "r", "SECRET_ROTATOR_SECRET_ID": "s"},
        )[0]
        == 2
    )


@pytest.mark.parametrize(
    "argv",
    [["audit"], ["annotate"], ["annotate", "--apply"], ["plan", "x/y"], ["run", "x/y"], ["run"]],
)
def test_a_live_command_without_the_rotators_approle_is_a_usage_error(argv):
    code, err = usage(*argv)
    assert code == 2
    assert "SECRET_ROTATOR_ROLE_ID and SECRET_ROTATOR_SECRET_ID are not set" in err


@pytest.mark.parametrize("argv", [["audit"], ["plan", "x/y"], ["run", "x/y"], ["run"]])
def test_a_live_command_that_reads_the_cluster_without_its_token_is_a_usage_error(argv):
    env = {"SECRET_ROTATOR_ROLE_ID": "r", "SECRET_ROTATOR_SECRET_ID": "s"}
    code, err = usage(*argv, env=env)
    assert code == 2
    assert "SECRET_ROTATOR_K8S_TOKEN is not set: the secret-rotator ServiceAccount's token" in err


def test_annotate_reads_no_cluster():
    env = {"SECRET_ROTATOR_ROLE_ID": ROLE_ID, "SECRET_ROTATOR_SECRET_ID": SECRET_ID}
    kube = lambda token: pytest.fail("annotate built a cluster client")  # noqa: E731
    assert (
        cli.main(["annotate"], opener=FakeOpenBao(), out=lambda line: None, environ=env, kube=kube)
        == 0
    )


def test_the_live_plan_takes_no_seed():
    env = {"SECRET_ROTATOR_ROLE_ID": "r", "SECRET_ROTATOR_SECRET_ID": "s"}
    code, err = usage("plan", "x/y", "--seed=s.yaml", env=env)
    assert code == 2 and "the live plan reads the store, not a seed" in err


def test_run_takes_a_leaf_and_runs_its_plan_in_the_terminal():
    store = store_of(**ACTIVATE_NONE)
    bao = fake_of(store)
    con = Console(io.StringIO("y\nSECRET-psk\nc\n"), io.StringIO())
    lines = []
    env = {
        "SECRET_ROTATOR_ROLE_ID": ROLE_ID,
        "SECRET_ROTATOR_SECRET_ID": SECRET_ID,
        "SECRET_ROTATOR_K8S_TOKEN": TOKEN,
    }
    code = cli.main(
        ["run", WIFI],
        opener=bao,
        out=lines.append,
        environ=env,
        console=lambda: con,
        kube=FakeCluster().kube,
        source=lambda: SOURCE,
    )
    assert code == 0
    assert lines == [f"secret-rotator run {WIFI}, {SOURCE}"]
    assert bao.data(WIFI) == {"password": "SECRET-psk"}
    assert "SECRET" not in con.stdout.getvalue()
    assert bao.data(LOCK_LEAF) == {} and bao.version(LOCK_LEAF) == 2


ENV = {
    cli.ROLE_ID_ENV: ROLE_ID,
    cli.SECRET_ID_ENV: SECRET_ID,
    cli.K8S_TOKEN_ENV: TOKEN,
}


def switches(**changes):
    settings = {
        "dry_run": False,
        "paused": False,
        "kinds_enabled": frozenset({"random"}),
        "max_rotations_per_run": 10,
        "card_tag": TAG,
        "telegram_chat_id": CHAT,
    }
    return lambda: Switches(**(settings | changes))


class TestTheNightlyRun:
    def test_paused_stops_it_before_it_does_anything(self):
        lines = []
        code = cli.main(
            ["run"],
            opener=lambda req: pytest.fail("a request"),
            out=lines.append,
            environ=ENV,
            kube=lambda token: pytest.fail("a cluster client"),
            switches=switches(paused=True),
            source=lambda: SOURCE,
        )
        assert code == 0
        assert lines == [
            f"secret-rotator run, {SOURCE}",
            "paused: the switches stop the nightly run before it does anything",
        ]

    def test_run_without_a_path_is_the_nightly_run(self):
        bao = nightly_world(due=(LEAF,))
        for leaf, key in ((TRELLO, "bearer-token"), (WEBHOOK, "token")):
            bao.meta(leaf)[f"rotated_at_{key}"] = "2999-01-01"  # not due on any real date
        youtrack, telegram, lines = FakeYouTrack(), FakeTelegram(), []
        code = cli.main(
            ["run"],
            opener=bao,
            out=lines.append,
            environ=ENV,
            kube=FakeCluster().kube,
            switches=switches(),
            youtrack=lambda token: YouTrack(token, opener=youtrack),
            telegram=lambda token, chat: Telegram(token, chat, opener=telegram),
            source=lambda: SOURCE,
        )
        assert code == 0, lines
        assert lines[0] == f"secret-rotator run, {SOURCE}"
        assert "rotated_at_token" in bao.meta(LEAF)
        taken = [
            r for r in bao.requests if r[:2] == ("POST", f"kv/data/{LOCK_LEAF}") and r[3]["data"]
        ]
        assert taken[0][3]["data"]["holder"].startswith("run on ")
        assert "Rotated 1:" in telegram.messages[-1]


REFUSED = "error: POST auth/approle/login: transport error: connection refused"


def refusing_login():
    bao = FakeOpenBao()
    bao.broken["auth/approle/login"] = urllib.error.URLError("connection refused")
    return bao


class TestEveryRunNamesItsCommitFirst:
    """Before anything that can fail, so a run that fails at its start-up has named it."""

    @pytest.mark.parametrize(
        ("argv", "first"),
        [
            (["run"], f"secret-rotator run, {SOURCE}"),
            (["run", WIFI], f"secret-rotator run {WIFI}, {SOURCE}"),
        ],
    )
    def test_a_run_whose_login_is_refused(self, argv, first):
        lines = []
        code = cli.main(
            argv,
            opener=refusing_login(),
            out=lines.append,
            environ=ENV,
            console=lambda: pytest.fail("a console"),
            switches=switches(),
            source=lambda: SOURCE,
        )
        assert code == 1
        assert lines == [first, REFUSED]

    def test_a_nightly_run_whose_switches_do_not_load(self):
        def broken():
            raise SwitchesError("dry_run: missing")

        lines = []
        code = cli.main(
            ["run"],
            opener=FakeOpenBao(),
            out=lines.append,
            environ=ENV,
            switches=broken,
            source=lambda: SOURCE,
        )
        assert code == 1
        assert lines == [f"secret-rotator run, {SOURCE}", "error: dry_run: missing"]

    @pytest.mark.parametrize("argv", [["run"], ["run", WIFI]])
    def test_a_run_without_its_credentials(self, argv):
        lines = []
        with redirect_stderr(io.StringIO()), pytest.raises(SystemExit):
            cli.main(argv, out=lines.append, environ={}, source=lambda: SOURCE)
        assert len(lines) == 1 and lines[0].endswith(f", {SOURCE}")

    def test_a_run_whose_start_up_breaks(self, monkeypatch):
        def regression():
            raise RuntimeError("a regression")

        monkeypatch.setattr(cli.registry, "load", regression)
        lines = []
        with pytest.raises(RuntimeError):
            cli.main(
                ["run", WIFI],
                opener=FakeOpenBao(),
                out=lines.append,
                environ=ENV,
                source=lambda: SOURCE,
            )
        assert lines == [f"secret-rotator run {WIFI}, {SOURCE}"]


class TestRunPathTellsTelegram:
    def test_not_until_a_chat_id_is_committed(self):
        con = Console(io.StringIO(), io.StringIO())
        assert (
            cli.notifier(OpenBao(opener=FakeOpenBao(), token=BAO_TOKEN), None, Telegram, con)
            is None
        )

    def test_with_the_bot_s_token_and_a_failed_message_is_said_not_raised(self):
        bao = FakeOpenBao({"rotator/telegram": {"data": {"token": BOT}, "meta": {}}})
        telegram = FakeTelegram()
        con = Console(io.StringIO(), io.StringIO())
        notify = cli.notifier(
            OpenBao(opener=bao, token=BAO_TOKEN),
            CHAT,
            lambda token, chat: Telegram(token, chat, opener=telegram),
            con,
        )
        notify("The random plan of x failed")
        assert telegram.messages == ["The random plan of x failed"]
        telegram.down = True
        notify("again")
        assert "The Telegram message about it is not sent: sendMessage: HTTP 502" in (
            con.stdout.getvalue()
        )


def own_store(now):
    """The rotator's own leaf and AppRole; its token is refused an hour after each login."""
    return FakeOpenBao(
        {
            cli.OWN_LEAF: {"data": {"role_id": ROLE_ID, "secret_id": SECRET_ID}, "meta": {}},
            "shared/x": {"data": {"k": "v"}, "meta": {}},
        },
        approles={"rotator": approle(ROLE_ID, **{SECRET_ID: "accessor-0"})},
        clock=lambda: now[0],
    )


def logins(bao):
    return [r[3]["secret_id"] for r in bao.requests if r[1] == "auth/approle/login"]


def test_a_wait_past_the_end_of_its_token_logs_in_again_with_the_secret_id_in_hand():
    now = [0.0]
    bao = own_store(now)
    c = cli.connect(ENV, bao, lambda: now[0])
    now[0] = 3290.0  # the last request before a step waits (jenkins.job: up to 30 min)
    c.metadata("shared/x")
    now[0] = 3700.0
    assert c.metadata("shared/x") == {}
    assert logins(bao) == [SECRET_ID, SECRET_ID]


@pytest.mark.parametrize("then", [3400.0, 3700.0], ids=["before-its-end", "past-its-end"])
def test_after_its_own_rotation_a_run_logs_in_again_with_the_new_secret_id(then):
    now = [0.0]
    bao = own_store(now)
    c = cli.connect(ENV, bao, lambda: now[0])
    # The approle kind's kv delivery: mint, kv.write's patch of the leaf, destroy the old one.
    _, minted = c.call("POST", "auth/approle/role/rotator/secret-id", {"ttl": "2160h"})
    c.patch(cli.OWN_LEAF, {"secret_id": minted["data"]["secret_id"]}, cas=1)
    destroy = {"secret_id_accessor": "accessor-0"}
    c.call("POST", "auth/approle/role/rotator/secret-id-accessor/destroy", destroy)
    now[0] = then
    assert c.metadata("shared/x") == {}
    assert logins(bao) == [SECRET_ID, "SECRET-rotator-new-1"]


def test_a_missing_seed_is_an_error_not_a_trace():
    lines = []
    env = {"SECRET_ROTATOR_ROLE_ID": "r", "SECRET_ROTATOR_SECRET_ID": "s"}
    assert (
        cli.main(
            ["annotate", "--seed=/nonexistent/seed.yaml"],
            opener=FakeOpenBao(),
            out=lines.append,
            environ=env,
        )
        == 1
    )
    assert lines[-1].startswith("error: "), lines


def test_it_runs_as_a_module():
    result = subprocess.run(
        [sys.executable, "-m", "secret_rotator", "--help"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert all(command in result.stdout for command in ("audit", "annotate", "plan", "run"))


class TestOffline:
    """audit --keys: the seed over the key names of a file, without OpenBao or credentials."""

    @pytest.fixture(autouse=True)
    def files(self):
        tmp = tempfile.TemporaryDirectory()
        d = Path(tmp.name)
        self.seed, self.keys = d / "seed.yaml", d / "keys.json"
        self.seed.write_text(
            yaml.safe_dump(
                {path: dict(meta) for path, (_, meta) in COMPLIANT.items()}, sort_keys=False
            )
        )
        self.names = {path: keys for path, (keys, _) in COMPLIANT.items()}
        yield
        tmp.cleanup()

    def check(self):
        self.keys.write_text(json.dumps(self.names))
        self.lines = []
        return cli.main(
            ["audit", f"--keys={self.keys}", f"--seed={self.seed}"],
            opener=lambda req: pytest.fail(f"the offline audit called {req.full_url}"),
            out=self.lines.append,
            environ={},
        )

    def test_the_seed_resolves_every_key(self):
        assert self.check() == 0, self.lines

    def test_a_leaf_the_seed_does_not_cover_is_a_finding(self):
        self.names["eso/prd/new/prd/thing"] = ["token"]
        assert self.check() == 1
        assert "eso/prd/new/prd/thing: rotation_mechanism: missing" in self.lines

    def test_a_seed_leaf_missing_from_the_key_file_is_reported_not_failed(self):
        del self.names["shared/wifi"]
        assert self.check() == 0, self.lines
        assert "seed leaf not in the key file: shared/wifi" in self.lines

    def test_a_key_the_seed_does_not_resolve_is_a_finding(self):
        self.names["eso/prd/app/prd/oidc"] = ["client_id", "client_secret", "url"]
        assert self.check() == 1
        assert (
            "eso/prd/app/prd/oidc: url: no kind resolves it: keycloak-client does not own "
            "it and no key_url names one"
        ) in self.lines

    def plan(self, leaf):
        self.keys.write_text(json.dumps(self.names))
        self.lines = []
        return cli.main(
            ["plan", leaf, f"--keys={self.keys}", f"--seed={self.seed}"],
            opener=lambda req: pytest.fail(f"the offline plan called {req.full_url}"),
            out=self.lines.append,
            environ={},
        )

    def test_plan_prints_a_leaf_s_plans_from_the_seed(self):
        assert self.plan("shared/wifi") == 0, self.lines
        assert self.lines[:2] == [
            "shared/wifi",
            "  manual plan of password · never due: rotated by hand only · paste a new password",
        ]
        assert (
            "      1  you   operator.credential           Mint a new password and enter it"
            in self.lines
        )

    def test_plan_of_a_leaf_activated_through_the_cluster_fails_offline(self):
        assert self.plan("eso/prd/app/prd/token") == 1
        assert (
            "eso/prd/app/prd/token: its activation is read from the cluster, which an offline "
            "plan does not reach" in self.lines[2]
        )

    def test_a_key_file_that_is_not_leaf_to_key_names(self):
        self.names = {"iac/x": "token"}
        assert self.check() == 1
        assert "not a JSON object of leaf path -> key names" in "\n".join(self.lines)
