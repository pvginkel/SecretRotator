"""annotate: the seed's compact form expanded into one rotation_<key> entry per data key, and a
leaf's custom metadata made exactly those entries, every other key removed and listed by leaf;
max_versions 20 on automatic leaves; written with PATCH kv/metadata only, idempotent, stopped by a
refusal; a live entry's expires_at kept; the marker leaves it declares created under rotator/; what
a seed may hold."""

import json
import tempfile
from pathlib import Path

import pytest
import yaml
from fake_cluster import TOKEN, FakeCluster
from fake_openbao import ROLE_ID, SECRET_ID, FakeOpenBao
from fixtures import COMPLIANT, data_of, fields_of

from secret_rotator import annotate as ann
from secret_rotator import cli
from secret_rotator.contract import dump_entry, entry_name

ENV = {cli.ROLE_ID_ENV: ROLE_ID, cli.SECRET_ID_ENV: SECRET_ID, cli.K8S_TOKEN_ENV: TOKEN}
N = len(COMPLIANT)
OIDC = "eso/prd/app/prd/oidc"
YOUTRACK = "jenkins/youtrack"

# What the store holds before the apply: the sweep's annotations and the old layout's keys, and one
# entry the seed changes.
BEFORE = {
    OIDC: {
        "rotation": "coordinated",
        "notes": "Transcript-migrated.",
        "rotation_mechanism": "keycloak-client",
        "key_client_id": "none",
        "rotation_client_secret": dump_entry(
            {"kind": "keycloak-client", "interval": "30d", "activate": "auto"}
        ),
    },
    "eso/prd/es/prd/creds": {
        "rotation": "coordinated",
        "notes": "Retire the filebeat_writer user at slice close.",
        "rotation_interval": "14d",
        "interval_password": "30d",
        "rotation_activate": "auto",
        "rotation_args": '{"user":"filebeat_writer"}',
        "rotation_expires_at": "2027-01-01",
        "rotator_status": "ok",
        "rotated_at_password": "2026-09-01",
    },
    "shared/wifi": {"rotation": "coordinated", "rotated_at": "2026-01-01"},
}
# The leaves of COMPLIANT with a key whose kind is neither manual nor none, a copy included.
AUTOMATIC = sorted(set(COMPLIANT) - {"shared/wifi"})

MARKERS = {
    "rotator/approle/eso": {
        "marker": "secret_id",
        "kind": "approle",
        "args": {"role": "eso", "delivery": "k8s_secret=ns/name"},
        "interval": "14d",
        "activate": "none",
    },
    "rotator/bootstrap/seal-key": {
        "marker": "seal-key",
        "kind": "manual",
        "interval": "never",
        "activate": "none",
        "notes": "the bootstrap tier",
    },
}


def unannotated_bao():
    return FakeOpenBao(
        {path: {"data": data_of(path), "meta": dict(BEFORE.get(path, {}))} for path in COMPLIANT}
    )


