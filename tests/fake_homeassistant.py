"""Home Assistant 2026.2.3's websocket API, as an opener for the home-assistant-token kind: at the
URL the kind reaches it at and nowhere else, one connection per open, which asks for a login with
an access token first and closes after a refused one. Its auth commands are Home Assistant's own
(homeassistant/components/auth/__init__.py:523-613, homeassistant/auth/__init__.py:498-506):
auth/refresh_tokens lists the connection's user's refresh tokens, the connection's own marked
current; auth/long_lived_access_token mints one, and answers a client_name one of the user's
long-lived tokens has already with unknown_error, as Home Assistant answers the ValueError it
raises there; auth/delete_refresh_token deletes one of the user's by id, answers invalid_token_id
for an id the user has none by, and closes every connection logged in with the token it deletes
before its answer reaches them."""

import itertools
import json

from secret_rotator.kinds.home_assistant_token.homeassistant import URL
from secret_rotator.kinds.home_assistant_token.websocket import WebSocketError

VERSION = "2026.2.3"
LONG_LIVED = "long_lived_access_token"
INVALID = "Invalid access token or password"


class FakeHomeAssistant:
    def __init__(self):
        # id -> {"user", "name", "type", "value", "lifespan"}
        self.tokens = {}
        self.serial = itertools.count(1)
        self.opened = 0
        self.sockets = []
        self.commands = []  # (command type, the id of the token its connection logged in with)
        self.down = None  # the OSError an open raises
        self.refused = {}  # command type -> the error code it is answered, having done nothing
        # (command type, the id of the connection's token) -> its error code; refused's by default
        self.refuse = lambda type, token: self.refused.get(type)
        self.broken = {}  # command type -> the OSError its send raises, Home Assistant without it
        self.lost = {}  # command type -> the OSError its answer is lost to, once it took effect
        self.before = {}  # command type -> called with its message before it takes effect

    def token(self, user, name, type=LONG_LIVED, lifespan=3650):
        """A refresh token of the user's, made as the user would; its id."""
        n = next(self.serial)
        id = f"rt{n:04d}"
        value = f"SECRET-ha-token-{n}"
        self.tokens[id] = {
            "user": user,
            "name": name,
            "type": type,
            "value": value,
            "lifespan": lifespan,
        }
        return id

    def value(self, id):
        return self.tokens[id]["value"]

    def by_value(self, value):
        """The id of the token of that value; None when no token is."""
        return next((id for id, t in self.tokens.items() if t["value"] == value), None)

    def done(self, type):
        """The commands of the type Home Assistant was sent."""
        return [c for c in self.commands if c[0] == type]

    def __call__(self, url):
        assert url == URL, url
        self.opened += 1
        if self.down is not None:
            raise self.down
        socket = FakeSocket(self)
        self.sockets.append(socket)
        return socket

    def delete(self, id):
        del self.tokens[id]
        for socket in self.sockets:
            if socket.token == id:
                socket.inbox.clear()
                socket.closed = True


class FakeSocket:
    def __init__(self, ha):
        self.ha = ha
        self.inbox = [{"type": "auth_required", "ha_version": VERSION}]
        self.token = None  # the id of the token it logged in with
        self.closed = False  # by Home Assistant
        self.closed_by_client = False
        self.last = 0
        self.lost = None

    def send(self, text):
        assert not self.closed_by_client
        if self.closed:
            raise BrokenPipeError(32, "Broken pipe")
        msg = json.loads(text)
        if self.token is None:
            assert set(msg) == {"type", "access_token"} and msg["type"] == "auth", msg
            self.token = self.ha.by_value(msg["access_token"])
            if self.token is None:
                self.inbox.append({"type": "auth_invalid", "message": INVALID})
                self.closed = True
            else:
                self.inbox.append({"type": "auth_ok", "ha_version": VERSION})
            return
        assert msg["id"] > self.last, msg
        self.last = msg["id"]
        type = msg["type"]
        self.ha.commands.append((type, self.token))
        if type in self.ha.broken:
            raise self.ha.broken[type]
        if code := self.ha.refuse(type, self.token):
            self.error(msg, code, "refused")
            return
        if type in self.ha.before:
            self.ha.before[type](msg)
        user = self.ha.tokens[self.token]["user"]
        if type == "auth/refresh_tokens":
            assert set(msg) == {"id", "type"}, msg
            self.result(msg, self.listing(user))
        elif type == "auth/long_lived_access_token":
            self.mint(msg, user)
        elif type == "auth/delete_refresh_token":
            assert set(msg) == {"id", "type", "refresh_token_id"}, msg
            id = msg["refresh_token_id"]
            if self.ha.tokens.get(id, {}).get("user") != user:
                self.error(msg, "invalid_token_id", "Received invalid token")
                return
            self.ha.delete(id)
            if not self.closed:
                self.result(msg, {})
        else:
            raise AssertionError(f"no such command: {type}")
        if type in self.ha.lost:
            self.inbox.clear()
            self.lost = self.ha.lost[type]

    def listing(self, user):
        return [
            {
                "auth_provider_type": "homeassistant",
                "client_icon": None,
                "client_id": None if t["type"] == LONG_LIVED else "https://homeassistant.home/",
                "client_name": t["name"],
                "created_at": "2026-09-01T00:00:00+00:00",
                "expire_at": None,
                "id": id,
                "is_current": id == self.token,
                "last_used_at": None,
                "last_used_ip": None,
                "type": t["type"],
            }
            for id, t in self.ha.tokens.items()
            if t["user"] == user
        ]

    def mint(self, msg, user):
        assert set(msg) == {"id", "type", "client_name", "lifespan"}, msg
        name, lifespan = msg["client_name"], msg["lifespan"]
        assert isinstance(name, str) and isinstance(lifespan, int), msg
        if any(
            t["user"] == user and t["type"] == LONG_LIVED and t["name"] == name
            for t in self.ha.tokens.values()
        ):
            self.error(msg, "unknown_error", "Unknown error")
            return
        id = self.ha.token(user, name, lifespan=lifespan)
        self.result(msg, self.ha.value(id))

    def result(self, msg, result):
        self.inbox.append({"id": msg["id"], "type": "result", "success": True, "result": result})

    def error(self, msg, code, message):
        self.inbox.append(
            {
                "id": msg["id"],
                "type": "result",
                "success": False,
                "error": {"code": code, "message": message},
            }
        )

    def recv(self):
        assert not self.closed_by_client
        if self.lost is not None:
            raise self.lost
        if not self.inbox:
            assert self.closed, "nothing to receive"
            raise WebSocketError("the server closed the connection")
        return json.dumps(self.inbox.pop(0))

    def close(self):
        self.closed_by_client = True
