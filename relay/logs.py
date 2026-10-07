"""In-memory ring buffer of recent log records.

The relay operator can dump the tail of this buffer from the C2 CLI with the
``logs`` command.  The buffer is attached to the root logger by :func:`install`
so it captures relay, websockets and asyncssh messages alike.
"""

from __future__ import annotations

import logging
from collections import deque


class LogBuffer(logging.Handler):
    def __init__(self, capacity: int = 2000):
        super().__init__()
        self.records: deque[str] = deque(maxlen=capacity)
        self.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
        )

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.records.append(self.format(record))
        except Exception:  # pragma: no cover - formatting must never break logging
            pass

    def tail(self, n: int) -> list[str]:
        if n <= 0:
            return []
        return list(self.records)[-n:]


_buffer = LogBuffer()


def install(level: int = logging.INFO) -> LogBuffer:
    _buffer.setLevel(level)
    root = logging.getLogger()
    if _buffer not in root.handlers:
        root.addHandler(_buffer)
    return _buffer


def tail(n: int = 100) -> list[str]:
    return _buffer.tail(n)
