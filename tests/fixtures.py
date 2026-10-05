"""A compliant store: each fixture leaf's data key names and the annotations that make it
compliant. Together they exercise each owned-keys rule, copies, per-key intervals, the activator
forms and a known kind not implemented yet."""

import copy

from secret_rotator.audit import Leaf
from secret_rotator.audit import audit as run_audit

COMPLIANT = {
    "eso/prd/app/prd/oidc": (
        ["client_id", "client_secret"],
        {
            "rotation_mechanism": "keycloak-client",
            "rotation_args": '{"realm":"homelab"}',
            "rotation_interval": "14d",
            "rotation_activate": "auto",
        },
    ),
    "eso/prd/app/prd/token": (
        ["token"],
        {"rotation_mechanism": "random", "rotation_interval": "14d", "rotation_activate": "auto"},
    ),
    "eso/prd/bot/prd/config": (
        ["jenkins-token", "telegram-bot-token", "telegram-chat-id"],
        {
            "rotation_mechanism": "jenkins-token",
            "key_telegram-bot-token": "manual",
            "key_telegram-chat-id": "none",
            "rotation_interval": "14d",
            "interval_telegram-bot-token": "365d",
            "rotation_activate": "auto",
        },
    ),
    "eso/prd/es/prd/creds": (
        ["password", "username"],
        {
            "rotation_mechanism": "elastic-user",
            "key_username": "none",
            "rotation_args": '{"user":"filebeat_writer"}',
            "rotation_interval": "14d",
            "rotation_activate": (
                "k8s-rollout:es-prd/deployment/a,es-prd/statefulset/b,manual:tell the operator"
            ),
        },
    ),
    "eso/prd/trello/prd/trello": (
        ["api-key", "bearer-token", "token"],
        {
            "rotation_mechanism": "manual",
            "key_bearer-token": "random",
            "rotation_interval": "never",
            "interval_bearer-token": "14d",
            "notes": "api-key and token cannot be rotated",
            "rotation_activate": "auto",
        },
    ),
    "eso/prd/kc/prd/catalog": (
        ["client-id", "client-secret", "jenkins-user"],
        {
            "rotation_mechanism": "manual",
            "key_client-id": "copy:eso/prd/app/prd/oidc#client_id",
            "key_client-secret": "copy:eso/prd/app/prd/oidc#client_secret",
            "key_jenkins-user": "none",
            "rotation_activate": "auto",
        },
    ),
    "eso/prd/yt/prd/webhook": (
        ["token"],
        {
            "rotation_mechanism": "random",
            "rotation_interval": "14d",
            "rotation_activate": "jenkins-job:YouTrack/YouTrackConfiguration?ROTATE_TOKEN=true",
        },
    ),
    "iac/copy": (
        ["token"],
        {"rotation_mechanism": "copy:eso/prd/app/prd/token#token", "rotation_activate": "none"},
    ),
    "jenkins/youtrack": (
        ["admin-token", "webhook-token"],
        {
            "rotation_mechanism": "youtrack-token",
            "key_webhook-token": "copy:eso/prd/yt/prd/webhook#token",
            "rotation_interval": "14d",
            "rotation_activate": "none",
            "rotation_expires_at": "2027-01-31",
        },
    ),
    "shared/ceph": (
        ["user_id", "user_key"],
        {
            "rotation_mechanism": "cephx",
            "key_user_id": "none",
            "rotation_interval": "365d",
            "rotation_activate": "none",
        },
    ),
    "shared/wifi": (
        ["password"],
        {
            "rotation_mechanism": "manual",
            "rotation_interval": "never",
            "notes": "PSK in every device",
            "rotation_activate": "none",
        },
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
