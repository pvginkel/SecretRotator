"""A compliant store: each fixture leaf's data key names and the entries that make it compliant.
Together they exercise each kind's keys, copies, per-key intervals, the activator forms, an
expiry and a known kind not implemented yet."""

import copy

from secret_rotator.audit import Leaf
from secret_rotator.audit import audit as run_audit
from secret_rotator.contract import dump_entry, entries_of, entry_name, load_entry, takes


def annotated(entries):
    """A leaf's metadata holding these entries, data key -> its fields."""
    return {entry_name(key): dump_entry(fields) for key, fields in entries.items()}


def fields_of(meta, key):
    """The fields of the key's entry in the metadata."""
    return load_entry(meta[entry_name(key)])


def edit(meta, key, **fields):
    """Sets fields of the key's entry in the metadata, creating it; a field set to None goes."""
    name = entry_name(key)
    current = load_entry(meta[name]) if name in meta else {}
    current.update(fields)
    meta[name] = dump_entry({k: v for k, v in current.items() if v is not None})


def set_activate(meta, value):
    """Sets the activate of every entry of the metadata that takes one: a scheduled key's, a
    copy's."""
    for key, fields in {k: load_entry(v) for k, v in entries_of(meta).items()}.items():
        if "activate" in takes(fields["kind"]):
            edit(meta, key, activate=value)


AUTO = {"interval": "14d", "activate": "auto"}

COMPLIANT = {
    "eso/prd/app/prd/oidc": (
        ["client_id", "client_secret"],
        annotated(
            {
                "client_id": {"kind": "none"},
                "client_secret": {
                    "kind": "keycloak-client",
                    "args": {"realm": "homelab"},
                    **AUTO,
                },
            }
        ),
    ),
    "eso/prd/app/prd/token": (["token"], annotated({"token": {"kind": "random", **AUTO}})),
    "eso/prd/bot/prd/config": (
        ["jenkins-token", "telegram-bot-token", "telegram-chat-id"],
        annotated(
            {
                "jenkins-token": {"kind": "jenkins-token", **AUTO},
                "telegram-bot-token": {"kind": "manual", "interval": "365d", "activate": "auto"},
                "telegram-chat-id": {"kind": "none"},
            }
        ),
    ),
    "eso/prd/es/prd/creds": (
        ["password", "username"],
        annotated(
            {
                "password": {
                    "kind": "elastic-user",
                    "args": {"user": "filebeat_writer"},
                    "interval": "14d",
                    "activate": (
                        "k8s-rollout:es-prd/deployment/a,es-prd/statefulset/b,manual:tell the "
                        "operator"
                    ),
                },
                "username": {"kind": "none"},
            }
        ),
    ),
    "eso/prd/trello/prd/trello": (
        ["api-key", "bearer-token", "token"],
        annotated(
            {
                "api-key": {
                    "kind": "manual",
                    "interval": "never",
                    "activate": "auto",
                    "notes": "cannot be rotated",
                },
                "bearer-token": {"kind": "random", **AUTO},
                "token": {
                    "kind": "manual",
                    "interval": "never",
                    "activate": "auto",
                    "notes": "cannot be rotated",
                },
            }
        ),
    ),
    "eso/prd/kc/prd/catalog": (
        ["client-id", "client-secret", "jenkins-user"],
        annotated(
            {
                "client-id": {"kind": "copy:eso/prd/app/prd/oidc#client_id", "activate": "auto"},
                "client-secret": {
                    "kind": "copy:eso/prd/app/prd/oidc#client_secret",
                    "activate": "auto",
                },
                "jenkins-user": {"kind": "none"},
            }
        ),
    ),
    "eso/prd/yt/prd/webhook": (
        ["token"],
        annotated(
            {
                "token": {
                    "kind": "random",
                    "interval": "14d",
                    "activate": "jenkins-job:YouTrack/YouTrackConfiguration?ROTATE_TOKEN=true",
                }
            }
        ),
    ),
    "iac/copy": (
        ["token"],
        annotated({"token": {"kind": "copy:eso/prd/app/prd/token#token", "activate": "none"}}),
    ),
    "jenkins/youtrack": (
        ["admin-token", "webhook-token"],
        annotated(
            {
                "admin-token": {
                    "kind": "youtrack-token",
                    "interval": "14d",
                    "activate": "none",
                    "expires_at": "2027-01-31",
                },
                "webhook-token": {"kind": "copy:eso/prd/yt/prd/webhook#token", "activate": "none"},
            }
        ),
    ),
    "shared/ceph": (
        ["user_id", "user_key"],
        annotated(
            {
                "user_id": {"kind": "none"},
                "user_key": {"kind": "cephx", "interval": "365d", "activate": "none"},
            }
        ),
    ),
    "shared/wifi": (
        ["password"],
        annotated(
            {
                "password": {
                    "kind": "manual",
                    "interval": "never",
                    "activate": "none",
                    "notes": "PSK in every device",
                }
            }
        ),
    ),
}

# Keys the contract leaves to others; the audit ignores them and the apply keeps them.
FOREIGN = {"rotation": "coordinated", "rotated_at": "2026-01-01", "rotator_status": "ok"}


def data_of(path):
    return {key: f"SECRET-{path}-{key}" for key in COMPLIANT[path][0]}


def compliant_store():
    return {
        path: Leaf(path, set(keys), copy.deepcopy(meta)) for path, (keys, meta) in COMPLIANT.items()
    }


def found(store):
    return [(f.leaf, f.key) for f in run_audit(store).findings]


def messages(store):
    return [str(f) for f in run_audit(store).findings]
