"""The contract's vocabulary: one rotation_<key> entry per data key and its fields, kinds and the
keys they may rotate, activators, intervals, dates."""

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


def test_every_kind_of_design_6_is_known():
    assert set(c.KINDS) == DESIGN_6_KINDS
    assert c.kind_error("keycloak") is not None


@pytest.mark.parametrize(
    "spec",
    [
        "eso",
        "k8s-rollout",
        "k8s-rollout:a/deployment/b,a/statefulset/c",
        "manual:set it then restart",
        "jenkins-job:YouTrack/YouTrackConfiguration?ROTATE_TOKEN=true",
    ],
)
def test_an_activator_reads_as_an_activate_writes_it(spec):
    (activator,) = c.parse_activate(spec)
    assert str(activator) == spec


def test_a_kind_that_names_its_keys_may_rotate_those_alone_any_other_any_key():
    assert c.may_rotate("keycloak-client", "client_secret")
    assert not c.may_rotate("keycloak-client", "client_id")
    assert not c.may_rotate("elastic-user", "username")
    assert c.may_rotate("cephx", "user_key") and c.may_rotate("manual", "anything")
    assert c.may_rotate("none", "username") and c.may_rotate("copy:eso/prd/a#token", "token")


def test_a_key_s_entry_is_rotation_and_its_name_verbatim():
    assert c.entry_name("telegram-bot-token") == "rotation_telegram-bot-token"
    meta = {"rotation_token": "{}", "rotation_a,b/c": "{}", "rotation": "coordinated", "notes": "n"}
    assert c.entries_of(meta) == {"token": "{}", "a,b/c": "{}"}


def test_an_entry_is_a_json_object_written_compact_in_the_contract_s_order():
    text = c.dump_entry(
        {"notes": "Gr\u00fc\u00dfe — n", "activate": "auto", "kind": "random", "interval": "14d"}
    )
    assert text == '{"kind":"random","interval":"14d","activate":"auto","notes":"Grüße — n"}'
    assert c.load_entry(text)["notes"] == "Grüße — n"
    for value, problem in (("{kind: random}", "not JSON"), ('["random"]', "not a JSON object")):
        with pytest.raises(c.ContractError, match=problem):
            c.load_entry(value)


def test_a_none_key_takes_its_kind_and_notes_a_copy_its_activate_too_a_scheduled_key_all():
    assert c.takes("none") == ("kind", "notes")
    assert c.takes("copy:eso/prd/a#token") == ("kind", "activate", "notes")
    assert (
        c.takes("random")
        == c.FIELDS
        == (
            "kind",
            "interval",
            "args",
            "activate",
            "expires_at",
            "notes",
        )
    )


def test_a_compliant_entry_has_no_problem():
    assert c.entry_problems({"kind": "none"}) == []
    assert (
        c.entry_problems({"kind": "copy:eso/prd/a#token", "activate": "auto", "notes": "n"}) == []
    )
    assert (
        c.entry_problems(
            {
                "kind": "approle",
                "interval": "90d",
                "args": {"role": "eso"},
                "activate": "k8s-rollout:ns/deployment/a,manual:say so",
                "expires_at": "2027-01-31",
                "notes": "n",
            }
        )
        == []
    )
    assert (
        c.entry_problems({"kind": "manual", "interval": "never", "activate": "none", "notes": "n"})
        == []
    )


@pytest.mark.parametrize(
    ("fields", "problems"),
    [
        ({}, ["kind: missing"]),
        ({"kind": 3}, ["kind: not a string"]),
        (
            {"kind": "keycloak", "activate": "auto"},
            ["kind: unknown kind 'keycloak': not a kind of design §6, none, or copy:<path>#<key>"],
        ),
        ({"kind": "random"}, ["activate: missing"]),
        ({"kind": "copy:eso/prd/a#token"}, ["activate: missing"]),
        (
            {"kind": "none", "activate": "auto", "interval": "14d"},
            ["interval: a none key takes none", "activate: a none key takes none"],
        ),
        (
            {
                "kind": "copy:eso/prd/a#token",
                "activate": "none",
                "args": {},
                "expires_at": "2027-01-31",
            },
            ["args: a copy takes none", "expires_at: a copy takes none"],
        ),
        (
            {"kind": "random", "activate": "auto", "mechanism": "random"},
            ["mechanism: not a field of the entry"],
        ),
        (
            {"kind": "random", "activate": "auto", "interval": "soon"},
            ["interval: 'soon' is not <n>d or never"],
        ),
        ({"kind": "random", "activate": "auto", "interval": 14}, ["interval: not a string"]),
        (
            {"kind": "manual", "activate": "none", "interval": "never"},
            ["interval: never without notes"],
        ),
        (
            {"kind": "manual", "activate": "none", "interval": "never", "notes": " "},
            ["interval: never without notes"],
        ),
        (
            {"kind": "random", "activate": "auto", "args": '{"length":20}'},
            ["args: not a JSON object"],
        ),
        (
            {"kind": "random", "activate": "restart,eso:x"},
            ["activate: unknown activator 'restart'", "activate: eso takes no argument"],
        ),
        (
            {"kind": "random", "activate": "auto", "expires_at": "2027-13-01"},
            ["expires_at: '2027-13-01' is not an ISO date (YYYY-MM-DD)"],
        ),
        ({"kind": "random", "activate": "auto", "notes": ["n"]}, ["notes: not a string"]),
    ],
)
def test_an_entry_s_problems_each_name_their_field(fields, problems):
    assert c.entry_problems(fields) == problems


def test_an_entry_reads_with_its_defaults():
    entry = c.Entry.load({"kind": "random", "activate": "auto"})
    assert entry == c.Entry(
        "random", 14, {}, (c.Activator("eso"), c.Activator("k8s-rollout")), None, ""
    )
    entry = c.Entry.load(
        {
            "kind": "manual",
            "interval": "never",
            "args": {"what": "PSK"},
            "activate": "none",
            "expires_at": "2027-01-31",
            "notes": "n",
        }
    )
    assert entry == c.Entry("manual", None, {"what": "PSK"}, (), datetime.date(2027, 1, 31), "n")
    assert c.Entry.load({"kind": "none"}).activate == ()


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
    assert c.parse_activate("jenkins-credential:724520d1-a0c1-4fa3-8a9e-a027de7f469a") == [
        A("jenkins-credential", "724520d1-a0c1-4fa3-8a9e-a027de7f469a")
    ]


def test_activate_names_every_problem():
    with pytest.raises(c.ContractError) as e:
        c.parse_activate("restart,eso:x,auto")
    assert str(e.value) == (
        "unknown activator 'restart'; eso takes no argument; auto stands alone, not in a list"
    )
