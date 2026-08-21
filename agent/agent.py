"""Internal agent: connects out to the relay and serves real PTY shells.

The agent dials the relay (so no inbound firewall rule is needed), authenticates
with its server id + token, and then keeps the WebSocket open.  When an operator
logs in via the C2 CLI the relay sends ``OPEN``; the agent forks a PTY running
the shell and streams bytes in both directions until the shell exits.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import platform
import socket
from dataclasses import dataclass, field

import websockets
from websockets.exceptions import ConnectionClosed, InvalidHandshake, InvalidStatus

from agent import protocol, sysinfo
from agent.pty_backend import create_pty, default_shell

log = logging.getLogger("agent")


class AuthError(Exception):
    pass


@dataclass
class AgentConfig:
    relay_url: str
    server_id: str
    token: str
    shell: str | None = None
    cwd: str | None = None
    verify_tls: bool = True
    ping_interval: float = 20.0
    ping_timeout: float = 20.0
    reconnect_min: float = 1.0
    reconnect_max: float = 30.0
    env: dict[str, str] = field(default_factory=dict)


class AgentSession:
    def __init__(
        self,
        agent: "Agent",
        session_id: int,
        shell: str,
        cols: int,
        rows: int,
        env: dict[str, str],
        cwd: str | None,
    ):
        self.agent = agent
        self.session_id = session_id
        self.pty = create_pty(shell, cols, rows, env, cwd)
        self._task: asyncio.Task | None = None
        self._closed = False

    def start(self) -> None:
        self._task = asyncio.ensure_future(self._pump())

    async def _pump(self) -> None:
        try:
            while True:
                data = await self.pty.read()
                if not data:
                    break
                await self.agent.send(protocol.DATA, self.session_id, data)
        except asyncio.CancelledError:  # pragma: no cover - shutdown
            raise
        except (ConnectionClosed, ConnectionError):
            pass
        except Exception:  # pragma: no cover - defensive
            log.exception("session %s pump failed", self.session_id)
        finally:
            await self._finish()

    async def _finish(self) -> None:
        if self._closed:
            return
        self._closed = True
        self.agent.sessions.pop(self.session_id, None)
        self.pty.close()
        try:
            await self.agent.send(protocol.CLOSE, self.session_id, b"")
        except (ConnectionClosed, ConnectionError):
            pass

    def write(self, data: bytes) -> None:
        self.pty.write(data)

    def resize(self, cols: int, rows: int) -> None:
        self.pty.resize(cols, rows)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self.pty.close()
        if self._task is not None:
            self._task.cancel()


class Agent:
    def __init__(self, config: AgentConfig):
        self.config = config
        self.sessions: dict[int, AgentSession] = {}
        self._ws = None
        self._send_lock = asyncio.Lock()
        self._stop = False

    async def send(self, msg_type: int, session_id: int = 0, payload: bytes | str = b"") -> None:
        async with self._send_lock:
            ws = self._ws
            if ws is None:
                return
            await ws.send(protocol.encode(msg_type, session_id, payload))

    async def run(self) -> None:
        backoff = self.config.reconnect_min
        while not self._stop:
            try:
                await self._connect_and_serve()
                backoff = self.config.reconnect_min
            except AuthError as exc:
                log.error("authentication rejected by relay: %s", exc)
                backoff = self.config.reconnect_max
            except (ConnectionClosed, OSError, InvalidHandshake, InvalidStatus, asyncio.TimeoutError) as exc:
                log.warning("relay connection lost: %s", exc)
            except Exception:  # pragma: no cover - defensive
                log.exception("unexpected agent error")
            if self._stop:
                break
            log.info("reconnecting in %.1fs", backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, self.config.reconnect_max)

    async def _connect_and_serve(self) -> None:
        log.info("connecting to relay %s as %s", self.config.relay_url, self.config.server_id)
        ssl_arg = None
        async with websockets.connect(
            self.config.relay_url,
            max_size=protocol.MAX_FRAME_SIZE,
            ping_interval=self.config.ping_interval,
            ping_timeout=self.config.ping_timeout,
            open_timeout=15,
            proxy=None,
            ssl=ssl_arg,
        ) as ws:
            self._ws = ws
            try:
                await self._authenticate(ws)
                log.info("authenticated; serving sessions")
                await self._message_loop(ws)
            finally:
                self._ws = None
                await self._close_all_sessions()

    async def _authenticate(self, ws) -> None:
        info = {
            "id": self.config.server_id,
            "token": self.config.token,
            "version": protocol.PROTOCOL_VERSION,
            "hostname": socket.gethostname(),
            "os": platform.platform(),
            "python": platform.python_version(),
            "ips": sysinfo.local_ips(),
        }
        await ws.send(protocol.encode(protocol.AUTH, 0, json.dumps(info)))
        raw = await asyncio.wait_for(ws.recv(), timeout=15)
        if isinstance(raw, str):
            raw = raw.encode("utf-8")
        msg_type, _sid, payload = protocol.decode(raw)
        if msg_type == protocol.AUTH_OK:
            return
        if msg_type == protocol.AUTH_FAIL:
            raise AuthError(payload.decode("utf-8", "replace"))
        raise AuthError(f"unexpected reply {protocol.type_name(msg_type)}")

    async def _message_loop(self, ws) -> None:
        async for raw in ws:
            if isinstance(raw, str):
                raw = raw.encode("utf-8")
            try:
                msg_type, session_id, payload = protocol.decode(raw)
            except protocol.ProtocolError as exc:
                log.warning("dropping malformed frame: %s", exc)
                continue

            if msg_type == protocol.OPEN:
                await self._open_session(session_id, payload)
            elif msg_type == protocol.DATA:
                session = self.sessions.get(session_id)
                if session is not None:
                    session.write(payload)
            elif msg_type == protocol.RESIZE:
                session = self.sessions.get(session_id)
                if session is not None:
                    try:
                        cols, rows = protocol.decode_resize(payload)
                        session.resize(cols, rows)
                    except protocol.ProtocolError:
                        pass
            elif msg_type == protocol.CLOSE:
                session = self.sessions.pop(session_id, None)
                if session is not None:
                    await session.close()
            elif msg_type == protocol.PING:
                await self.send(protocol.PONG, session_id, b"")
            elif msg_type == protocol.SYSINFO:
                await self._handle_sysinfo(session_id, payload)
            # AUTH_OK/PONG/unknown are ignored.

    async def _handle_sysinfo(self, session_id: int, payload: bytes) -> None:
        request: dict = {}
        try:
            request = protocol.decode_json(payload) if payload else {}
        except protocol.ProtocolError:
            pass
        cmd = request.get("cmd") or ""
        args = request.get("args") or {}
        try:
            data = sysinfo.collect(cmd, args)
            reply = {"cmd": cmd, "ok": True, "error": None, "data": data}
        except Exception as exc:
    async def _open_session(self, session_id: int, payload: bytes) -> None:
        try:
            options = protocol.decode_json(payload) if payload else {}
        except protocol.ProtocolError:
            options = {}
        shell = options.get("shell") or self.config.shell or default_shell()
        cols = int(options.get("cols") or 80)
        rows = int(options.get("rows") or 24)
        env = dict(os.environ)
        env.update(self.config.env)
        env.update(options.get("env") or {})
        env.setdefault("TERM", "xterm-256color")
        cwd = options.get("cwd") or self.config.cwd
        try:
            session = AgentSession(self, session_id, shell, cols, rows, env, cwd)
        except Exception as exc:
            log.exception("failed to spawn shell for session %s", session_id)
            await self.send(protocol.OPENED, session_id, json.dumps({"ok": False, "error": str(exc)}))
            return

        self.sessions[session_id] = session
        await self.send(protocol.OPENED, session_id, json.dumps({"ok": True, "error": None}))
        session.start()
        log.info("opened session %s (%s, %dx%d)", session_id, shell, cols, rows)

    async def _close_all_sessions(self) -> None:
        sessions = list(self.sessions.values())
        self.sessions.clear()
        for session in sessions:
            await session.close()

    def stop(self) -> None:
        self._stop = True
