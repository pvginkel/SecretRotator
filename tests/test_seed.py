"""The packaged seed over the packaged key-name inventory: every leaf the store holds after the
go-live expands to one compliant entry per key, and the seed says what AnsibleSpecs
secret-rotation/catalog.md says.

store-keys.json maps each leaf to its data key names (no values): the value-blind inventory of
2026-10-04 after slice 044's cutover, plus the rotator's own leaves and markers (catalog
§ rotator/) and jenkins/grafana-api, which the store holds once the operator stores Jenkins'
Grafana token (slice 046). A leaf added to the seed is added there with its key names."""

import datetime
import json
import tempfile
from pathlib import Path

import pytest

from secret_rotator import annotate as ann
from secret_rotator import audit as aud
from secret_rotator import cli
from secret_rotator.contract import MAX_VALUE_BYTES, dump_entry, is_scheduled
from secret_rotator.kinds.manual import TYPES

APPROLES = ("backup", "eso", "eso-dev", "iac-agent", "jenkins", "openbao-admin")
BOOTSTRAP = (
    "age-private-key",
    "ansible-vault-passphrase",
    "git-api-token",
    "jenkins-agent-secret",
    "jwk-provisioner-password",
    "seal-key",
)
CATALOG = "eso/prd/kubecoder/prd/catalog"


def audit_offline(keys_file: Path) -> tuple[int, list[str]]:
    lines: list[str] = []
    code = cli.main(["audit", "--keys", str(keys_file)], out=lines.append, environ={})
    return code, lines


@pytest.fixture(scope="module")
def seed():
    return ann.load_seed(ann.DEFAULT_SEED)


@pytest.fixture
def store():
    return json.loads(ann.DEFAULT_KEYS.read_text())


@pytest.fixture(scope="module")
def entries(seed):
    """Each leaf's entries as annotate expands them over the inventory's keys."""
    keys = json.loads(ann.DEFAULT_KEYS.read_text())
    return {leaf: ann.expand(seed.leaves[leaf], keys[leaf]) for leaf in seed.leaves}


def test_every_key_of_every_leaf_has_its_compliant_entry(store):
    code, lines = audit_offline(Path(str(ann.DEFAULT_KEYS)))
    assert lines[-1].startswith(
        f"0 finding(s) on 0 of {len(store)} leaf(s); blocked: 0 leaf(s), 0 key(s);"
    ), lines
    assert code == 0


def test_the_seed_covers_exactly_the_stores_leaves_and_keys(seed, store, entries):
    assert sorted(seed.leaves) == sorted(store)
    assert {leaf: sorted(e) for leaf, e in entries.items()} == {
        leaf: sorted(keys) for leaf, keys in store.items()
    }
    for leaf, held in seed.leaves.items():
        assert set(held.keys) <= set(store[leaf]), leaf


def test_every_entry_fits_the_metadata_s_512_bytes(entries):
    for leaf, by_key in entries.items():
        for key, fields in by_key.items():
            assert len(dump_entry(fields).encode()) <= MAX_VALUE_BYTES, (leaf, key)


def test_it_holds_once_keycloak_da_admin_is_deleted(store):
    # ANS-229 deletes the leaf; the seed keeps it until then.
    del store["jenkins/keycloak-da-admin"]
    with tempfile.TemporaryDirectory() as tmp:
        keys = Path(tmp, "keys.json")
        keys.write_text(json.dumps(store))
        code, lines = audit_offline(keys)
    assert lines[0] == "seed leaf not in the key file: jenkins/keycloak-da-admin"
    assert lines[-1].startswith(f"0 finding(s) on 0 of {len(store)} leaf(s)")
    assert code == 0


def test_it_holds_until_jenkins_grafana_token_is_stored(store):
    # The operator creates the token in Grafana and stores it (slice 046 Ruling D1).
    del store["jenkins/grafana-api"]
    with tempfile.TemporaryDirectory() as tmp:
        keys = Path(tmp, "keys.json")
        keys.write_text(json.dumps(store))
        code, lines = audit_offline(keys)
    assert lines[0] == "seed leaf not in the key file: jenkins/grafana-api"
    assert lines[-1].startswith(f"0 finding(s) on 0 of {len(store)} leaf(s)")
    assert code == 0


