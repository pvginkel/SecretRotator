"""A websocket client (RFC 6455) on the standard library, as much of it as Home Assistant's
websocket API needs: one connection that sends and receives text messages. Every frame it sends
is masked, as a client's must be; a ping is answered with a pong."""

import base64
import contextlib
import hashlib
import os
import socket
import ssl
import urllib.parse
from typing import BinaryIO

TIMEOUT = 30
# What RFC 6455 §1.3 appends to the handshake's key before it hashes it.
GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
TEXT, CLOSE, PING, PONG = 0x1, 0x8, 0x9, 0xA
NORMAL = (1000).to_bytes(2, "big")  # the status of a close frame that ends a connection as done


class WebSocketError(Exception):
    """A handshake the server did not answer as a websocket, or a connection it closed."""


def accept(key: str) -> str:
    """The Sec-WebSocket-Accept a server answers the handshake's key with."""
    return base64.b64encode(hashlib.sha1((key + GUID).encode()).digest()).decode()


def masked(payload: bytes, mask: bytes) -> bytes:
    return bytes(b ^ mask[i % 4] for i, b in enumerate(payload))


class WebSocket:
    def __init__(self, sock: socket.socket, reader: BinaryIO):
        self.sock = sock
        self.reader = reader

    def send(self, text: str) -> None:
        self._send(TEXT, text.encode())

    def recv(self) -> str:
        """The next text message, its fragments joined."""
        parts: list[bytes] = []
        while True:
            fin, opcode, payload = self._frame()
            if opcode == PING:
                self._send(PONG, payload)
            elif opcode == CLOSE:
                status = f" with status {int.from_bytes(payload[:2])}" if payload else ""
                raise WebSocketError(f"the server closed the connection{status}")
            elif opcode != PONG:
                parts.append(payload)
                if fin:
                    return b"".join(parts).decode()

    def close(self) -> None:
        """Sends a close frame where the connection still takes one, and closes it."""
        with contextlib.suppress(OSError):
            self._send(CLOSE, NORMAL)
        self.reader.close()
        self.sock.close()

    def _send(self, opcode: int, payload: bytes) -> None:
        n = len(payload)
        if n < 126:
            length = bytes([0x80 | n])
        elif n < 1 << 16:
            length = bytes([0x80 | 126]) + n.to_bytes(2, "big")
        else:
            length = bytes([0x80 | 127]) + n.to_bytes(8, "big")
        mask = os.urandom(4)
        self.sock.sendall(bytes([0x80 | opcode]) + length + mask + masked(payload, mask))

    def _frame(self) -> tuple[bool, int, bytes]:
        """A server's frame, which is never masked: whether it is a message's last, its opcode
        and its payload."""
        first, second = self._read(2)
        n = second & 0x7F
        if n == 126:
            n = int.from_bytes(self._read(2), "big")
        elif n == 127:
            n = int.from_bytes(self._read(8), "big")
        return bool(first & 0x80), first & 0x0F, self._read(n)

    def _read(self, n: int) -> bytes:
        data = self.reader.read(n)
        if len(data) < n:
            raise WebSocketError("the server closed the connection")
        return data


def connect(url: str, timeout: float = TIMEOUT) -> WebSocket:
    """A connection to the websocket at the ws:// or wss:// URL, its handshake done."""
    parts = urllib.parse.urlsplit(url)
    secure = parts.scheme == "wss"
    with contextlib.ExitStack() as stack:
        address = (parts.hostname, parts.port or (443 if secure else 80))
        sock = stack.enter_context(socket.create_connection(address, timeout))
        if secure:
            context = ssl.create_default_context()
            sock = stack.enter_context(context.wrap_socket(sock, server_hostname=parts.hostname))
        reader = stack.enter_context(sock.makefile("rb"))
        key = base64.b64encode(os.urandom(16)).decode()
        sock.sendall(
            f"GET {parts.path or '/'} HTTP/1.1\r\nHost: {parts.netloc}\r\nUpgrade: websocket\r\n"
            f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n"
            f"\r\n".encode()
        )
        status = reader.readline().decode("latin-1").strip()
        headers = {}
        while line := reader.readline().decode("latin-1").strip():
            name, _, value = line.partition(":")
            headers[name.strip().lower()] = value.strip()
        if status.split(" ")[1:2] != ["101"]:
            raise WebSocketError(f"the handshake was answered {status or 'with nothing'}")
        if headers.get("sec-websocket-accept") != accept(key):
            raise WebSocketError("the handshake's answer does not accept its key")
        stack.pop_all()
    return WebSocket(sock, reader)
