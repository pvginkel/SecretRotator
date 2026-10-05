"""Telegram's Bot API over HTTP, as an opener for secret_rotator.telegram.Telegram: sendMessage."""

import io
import json
import urllib.error

from secret_rotator.telegram import ADDR

TOKEN = "SECRET-token-of-the-bot"
CHAT = -1001234567890  # the Homelab Alerts group


class FakeResponse(io.BytesIO):
    def __init__(self, status, body):
        super().__init__(body)
        self.status = status


class FakeTelegram:
    def __init__(self):
        self.messages = []
        self.down = False

    def __call__(self, req):
        assert req.full_url == f"{ADDR}/bot{TOKEN}/sendMessage", "not the bot's token"
        assert req.get_method() == "POST"
        body = json.loads(req.data)
        assert body["chat_id"] == CHAT and "parse_mode" not in body, body
        if self.down:
            doc = {"ok": False, "error_code": 502, "description": "Bad Gateway"}
            raise urllib.error.HTTPError(
                req.full_url, 502, "err", {}, io.BytesIO(json.dumps(doc).encode())
            )
        self.messages.append(body["text"])
        return FakeResponse(200, json.dumps({"ok": True, "result": {}}).encode())
