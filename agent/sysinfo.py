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


def _proc_meminfo() -> dict[str, int]:
    out: dict[str, int] = {}
    try:
        for line in _read("/proc/meminfo").splitlines():
            key, _, rest = line.partition(":")
            value = rest.strip().split()
            if value and value[0].isdigit():
                out[key.strip()] = int(value[0]) * 1024
    except Exception:
        return {}
    return out


def _hex_ipv4(raw: str) -> str:
    return socket.inet_ntoa(struct.pack("<I", int(raw, 16)))


def _hex_ipv6(raw: str) -> str:
    packed = b"".join(struct.pack("<I", int(raw[i : i + 8], 16)) for i in range(0, 32, 8))
    return socket.inet_ntop(socket.AF_INET6, packed)


def _proc_listen_ports() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path, decode in (("/proc/net/tcp", _hex_ipv4), ("/proc/net/tcp6", _hex_ipv6)):
        try:
            lines = _read(path).splitlines()[1:]
        except Exception:
            continue
        for line in lines:
            fields = line.split()
            if len(fields) < 4 or fields[3] != "0A":  # 0A == TCP_LISTEN
                continue
            address, port_hex = fields[1].split(":")
            try:
                ip = decode(address)
                port = int(port_hex, 16)
            except Exception:
                continue
            rows.append({"proto": "tcp", "laddr": f"{ip}:{port}", "state": "LISTEN", "pid": None, "process": None})
    return rows

