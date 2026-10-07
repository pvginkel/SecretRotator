"""The OpenBao client: AppRole login through the VIP's listener port, the walk, key names without
values, metadata and its patch with max_versions, create, and refusals and transport failures it
names."""

import urllib.error

import pytest
from fake_openbao import ROLE_ID, SECRET_ID, TOKEN, FakeOpenBao, approle
from fixtures import COMPLIANT, data_of, fields_of

from secret_rotator.openbao import (
    ADDR,
    RELOGIN_MARGIN,
    Metadata,
    OpenBao,
    OpenBaoError,
    Version,
)


def fake():
    return FakeOpenBao(
        {path: {"data": data_of(path), "meta": dict(meta)} for path, (_, meta) in COMPLIANT.items()}
    )


def client(bao):
    return OpenBao(opener=bao, token=TOKEN)


def test_it_reaches_the_listener_port_through_the_vip():
    assert ADDR == "https://secrets.home:8200"


def test_the_login_sends_the_approle_in_the_body_and_uses_the_token_after():
    bao = fake()
    c = OpenBao(opener=bao)
    c.login_approle(ROLE_ID, SECRET_ID)
    c.metadata("shared/wifi")
    (m, path, query, body, _), (_, _, _, _, _) = bao.requests
    assert (m, path, query) == ("POST", "auth/approle/login", {})
    assert body == {"role_id": ROLE_ID, "secret_id": SECRET_ID}
    assert c.token == TOKEN


def test_a_refused_login_raises_with_its_status():
    with pytest.raises(OpenBaoError) as e:
        OpenBao(opener=fake()).login_approle(ROLE_ID, "SECRET-wrong")
    assert e.value.status == 400
    assert "invalid role or secret ID" in str(e.value)
    assert "SECRET" not in str(e.value)


def test_the_walk_lists_every_leaf_of_the_mount_but_the_working_leaves():
    bao = fake()
    bao.leaves["rotator/lock"] = {"data": {}, "meta": {}}
    bao.leaves["rotator/state"] = {"data": {}, "meta": {}}
    bao.leaves["rotator/staging/random/a/b"] = {"data": {}, "meta": {}}
    bao.leaves["rotator/approle/eso"] = {"data": {"secret_id": "m"}, "meta": {}}
    assert client(bao).leaves() == sorted([*COMPLIANT, "rotator/approle/eso"])
    assert client(bao).leaves("rotator/") == ["rotator/approle/eso"]


def test_the_walk_under_the_staging_leaves_lists_them():
    bao = fake()
    bao.leaves["rotator/staging/random/a/b"] = {"data": {}, "meta": {}}
    bao.leaves["rotator/staging/manual/c"] = {"data": {}, "meta": {}}
    assert client(bao).leaves("rotator/staging/") == [
        "rotator/staging/manual/c",
        "rotator/staging/random/a/b",
    ]


def test_subkeys_are_names_only_and_none_for_a_deleted_version_or_no_leaf():
    bao = fake()
    bao.leaves["shared/wifi"]["data"] = None
    c = client(bao)
    assert c.subkeys("eso/prd/app/prd/oidc") == {"client_id", "client_secret"}
    assert c.subkeys("shared/wifi") is None
    assert c.subkeys("no/such/leaf") is None
    assert all(p.startswith("kv/subkeys/") for _, p, *_ in bao.requests)


def test_metadata_is_the_custom_metadata_or_none_for_no_leaf():
    c = client(fake())
    assert fields_of(c.metadata("shared/wifi"), "password")["notes"] == "PSK in every device"
    assert c.metadata("no/such/leaf") is None


def test_patch_sends_a_merge_patch_of_custom_metadata():
    bao = fake()
    client(bao).patch_metadata("shared/wifi", {"notes": "n"})
    assert bao.requests == [
        (
            "PATCH",
            "kv/metadata/shared/wifi",
            {},
            {"custom_metadata": {"notes": "n"}},
            "application/merge-patch+json",
        )
    ]