def compliant_seed():
    """COMPLIANT as a seed: every key's fields its own, no leaf default."""
    return {
        path: {ann.KEYS: {key: fields_of(meta, key) for key in keys}}
        for path, (keys, meta) in COMPLIANT.items()
    }


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
    def lines_of(self, run, leaf):
        """The dry run's lines under the leaf's own."""
        at = run.lines.index(leaf) + 1
        end = next(i for i, line in enumerate(run.lines[at:], at) if not line.startswith("  "))
        return run.lines[at:end]

    def test_a_dry_run_lists_each_change_and_removal_by_leaf_and_writes_nothing(self, run):
        assert run.apply() == 0, run.text
        assert run.bao.writes() == []
        assert self.lines_of(run, OIDC) == [
            '  add     rotation_client_id={"kind":"none"}',
            '  change  rotation_client_secret={"kind":"keycloak-client","interval":"14d",'
            '"args":{"realm":"homelab"},"activate":"auto"}  (was {"kind":"keycloak-client",'
            '"interval":"30d","activate":"auto"})',
            "  remove  key_client_id=none",
            "  remove  notes=Transcript-migrated.",
            "  remove  rotation=coordinated",
            "  remove  rotation_mechanism=keycloak-client",
            "  set     max_versions=20  (was 0)",
        ]
        removed = [line for line in self.lines_of(run, "eso/prd/es/prd/creds") if "remove" in line]
        assert removed == [
            "  remove  interval_password=30d",
            "  remove  notes=Retire the filebeat_writer user at slice close.",
            "  remove  rotated_at_password=2026-09-01",
            "  remove  rotation=coordinated",
            "  remove  rotation_activate=auto",
            '  remove  rotation_args={"user":"filebeat_writer"}',
            "  remove  rotation_expires_at=2027-01-01",
            "  remove  rotation_interval=14d",
            "  remove  rotator_status=ok",
        ]
        assert run.lines[-1] == (
            f"would patch (dry run; --apply writes) {N} leaf(s), 0 of them "
            f"new marker leaves; 0 unchanged, 0 absent from the store, 0 live "
            f"leaf(s) not in the seed"
        )

    def test_the_apply_reads_metadata_and_key_names_only(self, run):
        run.apply("--apply")
        assert {(m, p.split("/")[1]) for m, p, *_ in run.bao.requests[1:]} == {
            ("LIST", "metadata"),
            ("GET", "metadata"),
            ("GET", "subkeys"),
            ("PATCH", "metadata"),
        }

    def test_every_write_is_a_merge_patch_of_the_entries_and_the_removals(self, run):
        assert run.apply("--apply") == 0, run.text
        writes = run.bao.writes()
        assert len(writes) == N
        for method, path, *_, ctype in writes:
            assert (method, ctype) == ("PATCH", "application/merge-patch+json")
            assert path.startswith("kv/metadata/"), path
        wifi = next(b for _, p, _, b, _ in writes if p == "kv/metadata/shared/wifi")
        assert wifi == {
            "custom_metadata": {
                "rotation_password": '{"kind":"manual","interval":"never","activate":"none",'
                '"notes":"PSK in every device"}',
                "rotated_at": None,
                "rotation": None,
            }
        }

    def test_once_applied_a_leaf_s_custom_metadata_is_exactly_its_entries(self, run):
        assert run.apply("--apply") == 0, run.text
        for path, (_, meta) in COMPLIANT.items():
            assert run.bao.meta(path) == meta, path

    def test_an_entry_of_a_key_the_seed_gives_none_goes_and_is_listed(self, run):
        stale = dump_entry({"kind": "none"})
        run.bao.meta(OIDC).update({entry_name("url"): stale, entry_name("gone"): stale})
        run.bao.leaves[OIDC]["data"]["url"] = "https://app"
        assert run.apply() == 0, run.text
        assert f"  remove  rotation_gone={stale}" in run.lines
        assert f"  remove  rotation_url={stale}" in run.lines
        assert run.apply("--apply") == 0, run.text
        assert run.bao.meta(OIDC) == COMPLIANT[OIDC][1]

    def test_the_packaged_seed_takes_the_sweep_s_notes_off_the_elastic_leaves(self, run):
        # R4: the seed carries no note for either leaf, so the go-live's apply removes them.
        notes = {
            "eso/prd/filebeat/prd/elastic-credentials": "filebeat_writer: retire at slice close",
            "eso/prd/iot/prd/elastic-credentials": "iotsupport: retire at slice close",
        }
        run.bao = FakeOpenBao(
            {
                leaf: {
                    "data": {"password": "SECRET-p", "username": "SECRET-u"},
                    "meta": {"notes": note, "rotation": "coordinated"},
                }
                for leaf, note in notes.items()
            }
        )
        assert run("annotate") == 0, run.text
        for leaf, note in notes.items():
            assert f"  remove  notes={note}" in self.lines_of(run, leaf), leaf
        assert run("annotate", "--apply") == 0, run.text
        for leaf in notes:
            meta = run.bao.meta(leaf)
            assert sorted(meta) == ["rotation_password", "rotation_username"], leaf
            assert not any("notes" in fields_of(meta, key) for key in ("password", "username"))

    def test_after_the_apply_every_key_has_its_entry(self, run):
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

    def test_a_seed_note_is_the_key_s_entry_s_and_replaces_the_note_the_entry_held(self, run):
        seed = compliant_seed()
        seed[OIDC][ann.KEYS]["client_secret"]["notes"] = "the client of app"
        run.write_seed(seed)
        run.bao.meta(OIDC)["rotation_client_secret"] = dump_entry(
            {"kind": "keycloak-client", "activate": "auto", "notes": "Transcript-migrated."}
        )
        run.apply("--apply")
        assert fields_of(run.bao.meta(OIDC), "client_secret")["notes"] == "the client of app"
        assert "notes" not in fields_of(run.bao.meta(OIDC), "client_id")
        before = len(run.bao.writes())
        run.apply("--apply")
        assert len(run.bao.writes()) == before

    def test_changes_removes_every_key_but_the_entries(self):
        entries = {"token": {"kind": "random", "activate": "auto"}}
        current = {
            "rotation_token": dump_entry(entries["token"]),
            "rotation_mechanism": "random",
            "notes": "n",
        }
        assert ann.changes(entries, current) == {"notes": None, "rotation_mechanism": None}

    def test_a_seed_leaf_the_store_lacks_is_reported_and_skipped(self, run):
        del run.bao.leaves["shared/wifi"]
        assert run.apply("--apply") == 0, run.text
        assert "absent from the store, skipped: shared/wifi" in run.lines
        assert "kv/metadata/shared/wifi" not in [p for _, p, *_ in run.bao.writes()]
        assert "kv/data/shared/wifi" not in [p for _, p, *_ in run.bao.writes()]

    def test_a_leaf_whose_keys_cannot_be_read_is_reported_and_skipped(self, run):
        run.bao.leaves["shared/wifi"]["data"] = None
        assert run.apply("--apply") == 0, run.text
        assert (
            "its keys cannot be read (current version deleted or destroyed), skipped: shared/wifi"
            in run.lines
        )
        assert "kv/metadata/shared/wifi" not in [p for _, p, *_ in run.bao.writes()]

    def test_a_key_no_kind_of_the_seed_falls_to_and_a_seed_key_the_leaf_lacks_are_reported(
        self, run
    ):
        seed = compliant_seed()
        seed["shared/wifi"][ann.KEYS]["psk"] = {"kind": "manual", "activate": "none"}
        run.write_seed(seed)
        run.bao.leaves[OIDC]["data"]["url"] = "https://app"
        assert run.apply("--apply") == 0, run.text
        assert f"no kind in the seed, no entry: {OIDC}#url" in run.lines
        assert "named in the seed, not held by the leaf: shared/wifi#psk" in run.lines
        assert entry_name("url") not in run.bao.meta(OIDC)
        assert entry_name("psk") not in run.bao.meta("shared/wifi")

    def test_a_live_leaf_the_seed_lacks_is_reported_but_not_the_working_leaves(self, run):
        run.bao.leaves["eso/prd/new/prd/thing"] = {"data": {"k": "SECRET-x"}, "meta": {}}
        run.bao.leaves["rotator/lock"] = {"data": {"holder": ""}, "meta": {}}
        run.bao.leaves["rotator/state"] = {"data": {"eso/x": "{}"}, "meta": {}}
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

    def test_more_than_64_metadata_keys_once_patched_writes_nothing(self, run):
        run.bao.meta(OIDC).update({f"sweep_{i}": "x" for i in range(61)})
        assert run.apply() == 0, run.text
        seed = compliant_seed()
        seed[OIDC]["kind"] = "none"
        run.write_seed(seed)
        run.bao.leaves[OIDC]["data"].update({f"k{i}": "x" for i in range(63)})
        assert run.apply("--apply") == 1
        assert run.bao.writes() == []
        assert f"cannot write: {OIDC}: more than 64 metadata keys once patched" in run.lines

    def test_a_seed_problem_reaches_openbao_not_at_all(self, run):
        seed = compliant_seed()
        seed["shared/wifi"]["rotated_at"] = "2026-10-04"
        run.write_seed(seed)
        assert run.apply("--apply") == 1
        assert run.bao.requests == []
        assert "shared/wifi: rotated_at: not a field of the entry" in run.text


