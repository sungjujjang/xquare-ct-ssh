"""Shared wire protocol for the relay <-> agent link.

The relay talks to internal agents over a WebSocket carrying **binary** frames.
Every frame is a single protocol message with a fixed 5 byte header::

    +--------+------------------+-------------------+
    | type   | session id (u32) | payload           |
    | 1 byte | 4 bytes, BE      | variable          |
    +--------+------------------+-------------------+

The protocol is a raw byte stream, *not* a command/response RPC.  DATA frames
contain the exact bytes that travelled between the SSH client and the remote
shell's PTY, so ANSI escape sequences, control characters and partial UTF-8
sequences pass through untouched.

Session multiplexing
--------------------
A single agent keeps one WebSocket open to the relay and can serve several
concurrent operators.  Each operator shell is a *session* identified by an
unsigned 32 bit id.  Messages that belong to a session carry that id; control
messages (AUTH/PING/...) use session id ``0``.
"""

# NOTE: vendored copy. This file is duplicated as ``agent/protocol.py`` and
# ``relay/protocol.py`` so each folder can be shipped on its own. Keep identical.

from __future__ import annotations

import json
import struct
from typing import Any

PROTOCOL_VERSION = 1

# --- message types ---------------------------------------------------------
# control
AUTH = 0x01          # agent -> relay : {"id","token","version","hostname","os"}
AUTH_OK = 0x02       # relay -> agent
AUTH_FAIL = 0x03     # relay -> agent : reason
PING = 0x30
PONG = 0x31
EXIT = 0x40          # either side : agent shutting the connection down

# session lifecycle
OPEN = 0x10          # relay -> agent : {"shell","cols","rows","env"}
OPENED = 0x11        # agent -> relay : {"ok":bool,"error":str|None}
CLOSE = 0x22         # either side : session is finished (payload = final bytes)
DATA = 0x20          # both : raw terminal bytes
RESIZE = 0x21        # both : ">HH" cols, rows

_HEADER = struct.Struct(">BI")
HEADER_SIZE = _HEADER.size
_RESIZE = struct.Struct(">HH")

# Guard against a peer sending an absurdly large frame.  The relay and agent
# both set their WebSocket max_size to this value.
MAX_FRAME_SIZE = 4 * 1024 * 1024


class ProtocolError(Exception):
    """Raised when an incoming frame cannot be parsed."""


def encode(msg_type: int, session_id: int = 0, payload: bytes | str | None = None) -> bytes:
    """Encode a protocol message into a binary frame."""
    if payload is None:
        body = b""
    elif isinstance(payload, str):
        body = payload.encode("utf-8")
    else:
        body = bytes(payload)
    if len(body) > MAX_FRAME_SIZE:
        raise ProtocolError(f"frame too large: {len(body)} bytes")
    return _HEADER.pack(msg_type & 0xFF, session_id & 0xFFFFFFFF) + body


def decode(frame: bytes) -> tuple[int, int, bytes]:
    """Decode a binary frame into ``(type, session_id, payload)``."""
    if len(frame) < HEADER_SIZE:
        raise ProtocolError(f"frame too short: {len(frame)} bytes")
    msg_type, session_id = _HEADER.unpack_from(frame, 0)
    return msg_type, session_id, frame[HEADER_SIZE:]


def encode_json(msg_type: int, session_id: int, obj: Any) -> bytes:
    return encode(msg_type, session_id, json.dumps(obj, separators=(",", ":")))


def decode_json(payload: bytes) -> Any:
    try:
        return json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:  # pragma: no cover - defensive
        raise ProtocolError(f"invalid json payload: {exc}") from exc


def encode_resize(session_id: int, cols: int, rows: int) -> bytes:
    return encode(RESIZE, session_id, _RESIZE.pack(cols & 0xFFFF, rows & 0xFFFF))


def decode_resize(payload: bytes) -> tuple[int, int]:
    if len(payload) < _RESIZE.size:
        raise ProtocolError("resize payload too short")
    cols, rows = _RESIZE.unpack_from(payload, 0)
    return cols, rows


_TYPE_NAMES = {
    AUTH: "AUTH",
    AUTH_OK: "AUTH_OK",
    AUTH_FAIL: "AUTH_FAIL",
    PING: "PING",
    PONG: "PONG",
    EXIT: "EXIT",
    OPEN: "OPEN",
    OPENED: "OPENED",
    CLOSE: "CLOSE",
    DATA: "DATA",
    RESIZE: "RESIZE",
}


def type_name(msg_type: int) -> str:
    return _TYPE_NAMES.get(msg_type, f"0x{msg_type:02x}")
