"""Tests for the agent system/network/process collectors (agent/sysinfo.py)."""

import pytest

from agent import sysinfo


def test_local_ips_returns_list():
    ips = sysinfo.local_ips()
    assert isinstance(ips, list)
    assert all(isinstance(ip, str) for ip in ips)