def test_the_per_key_cadences_of_ruling_q1(entries):
    want = {
        "eso/prd/trello-mcp/prd/trello": {
            "api-key": ("manual", "365d"),
            "bearer-token": ("random", "14d"),
            "token": ("manual", "365d"),
        },
        "iac/tf-backend": {
            "age_public_key": ("none", None),
            "age_secret_key": ("external", "365d"),
            "github_token": ("manual", "365d"),
        },
        "shared/samba/users": {
            "mvdbovenkamp": ("manual", "never"),
            "pvginkel": ("samba-user", "365d"),
        },
    }
    for leaf, keys in want.items():
        got = {key: (f["kind"], f.get("interval")) for key, f in entries[leaf].items()}
        assert got == keys, leaf
    trello = entries["eso/prd/trello-mcp/prd/trello"]
    for key in ("api-key", "token"):
        assert trello[key]["notes"].endswith("How it really rotates is ANS-285's."), key


def test_every_never_manual_and_external_key_carries_its_own_notes(entries):
    for leaf, by_key in entries.items():
        for key, fields in by_key.items():
            if fields.get("interval") == "never" or fields["kind"] in ("manual", "external"):
                assert fields.get("notes", "").strip(), (leaf, key)


def test_a_note_the_catalog_gives_one_key_of_a_leaf_is_that_key_s_alone(entries):
    noted = {
        CATALOG: {
            "ansible-vault-password",
            "argocd-token",
            "grafana-api-key",
            "openai-api-key",
            "ssh-key-pve",
        },
        "iac/tf-backend": {"age_secret_key", "github_token"},
        "eso/prd/jenkins-mcp/prd/config": {"token"},
        "eso/prd/trello-mcp/prd/trello": {"api-key", "token"},
        "shared/samba/users": {"mvdbovenkamp"},
        "eso/prd/jenkins-telegram-bot/prd/config": {"telegram-bot-token"},
        "eso/prd/newsfilter/prd/telegram": {"bot-token"},
        "eso/prd/telegram-mcp/prd/telegram": {"bot-token"},
        "eso/prd/prometheus/prd/telegram": {"bot_token"},
    }
    for leaf, keys in noted.items():
        assert {k for k, f in entries[leaf].items() if "notes" in f} == keys, leaf
    assert entries[CATALOG]["ansible-vault-password"]["notes"].startswith("The bootstrap tier")
    assert entries["eso/prd/calendar-support/prd/google-service-account"]["key_json"]["notes"] == (
        "GCP project calendar-display-437018."
    )


def test_the_elastic_leaves_carry_no_note(entries):
    # R4: the sweep's notes on them are not the seed's.
    for leaf in ("eso/prd/filebeat/prd/elastic-credentials", "eso/prd/iot/prd/elastic-credentials"):
        assert not any("notes" in f for f in entries[leaf].values()), leaf


def test_the_bags_vault_passphrase_is_the_bootstrap_tier_external_and_activates_nothing(
    seed, entries
):
    assert entries[CATALOG]["ansible-vault-password"] | {"notes": ""} == {
        "kind": "external",
        "interval": "365d",
        "activate": "none",
        "notes": "",
    }
    store = ann.offline_store(Path(str(ann.DEFAULT_KEYS)), seed, print)
    result = aud.audit(store)
    assert result.findings == []
    due = [s for s in aud.due_keys(store, result, datetime.date(2026, 10, 6)) if s.leaf == CATALOG]
    assert {s.key for s in due if s.kind == "external"} == {"ansible-vault-password"}
    assert {s.key for s in due if s.kind == "manual"} == {
        "argocd-token",
        "grafana-api-key",
        "openai-api-key",
        "ssh-key-pve",
    }


def test_the_keys_rotated_outside_the_tool_are_external_at_365_days(entries):
    # design R82
    want = {
        *((f"rotator/bootstrap/{b}", b) for b in BOOTSTRAP),
        (CATALOG, "ansible-vault-password"),
        ("eso/prd/homeapps/extension-signing", "private-key"),
        ("shared/wifi-iot", "password"),
        ("iac/tf-backend", "age_secret_key"),
        ("eso/prd/storage/prd/s3-mirror", "password"),
        ("eso/prd/storage/prd/s3-mirror", "salt"),
    }
    external = {
        (leaf, key): fields
        for leaf, by_key in entries.items()
        for key, fields in by_key.items()
        if fields["kind"] == "external"
    }
    assert set(external) == want
    for at, fields in external.items():
        assert (fields["interval"], fields["activate"], "args" in fields) == ("365d", "none", False)
        assert fields["notes"].strip(), at
    markers = [external[f"rotator/bootstrap/{b}", b]["notes"] for b in BOOTSTRAP]
    assert len(set(markers)) == len(BOOTSTRAP)
    assert all(n.startswith("The bootstrap tier: ") for n in markers)
    wifi = external["shared/wifi-iot", "password"]["notes"]
    assert "beside the old" in wifi and "remove the old from the router" in wifi


