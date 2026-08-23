"""Best-effort host / network / process introspection for the C2 devops tools.

The C2 CLI can ask an agent for ``info`` (host + resources), ``net``
(interfaces, listening sockets, counters) or ``procs`` (top processes).  This
module gathers that data.

``psutil`` is used when it is installed for rich cross-platform data; otherwise
we fall back to reading ``/proc`` and using the standard library.  Every
collector is defensive - a partial failure degrades that one section instead of
breaking the whole reply.
"""

from __future__ import annotations

import os
import platform
import socket
import struct
import time
from typing import Any, Callable

try:  # optional, richer data
    import psutil  # type: ignore
except Exception:  # pragma: no cover - optional dependency
    psutil = None


def _safe(fn: Callable[[], Any], default: Any = None) -> Any:
    try:
        return fn()
