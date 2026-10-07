import struct

import pytest

from common import protocol


def test_roundtrip_data():
    frame = protocol.encode(protocol.DATA, 42, b"\x1b[31mred\x1b[0m")
    msg_type, session_id, payload = protocol.decode(frame)
    assert msg_type == protocol.DATA
    assert session_id == 42
    assert payload == b"\x1b[31mred\x1b[0m"


def test_roundtrip_str_payload_is_utf8():
    frame = protocol.encode(protocol.CLOSE, 1, "안녕")
    _, _, payload = protocol.decode(frame)
    assert payload.decode("utf-8") == "안녕"


def test_binary_safe():
    # every byte value including NUL must survive untouched
    blob = bytes(range(256))
    frame = protocol.encode(protocol.DATA, 0xFFFFFFFF, blob)
    _, session_id, payload = protocol.decode(frame)
    assert session_id == 0xFFFFFFFF
    assert payload == blob


def test_resize_helpers():
    frame = protocol.encode_resize(5, 120, 40)
    msg_type, session_id, payload = protocol.decode(frame)
    assert msg_type == protocol.RESIZE
    assert session_id == 5
    assert protocol.decode_resize(payload) == (120, 40)


def test_json_helpers():
    frame = protocol.encode_json(protocol.OPEN, 3, {"shell": "/bin/bash", "cols": 80})
    msg_type, session_id, payload = protocol.decode(frame)
    assert msg_type == protocol.OPEN
    assert session_id == 3
    assert protocol.decode_json(payload) == {"shell": "/bin/bash", "cols": 80}


def test_short_frame_rejected():
    with pytest.raises(protocol.ProtocolError):
        protocol.decode(b"\x01\x00")


def test_resize_short_payload_rejected():
    with pytest.raises(protocol.ProtocolError):
        protocol.decode_resize(struct.pack(">H", 1))


def test_type_name():
    assert protocol.type_name(protocol.DATA) == "DATA"
    assert protocol.type_name(0xEE) == "0xee"
