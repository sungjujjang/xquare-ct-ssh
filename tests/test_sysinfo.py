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
