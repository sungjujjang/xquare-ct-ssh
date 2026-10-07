"""SQLite backed registry of relay operators and internal servers.

Two kinds of credentials live here:

* ``relay_users``  - who may open an SSH session on the relay itself.
* ``servers``      - the internal machines.  Each row stores

  - ``token_hash``          the secret the *agent* uses to authenticate.
  - ``login_password_hash`` the secret an operator types after ``login <name>``.

All secrets are stored hashed (see :mod:`relay.security`); the plaintext agent
token is only ever shown once, when the server is created.
"""

from __future__ import annotations

import os
import sqlite3
import time
from dataclasses import dataclass
from typing import Iterable

from relay.security import hash_secret, new_token, verify_secret

_SCHEMA = """
CREATE TABLE IF NOT EXISTS relay_users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS servers (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    name               TEXT UNIQUE NOT NULL,
    token_hash         TEXT NOT NULL,
    login_password_hash TEXT NOT NULL,
    description        TEXT NOT NULL DEFAULT '',
    enabled            INTEGER NOT NULL DEFAULT 1,
    created_at         TEXT NOT NULL
);
"""


@dataclass(frozen=True)
class Server:
    id: int
    name: str
    token_hash: str
    login_password_hash: str
    description: str
    enabled: bool


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


class RegistryDB:
    """Thin synchronous wrapper around a SQLite database file."""

    def __init__(self, path: str):
        self.path = path

    # -- plumbing ----------------------------------------------------------
    def _connect(self) -> sqlite3.Connection:
        directory = os.path.dirname(os.path.abspath(self.path))
        if directory:
            os.makedirs(directory, exist_ok=True)
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def init_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(_SCHEMA)

    # -- relay operators ---------------------------------------------------
    def add_relay_user(self, username: str, password: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO relay_users (username, password_hash, created_at) "
                "VALUES (?, ?, ?) "
                "ON CONFLICT(username) DO UPDATE SET password_hash=excluded.password_hash",
                (username, hash_secret(password), _now()),
            )

    def verify_relay_user(self, username: str, password: str) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT password_hash FROM relay_users WHERE username = ?", (username,)
            ).fetchone()
        return bool(row) and verify_secret(password, row["password_hash"])

    def relay_user_exists(self, username: str) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM relay_users WHERE username = ?", (username,)
            ).fetchone()
        return row is not None

    def list_relay_users(self) -> list[str]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT username FROM relay_users ORDER BY username"
            ).fetchall()
        return [row["username"] for row in rows]

    def remove_relay_user(self, username: str) -> bool:
        with self._connect() as conn:
            cur = conn.execute("DELETE FROM relay_users WHERE username = ?", (username,))
        return cur.rowcount > 0

    def count_relay_users(self) -> int:
        with self._connect() as conn:
            row = conn.execute("SELECT COUNT(*) AS n FROM relay_users").fetchone()
        return int(row["n"])

    # -- internal servers --------------------------------------------------
    def add_server(
        self,
        name: str,
        login_password: str,
        *,
        description: str = "",
        token: str | None = None,
    ) -> str:
        """Create (or update) a server.  Returns the plaintext agent token."""
        token = token or new_token()
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO servers "
                "(name, token_hash, login_password_hash, description, enabled, created_at) "
                "VALUES (?, ?, ?, ?, 1, ?) "
                "ON CONFLICT(name) DO UPDATE SET "
                "  token_hash=excluded.token_hash, "
                "  login_password_hash=excluded.login_password_hash, "
                "  description=excluded.description",
                (name, hash_secret(token), hash_secret(login_password), description, _now()),
            )
        return token

    def set_server_enabled(self, name: str, enabled: bool) -> bool:
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE servers SET enabled = ? WHERE name = ?",
                (1 if enabled else 0, name),
            )
        return cur.rowcount > 0

    def remove_server(self, name: str) -> bool:
        with self._connect() as conn:
            cur = conn.execute("DELETE FROM servers WHERE name = ?", (name,))
        return cur.rowcount > 0

    def rotate_server_token(self, name: str, token: str | None = None) -> str | None:
        """Replace a server's agent token.  Returns the new plaintext token."""
        token = token or new_token()
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE servers SET token_hash = ? WHERE name = ?",
                (hash_secret(token), name),
            )
        return token if cur.rowcount > 0 else None

    def get_server(self, name: str) -> Server | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM servers WHERE name = ?", (name,)).fetchone()
        return _row_to_server(row) if row else None

    def list_servers(self) -> list[Server]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM servers ORDER BY name").fetchall()
        return [_row_to_server(row) for row in rows]

    def verify_agent_token(self, name: str, token: str) -> bool:
        server = self.get_server(name)
        if not server or not server.enabled:
            return False
        return verify_secret(token, server.token_hash)

    def verify_server_login(self, name: str, password: str) -> bool:
        server = self.get_server(name)
        if not server or not server.enabled:
            return False
        return verify_secret(password, server.login_password_hash)


def _row_to_server(row: sqlite3.Row) -> Server:
    return Server(
        id=row["id"],
        name=row["name"],
        token_hash=row["token_hash"],
        login_password_hash=row["login_password_hash"],
        description=row["description"],
        enabled=bool(row["enabled"]),
    )


def server_names(servers: Iterable[Server]) -> list[str]:
    return [s.name for s in servers]
