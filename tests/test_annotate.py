"""annotate: the seed written with PATCH kv/metadata only, idempotent, stopped by a refusal, and
the marker leaves it declares created under rotator/; what a seed may hold."""

import tempfile
from pathlib import Path

import pytest
import yaml
from fake_cluster import TOKEN, FakeCluster
from fake_openbao import ROLE_ID, SECRET_ID, FakeOpenBao
from fixtures import COMPLIANT, data_of

from secret_rotator import annotate as ann
from secret_rotator import cli

ENV = {cli.ROLE_ID_ENV: ROLE_ID, cli.SECRET_ID_ENV: SECRET_ID, cli.K8S_TOKEN_ENV: TOKEN}
N = len(COMPLIANT)

# What the store holds before the apply: the sweep's annotations.
BEFORE = {
    "eso/prd/app/prd/oidc": {
        "rotation": "coordinated",
        "rotation_mechanism": "keycloak",
        "notes": "Transcript-migrated.",
    },
    "eso/prd/es/prd/creds": {"rotation": "coordinated", "rotation_mechanism": "elasticsearch"},
    "shared/wifi": {
        "rotation": "coordinated",
        "rotation_mechanism": "wifi",
        "rotated_at": "2026-01-01",
    },
}

MARKERS = {
    "rotator/approle/eso": {
        "marker": "secret_id",
        "rotation_mechanism": "approle",
        "rotation_args": '{"role":"eso","delivery":"k8s_secret=ns/name"}',
        "rotation_interval": "14d",
        "rotation_activate": "none",
    },
    "rotator/bootstrap/seal-key": {
        "marker": "seal-key",
        "rotation_mechanism": "manual",
        "rotation_interval": "never",
        "rotation_activate": "none",
        "notes": "the bootstrap tier",
    },
}


def unannotated_bao():
    return FakeOpenBao(
        {path: {"data": data_of(path), "meta": dict(BEFORE.get(path, {}))} for path in COMPLIANT}
    )


def compliant_seed():
    return {path: dict(meta) for path, (_, meta) in COMPLIANT.items()}


class Run:
    """The command line against a fake OpenBao, with a seed in a temporary directory."""

    def __init__(self, bao):
        self.bao = bao
        tmp = tempfile.TemporaryDirectory()
        self._tmp = tmp
        self.dir = Path(tmp.name)
        self.seed = self.dir / "seed.yaml"
        self.write_seed(compliant_seed())

    def write_seed(self, seed):
        self.seed.write_text(yaml.safe_dump(seed, sort_keys=False))

    def __call__(self, *argv, env=ENV):
        self.lines = []
        code = cli.main(
            list(argv),
            opener=self.bao,
            out=self.lines.append,
            environ=env,
            kube=FakeCluster().kube,
        )
        self.text = "\n".join(self.lines)
        return code

    def apply(self, *extra):
        return self("annotate", f"--seed={self.seed}", *extra)


@pytest.fixture
def run():
    r = Run(unannotated_bao())
    yield r
    r._tmp.cleanup()


