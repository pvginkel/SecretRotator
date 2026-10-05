"""The contract's vocabulary: kinds and the keys they own, activators, intervals, dates."""

import datetime

import pytest

from secret_rotator import contract as c

DESIGN_6_KINDS = {
    # first table
    "random",
    "approle",
    "keycloak-client",
    "cnpg-role",
    "jenkins-token",
    "youtrack-token",
    "github-webhook-secret",
    "elastic-user",
    "home-assistant-token",
    "google-sa-key",
    "terraform",
    "mosquitto-user",
    "samba-user",
    "manual",
    # second table
    "k8s-sa-token",
    "cephx",
    "rgw-admin",
    "grafana-admin",
    "jenkins-admin-password",
    "pve-root-password",
    "kubecoder-client",
    "jenkins-job-token",
    "step-ca-password",
    "ssh-key",
}


def test_every_kind_of_design_6_is_known_and_five_are_implemented():
    assert set(c.KINDS) == DESIGN_6_KINDS
    assert {k for k in c.KINDS if c.is_implemented(k)} == {"random", "approle", "manual"}
    assert c.is_implemented("none")
    assert c.is_implemented("copy:eso/prd/x#token")
    assert c.kind_error("keycloak") is not None


def test_the_kind_of_every_key_is_resolved_as_design_3_1_says():
    assert c.resolve({"rotation_mechanism": "keycloak-client"}, ["client_id", "client_secret"]) == {
        "client_id": "none",
        "client_secret": "keycloak-client",
    }
    assert c.resolve(
        {
            "rotation_mechanism": "jenkins-token",
            "key_telegram-bot-token": "manual",
            "key_telegram-chat-id": "none",
        },
        ["jenkins-token", "telegram-bot-token", "telegram-chat-id"],
    ) == {
        "jenkins-token": "jenkins-token",
        "telegram-bot-token": "manual",
        "telegram-chat-id": "none",
    }
    assert c.resolve({"rotation_mechanism": "copy:eso/prd/a#token"}, ["token"]) == {
        "token": "copy:eso/prd/a#token"
    }
    assert c.resolve(
        {"rotation_mechanism": "manual", "key_bearer-token": "random"},
        ["api-key", "bearer-token", "token"],
    ) == {"api-key": "manual", "bearer-token": "random", "token": "manual"}
    assert c.resolve({"rotation_mechanism": "none"}, ["a", "b"]) == {"a": "none", "b": "none"}


def test_a_one_key_kind_owns_the_single_unnamed_key_and_none_of_two():
    assert c.resolve(
        {"rotation_mechanism": "cephx", "key_user_id": "none"}, ["user_id", "user_key"]
    ) == {"user_id": "none", "user_key": "cephx"}
    assert c.resolve({"rotation_mechanism": "cephx"}, ["user_id", "user_key"]) == {
        "user_id": None,
        "user_key": None,
    }


def test_an_override_naming_the_leafs_one_key_kind_claims_it_alone():
    # A7: the single-unnamed-key fallback applies only while no override names the kind.
    meta = {"rotation_mechanism": "jenkins-token", "key_token": "jenkins-token"}
    assert c.resolve(meta, ["token", "user"]) == {"token": "jenkins-token", "user": None}
    meta = {"rotation_mechanism": "jenkins-token", "key_tokn": "jenkins-token"}
    assert c.resolve(meta, ["token"]) == {"token": None}


def test_a_key_the_kind_does_not_own_resolves_to_nothing():
    assert c.resolve({"rotation_mechanism": "elastic-user"}, ["password", "username"]) == {
        "password": "elastic-user",
        "username": None,
    }
    assert c.resolve({}, ["token"]) == {"token": None}
    assert c.resolve({"rotation_mechanism": "bogus"}, ["token"]) == {"token": None}


def test_scheduled_kinds_are_neither_copies_nor_none():
    assert c.is_scheduled("random") and c.is_scheduled("cephx")
    assert not c.is_scheduled("none")
    assert not c.is_scheduled("copy:eso/prd/a#token")
    assert not c.is_scheduled(None)
    assert c.copy_target("copy:eso/prd/a/b#client-id") == ("eso/prd/a/b", "client-id")
    assert c.copy_target("random") is None


def test_the_working_leaves_are_the_staging_leaves_and_the_lock():
    assert c.is_working_leaf("rotator/lock")
    assert c.is_working_leaf("rotator/staging/random/eso/prd/app/prd/token")
    assert not c.is_working_leaf("rotator/locks")
    assert not c.is_working_leaf("rotator/approle/eso")


def test_intervals_are_days_or_never():
    assert c.parse_interval("14d") == 14
    assert c.parse_interval("365d") == 365
    assert c.parse_interval("never") is None
    for value in ("14", "2w", "0d", "14 d", "Never", "014d"):
        with pytest.raises(c.ContractError):
            c.parse_interval(value)


def test_dates_are_iso_days():
    assert c.parse_date("2027-01-31") == datetime.date(2027, 1, 31)
    for value in ("2027-13-01", "20270131", "soon", "2027-01-31T00:00:00"):
        with pytest.raises(c.ContractError):
            c.parse_date(value)


def test_args_are_a_small_json_object():
    assert c.parse_args('{"role":"eso","delivery":"kv"}') == {"role": "eso", "delivery": "kv"}
    for value in ("{realm: homelab}", '["a"]', '{"x": "' + "y" * 520 + '"}'):
        with pytest.raises(c.ContractError):
            c.parse_args(value)


def test_activate_parses_into_specs():
    A = c.Activator
    assert c.parse_activate("auto") == [A("eso"), A("k8s-rollout")]
    assert c.parse_activate("none") == []
    assert c.parse_activate("k8s-rollout:ns/deployment/a,ns/daemonset/b,manual:say so") == [
        A("k8s-rollout", targets=("ns/deployment/a", "ns/daemonset/b")),
        A("manual", "say so"),
    ]
    assert c.parse_activate("eso,k8s-rollout") == [A("eso"), A("k8s-rollout")]
    assert c.parse_activate("jenkins-job:YouTrack/YouTrackConfiguration?ROTATE_TOKEN=true") == [
        A("jenkins-job", "YouTrack/YouTrackConfiguration?ROTATE_TOKEN=true")
    ]
    assert c.parse_activate("jenkins-credential:jenkins-vault-approle") == [
        A("jenkins-credential", "jenkins-vault-approle")
    ]


def test_activate_names_every_problem():
    with pytest.raises(c.ContractError) as e:
        c.parse_activate("restart,eso:x,auto")
    assert str(e.value) == (
        "unknown activator 'restart'; eso takes no argument; auto stands alone, not in a list"
    )
