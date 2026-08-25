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
    except Exception:
        return default


def _read(path: str) -> str:
    with open(path, "r", encoding="utf-8", errors="replace") as handle:
        return handle.read()


def local_ips() -> list[str]:
    """Non-loopback IPv4 addresses of this host (best effort)."""
    ips: set[str] = set()
    if psutil is not None:
        try:
            for addrs in psutil.net_if_addrs().values():
                for addr in addrs:
                    if addr.family == socket.AF_INET and not addr.address.startswith("127."):
                        ips.add(addr.address)
            if ips:
                return sorted(ips)
        except Exception:
            pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if not ip.startswith("127."):
                ips.add(ip)
    except Exception:
        pass
    return sorted(ips)


# --- /proc helpers ---------------------------------------------------------


def _proc_uptime() -> float | None:
    try:
        return float(_read("/proc/uptime").split()[0])
    except Exception:
        return None