class TestApply:
    def test_a_dry_run_lists_each_change_and_writes_nothing(self, run):
        assert run.apply() == 0, run.text
        assert run.bao.writes() == []
        assert "eso/prd/app/prd/oidc" in run.lines
        assert "  add     rotation_interval=14d" in run.lines
        assert "  change  rotation_mechanism=keycloak-client  (was keycloak)" in run.lines
        assert run.lines[-1] == (
            f"would patch (dry run; --apply writes) {N} leaf(s), 0 of them "
            f"new marker leaves; 0 unchanged, 0 absent from the store, 0 live "
            f"leaf(s) not in the seed"
        )

    def test_the_apply_reads_metadata_only(self, run):
        run.apply("--apply")
        assert {(m, p.split("/")[1]) for m, p, *_ in run.bao.requests[1:]} == {
            ("LIST", "metadata"),
            ("GET", "metadata"),
            ("PATCH", "metadata"),
        }

    def test_every_write_is_a_merge_patch_of_the_changed_keys(self, run):
        assert run.apply("--apply") == 0, run.text
        writes = run.bao.writes()
        assert len(writes) == N
        for method, path, _, body, ctype in writes:
            assert (method, ctype) == ("PATCH", "application/merge-patch+json")
            assert path.startswith("kv/metadata/"), path
            assert list(body) == ["custom_metadata"]
        wifi = next(b for _, p, _, b, _ in writes if p == "kv/metadata/shared/wifi")
        assert wifi["custom_metadata"] == {
            "rotation_mechanism": "manual",
            "rotation_interval": "never",
            "notes": "PSK in every device",
            "rotation_activate": "none",
        }

    def test_keys_the_seed_does_not_name_survive(self, run):
        run.apply("--apply")
        assert run.bao.meta("shared/wifi")["rotated_at"] == "2026-01-01"
        assert run.bao.meta("eso/prd/app/prd/oidc")["rotation"] == "coordinated"

    def test_after_the_apply_the_audit_passes(self, run):
        run.apply("--apply")
        assert run("audit") == 0, run.text

    def test_a_second_apply_changes_nothing(self, run):
        run.apply("--apply")
        before = len(run.bao.writes())
        assert run.apply("--apply") == 0
        assert len(run.bao.writes()) == before
        assert run.lines[-1] == (
            f"patching 0 leaf(s), 0 of them new marker leaves; {N} "
            f"unchanged, 0 absent from the store, 0 live leaf(s) not in the "
            f"seed"
        )

    def test_seed_notes_keep_the_earlier_notes(self, run):
        seed = compliant_seed()
        seed["eso/prd/app/prd/oidc"]["notes"] = "the client of app"
        run.write_seed(seed)
        run.apply("--apply")
        assert (
            run.bao.meta("eso/prd/app/prd/oidc")["notes"]
            == "the client of app | earlier: Transcript-migrated."
        )
        before = len(run.bao.writes())
        run.apply("--apply")
        assert len(run.bao.writes()) == before

    def test_a_seed_note_inside_a_different_earlier_note_is_still_written(self, run):
        seed = compliant_seed()
        seed["eso/prd/app/prd/oidc"]["notes"] = "migrated"
        run.write_seed(seed)
        run.apply("--apply")
        assert (
            run.bao.meta("eso/prd/app/prd/oidc")["notes"]
            == "migrated | earlier: Transcript-migrated."
        )
        before = len(run.bao.writes())
        run.apply("--apply")
        assert len(run.bao.writes()) == before

    def test_a_seed_leaf_the_store_lacks_is_reported_and_skipped(self, run):
        del run.bao.leaves["shared/wifi"]
        assert run.apply("--apply") == 0, run.text
        assert "absent from the store, skipped: shared/wifi" in run.lines
        assert "kv/metadata/shared/wifi" not in [p for _, p, *_ in run.bao.writes()]
        assert "kv/data/shared/wifi" not in [p for _, p, *_ in run.bao.writes()]

    def test_a_live_leaf_the_seed_lacks_is_reported_but_not_the_working_leaves(self, run):
        run.bao.leaves["eso/prd/new/prd/thing"] = {"data": {"k": "SECRET-x"}, "meta": {}}
        run.bao.leaves["rotator/lock"] = {"data": {"holder": ""}, "meta": {}}
        run.bao.leaves["rotator/staging/random/x"] = {"data": {"k": "SECRET-y"}, "meta": {}}
        assert run.apply() == 0
        assert [line for line in run.lines if line.startswith("not in the seed")] == [
            "not in the seed: eso/prd/new/prd/thing"
        ]

    def test_a_refused_write_stops_the_run_and_names_the_capability(self, run):
        run.bao.refuse["PATCH", "kv/metadata/eso/prd/app/prd/token"] = 403
        assert run.apply("--apply") == 1
        assert [p for _, p, *_ in run.bao.writes()] == [
            "kv/metadata/eso/prd/app/prd/oidc",
            "kv/metadata/eso/prd/app/prd/token",
        ]
        last = run.lines[-1]
        assert last.startswith("stopped at eso/prd/app/prd/token: OpenBao refused"), last
        assert "the openbao role's rotator policy grants patch on the kv mount" in last
        assert f"Patched 1 of {N} leaf(s); run the apply again after the converge" in last

    def test_a_refusal_at_the_first_write_writes_nothing_more(self, run):
        run.bao.refuse = {("PATCH", f"kv/metadata/{path}"): 403 for path in COMPLIANT}
        assert run.apply("--apply") == 1
        assert len(run.bao.writes()) == 1
        assert f"Patched 0 of {N}" in run.lines[-1]

    def test_any_other_failed_write_stops_the_run_too(self, run):
        run.bao.refuse["PATCH", "kv/metadata/eso/prd/app/prd/oidc"] = 500
        assert run.apply("--apply") == 1
        assert len(run.bao.writes()) == 1
        assert "HTTP 500" in run.lines[-1]
        assert "capability" not in run.lines[-1]

    def test_a_merged_notes_too_long_writes_nothing(self, run):
        run.bao.leaves["eso/prd/app/prd/oidc"]["meta"]["notes"] = "x" * 500
        seed = compliant_seed()
        seed["eso/prd/app/prd/oidc"]["notes"] = "the client of app"
        run.write_seed(seed)
        assert run.apply("--apply") == 1
        assert run.bao.writes() == []
        assert (
            "cannot write: eso/prd/app/prd/oidc: notes: with the earlier notes kept, "
            "longer than 512 bytes"
        ) in run.lines

    def test_a_seed_problem_reaches_openbao_not_at_all(self, run):
        seed = compliant_seed()
        seed["shared/wifi"]["rotated_at"] = "2026-10-04"
        run.write_seed(seed)
        assert run.apply("--apply") == 1
        assert run.bao.requests == []
        assert "shared/wifi: rotated_at: not an operator key of the contract" in run.text