class TestExpiry:
    """A seed's expires_at is written only with the entry it creates; once the entry exists, its
    expires_at, or its absence, is the rotations' and the operator's (design §3.2, §5)."""

    @pytest.fixture
    def run(self, run):
        seed = compliant_seed()
        seed[YOUTRACK][ann.KEYS]["admin-token"]["expires_at"] = "2026-11-30"
        run.write_seed(seed)
        return run

    def entry(self, run):
        return fields_of(run.bao.meta(YOUTRACK), "admin-token")

    def test_the_seed_s_expiry_is_written_with_the_entry_it_creates(self, run):
        assert run.apply("--apply") == 0, run.text
        assert self.entry(run)["expires_at"] == "2026-11-30"

    def test_an_existing_entry_keeps_its_own_expiry_and_lists_no_change(self, run):
        run.apply("--apply")
        fields = self.entry(run) | {"expires_at": "2027-03-01"}
        run.bao.meta(YOUTRACK)["rotation_admin-token"] = dump_entry(fields)
        before = len(run.bao.writes())
        assert run.apply() == 0
        assert YOUTRACK not in run.lines
        assert run.apply("--apply") == 0
        assert len(run.bao.writes()) == before
        assert self.entry(run)["expires_at"] == "2027-03-01"

    def test_an_existing_entry_a_rotation_cleared_stays_without_one(self, run):
        run.apply("--apply")
        fields = {k: v for k, v in self.entry(run).items() if k != "expires_at"}
        run.bao.meta(YOUTRACK)["rotation_admin-token"] = dump_entry(fields)
        assert run.apply("--apply") == 0
        assert "expires_at" not in self.entry(run)

    def test_a_rewrite_of_an_existing_entry_keeps_its_expiry(self, run):
        run.apply("--apply")
        fields = self.entry(run) | {"expires_at": "2027-03-01", "interval": "30d"}
        run.bao.meta(YOUTRACK)["rotation_admin-token"] = dump_entry(fields)
        assert run.apply("--apply") == 0
        assert self.entry(run) == {
            "kind": "youtrack-token",
            "interval": "14d",
            "activate": "none",
            "expires_at": "2027-03-01",
        }

    def test_changes_keeps_a_live_expiry_and_its_absence(self):
        seed = {"token": {"kind": "random", "activate": "auto", "expires_at": "2026-11-30"}}
        assert ann.changes(seed, {}) == {
            "rotation_token": json.dumps(seed["token"], separators=(",", ":"))
        }
        cleared = {"rotation_token": dump_entry({"kind": "random", "activate": "auto"})}
        assert ann.changes(seed, cleared) == {}
        kept = {"rotation_token": dump_entry(seed["token"] | {"expires_at": "2027-01-01"})}
        assert ann.changes(seed, kept) == {}
        broken = {"rotation_token": "not json"}
        assert ann.changes(seed, broken) == {
            "rotation_token": dump_entry({"kind": "random", "activate": "auto"})
        }