def test_a_patch_removes_the_keys_it_sets_to_null_and_may_set_max_versions():
    bao = fake()
    c = client(bao)
    c.patch_metadata("shared/wifi", {"rotation_password": None, "notes": "n"}, max_versions=20)
    assert bao.requests[-1][3] == {
        "custom_metadata": {"rotation_password": None, "notes": "n"},
        "max_versions": 20,
    }
    assert c.leaf_metadata("shared/wifi") == Metadata({"notes": "n"}, 20)
    c.patch_metadata("shared/wifi", {}, max_versions=5)
    assert bao.requests[-1][3] == {"max_versions": 5}
    assert c.leaf_metadata("shared/wifi") == Metadata({"notes": "n"}, 5)


def test_leaf_metadata_is_the_custom_metadata_and_max_versions_or_none_for_no_leaf():
    c = client(fake())
    assert c.leaf_metadata("iac/copy") == Metadata(COMPLIANT["iac/copy"][1], 0)
    assert c.leaf_metadata("no/such/leaf") is None


def test_create_writes_the_first_version_only():
    bao = fake()
    c = client(bao)
    c.create("rotator/approle/eso", {"secret_id": "marker"})
    assert bao.requests[-1][3] == {"options": {"cas": 0}, "data": {"secret_id": "marker"}}
    with pytest.raises(OpenBaoError) as e:
        c.create("rotator/approle/eso", {"secret_id": "marker"})
    assert e.value.status == 400


def test_a_metadata_patch_of_a_leaf_that_is_gone_is_refused():
    with pytest.raises(OpenBaoError) as e:
        client(fake()).patch_metadata("no/such/leaf", {"notes": "n"})
    assert e.value.status == 404
    assert str(e.value) == "PATCH kv/metadata/no/such/leaf: HTTP 404: no leaf no/such/leaf"


def test_read_gives_a_version_s_number_and_data_or_none():
    bao = fake()
    c = client(bao)
    assert c.read("shared/wifi") == Version(1, data_of("shared/wifi"))
    c.write("shared/wifi", {"password": "SECRET-new"})
    assert c.read("shared/wifi") == Version(2, {"password": "SECRET-new"})
    assert c.read("shared/wifi", version=1) == Version(1, data_of("shared/wifi"))
    assert c.read("shared/wifi", version=5) is None
    assert c.read("no/such/leaf") is None
    assert bao.requests[-2][2] == {"version": "5"}


def test_write_puts_the_whole_data_with_an_optional_check_and_set():
    bao = fake()
    c = client(bao)
    assert c.write("shared/wifi", {"a": "1"}) == 2
    assert bao.requests[-1][3] == {"data": {"a": "1"}}
    assert c.write("shared/wifi", {"b": "2"}, cas=2) == 3
    assert bao.requests[-1][3] == {"data": {"b": "2"}, "options": {"cas": 2}}
    with pytest.raises(OpenBaoError, match="check-and-set") as e:
        c.write("shared/wifi", {"c": "3"}, cas=2)
    assert e.value.status == 400


def test_patch_merges_into_the_data_under_check_and_set():
    bao = fake()
    c = client(bao)
    assert c.patch("eso/prd/app/prd/oidc", {"client_secret": "SECRET-new"}, cas=1) == 2
    method, path, _, body, ctype = bao.requests[-1]
    assert (method, path, ctype) == (
        "PATCH",
        "kv/data/eso/prd/app/prd/oidc",
        "application/merge-patch+json",
    )
    assert body == {"data": {"client_secret": "SECRET-new"}, "options": {"cas": 1}}
    assert bao.data("eso/prd/app/prd/oidc") == data_of("eso/prd/app/prd/oidc") | {
        "client_secret": "SECRET-new"
    }
    with pytest.raises(OpenBaoError) as e:
        c.patch("eso/prd/app/prd/oidc", {"client_secret": "x"}, cas=1)
    assert e.value.status == 400
    with pytest.raises(OpenBaoError, match="HTTP 404: no leaf"):
        c.patch("no/such/leaf", {"k": "v"}, cas=1)


def test_destroy_deletes_the_metadata_with_every_version():
    bao = fake()
    client(bao).destroy("shared/wifi")
    assert bao.requests[-1][:2] == ("DELETE", "kv/metadata/shared/wifi")
    assert "shared/wifi" not in bao.leaves