class TestMarkers:
    """A marker leaf the seed declares is created with its one data key, then annotated."""

    @pytest.fixture
    def run(self, run):
        run.write_seed({**compliant_seed(), **MARKERS})
        return run

    def test_a_dry_run_lists_the_creation_and_writes_nothing(self, run):
        assert run.apply() == 0, run.text
        assert run.bao.writes() == []
        at = run.lines.index("rotator/approle/eso")
        assert run.lines[at + 1] == "  create  marker leaf, data key secret_id"
        assert "  add     rotation_mechanism=approle" in run.lines[at + 2 :]
        assert ", 2 of them new marker leaves;" in run.lines[-1]

    def test_the_apply_creates_each_marker_with_its_one_key_then_patches_it(self, run):
        assert run.apply("--apply") == 0, run.text
        creates = [(p, b) for m, p, _, b, _ in run.bao.writes() if m == "POST"]
        assert creates == [
            (
                "kv/data/rotator/approle/eso",
                {"options": {"cas": 0}, "data": {"secret_id": ann.MARKER_VALUE}},
            ),
            (
                "kv/data/rotator/bootstrap/seal-key",
                {"options": {"cas": 0}, "data": {"seal-key": ann.MARKER_VALUE}},
            ),
        ]
        assert run.bao.leaves["rotator/approle/eso"]["data"] == {"secret_id": ann.MARKER_VALUE}
        assert run.bao.meta("rotator/approle/eso")["rotation_mechanism"] == "approle"
        assert "marker" not in run.bao.meta("rotator/approle/eso")
        assert "created and patched rotator/approle/eso" in run.lines
        assert run("audit") == 0, run.text

    def test_an_existing_marker_is_annotated_and_its_data_left_alone(self, run):
        run.bao.leaves["rotator/approle/eso"] = {
            "data": {"secret_id": "rewritten by the kind"},
            "meta": {},
        }
        assert run.apply("--apply") == 0, run.text
        assert [p for m, p, *_ in run.bao.writes() if m == "POST"] == [
            "kv/data/rotator/bootstrap/seal-key"
        ]
        assert run.bao.leaves["rotator/approle/eso"]["data"] == {
            "secret_id": "rewritten by the kind"
        }
        before = len(run.bao.writes())
        assert run.apply("--apply") == 0
        assert len(run.bao.writes()) == before

    def test_a_marker_the_store_gained_meanwhile_stops_the_run(self, run):
        run.bao.refuse["POST", "kv/data/rotator/approle/eso"] = 400
        assert run.apply("--apply") == 1
        assert run.lines[-1].startswith(
            "stopped at rotator/approle/eso: POST kv/data/rotator/approle/eso: HTTP 400"
        )


