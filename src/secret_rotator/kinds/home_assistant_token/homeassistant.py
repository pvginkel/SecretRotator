"""Home Assistant's websocket API (2026.2, /api/websocket), as much of it as the
home-assistant-token kind uses: a connection logged in with an access token, and on it the
refresh tokens of the token's user, listed, a long-lived access token minted, and one deleted by
its id (homeassistant/components/auth/__init__.py). Each long-lived access token is one of its
user's refresh tokens, listed by id and client_name, never by value. No message or error carries
a token."""

import json
from collections.abc import Callable
from dataclasses import dataclass

from secret_rotator.kinds.home_assistant_token import websocket
from secret_rotator.kinds.home_assistant_token.websocket import WebSocketError
from secret_rotator.model import StepFailed

# Home Assistant's address, HomeassistantMcpDeploy config/prd/values.yaml's homeAssistant.url.
URL = "wss://homeassistant.webathome.org/api/websocket"
LONG_LIVED = "long_lived_access_token"  # a refresh token's type
# The code of a login Home Assistant refuses: its auth_invalid answer, which closes the connection.
LOGIN_REFUSED = "auth_invalid"
INVALID_ID = "invalid_token_id"  # the error of a delete by an id the user has no token by
# The error Home Assistant answers a command whose handler raised anything but the exceptions it
# maps to codes of their own (websocket_api/connection.py's async_handle_exception), whatever it did
# before. The three commands the kind sends raise only ValueError, which gets this code.
UNKNOWN = "unknown_error"


class HomeAssistantError(StepFailed):
    """A login or a command Home Assistant refused (code set: its error code, LOGIN_REFUSED for a
    login), or a transport failure (code None). A StepFailed, so a step reports it by its one
    sentence, which carries Home Assistant's own message and never a token."""

    def __init__(self, message: str, code: str | None = None):
        super().__init__(message)
        self.code = code

    @property
    def refused(self) -> bool:
        """Whether Home Assistant refused the command and it changed nothing: an error its
        handler answered, not UNKNOWN. That holds for the three commands the kind sends; a command
        whose handler may raise a mapped exception after acting needs its own rule."""
        return self.code is not None and self.code != UNKNOWN


@dataclass(frozen=True)
class Token:
    id: str
    name: str  # its client_name, unique among its user's long-lived access tokens
    type: str
    current: bool  # the one the connection logged in with


# What opens a websocket by its URL: send(text), recv() -> text, close().
Opener = Callable[[str], websocket.WebSocket]


class Session:
    """A connection logged in with a token, closed by its with block."""

    def __init__(self, socket: websocket.WebSocket):
        self.socket = socket
        self.last = 0  # the id of its last command; Home Assistant takes each id once, ascending

    def __enter__(self) -> "Session":
        return self

    def __exit__(self, *exc) -> None:
        self.socket.close()

    def command(self, type: str, **fields) -> object:
        """The result of the command; an error answered is raised with its code."""
        self.last += 1
        try:
            self.socket.send(json.dumps({"id": self.last, "type": type, **fields}))
            answer = json.loads(self.socket.recv())
        except (OSError, WebSocketError) as e:
            raise HomeAssistantError(f"{type}: transport error: {e!r}") from None
        except ValueError:
            raise HomeAssistantError(f"{type}: not a JSON answer") from None
        if not answer["success"]:
            error = answer["error"]
            raise HomeAssistantError(f"{type}: {error['code']}: {error['message']}", error["code"])
        return answer["result"]

    def tokens(self) -> list[Token]:
        """The refresh tokens of the connection's user."""
        return [
            Token(t["id"], t["client_name"] or "", t["type"], t["is_current"])
            for t in self.command("auth/refresh_tokens")
        ]

    def mint(self, name: str, days: int) -> str:
        """A new long-lived access token of the connection's user, named name, that expires in
        that many days: its value."""
        return self.command("auth/long_lived_access_token", client_name=name, lifespan=days)

    def delete(self, token: str) -> None:
        """Deletes the user's refresh token by its id. Deleting the connection's own closes it
        before its answer."""
        self.command("auth/delete_refresh_token", refresh_token_id=token)


def connect(token: str, opener: Opener | None = None) -> Session:
    """A connection to Home Assistant logged in with the access token."""
    try:
        socket = (opener or websocket.connect)(URL)
    except (OSError, WebSocketError) as e:
        raise HomeAssistantError(f"{URL}: transport error: {e!r}") from None
    try:
        socket.recv()  # auth_required
        socket.send(json.dumps({"type": "auth", "access_token": token}))
        answer = json.loads(socket.recv())
    except (OSError, WebSocketError, ValueError) as e:
        socket.close()
        raise HomeAssistantError(f"{URL}: transport error at the login: {e!r}") from None
    if answer["type"] != "auth_ok":
        socket.close()
        raise HomeAssistantError(f"{URL}: login refused: {answer['message']}", LOGIN_REFUSED)
    return Session(socket)
