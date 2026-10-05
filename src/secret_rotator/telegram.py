"""The Secret Rotator bot (design §3.6): messages into the Homelab Alerts group through Telegram's
Bot API, with the bot's token from rotator/telegram. Plain text, no parse mode, so nothing a path
or an error holds needs escaping. The token is part of every request's URL: no error names it."""

import functools
import http.client
import json
import ssl
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable

ADDR = "https://api.telegram.org"
TIMEOUT = 30
TOKEN = ("rotator/telegram", "token")  # the leaf and key of the bot's token
LIMIT = 4096  # the characters one message holds


class TelegramError(Exception):
    pass


def chunks(text: str) -> list[str]:
    """The text in messages of at most LIMIT characters, split between lines where one can be."""
    out: list[str] = []
    current = ""
    for line in text.split("\n"):
        while len(line) > LIMIT:
            if current:
                out.append(current)
                current = ""
            out.append(line[:LIMIT])
            line = line[LIMIT:]
        joined = f"{current}\n{line}" if current else line
        if len(joined) > LIMIT:
            out.append(current)
            joined = line
        current = joined
    return [*out, current] if current else out


class Telegram:
    def __init__(self, token: str, chat_id: int, opener: Callable | None = None):
        self.token = token
        self.chat_id = chat_id
        self.open = opener or functools.partial(
            urllib.request.urlopen, context=ssl.create_default_context(), timeout=TIMEOUT
        )

    def send(self, text: str) -> None:
        for chunk in chunks(text):
            self._post(chunk)

    def _post(self, text: str) -> None:
        body = {"chat_id": self.chat_id, "text": text, "disable_web_page_preview": True}
        req = urllib.request.Request(
            f"{ADDR}/bot{self.token}/sendMessage",
            method="POST",
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"},
        )
        try:
            with self.open(req) as resp:
                resp.read()
        except urllib.error.HTTPError as e:
            try:
                why = json.loads(e.read()).get("description", "")
            except ValueError:
                why = ""
            raise TelegramError(
                f"sendMessage: HTTP {e.code}" + (f": {why}" if why else "")
            ) from None
        except urllib.error.URLError as e:
            raise TelegramError(f"sendMessage: transport error: {e.reason}") from None
        except (OSError, http.client.HTTPException) as e:
            raise TelegramError(f"sendMessage: transport error: {type(e).__name__}") from None


def failed(plan: str, keys: Iterable[str], step: str, error: str, *, rollback: bool = False) -> str:
    """The message of one failure, of a plan or of its rollback (design R66)."""
    what = f"The rollback of the {plan}" if rollback else f"The {plan}"
    return f"{what} ({', '.join(keys)}) failed at {step}: {error}"
