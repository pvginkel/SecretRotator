"""The per-key schedule of design §3.2: each scheduled key from its own stamp in the run state and
its entry's interval, brought forward by its entry's expires_at less the 7-day lead."""

import datetime

from secret_rotator.contract import Entry
from secret_rotator.schedule import schedule

D = datetime.date


def keys(entries, stamps=None):
    """Each key's schedule from its entry's fields, data key -> fields."""
    loaded = {key: Entry.load({"activate": "none", **fields}) for key, fields in entries.items()}
    return {s.key: s for s in schedule("leaf", loaded, stamps or {})}


def test_with_no_stamp_every_scheduled_key_is_due_now():
    got = keys(
        {
            "a": {"kind": "manual", "interval": "365d"},
            "b": {"kind": "random", "interval": "365d"},
            "c": {"kind": "none"},
            "d": {"kind": "copy:x#y"},
        }
    )
    assert set(got) == {"a", "b"}
    for s in got.values():
        assert s.rotated_at is None and s.due_at == D.min
        assert s.due(D(2026, 10, 5))


def test_a_stamped_key_is_due_at_its_stamp_plus_its_interval():
    s = keys({"token": {"kind": "random", "interval": "14d"}}, {"token": "2026-10-01"})["token"]
    assert (s.kind, s.interval, s.rotated_at, s.due_at) == (
        "random",
        14,
        D(2026, 10, 1),
        D(2026, 10, 15),
    )
    assert not s.due(D(2026, 10, 14))
    assert s.due(D(2026, 10, 15))


def test_each_key_keeps_its_own_stamp_and_interval():
    got = keys(
        {
            "api-key": {"kind": "manual", "interval": "365d"},
            "bearer-token": {"kind": "random", "interval": "14d"},
        },
        {"bearer-token": "2026-10-01", "api-key": "2026-01-01"},
    )
    assert got["bearer-token"].due_at == D(2026, 10, 15)
    assert got["api-key"].due_at == D(2027, 1, 1)


def test_a_key_whose_entry_gives_no_interval_follows_the_14_day_default():
    s = keys({"token": {"kind": "random"}}, {"token": "2026-10-01"})["token"]
    assert s.interval == 14 and s.due_at == D(2026, 10, 15)


def test_a_never_key_is_never_due_stamped_or_not():
    s = keys({"password": {"kind": "manual", "interval": "never", "notes": "n"}})["password"]
    assert s.interval is None and s.due_at is None
    assert not s.due(D(2100, 1, 1))


def test_a_key_whose_next_rotation_falls_past_its_expiry_less_the_lead_is_due_from_there():
    s = keys(
        {"token": {"kind": "manual", "interval": "365d", "expires_at": "2027-01-01"}},
        {"token": "2026-10-01"},
    )["token"]
    assert s.cap == D(2026, 12, 25)
    assert s.due_at == D(2026, 12, 25)
    assert not s.due(D(2026, 12, 24))
    assert s.due(D(2026, 12, 25)) and s.due(D(2027, 3, 1))


def test_an_expiry_further_out_than_the_next_rotation_changes_nothing():
    # An AppRole secret_id minted with four times its interval (design §6).
    s = keys(
        {"secret_id": {"kind": "approle", "interval": "14d", "expires_at": "2026-12-24"}},
        {"secret_id": "2026-10-01"},
    )["secret_id"]
    assert s.cap == D(2026, 12, 17)
    assert s.due_at == D(2026, 10, 15)


def test_a_key_s_expiry_brings_that_key_forward_alone():
    got = keys(
        {
            "a": {"kind": "random", "interval": "365d", "expires_at": "2027-02-01"},
            "b": {"kind": "random", "interval": "365d"},
        },
        {"a": "2026-10-01", "b": "2026-10-01"},
    )
    assert got["a"].due_at == D(2027, 1, 25)
    assert got["b"].cap is None and got["b"].due_at == D(2027, 10, 1)


def test_a_never_key_with_an_expiry_is_due_the_lead_before_it():
    s = keys(
        {"token": {"kind": "manual", "interval": "never", "notes": "n", "expires_at": "2027-01-31"}}
    )["token"]
    assert s.due_at == D(2027, 1, 24)
