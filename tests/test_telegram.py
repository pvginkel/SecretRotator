"""The Telegram client: plain text to the chat, a long message split between lines, and a refusal
that never names the token its URL holds."""

import pytest
from fake_telegram import CHAT, TOKEN, FakeTelegram

from secret_rotator.telegram import LIMIT, Telegram, TelegramError, chunks, failed


def test_a_message_goes_to_the_chat_as_plain_text():
    fake = FakeTelegram()
    Telegram(TOKEN, CHAT, opener=fake).send("Manual rotation of token at `eso/x` is due")
    assert fake.messages == ["Manual rotation of token at `eso/x` is due"]


def test_a_long_message_is_split_between_lines():
    lines = [f"{n:04d} " + "x" * 95 for n in range(100)]
    parts = chunks("\n".join(lines))
    assert len(parts) == 3 and all(len(p) <= LIMIT for p in parts)
    assert "\n".join(parts).split("\n") == lines


def test_a_line_longer_than_a_message_is_cut():
    parts = chunks("a\n" + "y" * (LIMIT + 10))
    assert parts == ["a", "y" * LIMIT, "y" * 10]


def test_a_refusal_names_its_status_and_reason_and_never_the_token():
    fake = FakeTelegram()
    fake.down = True
    with pytest.raises(TelegramError) as e:
        Telegram(TOKEN, CHAT, opener=fake).send("x")
    assert str(e.value) == "sendMessage: HTTP 502: Bad Gateway"
    assert TOKEN not in repr(e.value) and e.value.__cause__ is None


def test_a_failure_names_the_plan_its_keys_the_step_and_the_error():
    assert failed("random plan of eso/x", ("a", "b"), "write eso/x", "HTTP 403") == (
        "The random plan of eso/x (a, b) failed at write eso/x: HTTP 403"
    )
    assert failed("random plan of eso/x", ("a",), "undo: write eso/x", "e", rollback=True) == (
        "The rollback of the random plan of eso/x (a) failed at undo: write eso/x: e"
    )