def test_the_never_keys_that_stay_until_their_cards_land(entries):
    # design R84
    never = {
        (leaf, key, fields["kind"])
        for leaf, by_key in entries.items()
        for key, fields in by_key.items()
        if fields.get("interval") == "never"
    }
    assert never == {
        ("jenkins/mydownloads-android-keystore", "password", "manual"),
        ("jenkins/scantopdf-android-keystore", "password", "manual"),
        ("eso/prd/prometheus/prd/healthchecks", "ping_url", "manual"),
        ("jenkins/iot-mqtt", "password", "manual"),
        ("eso/prd/media/prd/mydownloads-users", "users.yml", "manual"),
        ("shared/samba/users", "mvdbovenkamp", "manual"),
        ("shared/jenkins/admin-password", "password", "manual"),
        ("rotator/approle/eso-dev", "secret_id", "approle"),
    }


def test_the_leaves_once_manual_carry_the_kind_the_catalog_gives(entries):
    want = {
        "eso/prd/argocd/prd/webhook": "random",
        "eso/prd/fieldnotes/prd/kubecoder-controller": "kubecoder-client",
        "eso/prd/grafana/prd/admin": "grafana-admin",
        "eso/prd/iot/prd/architecture-pipeline": "jenkins-job-token",
        "eso/prd/kubecoder/prd/step-ca-provisioner-password": "step-ca-password",
        "iac/ansible-ssh-key": "ssh-key",
        "iac/proxmox": "pve-root-password",
        "shared/dev/ceph-csi": "cephx",
        "shared/dev/ceph-rgw/s3": "rgw-admin",
        "shared/prd/ceph-csi": "cephx",
        "shared/prd/ceph-rgw/s3": "rgw-admin",
        "shared/samba/users": "samba-user",
    }
    for leaf, kind in want.items():
        assert kind in {f["kind"] for f in entries[leaf].values()}, leaf
    for key in ("kubeconfig", "kubeconfig-dev-write", "kubeconfig-prd-write"):
        assert entries[CATALOG][key]["kind"] == "k8s-sa-token"


def test_every_scheduled_manual_key_names_the_type_the_catalog_gives_it(entries):
    want = {
        ("eso/prd/argocd-hooks/git", "token"): "github-pat",
        ("eso/prd/argocd/prd/git", "token"): "github-pat",
        ("eso/prd/fieldnotes/prd/store-token", "token"): "github-pat",
        ("eso/prd/git-sync/prd/github", "token"): "github-pat",
        ("eso/prd/kubecoder/dev/github-token", "token"): "github-pat",
        ("eso/prd/kubecoder/prd/github-token", "token"): "github-pat",
        ("iac/tf-backend", "github_token"): "github-pat",
        ("jenkins/sops-publish", "token"): "github-pat",
        ("rotator/github", "token"): "github-pat",
        ("eso/prd/jenkins-telegram-bot/prd/config", "telegram-bot-token"): "telegram-bot-token",
        ("eso/prd/kubecoder/dev/bot-token", "token"): "telegram-bot-token",
        ("eso/prd/kubecoder/prd/bot-token", "token"): "telegram-bot-token",
        ("eso/prd/newsfilter/prd/telegram", "bot-token"): "telegram-bot-token",
        ("eso/prd/prometheus/prd/telegram", "bot_token"): "telegram-bot-token",
        ("eso/prd/telegram-mcp/prd/telegram", "bot-token"): "telegram-bot-token",
        ("rotator/telegram", "token"): "telegram-bot-token",
        ("eso/prd/electronics-inventory/prd/openai", "api_key"): "openai-api-key",
        ("eso/prd/intercom/prd/openai", "api_key"): "openai-api-key",
        ("eso/prd/newsfilter/prd/openai", "api_key"): "openai-api-key",
        (CATALOG, "openai-api-key"): "openai-api-key",
        ("jenkins/openai-ci-cd", "api_key"): "openai-api-key",
        ("eso/prd/electronics-inventory/prd/mouser", "search_api_key"): "mouser-api-key",
        ("eso/prd/media/prd/mydownloads-tvdb", "tvdb-api-key"): "tvdb-api-key",
        ("eso/prd/media/prd/gluetun-wg", "config"): "torguard-wireguard",
        (CATALOG, "argocd-token"): "argocd-token",
        (CATALOG, "grafana-api-key"): "grafana-api-key",
        ("jenkins/grafana-api", "token"): "grafana-api-key",
        (CATALOG, "ssh-key-pve"): "ssh-private-key",
        ("eso/prd/trello-mcp/prd/trello", "api-key"): "trello-api-credential",
        ("eso/prd/trello-mcp/prd/trello", "token"): "trello-api-credential",
    }
    manual = {
        (leaf, key): fields
        for leaf, by_key in entries.items()
        for key, fields in by_key.items()
        if fields["kind"] == "manual"
    }
    scheduled = {at: f for at, f in manual.items() if f.get("interval") != "never"}
    assert {at: f.get("args", {}).get("type") for at, f in scheduled.items()} == want
    assert set(want.values()) == set(TYPES)
    assert [at for at, f in manual.items() if f.get("interval") == "never" and "args" in f] == []