class TestSeed:
    """What a seed may hold: operator keys of design §5, strings, within the metadata limits,
    and marker declarations under rotator/."""

    def load(self, text):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "seed.yaml"
            path.write_text(text)
            return ann.load_seed(path)

    def problems(self, text):
        with pytest.raises(ann.SeedError) as e:
            self.load(text)
        return str(e.value)

    def test_a_seed_maps_leaf_paths_to_string_metadata(self):
        seed = self.load(
            "iac/x:\n  rotation_mechanism: manual\n  key_a-b_c: none\n  interval_a-b_c: never\n"
        )
        assert seed.annotations == {
            "iac/x": {
                "rotation_mechanism": "manual",
                "key_a-b_c": "none",
                "interval_a-b_c": "never",
            }
        }
        assert seed.markers == {}

    def test_keys_that_are_not_the_operators(self):
        text = self.problems(
            "iac/x:\n  rotated_at: '2026-10-04'\n  rotator_status: ok\n"
            "  rotation: coordinated\n  rotated_at_token: '2026-10-04'\n"
        )
        for key in ("rotated_at", "rotator_status", "rotation", "rotated_at_token"):
            assert f"iac/x: {key}: not an operator key of the contract" in text

    def test_values_that_are_not_strings_or_too_long(self):
        text = self.problems(f"iac/x:\n  rotation_interval: 14\n  notes: {'n' * 513}\n")
        assert "iac/x: rotation_interval: not a string" in text
        assert "iac/x: notes: longer than 512 bytes" in text

    def test_too_many_keys_for_one_leaf(self):
        keys = "".join(f"  key_k{i}: none\n" for i in range(65))
        assert "iac/x: 65 metadata keys, more than 64" in self.problems(f"iac/x:\n{keys}")

    def test_a_leaf_or_key_given_twice(self):
        assert "iac/x appears twice" in self.problems("iac/x:\n  notes: a\niac/x:\n  notes: b\n")
        assert "notes appears twice" in self.problems("iac/x:\n  notes: a\n  notes: b\n")

    def test_paths_that_are_not_leaves(self):
        for path in ("/iac/x", "iac/x/", "iac//x"):
            assert "not a leaf path" in self.problems(f"'{path}':\n  notes: a\n"), path

    def test_a_seed_that_is_not_a_mapping(self):
        assert "not a mapping of leaf paths" in self.problems("- iac/x\n")
        assert "iac/x: not a mapping of metadata keys" in self.problems("iac/x: manual\n")

    def test_a_marker_declares_its_data_key_and_is_no_metadata(self):
        seed = self.load("rotator/approle/eso:\n  marker: secret_id\n  notes: n\n")
        assert seed.markers == {"rotator/approle/eso": "secret_id"}
        assert seed.annotations == {"rotator/approle/eso": {"notes": "n"}}

    def test_an_entry_that_only_declares_a_marker_has_no_metadata(self):
        assert "rotator/x: not a mapping of metadata keys" in self.problems(
            "rotator/x:\n  marker: k\n"
        )

    def test_a_marker_lives_under_rotator_and_names_a_key(self):
        for leaf in ("iac/x", "rotator/staging/x", "rotator/lock"):
            assert f"{leaf}: marker: a marker leaf lives under rotator/" in self.problems(
                f"{leaf}:\n  marker: k\n  notes: n\n"
            ), leaf
        for key in ("a/b", "''", "a b", "1"):
            assert "rotator/x: marker: not a data key name" in self.problems(
                f"rotator/x:\n  marker: {key}\n  notes: n\n"
            ), key
