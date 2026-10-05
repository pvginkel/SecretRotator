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
from fake_openbao import FakeOpenBao
from fixtures import COMPLIANT

from secret_rotator import cli


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


@pytest.mark.parametrize("argv", [["audit"], ["annotate"], ["annotate", "--apply"]])
def test_a_live_command_without_the_rotators_approle_is_a_usage_error(argv):
    code, err = usage(*argv)
    assert code == 2
    assert "SECRET_ROTATOR_ROLE_ID and SECRET_ROTATOR_SECRET_ID are not set" in err


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
    assert "audit" in result.stdout and "annotate" in result.stdout


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

    def test_a_key_file_that_is_not_leaf_to_key_names(self):
        self.names = {"iac/x": "token"}
        assert self.check() == 1
        assert "not a JSON object of leaf path -> key names" in "\n".join(self.lines)