def test_the_catalog_corrections(entries):
    assert (
        entries["eso/prd/argocd/prd/oidc"]["client_secret"]["activate"]
        == "k8s-rollout:argocd-prd/deployment/argocd-prd-server"
    )
    assert (
        entries["eso/prd/youtrack/prd/webhook-token"]["token"]["activate"]
        == "jenkins-job:YouTrack/YouTrackConfiguration?ROTATE_TOKEN=true"
    )
    for stage in ("prd", "dev"):
        bag = entries[f"eso/prd/kubecoder/{stage}/catalog"]
        activated = {
            f.get("activate") for f in bag.values() if f["kind"] not in ("none", "external")
        }
        assert activated == {f"k8s-rollout:kubecoder-{stage}/deployment/kubecoder-controller"}


def test_the_rotators_own_leaves_are_annotated(entries):
    approle = entries["iac/rotator-approle"]
    assert approle["secret_id"]["kind"] == "approle"
    assert approle["role_id"] == {"kind": "none"}
    assert approle["secret_id"]["args"] == {"role": "rotator", "delivery": "kv"}
    assert entries["iac/rotator-k8s-token"]["token"]["kind"] == "k8s-sa-token"
    assert entries["rotator/telegram"]["token"]["kind"] == "manual"
    assert entries["rotator/youtrack"]["token"]["kind"] == "youtrack-token"
    assert entries["rotator/youtrack-token/credentials"]["token"]["kind"] == "youtrack-token"
    assert entries["rotator/jenkins"]["token"]["kind"] == "jenkins-token"
    assert entries["rotator/jenkins"]["user"] == {"kind": "none"}
    for leaf in (
        "iac/rotator-approle",
        "iac/rotator-k8s-token",
        "rotator/telegram",
        "rotator/youtrack",
        "rotator/youtrack-token/credentials",
        "rotator/jenkins",
    ):
        scheduled = [f for f in entries[leaf].values() if is_scheduled(f["kind"])]
        assert [f["activate"] for f in scheduled] == ["none"], leaf


def test_the_markers_are_the_approles_and_the_bootstrap_tier(seed, store):
    want = {f"rotator/approle/{r}": "secret_id" for r in APPROLES}
    want |= {f"rotator/bootstrap/{b}": b for b in BOOTSTRAP}
    assert seed.markers == want
    for leaf, key in want.items():
        assert store[leaf] == [key], leaf


def test_every_approle_has_the_one_args_shape(entries):
    roles = {}
    for leaf, by_key in entries.items():
        for fields in by_key.values():
            if fields["kind"] == "approle":
                assert set(fields["args"]) == {"role", "delivery"}, leaf
                roles[fields["args"]["role"]] = fields["args"]["delivery"]
    assert roles == {
        "rotator": "kv",
        "eso": "k8s_secret=external-secrets-prd/openbao-eso-approle",
        "eso-dev": "k8s_secret=external-secrets/openbao-eso-approle",
        "jenkins": "jenkins_credential=724520d1-a0c1-4fa3-8a9e-a027de7f469a",
        "backup": "playbook",
        "iac-agent": "manual=Paste it as OPENBAO_SECRET_ID in srviac /etc/iac/secrets.yaml",
        "openbao-admin": "manual=Re-vault it as openbao_admin_secret_id in Ansible "
        "inventories/prd/group_vars/openbao.yml and commit",
    }
    assert entries["rotator/approle/eso-dev"]["secret_id"]["interval"] == "never"
    assert entries["rotator/approle/iac-agent"]["secret_id"]["interval"] == "90d"
    assert entries["rotator/approle/openbao-admin"]["secret_id"]["interval"] == "90d"
    assert (
        entries["rotator/approle/eso"]["secret_id"]["activate"]
        == "k8s-rollout:external-secrets-prd/deployment/external-secrets-prd"
    )
