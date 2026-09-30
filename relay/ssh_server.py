"""SSH entry point and the C2 interactive shell.

Two ways to use the relay over SSH:

* **Operator / admin** - log in with a ``relay_users`` account.  The session
  runs the C2 command menu (a small line editor) with which servers can be
  created, connected to, disabled, removed, etc.
* **Direct user** - log in with the *server id* as the SSH username and the
  *server's login password* as the SSH password.  The session skips the menu
  and drops straight into that server's shell.

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
import re

import asyncssh

from relay import logs, protocol
from relay.config import RelayConfig
from relay.db import RegistryDB
from relay.lineeditor import LineEditor
from relay.manage import agent_command, install_command
from relay.registry import Registry, SessionBridge, allocate_session_id

log = logging.getLogger("relay.ssh")

_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,62}$")


def _human_bytes(value: object) -> str:
    if value is None:
        return "-"
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return str(value)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB", "PiB"):
        if abs(number) < 1024 or unit == "PiB":
            return f"{number:.0f} {unit}" if unit == "B" else f"{number:.1f} {unit}"
        number /= 1024
    return f"{number:.1f} PiB"


def _human_duration(seconds: object) -> str:
    if seconds is None:
        return "-"
    try:
        total = int(float(seconds))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return str(seconds)
    days, rem = divmod(total, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, _ = divmod(rem, 60)
    if days:
        return f"{days}d {hours}h {minutes}m"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


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
        conn: "asyncssh.SSHServerConnection | None" = None,
        direct_server: str | None = None,
    ):
        self.username = username
        self.db = db
        self.registry = registry
        self.config = config
        self._conn = conn
        # When set, the session bypasses the C2 menu and attaches straight to
        # this server (non-admin access via server id + login password).
        self.direct_server = direct_server

        self._chan: asyncssh.SSHServerChannel | None = None
        self._writer: ChannelWriter | None = None
        self._inbound: asyncio.Queue[bytes | None] = asyncio.Queue()
        self._task: asyncio.Task | None = None
        self._exec_command: str | None = None
        self._exec_target: str | None = None
        # Set once the client tells us whether it wants a shell or ran an exec
        # command, so we know whether to show the C2 menu or attach directly.
        self._session_mode_ready = asyncio.Event()

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
        self._session_mode_ready.set()
        return True

    def exec_requested(self, command: str) -> bool:
        # `ssh -t operator@relay attach <server>` (or just `<server>`) attaches
        # straight to that server's shell, skipping the C2 menu entirely.
        self._exec_command = command
        self._exec_target = self._parse_exec(command)
        self._session_mode_ready.set()
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
            if self.direct_server is not None:
                await self._attach_direct(self.direct_server)
            else:
                # Wait (briefly) for the client to tell us shell-vs-exec so we
                # don't flash the C2 menu before an exec attach.
                try:
                    await asyncio.wait_for(self._session_mode_ready.wait(), timeout=1.0)
                except asyncio.TimeoutError:
                    pass
                if self._exec_target is not None:
                    await self._attach_admin(self._exec_target)
                else:
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
                # The line editor already echoes each keystroke incrementally
                # (and redraws itself for Ctrl+C/arrows/history).  Only print a
                # fresh prompt once a command has completed, otherwise every
                # keypress would redraw the whole prompt+line.
                if lines and self._bridge is None and not self._exit_requested:
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
        elif command in ("sessions", "who"):
            self._print_sessions()
        elif command in ("add-server", "addserver", "register"):
            await self._cmd_add_server(args)
        elif command in ("login", "connect", "attach"):
            await self._cmd_login(args)
        elif command in ("enable", "start"):
            await self._cmd_set_enabled(args, True)
        elif command in ("disable", "stop"):
            await self._cmd_set_enabled(args, False)
        elif command in ("remove-server", "del-server", "delete"):
            await self._cmd_remove_server(args)
        elif command in ("kick", "disconnect"):
            await self._cmd_kick(args)
        elif command in ("reset-token", "rotate-token"):
            await self._cmd_reset_token(args)
        elif command in ("users", "admins"):
            self._print_users()
        elif command in ("add-user", "add-admin"):
            await self._cmd_add_user(args)
        elif command in ("remove-user", "del-user", "remove-admin"):
            await self._cmd_remove_user(args)
        elif command in ("passwd", "password", "passwd-me"):
            await self._cmd_passwd(args)
        elif command in ("reset-password", "reset-passwd", "set-user-password"):
            await self._cmd_reset_password(args)
        elif command in ("set-password", "server-password"):
            await self._cmd_set_server_password(args)
        elif command in ("info", "sysinfo"):
            await self._cmd_info(args)
        elif command in ("net", "netstat", "network"):
            await self._cmd_net(args)
        elif command in ("procs", "ps", "top"):
            await self._cmd_procs(args)
        elif command in ("overview", "fleet", "all"):
            await self._cmd_overview(args)
        elif command in ("logs", "log", "tail"):
            self._cmd_logs(args)
        elif command in ("whoami", "me"):
            self._emit_text(
                f"{self.username} (\x1b[32moperator\x1b[0m)\r\n"
                if self.direct_server is None
                else f"{self.username} (server)\r\n"
            )
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
            "  \x1b[36mlist\x1b[0m                        list known internal servers\r\n"
            "  \x1b[36msessions\x1b[0m                    show live session counts per server\r\n"
            "  \x1b[36madd-server <id> [pw]\x1b[0m        create a server and print its one-line installer\r\n"
            "  \x1b[36mlogin <server> [pw]\x1b[0m          attach to a server's shell (admin: no pw needed)\r\n"
            "  \x1b[36menable\x1b[0m <server>             allow the server / agent to connect\r\n"
            "  \x1b[36mdisable\x1b[0m <server>            block the server and drop its agent\r\n"
            "  \x1b[36mremove-server\x1b[0m <server>      delete a server and its credentials\r\n"
            "  \x1b[36mkick\x1b[0m <server>                drop the live agent connection\r\n"
            "  \x1b[36mreset-token\x1b[0m <server>          rotate the agent token and reprint the installer\r\n"
            "  \x1b[36moverview\x1b[0m (`fleet`)             all servers' resources at a glance\r\n"
            "  \x1b[36musers\x1b[0m                       list operator accounts\r\n"
            "  \x1b[36madd-user\x1b[0m <name> [pw]          create/update an operator account\r\n"
            "  \x1b[36mremove-user\x1b[0m <name>            delete an operator account\r\n"
            "  \x1b[36mpasswd\x1b[0m [pw]                   change your own operator password\r\n"
            "  \x1b[36mreset-password\x1b[0m <name> [pw]    set another operator's password\r\n"
            "  \x1b[36mset-password\x1b[0m <server> [pw]    change a server's login password\r\n"
            "  \x1b[36minfo\x1b[0m <server>                host, CPU, memory and disk summary\r\n"
            "  \x1b[36mnet\x1b[0m <server>                 interfaces, listening ports, connections\r\n"
            "  \x1b[36mprocs\x1b[0m <server> [n]             top n processes by CPU (default 15)\r\n"
            "  \x1b[36mlogs\x1b[0m [n]                    show the last n relay log lines\r\n"
            "  \x1b[36mwhoami\x1b[0m                      show which account you are using\r\n"
            "  \x1b[36mping\x1b[0m                        check the C2 CLI is responsive\r\n"
            "  \x1b[36mhelp\x1b[0m                        show this help\r\n"
            "  \x1b[36mexit\x1b[0m                        disconnect\r\n"
            "\r\n"
        )

    def _print_servers(self) -> None:
        servers = self.db.list_servers()
        if not servers:
            self._emit_text("No servers registered.\r\n")
            return
        counts = self.registry.session_counts()
        self._emit_text(
            "\r\n\x1b[1m  SERVER           STATUS     SESS  DESCRIPTION\x1b[0m\r\n"
        )
        for server in servers:
            online = self.registry.is_online(server.name)
            if not server.enabled:
                status = "\x1b[33mdisabled\x1b[0m"
            elif online:
                status = "\x1b[32monline  \x1b[0m"
            else:
                status = "\x1b[31moffline \x1b[0m"
            sessions = counts.get(server.name, 0) if online else 0
            self._emit_text(
                f"  {server.name:<16} {status} {sessions:>4}  {server.description}\r\n"
            )
        self._emit_text("\r\n")

    def _print_sessions(self) -> None:
        counts = {n: c for n, c in self.registry.session_counts().items() if c}
        if not counts:
            self._emit_text("No active sessions.\r\n")
            return
        self._emit_text("\r\n\x1b[1m  SERVER           ACTIVE SESSIONS\x1b[0m\r\n")
        for name in sorted(counts):
            self._emit_text(f"  {name:<16} {counts[name]}\r\n")
        self._emit_text("\r\n")

    def _print_users(self) -> None:
        users = self.db.list_relay_users()
        if not users:
            self._emit_text("No operator accounts.\r\n")
            return
        self._emit_text("\r\n\x1b[1m  OPERATOR\x1b[0m\r\n")
        for user in users:
            marker = " \x1b[32m(you)\x1b[0m" if user == self.username else ""
            self._emit_text(f"  {user}{marker}\r\n")
        self._emit_text("\r\n")

    def _default_host(self) -> str:
        if self.config.advertise_host:
            return self.config.advertise_host
        conn = self._conn
        if conn is not None:
            try:
                sockname = conn.get_extra_info("sockname")
            except Exception:  # pragma: no cover - defensive
                sockname = None
            if sockname:
                return sockname[0]
        return ""

    async def _cmd_add_server(self, args: list[str]) -> None:
        if not args:
            self._emit_text("usage: add-server <id> [password]\r\n")
            return
        name = args[0]
        if not _NAME_RE.match(name):
            self._emit_text(
                "\x1b[31minvalid server id\x1b[0m "
                "(lowercase letters, digits, '.', '_', '-'; start alphanumeric)\r\n"
            )
            return
        if self.db.get_server(name) is not None:
            self._emit_text(
                f"server {name} already exists; remove it with the manage CLI then retry\r\n"
            )
            return

        password = args[1] if len(args) > 1 else None
        if password is None:
            password = await self._read_password(f"Login password for new server {name}: ")
            if password is None:
                self._emit_text("cancelled\r\n")
                return
        if not password:
            self._emit_text("password must not be empty\r\n")
            return

        token = self.db.add_server(name, password, description="created from C2")
        host = self._default_host()
        self._emit_text(f"\r\n\x1b[32mServer '{name}' created.\x1b[0m\r\n\r\n")

        installer = install_command(self.config, name, token, host) if host else None
        if installer:
            self._emit_text(
                "Run this one-liner on the internal server (as root):\r\n\r\n"
                f"  \x1b[36m{installer}\x1b[0m\r\n\r\n"
                "It installs the agent, starts it now, and enables it on boot.\r\n"
                f"The server appears as \x1b[32monline\x1b[0m in 'list' once connected.\r\n"
            )
        else:
            self._emit_text(
                "Agent token (shown once - keep it secret):\r\n"
                f"  {token}\r\n\r\n"
                "On the internal server, run:\r\n"
                f"  {agent_command(self.config, name, token, host or None, None)}\r\n"
            )

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

        # Operators are already authenticated against the relay, so they may
        # attach without the per-server login password.  An explicit password is
        # still accepted (and verified) for scripted/back-compat use.
        if password is not None and not self.db.verify_server_login(name, password):
            self._emit_text("\x1b[31mauthentication failed\x1b[0m\r\n")
            return

        if await self._open_bridge(name):
            self._emit_text(f"\r\n\x1b[32mConnected to {name}\x1b[0m\r\n\r\n")

    @staticmethod
    def _parse_exec(command: str | None) -> str | None:
        """Interpret an SSH exec command as an admin attach target."""
        if not command:
            return None
        parts = command.strip().split()
        if not parts:
            return None
        if parts[0] in ("attach", "login", "connect") and len(parts) > 1:
            return parts[1]
        if len(parts) == 1 and _NAME_RE.match(parts[0]):
            return parts[0]
        return None

    async def _attach_admin(self, name: str) -> None:
        """Exec path: attach as an authenticated operator (no server password)."""
        server = self.db.get_server(name)
        if server is None:
            self._emit_text(f"\x1b[31munknown server:\x1b[0m {name}\r\n")
            self._exit_requested = True
            return
        if not server.enabled:
            self._emit_text(f"\x1b[31mserver {name} is disabled\x1b[0m\r\n")
            self._exit_requested = True
            return
        if not await self._open_bridge(name):
            self._exit_requested = True

    async def _attach_direct(self, name: str) -> None:
        """Non-admin path: go straight into the named server's shell."""
        server = self.db.get_server(name)
        if server is None:
            self._emit_text("\x1b[31mserver no longer exists\x1b[0m\r\n")
            self._exit_requested = True
            return
        if not server.enabled:
            self._emit_text(f"\x1b[31mserver {name} is disabled\x1b[0m\r\n")
            self._exit_requested = True
            return
        if not await self._open_bridge(name):
            self._exit_requested = True

    async def _open_bridge(self, name: str) -> bool:
        """Open a PTY session on ``name`` and mark the session as bridged."""
        agent = self.registry.get(name)
        if agent is None or agent.closed:
            self._emit_text(f"server {name} is offline (agent not connected)\r\n")
            return False

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
            return False

        try:
            result = await asyncio.wait_for(bridge.opened, timeout=self.config.open_timeout)
        except asyncio.TimeoutError:
            await bridge.close(b"")
            self._emit_text("timed out opening remote shell\r\n")
            return False

        if not result or not result.get("ok"):
            error = (result or {}).get("error") or "unknown error"
            await bridge.close(b"")
            self._emit_text(f"\x1b[31mfailed to open shell:\x1b[0m {error}\r\n")
            return False

        bridge.on_closed(self._on_bridge_closed)
        self._bridge = bridge
        return True

    def _on_bridge_closed(self) -> None:
        # The remote shell exited (or the agent dropped).
        self._bridge = None
        if self._chan is None:
            return
        self._emit_text("\r\n\x1b[33m[disconnected from remote shell]\x1b[0m\r\n")
        if self.direct_server is not None:
            # Direct sessions are single-use; end the connection.
            self._exit_requested = True
            self._inbound.put_nowait(None)
            return
        self._editor.reset()
        self._editor.render_prompt()

    async def _cmd_set_enabled(self, args: list[str], enabled: bool) -> None:
        if not args:
            self._emit_text("usage: enable|disable <server>\r\n")
            return
        name = args[0]
        if not self.db.set_server_enabled(name, enabled):
            self._emit_text(f"\x1b[31munknown server:\x1b[0m {name}\r\n")
            return
        verb = "enabled" if enabled else "disabled"
        self._emit_text(f"server {name} {verb}\r\n")
        if not enabled:
            if await self.registry.disconnect(name, b"disabled by operator"):
                self._emit_text(f"live agent for {name} disconnected\r\n")

    async def _cmd_remove_server(self, args: list[str]) -> None:
        if not args:
            self._emit_text("usage: remove-server <server>\r\n")
            return
        name = args[0]
        await self.registry.disconnect(name, b"server removed by operator")
        if not self.db.remove_server(name):
            self._emit_text(f"\x1b[31munknown server:\x1b[0m {name}\r\n")
            return
        self._emit_text(f"server {name} removed\r\n")

    async def _cmd_kick(self, args: list[str]) -> None:
        if not args:
            self._emit_text("usage: kick <server>\r\n")
            return
        name = args[0]
        if not await self.registry.disconnect(name, b"kicked by operator"):
            self._emit_text(f"server {name} is not connected\r\n")
            return
        self._emit_text(f"agent for {name} disconnected (it will reconnect)\r\n")

    async def _cmd_reset_token(self, args: list[str]) -> None:
        if not args:
            self._emit_text("usage: reset-token <server>\r\n")
            return
        name = args[0]
        token = self.db.rotate_server_token(name)
        if token is None:
            self._emit_text(f"\x1b[31munknown server:\x1b[0m {name}\r\n")
            return
        await self.registry.disconnect(name, b"agent token rotated")
        host = self._default_host()
        self._emit_text(f"\r\n\x1b[32mNew agent token for '{name}' (shown once):\x1b[0m\r\n\r\n")
        installer = install_command(self.config, name, token, host) if host else None
        if installer:
            self._emit_text(
                "Run this one-liner on the server to (re)install the agent:\r\n\r\n"
                f"  \x1b[36m{installer}\x1b[0m\r\n\r\n"
            )
        else:
            self._emit_text(f"  {token}\r\n\r\n")

    async def _cmd_add_user(self, args: list[str]) -> None:
        if not args:
            self._emit_text("usage: add-user <name> [password]\r\n")
            return
        name = args[0]
        password = args[1] if len(args) > 1 else None
        if password is None:
            password = await self._read_password(f"Password for operator {name}: ")
            if password is None:
                self._emit_text("cancelled\r\n")
                return
        if not password:
            self._emit_text("password must not be empty\r\n")
            return
        self.db.add_relay_user(name, password)
        self._emit_text(f"\x1b[32moperator '{name}' saved.\x1b[0m\r\n")

    async def _cmd_remove_user(self, args: list[str]) -> None:
        if not args:
            self._emit_text("usage: remove-user <name>\r\n")
            return
        name = args[0]
        if name == self.username:
            self._emit_text("\x1b[33mcannot remove the account you are logged in with\x1b[0m\r\n")
            return
        if not self.db.relay_user_exists(name):
            self._emit_text(f"\x1b[31munknown operator:\x1b[0m {name}\r\n")
            return
        if self.db.count_relay_users() <= 1:
            self._emit_text("\x1b[33mrefusing to remove the last operator account\x1b[0m\r\n")
            return
        self.db.remove_relay_user(name)
        self._emit_text(f"operator '{name}' removed\r\n")

    async def _cmd_passwd(self, args: list[str]) -> None:
        new = args[0] if args else None
        if new is None:
            new = await self._read_password("New password: ")
            if new is None:
                self._emit_text("cancelled\r\n")
                return
            confirm = await self._read_password("Confirm password: ")
            if confirm is None:
                self._emit_text("cancelled\r\n")
                return
            if confirm != new:
                self._emit_text("\x1b[31mpasswords do not match\x1b[0m\r\n")
                return
        if not new:
            self._emit_text("password must not be empty\r\n")
            return
        self.db.add_relay_user(self.username, new)
        self._emit_text("\x1b[32mpassword changed\x1b[0m (applies to your next login)\r\n")

    async def _cmd_reset_password(self, args: list[str]) -> None:
        if not args:
            self._emit_text("usage: reset-password <operator> [password]\r\n")
            return
        name = args[0]
        if not self.db.relay_user_exists(name):
            self._emit_text(f"\x1b[31munknown operator:\x1b[0m {name}\r\n")
            return
        new = args[1] if len(args) > 1 else None
        if new is None:
            new = await self._read_password(f"New password for operator {name}: ")
            if new is None:
                self._emit_text("cancelled\r\n")
                return
        if not new:
            self._emit_text("password must not be empty\r\n")
            return
        self.db.add_relay_user(name, new)
        self._emit_text(f"\x1b[32mpassword for operator '{name}' updated.\x1b[0m\r\n")

    async def _cmd_set_server_password(self, args: list[str]) -> None:
        if not args:
            self._emit_text("usage: set-password <server> [password]\r\n")
            return
        name = args[0]
        if self.db.get_server(name) is None:
            self._emit_text(f"\x1b[31munknown server:\x1b[0m {name}\r\n")
            return
        new = args[1] if len(args) > 1 else None
        if new is None:
            new = await self._read_password(f"New login password for server {name}: ")
            if new is None:
                self._emit_text("cancelled\r\n")
                return
        if not new:
            self._emit_text("password must not be empty\r\n")
            return
        self.db.set_server_login_password(name, new)
        self._emit_text(
            f"\x1b[32mlogin password for '{name}' updated.\x1b[0m "
            "(direct SSH login and 'login' use it)\r\n"
        )

    async def _sysinfo(self, name: str, cmd: str, args: dict | None = None) -> dict | None:
        agent = self.registry.get(name)
        if agent is None or agent.closed:
            self._emit_text(f"server {name} is offline (agent not connected)\r\n")
            return None
        self._emit_text(f"querying {name}...\r\n")
        try:
            reply = await agent.request_sysinfo(cmd, args)
        except (ConnectionError, OSError) as exc:
            self._emit_text(f"\x1b[31m{name}: {exc}\x1b[0m\r\n")
            return None
        if not reply or not reply.get("ok"):
            error = (reply or {}).get("error") or "request failed"
            self._emit_text(f"\x1b[31m{name}: {error}\x1b[0m\r\n")
            return None
        return reply.get("data") or {}

    async def _cmd_info(self, args: list[str]) -> None:
        if not args:
            self._emit_text("usage: info <server>\r\n")
            return
        name = args[0]
        data = await self._sysinfo(name, "info")
        if data is None:
            return
        self._print_info(name, data)

    async def _cmd_net(self, args: list[str]) -> None:
        if not args:
            self._emit_text("usage: net <server>\r\n")
            return
        name = args[0]
        data = await self._sysinfo(name, "net")
        if data is None:
            return
        self._print_net(name, data)

    async def _cmd_procs(self, args: list[str]) -> None:
        if not args:
            self._emit_text("usage: procs <server> [n]\r\n")
            return
        name = args[0]
        limit = 15
        if len(args) > 1:
            try:
                limit = max(1, min(int(args[1]), 100))
            except ValueError:
                pass
        data = await self._sysinfo(name, "procs", {"n": limit})
        if data is None:
            return
        self._print_procs(name, data)

    def _print_info(self, name: str, data: dict) -> None:
        self._emit_text(
            f"\r\n\x1b[1m{name}\x1b[0m  {data.get('hostname', '')}  {data.get('os', '')}\r\n"
        )
        ips = ", ".join(data.get("ips") or []) or "-"
        self._emit_text(f"  {'kernel':<10} {data.get('kernel', '')} ({data.get('arch', '')})\r\n")
        self._emit_text(f"  {'python':<10} {data.get('python', '')}\r\n")
        self._emit_text(f"  {'ips':<10} {ips}\r\n")
        self._emit_text(f"  {'uptime':<10} {_human_duration(data.get('uptime_seconds'))}\r\n")
        load = data.get("loadavg")
        load_s = " ".join(f"{x:g}" for x in load) if load else "-"
        cpu = data.get("cpu_percent")
        cpu_s = f"{cpu:.0f}%" if isinstance(cpu, (int, float)) else "-"
        self._emit_text(
            f"  {'load':<10} {load_s}   cpu {cpu_s}   {data.get('cpu_count') or '?'} cores\r\n"
        )
        mem = data.get("memory")
        if mem:
            self._emit_text(
                f"  {'memory':<10} {_human_bytes(mem.get('used'))} / {_human_bytes(mem.get('total'))} "
                f"({(mem.get('percent') or 0):.0f}%)\r\n"
            )
        swap = data.get("swap")
        if swap and swap.get("total"):
            self._emit_text(
                f"  {'swap':<10} {_human_bytes(swap.get('used'))} / {_human_bytes(swap.get('total'))} "
                f"({(swap.get('percent') or 0):.0f}%)\r\n"
            )
        for disk in data.get("disk") or []:
            self._emit_text(
                f"  {'disk':<10} {disk.get('path', ''):<16} {_human_bytes(disk.get('used'))} / "
                f"{_human_bytes(disk.get('total'))} ({(disk.get('percent') or 0):.0f}%)\r\n"
            )
        self._emit_text("\r\n")

    def _print_net(self, name: str, data: dict) -> None:
        self._emit_text(f"\r\n\x1b[1mInterfaces\x1b[0m ({name})\r\n")
        interfaces = data.get("interfaces") or []
        if not interfaces:
            self._emit_text("  (none reported)\r\n")
        else:
            self._emit_text(f"  {'NAME':<12} {'UP':<4} {'RX':>10} {'TX':>10}  ADDRS\r\n")
            for iface in interfaces:
                up = "yes" if iface.get("up") else ("no" if iface.get("up") is False else "?")
                addrs = ", ".join(iface.get("addrs") or [])
                self._emit_text(
                    f"  {str(iface.get('name', '')):<12} {up:<4} "
                    f"{_human_bytes(iface.get('rx_bytes')):>10} "
                    f"{_human_bytes(iface.get('tx_bytes')):>10}  {addrs}\r\n"
                )
        listening = data.get("listening") or []
        self._emit_text(f"\r\n\x1b[1mListening\x1b[0m ({len(listening)})\r\n")
        for row in listening[:30]:
            pid = row.get("pid")
            pid_s = f"pid {pid}" if pid else ""
            self._emit_text(
                f"  {str(row.get('proto', 'tcp')):<4} {str(row.get('laddr', '')):<24} "
                f"{pid_s:<9} {row.get('process') or ''}\r\n"
            )
        if len(listening) > 30:
            self._emit_text(f"  ... {len(listening) - 30} more\r\n")
        count = data.get("connection_count")
        if count is not None:
            self._emit_text(f"\r\n  connections: {count}\r\n")
        self._emit_text("\r\n")

    def _print_procs(self, name: str, data: dict) -> None:
        rows = data.get("processes") or []
        self._emit_text(f"\r\n\x1b[1mTop processes\x1b[0m ({name})\r\n")
        self._emit_text(f"  {'PID':>7}  {'CPU%':>5}  {'MEM%':>5}  {'USER':<12} NAME\r\n")
        for proc in rows:
            cpu = f"{proc['cpu_percent']:.1f}" if isinstance(proc.get("cpu_percent"), (int, float)) else "-"
            memp = (
                f"{proc['memory_percent']:.1f}"
                if isinstance(proc.get("memory_percent"), (int, float))
                else "-"
            )
            user = str(proc.get("username") or "")[:12]
            self._emit_text(
                f"  {str(proc.get('pid', '')):>7}  {cpu:>5}  {memp:>5}  {user:<12} {proc.get('name', '')}\r\n"
            )
        self._emit_text("\r\n")

    async def _fetch_info(self, name: str) -> dict | None:
        agent = self.registry.get(name)
        if agent is None or agent.closed:
            return None
        try:
            reply = await agent.request_sysinfo("info", timeout=8.0)
        except (ConnectionError, OSError):
            return None
        if not reply or not reply.get("ok"):
            return None
        return reply.get("data") or {}

    async def _cmd_overview(self, args: list[str]) -> None:
        servers = self.db.list_servers()
        if not servers:
            self._emit_text("No servers registered.\r\n")
            return
        online = [s.name for s in servers if s.enabled and self.registry.is_online(s.name)]
        if online:
            self._emit_text(f"gathering resources from {len(online)} online server(s)...\r\n")
        infos = await asyncio.gather(*(self._fetch_info(name) for name in online))
        self._print_overview(servers, dict(zip(online, infos)))

    def _print_overview(self, servers, infos: dict[str, dict | None]) -> None:
        self._emit_text(
            "\r\n\x1b[1m  "
            f"{'SERVER':<16} {'HOST':<16} {'IP':<15} {'UPTIME':<11} "
            f"{'LOAD':>5} {'CPU':>4} {'MEM':>4} {'DISK':>4}\x1b[0m\r\n"
        )
        for server in servers:
            if not server.enabled:
                self._emit_text(f"  {server.name:<16} \x1b[33m(disabled)\x1b[0m\r\n")
                continue
            data = infos.get(server.name)
            if data is None:
                self._emit_text(f"  {server.name:<16} \x1b[31m(offline)\x1b[0m\r\n")
                continue
            host = str(data.get("hostname") or "")[:16]
            ips = data.get("ips") or []
            ip = str(ips[0])[:15] if ips else "-"
            uptime = _human_duration(data.get("uptime_seconds"))
            load = data.get("loadavg") or []
            load_s = f"{load[0]:g}" if load else "-"
            cpu = data.get("cpu_percent")
    def _cmd_logs(self, args: list[str]) -> None:
        count = 100
        if args:
            try:
                count = max(1, min(int(args[0]), 500))
            except ValueError:
                self._emit_text("usage: logs [n]\r\n")
                return
        lines = logs.tail(count)
        if not lines:
            self._emit_text("No log lines buffered yet.\r\n")
            return
        self._emit_text("\r\n")
        for line in lines:
            self._emit_text(line + "\r\n")
        self._emit_text("\r\n")

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
        commands = [
            "help", "list", "sessions", "add-server", "login", "enable", "disable",
            "remove-server", "kick", "reset-token", "users", "add-user",
            "remove-user", "logs", "whoami", "ping", "exit",
        ]
        # completing the command itself
        if " " not in prefix:
            return [c for c in commands if c.startswith(prefix)]
        parts = prefix.split()
        command = parts[0]
        word = "" if prefix.endswith(" ") else parts[-1]
        if command in ("login", "connect", "attach", "enable", "disable", "remove-server",
                       "del-server", "delete", "kick", "reset-token", "rotate-token"):
            names = [s.name for s in self.db.list_servers()]
            return [n for n in names if n.startswith(word)]
        if command in ("remove-user", "del-user", "remove-admin"):
            names = self.db.list_relay_users()
            return [n for n in names if n.startswith(word)]
        return []