class TestMaxVersions:
    """An automatic leaf keeps 20 KV versions; annotate sets it (design §3.4)."""

    def versions(self, run):
        return {path: run.bao.leaves[path].get("max_versions", 0) for path in COMPLIANT}

    def test_the_apply_sets_20_on_automatic_leaves_and_leaves_the_others(self, run):
        run.bao.leaves["shared/wifi"]["max_versions"] = 5
        assert run.apply("--apply") == 0, run.text
        assert self.versions(run) == {path: 20 for path in AUTOMATIC} | {"shared/wifi": 5}
        for _, path, _, body, _ in run.bao.writes():
            leaf = path.removeprefix("kv/metadata/")
            assert body.get("max_versions") == (20 if leaf in AUTOMATIC else None), leaf

    def test_a_copy_makes_its_leaf_automatic_and_none_or_manual_do_not(self, run):
        assert ann.automatic({"t": {"kind": "copy:eso/prd/a#t"}, "u": {"kind": "none"}})
        assert ann.automatic({"t": {"kind": "random"}})
        assert not ann.automatic({"t": {"kind": "manual"}, "u": {"kind": "none"}})
        assert not ann.automatic({})

    def test_a_leaf_lacking_only_its_max_versions_is_patched_with_it_alone(self, run):
        run.apply("--apply")
        run.bao.leaves["iac/copy"]["max_versions"] = 50
        before = len(run.bao.writes())
        assert run.apply() == 0, run.text
        at = run.lines.index("iac/copy")
        assert run.lines[at + 1] == "  set     max_versions=20  (was 50)"
        assert run.lines[-1].startswith("would patch (dry run; --apply writes) 1 leaf(s)")
        assert run.apply("--apply") == 0, run.text
        assert run.bao.writes()[before:] == [
            (
                "PATCH",
                "kv/metadata/iac/copy",
                {},
                {"max_versions": 20},
                "application/merge-patch+json",
            )
        ]
        assert run.bao.leaves["iac/copy"]["max_versions"] == 20


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
        assert run.lines[at + 2] == (
            '  add     rotation_secret_id={"kind":"approle","interval":"14d","args":{"role":"eso",'
            '"delivery":"k8s_secret=ns/name"},"activate":"none"}'
        )
        assert run.lines[at + 3] == "  set     max_versions=20  (was 0)"
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
        assert fields_of(run.bao.meta("rotator/approle/eso"), "secret_id")["kind"] == "approle"
        assert list(run.bao.meta("rotator/approle/eso")) == ["rotation_secret_id"]
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
    """What a seed may hold: a leaf default and per-key fields of design §5's entry, args a mapping
    and the rest strings, within the metadata limits, and marker declarations under rotator/."""

    def load(self, text):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "seed.yaml"
            path.write_text(text)
            return ann.load_seed(path)

    def problems(self, text):
        with pytest.raises(ann.SeedError) as e:
            self.load(text)
        return str(e.value)

    def test_a_seed_maps_leaf_paths_to_a_default_and_per_key_fields(self):
        seed = self.load(
            "iac/x:\n  kind: manual\n  interval: 365d\n  args: {type: github-pat}\n"
            "  activate: none\n  keys:\n    a-b_c: {kind: none}\n"
            "    d:\n      interval: never\n      notes: n\n"
        )
        assert seed.leaves == {
            "iac/x": ann.SeedLeaf(
                {
                    "kind": "manual",
                    "interval": "365d",
                    "args": {"type": "github-pat"},
                    "activate": "none",
                },
                {"a-b_c": {"kind": "none"}, "d": {"interval": "never", "notes": "n"}},
            )
        }
        assert seed.markers == {}

    def test_fields_that_are_not_the_entry_s(self):
        text = self.problems(
            "iac/x:\n  rotated_at: '2026-10-04'\n  rotator_status: ok\n"
            "  rotation: coordinated\n  rotation_mechanism: manual\n"
            "  keys:\n    a: {key_a: none}\n"
        )
        for key in ("rotated_at", "rotator_status", "rotation", "rotation_mechanism"):
            assert f"iac/x: {key}: not a field of the entry" in text
        assert "iac/x: keys: a: key_a: not a field of the entry" in text

    def test_values_of_the_wrong_type_or_an_unknown_kind(self):
        text = self.problems(
            "iac/x:\n  interval: 14\n  args: '{\"a\":1}'\n  kind: keycloak\n"
            "  keys:\n    a: {expires_at: 2027-01-31}\n"
        )
        assert "iac/x: interval: not a string" in text
        assert "iac/x: args: not a mapping" in text
        assert "iac/x: kind: unknown kind 'keycloak'" in text
        assert "iac/x: keys: a: expires_at: not a string" in text

    def test_an_entry_over_512_bytes_refuses_the_seed(self):
        text = self.problems(f"iac/x:\n  kind: manual\n  activate: none\n  notes: {'n' * 513}\n")
        assert (
            "iac/x: the leaf default's entry: 585 bytes with an expires_at, more than 512" in text
        )

    def test_a_scheduled_entry_counts_the_expires_at_a_rotation_adds(self):
        notes = "n" * 454  # 500 bytes as the seed gives it, 526 once a rotation adds an expires_at
        text = self.problems(f"iac/x:\n  kind: manual\n  activate: none\n  notes: {notes}\n")
        assert (
            "iac/x: the leaf default's entry: 526 bytes with an expires_at, more than 512" in text
        )

    def test_a_none_or_copy_entry_counts_no_expires_at(self):
        # Each entry 500 bytes, which neither kind adds an expires_at to.
        seed = self.load(
            f"iac/x:\n  kind: none\n  notes: {'n' * 474}\n"
            f"iac/z:\n  kind: 'copy:iac/y#k'\n  activate: none\n  notes: {'n' * 448}\n"
        )
        assert set(seed.leaves) == {"iac/x", "iac/z"}

    def test_a_key_s_entry_counts_the_default_s_fields_it_takes(self):
        activate = "manual:" + "a" * 250
        notes = "n" * 250
        text = self.problems(
            f"iac/x:\n  kind: random\n  activate: '{activate}'\n"
            f"  keys:\n    k:\n      notes: {notes}\n    u: {{kind: none, notes: {notes}}}\n"
        )
        assert "iac/x: keys: k: its entry: 575 bytes with an expires_at, more than 512" in text
        assert "keys: u" not in text

    def test_a_key_whose_entry_name_is_over_128_bytes(self):
        key = "k" * 120
        assert f"iac/x: keys: {key}: rotation_{key} is longer than 128 bytes" in self.problems(
            f"iac/x:\n  kind: none\n  keys:\n    {key}: {{notes: n}}\n"
        )

    def test_a_leaf_or_a_field_given_twice(self):
        assert "iac/x appears twice" in self.problems("iac/x:\n  notes: a\niac/x:\n  notes: b\n")
        assert "notes appears twice" in self.problems("iac/x:\n  notes: a\n  notes: b\n")

    def test_paths_that_are_not_leaves(self):
        for path in ("/iac/x", "iac/x/", "iac//x"):
            assert "not a leaf path" in self.problems(f"'{path}':\n  notes: a\n"), path

    def test_a_seed_that_is_not_a_mapping(self):
        assert "not a mapping of leaf paths" in self.problems("- iac/x\n")
        assert "iac/x: not a mapping of the entry's fields" in self.problems("iac/x: manual\n")

    def test_keys_that_are_no_mapping_of_data_keys_to_fields(self):
        assert "iac/x: keys: not a mapping of data keys" in self.problems(
            "iac/x:\n  kind: none\n  keys: [a]\n"
        )
        text = self.problems("iac/x:\n  kind: none\n  keys:\n    a/b: {notes: n}\n    c: none\n")
        assert "iac/x: keys: 'a/b': not a data key name" in text
        assert "iac/x: keys: c: not a mapping of the entry's fields" in text

    def test_a_marker_declares_its_data_key_and_is_no_field(self):
        seed = self.load("rotator/approle/eso:\n  marker: secret_id\n  notes: n\n")
        assert seed.markers == {"rotator/approle/eso": "secret_id"}
        assert seed.leaves == {"rotator/approle/eso": ann.SeedLeaf({"notes": "n"})}

    def test_an_entry_that_only_declares_a_marker_has_no_fields(self):
        assert "rotator/x: not a mapping of the entry's fields" in self.problems(
            "rotator/x:\n  marker: k\n"
        )

    def test_a_marker_lives_under_rotator_and_names_a_key(self):
        for leaf in ("iac/x", "rotator/staging/x", "rotator/lock", "rotator/state"):
            assert f"{leaf}: marker: a marker leaf lives under rotator/" in self.problems(
                f"{leaf}:\n  marker: k\n  notes: n\n"
            ), leaf
        for key in ("a/b", "''", "a b", "1"):
            assert "rotator/x: marker: not a data key name" in self.problems(
                f"rotator/x:\n  marker: {key}\n  notes: n\n"
            ), key


