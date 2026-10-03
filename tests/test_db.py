"""Tests for the SQLite registry (relay/db.py)."""

import os
import tempfile

from relay.db import RegistryDB


def _db() -> RegistryDB:
    tmp = tempfile.mkdtemp(prefix="xq-db-")
    db = RegistryDB(os.path.join(tmp, "relay.db"))
    db.init_schema()
    return db


def test_operator_accounts():
    db = _db()
    db.add_relay_user("alice", "pw1")
    db.add_relay_user("bob", "pw2")
    assert db.list_relay_users() == ["alice", "bob"]
    assert db.count_relay_users() == 2
    assert db.verify_relay_user("alice", "pw1")
    assert not db.verify_relay_user("alice", "wrong")

    assert db.remove_relay_user("bob")
    assert not db.remove_relay_user("bob")
    assert db.list_relay_users() == ["alice"]


def test_server_enable_disable_and_remove():
    db = _db()
    db.add_server("s1", "opsecret", token="t1")
    assert db.verify_agent_token("s1", "t1")
    assert db.verify_server_login("s1", "opsecret")

    assert db.set_server_enabled("s1", False)
    assert not db.verify_agent_token("s1", "t1")
    assert not db.verify_server_login("s1", "opsecret")
    assert db.set_server_enabled("s1", True)

    assert db.remove_server("s1")
    assert not db.remove_server("s1")
    assert db.get_server("s1") is None


def test_rotate_server_token():
    db = _db()
    db.add_server("s2", "opsecret", token="old")
    new = db.rotate_server_token("s2")
    assert new and new != "old"
    assert db.verify_agent_token("s2", new)
    assert not db.verify_agent_token("s2", "old")
    assert db.rotate_server_token("missing") is None


def test_set_server_login_password():
    db = _db()
    db.add_server("s3", "opsecret", token="t3")
    assert db.set_server_login_password("s3", "newsecret")
    assert db.verify_server_login("s3", "newsecret")
    assert not db.verify_server_login("s3", "opsecret")
    # the agent token is untouched
    assert db.verify_agent_token("s3", "t3")
    assert not db.set_server_login_password("missing", "x")


def test_change_operator_password():
    db = _db()
    db.add_relay_user("carol", "first")
    assert db.verify_relay_user("carol", "first")
    db.add_relay_user("carol", "second")
    assert db.verify_relay_user("carol", "second")
    assert not db.verify_relay_user("carol", "first")
