"""The packaged seed over the packaged key-name inventory: every leaf the store holds after the
go-live resolves, and the seed says what AnsibleSpecs secret-rotation/catalog.md says.

store-keys.json maps each leaf to its data key names (no values): the value-blind inventory of
2026-10-04 after slice 044's cutover, plus the rotator's own leaves and markers (catalog
§ rotator/). A leaf added to the seed is added there with its key names."""

import datetime
import json
import tempfile
from pathlib import Path

import pytest

from secret_rotator import annotate as ann
from secret_rotator import audit as aud
from secret_rotator import cli
from secret_rotator.contract import parse_args

APPROLES = ("backup", "eso", "eso-dev", "iac-agent", "jenkins", "openbao-admin")
BOOTSTRAP = (
    "age-private-key",
    "ansible-vault-passphrase",
    "git-api-token",
    "jenkins-agent-secret",
    "jwk-provisioner-password",
    "seal-key",
)


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


def test_every_key_of_every_leaf_resolves(store):
    code, lines = audit_offline(Path(str(ann.DEFAULT_KEYS)))
    assert lines[-1].startswith(
        f"0 finding(s) on 0 of {len(store)} leaf(s); blocked: 0 leaf(s), 0 key(s);"
    ), lines
    assert code == 0


def test_the_seed_covers_exactly_the_stores_leaves(seed, store):
    assert sorted(seed.annotations) == sorted(store)


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


def test_the_per_key_cadences_of_ruling_q1(seed):
    want = {
        "eso/prd/trello-mcp/prd/trello": {
            "rotation_mechanism": "manual",
            "rotation_interval": "never",
            "key_bearer-token": "random",
            "interval_bearer-token": "14d",
        },
        "iac/tf-backend": {
            "rotation_mechanism": "manual",
            "rotation_interval": "never",
            "interval_github_token": "365d",
        },
        "shared/samba/users": {
            "rotation_mechanism": "samba-user",
            "key_mvdbovenkamp": "manual",
            "rotation_interval": "365d",
            "interval_mvdbovenkamp": "never",
        },
    }
    for leaf, keys in want.items():
        meta = seed.annotations[leaf]
        assert {k: meta.get(k) for k in keys} == keys, leaf
        assert meta.get("notes", "").strip(), leaf
    assert "cannot be rotated" in seed.annotations["eso/prd/trello-mcp/prd/trello"]["notes"]


def test_the_bags_vault_passphrase_is_the_bootstrap_tier_and_never_due(seed):
    bag = "eso/prd/kubecoder/prd/catalog"
    meta = seed.annotations[bag]
    assert meta["interval_ansible-vault-password"] == "never"
    assert "bootstrap tier" in meta["notes"]
    store = ann.offline_store(Path(str(ann.DEFAULT_KEYS)), seed, print)
    result = aud.audit(store)
    assert result.findings == []
    assert (bag, "ansible-vault-password") in result.never
    due = [s for s in aud.due_keys(store, result, datetime.date(2026, 10, 6)) if s.leaf == bag]
    assert "ansible-vault-password" not in {s.key for s in due}
    assert {s.key for s in due if s.kind == "manual"} == {
        "argocd-token",
        "grafana-api-key",
        "openai-api-key",
        "ssh-key-pve",
    }


def test_the_leaves_once_manual_carry_the_kind_the_catalog_gives(seed):
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
        "shared/jenkins/admin-password": "jenkins-admin-password",
        "shared/prd/ceph-csi": "cephx",
        "shared/prd/ceph-rgw/s3": "rgw-admin",
        "shared/samba/users": "samba-user",
    }
    assert {leaf: seed.annotations[leaf]["rotation_mechanism"] for leaf in want} == want
    catalog = seed.annotations["eso/prd/kubecoder/prd/catalog"]
    for key in ("kubeconfig", "kubeconfig-dev-write", "kubeconfig-prd-write"):
        assert catalog[f"key_{key}"] == "k8s-sa-token"


def test_the_catalog_corrections(seed):
    a = seed.annotations
    assert (
        a["eso/prd/argocd/prd/oidc"]["rotation_activate"]
        == "k8s-rollout:argocd-prd/deployment/argocd-prd-server"
    )
    assert (
        a["eso/prd/youtrack/prd/webhook-token"]["rotation_activate"]
        == "jenkins-job:YouTrack/YouTrackConfiguration?ROTATE_TOKEN=true"
    )
    for stage in ("prd", "dev"):
        assert (
            a[f"eso/prd/kubecoder/{stage}/catalog"]["rotation_activate"]
            == f"k8s-rollout:kubecoder-{stage}/deployment/kubecoder-controller"
        )


def test_the_rotators_own_leaves_are_annotated(seed):
    a = seed.annotations
    assert a["iac/rotator-approle"]["rotation_mechanism"] == "approle"
    assert a["iac/rotator-approle"]["key_role_id"] == "none"
    assert parse_args(a["iac/rotator-approle"]["rotation_args"]) == {
        "role": "rotator",
        "delivery": "kv",
    }
    assert a["iac/rotator-k8s-token"]["rotation_mechanism"] == "k8s-sa-token"
    assert a["rotator/telegram"]["rotation_mechanism"] == "manual"
    assert a["rotator/youtrack"]["rotation_mechanism"] == "youtrack-token"
    assert a["rotator/jenkins"]["rotation_mechanism"] == "jenkins-token"
    assert a["rotator/jenkins"]["key_user"] == "none"
    for leaf in (
        "iac/rotator-approle",
        "iac/rotator-k8s-token",
        "rotator/telegram",
        "rotator/youtrack",
        "rotator/jenkins",
    ):
        assert a[leaf]["rotation_activate"] == "none", leaf


def test_the_markers_are_the_approles_and_the_bootstrap_tier(seed, store):
    want = {f"rotator/approle/{r}": "secret_id" for r in APPROLES}
    want |= {f"rotator/bootstrap/{b}": b for b in BOOTSTRAP}
    assert seed.markers == want
    for leaf, key in want.items():
        assert store[leaf] == [key], leaf


def test_every_approle_has_the_one_args_shape(seed):
    roles = {}
    for leaf, meta in seed.annotations.items():
        if meta.get("rotation_mechanism") == "approle":
            args = parse_args(meta["rotation_args"])
            assert set(args) == {"role", "delivery"}, leaf
            roles[args["role"]] = args["delivery"]
    assert roles == {
        "rotator": "kv",
        "eso": "k8s_secret=external-secrets-prd/openbao-eso-approle",
        "eso-dev": "k8s_secret=external-secrets/openbao-eso-approle",
        "jenkins": "jenkins_credential=jenkins-vault-approle",
        "backup": "playbook",
        "iac-agent": "manual=Paste it as OPENBAO_SECRET_ID in srviac /etc/iac/secrets.yaml",
        "openbao-admin": "manual=Re-vault it as openbao_admin_secret_id in Ansible "
        "inventories/prd/group_vars/openbao.yml and commit",
    }
    a = seed.annotations
    assert a["rotator/approle/eso-dev"]["rotation_interval"] == "never"
    assert a["rotator/approle/iac-agent"]["rotation_interval"] == "90d"
    assert a["rotator/approle/openbao-admin"]["rotation_interval"] == "90d"
    assert (
        a["rotator/approle/eso"]["rotation_activate"]
        == "k8s-rollout:external-secrets-prd/deployment/external-secrets-prd"
    )
