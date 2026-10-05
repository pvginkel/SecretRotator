"""The SecretRotator commit a run logs, from the record a VCS install leaves (PEP 610
direct_url.json)."""

import json
from importlib import metadata

from secret_rotator import provenance

COMMIT = "0394711c7d5e4b0f8a1d2c3b4a5968778695a4b3"
# The record `uv tool install git+https://github.com/pvginkel/SecretRotator@prd` leaves.
VCS = {
    "url": "https://github.com/pvginkel/SecretRotator",
    "vcs_info": {"vcs": "git", "requested_revision": "prd", "commit_id": COMMIT},
}
# The record `poetry install` leaves in a checkout.
EDITABLE = {"url": "file:///work/SecretRotator", "dir_info": {"editable": True}}


def test_a_vcs_install_names_its_commit():
    assert provenance.describe(json.dumps(VCS)) == f"commit {COMMIT}"


def test_any_other_install_names_where_it_came_from():
    assert (
        provenance.describe(json.dumps(EDITABLE)) == "file:///work/SecretRotator, not a VCS install"
    )


def test_an_install_without_a_record_says_so():
    assert provenance.describe(None) == "no install record"


def test_source_reads_the_installed_distribution_s_record(monkeypatch):
    class Distribution:
        def read_text(self, name):
            assert name == "direct_url.json"
            return json.dumps(VCS)

    asked = []
    monkeypatch.setattr(metadata, "distribution", lambda name: asked.append(name) or Distribution())
    assert provenance.source() == f"commit {COMMIT}"
    assert asked == ["secret-rotator"]