def test_a_refusal_carries_its_status():
    bao = fake()
    bao.refuse["PATCH", "kv/metadata/shared/wifi"] = 403
    with pytest.raises(OpenBaoError) as e:
        client(bao).patch_metadata("shared/wifi", {"notes": "n"})
    assert e.value.status == 403
    assert (
        str(e.value)
        == "PATCH kv/metadata/shared/wifi: HTTP 403: 1 error occurred: * permission denied"
    )


@pytest.mark.parametrize(
    "error",
    [
        ConnectionResetError("connection reset by peer"),
        TimeoutError("timed out"),
        urllib.error.URLError("no route to host"),
    ],
)
def test_a_transport_failure_is_named_and_has_no_status(error):
    bao = fake()
    bao.broken["kv/metadata/shared/wifi"] = error
    with pytest.raises(OpenBaoError) as e:
        client(bao).metadata("shared/wifi")
    assert e.value.status is None
    assert str(e.value).startswith("GET kv/metadata/shared/wifi: transport error: ")


class TestLoggingInAgain:
    """The rotator's token lives 1 h from login and cannot renew itself: a client logs in again
    before its lease ends, from credentials read with the token still valid."""

    LEAF = "iac/rotator-approle"

    def logged_in(self, bao, now):
        bao.leaves[self.LEAF] = {"data": {"role_id": ROLE_ID, "secret_id": SECRET_ID}, "meta": {}}
        c = OpenBao(opener=bao, clock=lambda: now[0])
        c.login_approle(ROLE_ID, SECRET_ID)
        c.credential_leaf = self.LEAF
        return c

    def logins(self, bao):
        return [r for r in bao.requests if r[1] == "auth/approle/login"]

    def reads(self, bao):
        return [r for r in bao.requests if r[1] == f"kv/data/{self.LEAF}"]

    def test_within_its_lease_less_the_margin_it_does_not(self):
        bao, now = fake(), [0.0]
        c = self.logged_in(bao, now)
        now[0] = 3600 - RELOGIN_MARGIN - 1
        c.metadata("shared/wifi")
        assert len(self.logins(bao)) == 1 and self.reads(bao) == []

    def test_near_the_end_of_its_lease_it_logs_in_first_and_once(self):
        bao, now = fake(), [0.0]
        c = self.logged_in(bao, now)
        now[0] = 3600 - RELOGIN_MARGIN
        c.metadata("shared/wifi")
        c.metadata("shared/wifi")
        assert len(self.logins(bao)) == 2
        assert [r[:2] for r in bao.requests[-5:]] == [
            ("GET", f"kv/data/{self.LEAF}"),
            ("GET", f"kv/data/{self.LEAF}"),
            ("POST", "auth/approle/login"),
            ("GET", "kv/metadata/shared/wifi"),
            ("GET", "kv/metadata/shared/wifi"),
        ]
        assert c.expires == now[0] + 3600

    def test_past_the_end_of_its_lease_it_logs_in_with_the_credentials_in_hand(self):
        bao, now = fake(), [0.0]
        c = self.logged_in(bao, now)
        now[0] = 3600
        c.metadata("shared/wifi")
        assert len(self.logins(bao)) == 2 and self.reads(bao) == []

    def test_a_write_of_the_credential_leaf_puts_what_it_holds_in_hand(self):
        bao, now = fake(), [0.0]
        bao.approles = {"rotator": approle(ROLE_ID, **{SECRET_ID: "a-0", "SECRET-new": "a-1"})}
        c = self.logged_in(bao, now)
        c.patch(self.LEAF, {"secret_id": "SECRET-new"}, cas=1)
        now[0] = 3600
        c.metadata("shared/wifi")
        assert self.logins(bao)[-1][3] == {"role_id": ROLE_ID, "secret_id": "SECRET-new"}

    def test_a_client_without_credentials_never_does(self):
        bao, now = fake(), [0.0]
        c = OpenBao(opener=bao, clock=lambda: now[0])
        c.login_approle(ROLE_ID, SECRET_ID)
        now[0] = 7200
        c.metadata("shared/wifi")
        assert len(self.logins(bao)) == 1


def test_value_reads_one_key_and_refuses_a_missing_one():
    c = client(fake())
    assert c.value("shared/wifi", "password") == "SECRET-shared/wifi-password"
    with pytest.raises(OpenBaoError, match="shared/wifi#nope cannot be read") as e:
        c.value("shared/wifi", "nope")
    assert e.value.status == 404