class TestExpand:
    """The compact form expanded over a leaf's data keys (design §3.1, §5)."""

    def kinds(self, default, keys, own=None):
        leaf = ann.SeedLeaf(default, own or {})
        return {key: fields["kind"] for key, fields in ann.expand(leaf, keys).items()}

    def test_the_default_s_kind_falls_to_each_key_it_may_rotate(self):
        assert self.kinds({"kind": "keycloak-client"}, ["client_id", "client_secret"]) == {
            "client_id": "none",
            "client_secret": "keycloak-client",
        }
        assert self.kinds(
            {"kind": "jenkins-token"},
            ["jenkins-token", "telegram-bot-token", "telegram-chat-id"],
            {"telegram-bot-token": {"kind": "manual"}, "telegram-chat-id": {"kind": "none"}},
        ) == {
            "jenkins-token": "jenkins-token",
            "telegram-bot-token": "manual",
            "telegram-chat-id": "none",
        }
        assert self.kinds({"kind": "copy:eso/prd/a#token"}, ["token"]) == {
            "token": "copy:eso/prd/a#token"
        }
        assert self.kinds(
            {"kind": "manual"},
            ["api-key", "bearer-token", "token"],
            {"bearer-token": {"kind": "random"}},
        ) == {"api-key": "manual", "bearer-token": "random", "token": "manual"}
        assert self.kinds({"kind": "none"}, ["a", "b"]) == {"a": "none", "b": "none"}

    def test_a_one_key_kind_falls_to_the_single_key_without_a_kind_and_to_none_of_two(self):
        assert self.kinds(
            {"kind": "cephx"}, ["user_id", "user_key"], {"user_id": {"kind": "none"}}
        ) == {
            "user_id": "none",
            "user_key": "cephx",
        }
        assert self.kinds({"kind": "cephx"}, ["user_id", "user_key"]) == {}

    def test_a_key_naming_the_one_key_kind_claims_it_alone(self):
        # A7: the single-key fallback applies only while no key names the kind.
        assert self.kinds(
            {"kind": "jenkins-token"}, ["token", "user"], {"token": {"kind": "jenkins-token"}}
        ) == {"token": "jenkins-token"}

    def test_a_key_no_kind_falls_to_has_no_entry(self):
        assert self.kinds({"kind": "elastic-user"}, ["password", "username"]) == {
            "password": "elastic-user"
        }
        assert self.kinds({"interval": "14d"}, ["token"]) == {}

    def test_the_default_s_other_fields_go_where_the_kind_takes_them_and_a_key_s_own_win(self):
        leaf = ann.SeedLeaf(
            {
                "kind": "manual",
                "interval": "365d",
                "args": {"type": "github-pat"},
                "activate": "auto",
                "notes": "n",
            },
            {
                "copy": {"kind": "copy:eso/prd/a#token"},
                "id": {"kind": "none"},
                "never": {"interval": "never", "notes": "why"},
                "bearer": {"kind": "random", "args": {}},
            },
        )
        assert ann.expand(leaf, ["bearer", "copy", "id", "never", "pat"]) == {
            "bearer": {
                "kind": "random",
                "interval": "365d",
                "args": {},
                "activate": "auto",
                "notes": "n",
            },
            "copy": {"kind": "copy:eso/prd/a#token", "activate": "auto", "notes": "n"},
            "id": {"kind": "none", "notes": "n"},
            "never": {
                "kind": "manual",
                "interval": "never",
                "args": {"type": "github-pat"},
                "activate": "auto",
                "notes": "why",
            },
            "pat": {
                "kind": "manual",
                "interval": "365d",
                "args": {"type": "github-pat"},
                "activate": "auto",
                "notes": "n",
            },
        }

    def test_a_key_the_seed_names_that_the_leaf_lacks_has_no_entry(self):
        leaf = ann.SeedLeaf({"kind": "none"}, {"gone": {"notes": "n"}})
        assert ann.expand(leaf, ["here"]) == {"here": {"kind": "none"}}
