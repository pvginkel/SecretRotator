"""The command line: its usage errors, and the offline audit over a key file."""

import io
import json
import subprocess
import sys
import tempfile
from contextlib import redirect_stderr
from pathlib import Path

import pytest
import yaml
from fake_cluster import TOKEN, FakeCluster
from fake_openbao import ROLE_ID, SECRET_ID, FakeOpenBao
from fixtures import COMPLIANT
from plans import fake_of
from test_kinds import ACTIVATE_NONE, WIFI, store_of

from secret_rotator import cli
from secret_rotator.console import Console
from secret_rotator.contract import LOCK_LEAF


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
    "argv", [["audit"], ["annotate"], ["annotate", "--apply"], ["plan", "x/y"], ["run", "x/y"]]
)
def test_a_live_command_without_the_rotators_approle_is_a_usage_error(argv):
    code, err = usage(*argv)
    assert code == 2
    assert "SECRET_ROTATOR_ROLE_ID and SECRET_ROTATOR_SECRET_ID are not set" in err


@pytest.mark.parametrize("argv", [["audit"], ["plan", "x/y"], ["run", "x/y"]])
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
    env = {
        "SECRET_ROTATOR_ROLE_ID": ROLE_ID,
        "SECRET_ROTATOR_SECRET_ID": SECRET_ID,
        "SECRET_ROTATOR_K8S_TOKEN": TOKEN,
    }
    code = cli.main(
        ["run", WIFI], opener=bao, environ=env, console=lambda: con, kube=FakeCluster().kube
    )
    assert code == 0
    assert bao.data(WIFI) == {"password": "SECRET-psk"}
    assert "SECRET" not in con.stdout.getvalue()
    assert bao.data(LOCK_LEAF) == {} and bao.version(LOCK_LEAF) == 2


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
        assert "      1  you   operator.credential   Mint a new password and enter it" in self.lines

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
