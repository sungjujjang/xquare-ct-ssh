"""Tests for the agent system/network/process collectors (agent/sysinfo.py)."""

import pytest

from agent import sysinfo


def test_local_ips_returns_list():
    ips = sysinfo.local_ips()
    assert isinstance(ips, list)
    assert all(isinstance(ip, str) for ip in ips)


def test_collect_info_shape():
    data = sysinfo.collect("info")
    assert data["hostname"]
    assert "ips" in data and isinstance(data["ips"], list)
    assert "disk" in data
    for entry in data["disk"]:
        assert {"path", "total", "used", "percent"} <= set(entry)


def test_collect_net_shape():
    data = sysinfo.collect("net")
    assert isinstance(data.get("interfaces"), list)
    assert isinstance(data.get("listening"), list)
    for row in data["listening"]:
        assert "laddr" in row and "proto" in row


def test_collect_procs_respects_limit():
    data = sysinfo.collect("procs", {"n": 3})
    procs = data["processes"]
    assert isinstance(procs, list)
    assert len(procs) <= 3


def test_unknown_command_raises():
    with pytest.raises(ValueError):
        sysinfo.collect("nope")


def test_human_helpers():
    from relay.ssh_server import _human_bytes, _human_duration

    assert _human_bytes(0) == "0 B"
    assert _human_bytes(1024) == "1.0 KiB"
    assert _human_bytes(1536).endswith("KiB")
    assert _human_bytes(None) == "-"
    assert _human_duration(90) == "1m"
    assert _human_duration(3661) == "1h 1m"
    assert _human_duration(90061) == "1d 1h 1m"
    assert _human_duration(None) == "-"
