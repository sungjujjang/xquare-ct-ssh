"""SSH entry point and the C2 interactive shell.

Flow::

    SSH client --(pty)--> RelaySession
                             |
                             |  'login <server> <password>'  (C2 CLI)
                             v
                        SessionBridge  <--raw bytes-->  agent WebSocket

Before login the session runs the C2 command menu (a small line editor).
After a successful ``login`` the session switches into *bridge mode*: every
byte from the SSH client is forwarded verbatim to the agent PTY and every byte
from the PTY is written straight back to the SSH channel.  No command parsing
happens in bridge mode, which is what makes ``vim``/``top``/``htop`` work.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os

import asyncssh

from common import protocol
from relay.config import RelayConfig
from relay.db import RegistryDB
from relay.lineeditor import LineEditor
from relay.registry import Registry, SessionBridge, allocate_session_id

log = logging.getLogger("relay.ssh")


def load_or_create_host_key(path: str) -> asyncssh.SSHKey:
    if not os.path.exists(path):
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        key = asyncssh.generate_private_key("ssh-ed25519")
        with open(path, "wb") as handle:
            handle.write(key.export_private_key())
        try:
            os.chmod(path, 0o600)
        except OSError:  # pragma: no cover - best effort
            pass
        log.info("generated new relay host key at %s", path)
        return key
    with open(path, "rb") as handle:
        return asyncssh.import_private_key(handle.read())


class ChannelWriter:
    """Adapts an asyncssh channel to the async ``write`` the bridge expects.

    asyncssh exposes backpressure through the session's ``pause_writing`` /
    ``resume_writing`` callbacks rather than a ``drain()`` coroutine.  We wait
    on the session's gate before delivering the next chunk so a slow SSH client
    naturally throttles the agent (and therefore the remote shell).
    """

    def __init__(self, chan: asyncssh.SSHServerChannel, session: "RelaySession"):
        self._chan = chan
        self._session = session
        self._closed = False

    async def write(self, data: bytes) -> None:
        if self._closed:
            return
        try:
            self._chan.write(data)
        except (BrokenPipeError, ConnectionError, asyncssh.Error):
            self._closed = True
            return
        gate = self._session.writing_allowed
        if not gate.is_set():
            await gate.wait()

    def close(self) -> None:
        self._closed = True


class RelaySession(asyncssh.SSHServerSession):
    def __init__(
        self,
        username: str,
        db: RegistryDB,
        registry: Registry,
        config: RelayConfig,
    ):
        self.username = username
        self.db = db
        self.registry = registry
        self.config = config

        self._chan: asyncssh.SSHServerChannel | None = None
        self._writer: ChannelWriter | None = None
        self._inbound: asyncio.Queue[bytes | None] = asyncio.Queue()
        self._task: asyncio.Task | None = None

        self._term_type = config.default_term
        self._cols = 80
        self._rows = 24
        self._bridge: SessionBridge | None = None
        self._exit_requested = False
        # Set == the SSH channel is accepting writes; cleared while asyncssh
        # has paused us because the client's receive window is full.
        self.writing_allowed = asyncio.Event()
        self.writing_allowed.set()

        self._editor = LineEditor(prompt="C2> ", emit=self._emit_text, completer=self._complete)
        self._prompt = "C2> "

    # -- asyncssh SSHServerSession hooks ----------------------------------
    def connection_made(self, chan: asyncssh.SSHServerChannel) -> None:
        self._chan = chan
        self._writer = ChannelWriter(chan, self)
        self._task = asyncio.ensure_future(self._run())

    def pause_writing(self) -> None:
        self.writing_allowed.clear()

    def resume_writing(self) -> None:
        self.writing_allowed.set()

    def pty_requested(self, term_type, term_size, term_modes) -> bool:
        if term_type:
            self._term_type = term_type
        if term_size:
            self._cols, self._rows = term_size[0], term_size[1]
        return True

    def shell_requested(self) -> bool:
        return True

    def exec_requested(self, command: str) -> bool:
        return True

    def terminal_size_changed(self, width, height, pixwidth, pixheight) -> None:
        if width and height:
            self._cols, self._rows = width, height
        if self._bridge is not None:
            asyncio.ensure_future(self._bridge.send_resize(self._cols, self._rows))

    def data_received(self, data, datatype) -> None:
        if isinstance(data, str):
            data = data.encode("utf-8")
        self._inbound.put_nowait(data)

    def eof_received(self) -> bool:
        self._inbound.put_nowait(None)
        return False

    def connection_lost(self, exc) -> None:
        self.writing_allowed.set()
        self._inbound.put_nowait(None)
        if self._task is not None:
            self._task.cancel()

    def break_received(self, msec) -> bool:
        # Forward a real BREAK as CTRL+C is not possible; treat as ^C to the shell.
        if self._bridge is not None:
            asyncio.ensure_future(self._bridge.send_input(b"\x03"))
        return True

    # -- output helpers ----------------------------------------------------
    def _emit_text(self, text: str) -> None:
        if self._chan is None:
            return
        try:
            self._chan.write(text.encode("utf-8"))
        except (BrokenPipeError, ConnectionError, asyncssh.Error):
            pass

    def _emit_bytes(self, data: bytes) -> None:
        if self._chan is None:
            return
        try:
            self._chan.write(data)
        except (BrokenPipeError, ConnectionError, asyncssh.Error):
            pass

    # -- main loop ---------------------------------------------------------
    async def _run(self) -> None:
        try:
            self._emit_text(self.config.banner)
            self._editor.set_prompt(self._prompt)
            self._editor.render_prompt()
            while not self._exit_requested:
                data = await self._inbound.get()
                if data is None:
                    break
                if self._bridge is not None:
                    await self._bridge.send_input(data)
                    continue
                lines = self._editor.feed(data)
                for line in lines:
                    await self._handle_command(line)
                    if self._bridge is not None or self._exit_requested:
                        break
                if self._editor.eof:
                    break
                if self._bridge is None and not self._exit_requested:
                    self._editor.render_prompt()
        except asyncio.CancelledError:  # pragma: no cover - shutdown
            pass
        except (BrokenPipeError, ConnectionError, asyncssh.Error):
            pass
        except Exception:  # pragma: no cover - unexpected
            log.exception("session error for %s", self.username)
        finally:
            await self._cleanup()

    async def _cleanup(self) -> None:
        if self._bridge is not None:
            bridge, self._bridge = self._bridge, None
            try:
                await bridge.close(b"client disconnected")
            except Exception:  # pragma: no cover
                pass
        if self._chan is not None:
            try:
                self._chan.close()
            except Exception:  # pragma: no cover
                pass

    # -- command handling --------------------------------------------------
    async def _handle_command(self, raw_line: str) -> None:
        line = raw_line.strip()
        if not line:
            return
        self._editor.add_history(line)
        parts = line.split()
        command = parts[0].lower()
        args = parts[1:]

        if command in ("help", "?"):
            self._print_help()
        elif command in ("list", "servers", "ls"):
            self._print_servers()
        elif command in ("login", "connect"):
            await self._cmd_login(args)
        elif command in ("ping",):
            self._emit_text("pong\r\n")
        elif command in ("exit", "quit", "logout", "bye"):
            self._emit_text("Bye.\r\n")
            self._exit_requested = True
        else:
            self._emit_text(f"\x1b[31munknown command:\x1b[0m {command} (try 'help')\r\n")

    def _print_help(self) -> None:
        self._emit_text(
            "\r\n\x1b[1mAvailable commands\x1b[0m\r\n"
            "  \x1b[36mlist\x1b[0m                    list known internal servers\r\n"
            "  \x1b[36mlogin <server> [password]\x1b[0m log in and attach to a server's shell\r\n"
            "  \x1b[36mping\x1b[0m                    check the C2 CLI is responsive\r\n"
            "  \x1b[36mhelp\x1b[0m                    show this help\r\n"
            "  \x1b[36mexit\x1b[0m                    disconnect\r\n"
            "\r\n"
        )

    def _print_servers(self) -> None:
        servers = self.db.list_servers()
        if not servers:
            self._emit_text("No servers registered.\r\n")
            return
        self._emit_text("\r\n\x1b[1m  SERVER           STATUS    DESCRIPTION\x1b[0m\r\n")
        for server in servers:
            online = self.registry.is_online(server.name)
            if not server.enabled:
                status = "\x1b[33mdisabled\x1b[0m"
            elif online:
                status = "\x1b[32monline \x1b[0m"
            else:
                status = "\x1b[31moffline\x1b[0m"
            self._emit_text(f"  {server.name:<16} {status}  {server.description}\r\n")
        self._emit_text("\r\n")

    async def _cmd_login(self, args: list[str]) -> None:
        if not args:
            self._emit_text("usage: login <server> [password]\r\n")
            return
        name = args[0]
        password = args[1] if len(args) > 1 else None

        server = self.db.get_server(name)
        if server is None:
            self._emit_text(f"\x1b[31munknown server:\x1b[0m {name}\r\n")
            return
        if not server.enabled:
            self._emit_text(f"server {name} is disabled\r\n")
            return

        if password is None:
            password = await self._read_password(f"Password for {name}: ")
            if password is None:
                self._emit_text("cancelled\r\n")
                return

        if not self.db.verify_server_login(name, password):
            self._emit_text("\x1b[31mauthentication failed\x1b[0m\r\n")
            return

        agent = self.registry.get(name)
        if agent is None or agent.closed:
            self._emit_text(f"server {name} is offline (agent not connected)\r\n")
            return

        loop = asyncio.get_event_loop()
        session_id = allocate_session_id()
        bridge = SessionBridge(session_id, agent, client=self._writer)
        bridge.opened = loop.create_future()
        agent.add_session(bridge)

        env = {
            "TERM": self._term_type,
            "LANG": os.environ.get("LANG", "C.UTF-8"),
            "COLORTERM": "truecolor",
        }
        try:
            await agent.send(
                protocol.OPEN,
                session_id,
                json.dumps({"shell": None, "cols": self._cols, "rows": self._rows, "env": env}),
            )
        except ConnectionError:
            agent.remove_session(session_id)
            self._emit_text("agent connection lost\r\n")
            return

        try:
            result = await asyncio.wait_for(bridge.opened, timeout=self.config.open_timeout)
        except asyncio.TimeoutError:
            await bridge.close(b"")
            self._emit_text("timed out opening remote shell\r\n")
            return

        if not result or not result.get("ok"):
            error = (result or {}).get("error") or "unknown error"
            await bridge.close(b"")
            self._emit_text(f"\x1b[31mfailed to open shell:\x1b[0m {error}\r\n")
            return

        bridge.on_closed(self._on_bridge_closed)
        self._bridge = bridge
        self._emit_text(f"\r\n\x1b[32mConnected to {name}\x1b[0m\r\n\r\n")

    def _on_bridge_closed(self) -> None:
        # The remote shell exited (or the agent dropped) - fall back to the CLI.
        self._bridge = None
        if self._chan is None:
            return
        self._emit_text("\r\n\x1b[33m[disconnected from remote shell]\x1b[0m\r\n")
        self._editor.reset()
        self._editor.render_prompt()

    async def _read_password(self, prompt: str) -> str | None:
        self._emit_text(prompt)
        buffer: list[str] = []
        while True:
            data = await self._inbound.get()
            if data is None:
                return None
            if self._bridge is not None:
                # Should not happen, but keep the bytes for the bridge.
                await self._bridge.send_input(data)
                continue
            for ch in data.decode("utf-8", "replace"):
                if ch in ("\r", "\n"):
                    self._emit_text("\r\n")
                    return "".join(buffer)
                if ch in ("\x7f", "\x08"):
                    if buffer:
                        buffer.pop()
                        self._emit_text("\b \b")
                elif ch == "\x03":
                    self._emit_text("^C\r\n")
                    return None
                elif ch == "\x04":
                    return None
                elif ch >= " ":
                    buffer.append(ch)
                    self._emit_text("*")

    def _complete(self, line: str, cursor: int) -> list[str]:
        prefix = line[:cursor]
        # completing the command itself
        if " " not in prefix:
            commands = ["help", "list", "login", "ping", "exit"]
            return [c for c in commands if c.startswith(prefix)]
        parts = prefix.split()
        if parts and parts[0] in ("login", "connect"):
            word = "" if prefix.endswith(" ") else parts[-1]
            names = [s.name for s in self.db.list_servers()]
            matches = [n for n in names if n.startswith(word)]
            if prefix.endswith(" "):
                return matches
            return [n for n in matches]
        return []


class RelaySSHServer(asyncssh.SSHServer):
    def __init__(self, db: RegistryDB, registry: Registry, config: RelayConfig):
        self.db = db
        self.registry = registry
        self.config = config
        self._conn: asyncssh.SSHServerConnection | None = None
        self._username = ""
        self._authorized_keys = self._load_authorized_keys()

    def _load_authorized_keys(self) -> set[bytes]:
        path = self.config.authorized_keys
        if not path or not os.path.exists(path):
            return set()
        keys: set[bytes] = set()
        with open(path, "r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                try:
                    key = asyncssh.import_public_key(line)
                    keys.add(key.export_public_key())
                except (asyncssh.KeyImportError, ValueError):
                    continue
        return keys

    def connection_made(self, conn) -> None:
        self._conn = conn

    def begin_auth(self, username: str) -> bool:
        self._username = username
        return True

    def password_auth_supported(self) -> bool:
        return True

    def validate_password(self, username: str, password: str) -> bool:
        if self.db.verify_relay_user(username, password):
            return True
        if self.config.allow_anonymous:
            log.warning("allowing anonymous SSH login for %r (allow_anonymous=true)", username)
            return True
        return False

    def public_key_auth_supported(self) -> bool:
        return bool(self._authorized_keys)

    def validate_public_key(self, username: str, key) -> bool:
        return key.export_public_key() in self._authorized_keys

    def session_requested(self) -> RelaySession:
        return RelaySession(self._username, self.db, self.registry, self.config)


async def start_ssh_server(db: RegistryDB, registry: Registry, config: RelayConfig):
    host_key = load_or_create_host_key(config.host_key)
    return await asyncssh.listen(
        config.ssh_host,
        config.ssh_port,
        server_factory=lambda: RelaySSHServer(db, registry, config),
        server_host_keys=[host_key],
        encoding=None,
        line_editor=False,
        line_echo=False,
        allow_scp=False,
        compression_algs=None,
    )
