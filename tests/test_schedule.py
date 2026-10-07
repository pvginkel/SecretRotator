"""The per-key schedule of design §3.2: each scheduled key from its own stamp in the run state and
its interval, capped by the leaf's expiries less the 7-day lead."""

import datetime

from secret_rotator.contract import resolve
from secret_rotator.schedule import schedule

D = datetime.date


def keys(meta, names, stamps=None):
    return {s.key: s for s in schedule("leaf", meta, resolve(meta, names), stamps or {})}


def test_with_no_stamp_every_scheduled_key_is_due_now():
    meta = {
        "rotation_mechanism": "manual",
        "key_b": "random",
        "key_c": "none",
        "key_d": "copy:x#y",
        "rotation_interval": "365d",
    }
    got = keys(meta, ["a", "b", "c", "d"])
    assert set(got) == {"a", "b"}
    for s in got.values():
        assert s.rotated_at is None and s.due_at == D.min
        assert s.due(D(2026, 10, 5))


def test_a_stamped_key_is_due_at_its_stamp_plus_its_interval():
    meta = {
        "rotation_mechanism": "random",
        "rotation_interval": "14d",
    }
    s = keys(meta, ["token"], {"token": "2026-10-01"})["token"]
    assert (s.kind, s.interval, s.rotated_at, s.due_at) == (
        "random",
        14,
        D(2026, 10, 1),
        D(2026, 10, 15),
    )
    assert not s.due(D(2026, 10, 14))
    assert s.due(D(2026, 10, 15))


def test_each_key_keeps_its_own_stamp_and_interval():
    meta = {
        "rotation_mechanism": "manual",
        "key_bearer-token": "random",
        "rotation_interval": "365d",
        "interval_bearer-token": "14d",
        "rotated_at": "2020-01-01",
    }
    got = keys(
        meta, ["api-key", "bearer-token"], {"bearer-token": "2026-10-01", "api-key": "2026-01-01"}
    )
    assert got["bearer-token"].due_at == D(2026, 10, 15)
    assert got["api-key"].due_at == D(2027, 1, 1)


def test_a_stamp_on_the_leaf_s_metadata_is_not_read():
    meta = {"rotation_mechanism": "random", "rotated_at_token": "2026-10-01"}
    assert keys(meta, ["token"])["token"].due_at == D.min


def test_a_leaf_without_an_interval_follows_the_14_day_default():
    s = keys({"rotation_mechanism": "random"}, ["token"], {"token": "2026-10-01"})["token"]
    assert s.interval == 14 and s.due_at == D(2026, 10, 15)


def test_a_never_key_is_never_due_stamped_or_not():
    meta = {"rotation_mechanism": "manual", "rotation_interval": "never", "notes": "n"}
    s = keys(meta, ["password"])["password"]
    assert s.interval is None and s.due_at is None
    assert not s.due(D(2100, 1, 1))


def test_a_key_whose_next_rotation_falls_past_the_expiry_less_the_lead_is_due_from_there():
    meta = {
        "rotation_mechanism": "manual",
        "rotation_interval": "365d",
        "rotation_expires_at": "2027-01-01",
    }
    s = keys(meta, ["token"], {"token": "2026-10-01"})["token"]
    assert s.cap == D(2026, 12, 25)
    assert s.due_at == D(2026, 12, 25)
    assert not s.due(D(2026, 12, 24))
    assert s.due(D(2026, 12, 25)) and s.due(D(2027, 3, 1))


def test_an_expiry_further_out_than_the_next_rotation_changes_nothing():
    # An AppRole secret_id minted with twice its interval (design §6).
    meta = {
        "rotation_mechanism": "approle",
        "rotation_interval": "14d",
        "rotator_expires_at": "2026-10-29",
    }
    s = keys(meta, ["secret_id"], {"secret_id": "2026-10-01"})["secret_id"]
    assert s.cap == D(2026, 10, 22)
    assert s.due_at == D(2026, 10, 15)


def test_the_earliest_of_both_expiries_caps_every_scheduled_key():
    meta = {
        "rotation_mechanism": "random",
        "rotation_interval": "365d",
        "rotation_expires_at": "2027-03-01",
        "rotator_expires_at": "2027-02-01",
    }
    got = keys(meta, ["a", "b"], {"a": "2026-10-01", "b": "2026-10-01"})
    assert got["a"].due_at == got["b"].due_at == D(2027, 1, 25)


def test_a_never_key_with_an_expiry_is_due_the_lead_before_it():
    meta = {
        "rotation_mechanism": "manual",
        "rotation_interval": "never",
        "notes": "n",
        "rotation_expires_at": "2027-01-31",
    }
    assert keys(meta, ["token"])["token"].due_at == D(2027, 1, 24)
