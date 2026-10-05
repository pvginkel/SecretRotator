"""The committed switches: the copy inside the package, and what a switches file may hold."""

import pytest
import yaml

from secret_rotator import switches as sw

VALID = {
    "dry_run": True,
    "paused": False,
    "kinds_enabled": ["random"],
    "max_rotations_per_run": 10,
    "card_tag": "Secret Rotator",
    "telegram_chat_id": -1001234567890,
}


def parse(**changes):
    doc = {**VALID, **changes}
    return sw.parse(yaml.safe_dump({k: v for k, v in doc.items() if v is not ...}))


def test_the_packaged_switches_ship_inside_the_package_and_start_in_dry_run():
    assert sw.SWITCHES.is_file()
    assert sw.load() == sw.Switches(
        dry_run=True,
        paused=False,
        kinds_enabled=frozenset({"random"}),
        max_rotations_per_run=10,
        card_tag="Secret Rotator",
        telegram_chat_id=None,
    )


def test_a_valid_file_parses():
    assert parse().telegram_chat_id == -1001234567890
    assert parse(kinds_enabled=["random", "approle", "manual"]).kinds_enabled == {
        "random",
        "approle",
        "manual",
    }
    assert parse(kinds_enabled=[]).kinds_enabled == frozenset()


@pytest.mark.parametrize(
    ("changes", "problem"),
    [
        ({"dry_run": "yes"}, "dry_run: not true or false"),
        ({"paused": 0}, "paused: not true or false"),
        ({"kinds_enabled": "random"}, "kinds_enabled: not a list of kind names"),
        (
            {"kinds_enabled": ["keycloak-client"]},
            "kinds_enabled: 'keycloak-client' is not an implemented kind",
        ),
        ({"kinds_enabled": ["none"]}, "kinds_enabled: 'none' is not an implemented kind"),
        ({"max_rotations_per_run": 0}, "max_rotations_per_run: not a whole number from 1"),
        ({"max_rotations_per_run": True}, "max_rotations_per_run: not a whole number from 1"),
        ({"card_tag": " "}, "card_tag: not a tag name"),
        ({"telegram_chat_id": "-100"}, "telegram_chat_id: not a chat id or null"),
        ({"dry_run": ...}, "dry_run: missing"),
        ({"dry": True}, "dry: not a switch"),
    ],
)
def test_every_problem_is_named(changes, problem):
    with pytest.raises(sw.SwitchesError) as e:
        parse(**changes)
    assert str(e.value) == problem


def test_a_file_that_is_not_a_mapping():
    with pytest.raises(sw.SwitchesError, match="not a mapping of switches"):
        sw.parse("- dry_run\n")
