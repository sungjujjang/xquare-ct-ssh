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


# --- collectors ------------------------------------------------------------


def _uptime_seconds() -> float | None:
    if psutil is not None:
        boot = _safe(psutil.boot_time)
        if boot:
            return max(0.0, time.time() - boot)
    return _proc_uptime()


def _memory() -> dict[str, Any] | None:
    if psutil is not None:
        vm = _safe(psutil.virtual_memory)
        if vm is not None:
            return {"total": vm.total, "used": vm.used, "available": vm.available, "percent": vm.percent}
    mem = _proc_meminfo()
    if not mem:
        return None
    total = mem.get("MemTotal", 0)
    available = mem.get("MemAvailable", mem.get("MemFree", 0))
    used = max(0, total - available)
    percent = round(used / total * 100, 1) if total else 0.0
    return {"total": total, "used": used, "available": available, "percent": percent}


def _swap() -> dict[str, Any] | None:
    if psutil is not None:
        sw = _safe(psutil.swap_memory)
        if sw is not None:
            return {"total": sw.total, "used": sw.used, "percent": sw.percent}
    mem = _proc_meminfo()
    total = mem.get("SwapTotal", 0)
    free = mem.get("SwapFree", 0)
    used = max(0, total - free)
    return {"total": total, "used": used, "percent": round(used / total * 100, 1) if total else 0.0}


def _disk(path: str | None = None) -> list[dict[str, Any]]:
    target = path or os.path.expanduser("~") or "/"
    try:
        import shutil

        usage = shutil.disk_usage(target)
    except Exception:
        return []
    percent = round(usage.used / usage.total * 100, 1) if usage.total else 0.0
    return [{"path": target, "total": usage.total, "used": usage.used, "free": usage.free, "percent": percent}]


def _loadavg() -> list[float] | None:
    try:
        return [round(x, 2) for x in os.getloadavg()]
    except (OSError, AttributeError):
        return None


def collect_info() -> dict[str, Any]:
    cpu_percent = None
    if psutil is not None:
        cpu_percent = _safe(lambda: psutil.cpu_percent(interval=0.15))
    return {
        "hostname": _safe(socket.gethostname, ""),
        "os": _safe(platform.platform, ""),
        "kernel": _safe(platform.release, ""),
        "arch": _safe(platform.machine, ""),
        "python": platform.python_version(),
        "ips": local_ips(),
        "uptime_seconds": _uptime_seconds(),
        "loadavg": _loadavg(),
        "cpu_count": _safe(os.cpu_count),
        "cpu_percent": cpu_percent,
        "memory": _memory(),
        "swap": _swap(),
        "disk": _disk(),
    }


def _net_interfaces() -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    if psutil is not None:
        stats = _safe(psutil.net_if_stats, {}) or {}
        addrs = _safe(psutil.net_if_addrs, {}) or {}
        counters = _safe(psutil.net_io_counters, None)
        per_nic = _safe(lambda: psutil.net_io_counters(pernic=True), {}) or {}
        for name, nic in per_nic.items():
            st = stats.get(name)
            addresses = [
                a.address
                for a in addrs.get(name, [])
                if a.family in (socket.AF_INET, socket.AF_INET6)
            ]
            result.append(
                {
                    "name": name,
                    "up": bool(getattr(st, "isup", False)) if st else None,
                    "speed": getattr(st, "speed", None) if st else None,
                    "mtu": getattr(st, "mtu", None) if st else None,
                    "addrs": addresses,
                    "rx_bytes": nic.bytes_recv,
                    "tx_bytes": nic.bytes_sent,
                    "rx_packets": nic.packets_recv,
                    "tx_packets": nic.packets_sent,
                    "errors": nic.errin + nic.errout,
                    "drops": nic.dropin + nic.dropout,
                }
            )
        if result:
            if counters is not None:
                result.sort(key=lambda r: r["rx_bytes"] + r["tx_bytes"], reverse=True)
            return result
    # /proc/net/dev fallback
    try:
        lines = _read("/proc/net/dev").splitlines()[2:]
    except Exception:
        return result
    for line in lines:
        name, _, rest = line.partition(":")
        fields = rest.split()
        if len(fields) < 16:
            continue
        result.append(
            {
                "name": name.strip(),
                "up": None,
                "speed": None,
                "mtu": None,
                "addrs": [],
                "rx_bytes": int(fields[0]),
                "tx_bytes": int(fields[8]),
                "rx_packets": int(fields[1]),
                "tx_packets": int(fields[9]),
                "errors": int(fields[2]) + int(fields[10]),
                "drops": int(fields[3]) + int(fields[11]),
            }
        )
    return result


def _listening() -> list[dict[str, Any]]:
    if psutil is not None:
        rows: list[dict[str, Any]] = []
        for conn in _safe(lambda: psutil.net_connections(kind="inet"), []) or []:
            if conn.status != psutil.CONN_LISTEN:
                continue
            laddr = conn.laddr
            address = getattr(laddr, "ip", None) or (laddr[0] if laddr else "")
            port = getattr(laddr, "port", None) or (laddr[1] if laddr and len(laddr) > 1 else None)
            name = None
            if conn.pid:
                name = _safe(lambda: psutil.Process(conn.pid).name())
            rows.append(
                {
                    "proto": "tcp",
                    "laddr": f"{address}:{port}",
                    "state": "LISTEN",
                    "pid": conn.pid,
                    "process": name,
                }
            )
        if rows:
            rows.sort(key=lambda r: (r["pid"] is None, r["laddr"]))
            return rows
    return _proc_listen_ports()


def collect_net() -> dict[str, Any]:
    listening = _listening()
    connection_count = None
    if psutil is not None:
        connection_count = _safe(lambda: len(psutil.net_connections(kind="inet")))
    return {
        "ips": local_ips(),
        "interfaces": _net_interfaces(),
        "listening": listening,
        "connection_count": connection_count,
    }


def _proc_fallback(limit: int) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    try:
        pids = [p for p in os.listdir("/proc") if p.isdigit()]
    except Exception:
        return rows
    page = os.sysconf("SC_PAGE_SIZE") if hasattr(os, "sysconf") else 4096
    for pid in pids:
        try:
            stat = _read(f"/proc/{pid}/stat")
            rparen = stat.rfind(")")
            name = stat[stat.find("(") + 1 : rparen]
            rest = stat[rparen + 2 :].split()
            rss = int(rest[21]) * page
            utime, stime = int(rest[11]), int(rest[12])
            rows.append(
                {
                    "pid": int(pid),
                    "name": name,
                    "username": None,
                    "cpu_percent": None,
                    "cpu_ticks": utime + stime,
                    "memory_percent": None,
                    "rss": rss,
                }
            )
        except Exception:
            continue
    rows.sort(key=lambda r: r["cpu_ticks"], reverse=True)
    return rows[:limit]