class RelaySSHServer(asyncssh.SSHServer):
    def __init__(self, db: RegistryDB, registry: Registry, config: RelayConfig):
        self.db = db
        self.registry = registry
        self.config = config
        self._conn: asyncssh.SSHServerConnection | None = None
        self._username = ""
        self._direct_server: str | None = None
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
        self._direct_server = None
        return True

    def password_auth_supported(self) -> bool:
        return True

    def validate_password(self, username: str, password: str) -> bool:
        # 1) operator/admin account -> the C2 CLI
        if self.db.verify_relay_user(username, password):
            self._direct_server = None
            return True
        # 2) server credentials -> straight into that server's shell
        if self.db.verify_server_login(username, password):
            self._direct_server = username
            log.info("direct SSH login for server %r", username)
            return True
        if self.config.allow_anonymous:
            log.warning("allowing anonymous SSH login for %r (allow_anonymous=true)", username)
            self._direct_server = None
            return True
        return False

    def public_key_auth_supported(self) -> bool:
        return bool(self._authorized_keys)

    def validate_public_key(self, username: str, key) -> bool:
        if key.export_public_key() in self._authorized_keys:
            self._direct_server = None
            return True
        return False

    def session_requested(self) -> RelaySession:
        return RelaySession(
            self._username,
            self.db,
            self.registry,
            self.config,
            self._conn,
            direct_server=self._direct_server,
        )


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
