"""In-memory registry of connected agents and their live shell sessions.

The registry is shared between the SSH server and the WebSocket server which
both run inside the same asyncio event loop.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import logging
from typing import Any, Protocol

from relay import protocol

log = logging.getLogger("relay.registry")


class AgentSink(Protocol):
    """Minimal send interface the bridge needs from a transport."""

    async def send_raw(self, data: bytes) -> None: ...

    async def close(self, code: int = 1000, reason: str = "") -> None: ...


_session_ids = itertools.count(1)


def allocate_session_id() -> int:
    return next(_session_ids)


class SessionBridge:
    """Bridges one SSH session to one agent PTY session.

    ``client`` only has to expose an async ``write(bytes)`` method - it is the
    SSH channel in production and a fake in tests.
    """

    def __init__(self, session_id: int, agent: "AgentConnection", client: Any):
        self.session_id = session_id
        self.agent = agent
        self.client = client
        self.opened: asyncio.Future[dict] | None = None
        self.closed = False
        self._close_callbacks: list[Any] = []

    # -- called from the SSH side -----------------------------------------
    async def send_input(self, data: bytes) -> None:
        if self.closed:
            return
        await self.agent.send(protocol.DATA, self.session_id, data)

    async def send_resize(self, cols: int, rows: int) -> None:
        if self.closed:
            return
        await self.agent.send(protocol.RESIZE, self.session_id, protocol._RESIZE.pack(cols, rows))

    async def close(self, reason: bytes = b"") -> None:
        if self.closed:
            return
        self.closed = True
        await self.agent.send(protocol.CLOSE, self.session_id, reason)
        self.agent.remove_session(self.session_id)
        await self._fire_callbacks()

    # -- called from the agent WebSocket side -----------------------------
    async def feed_client(self, data: bytes) -> None:
        if self.closed:
            return
        await self.client.write(data)

    async def on_agent_close(self, final: bytes) -> None:
        if self.closed:
            return
        self.closed = True
        if final:
            try:
                await self.client.write(final)
            except Exception:  # pragma: no cover - client may already be gone
                pass
        self.agent.remove_session(self.session_id)
        await self._fire_callbacks()

    def on_closed(self, callback) -> None:
        self._close_callbacks.append(callback)

    async def _fire_callbacks(self) -> None:
        callbacks, self._close_callbacks = self._close_callbacks, []
        for callback in callbacks:
            try:
                result = callback()
                if asyncio.iscoroutine(result):
                    await result
            except Exception:  # pragma: no cover - defensive
                log.exception("session close callback failed")


class AgentConnection:
    """A single agent WebSocket plus the sessions multiplexed over it."""

    def __init__(self, server_name: str, sink: AgentSink, info: dict[str, Any] | None = None):
        self.server_name = server_name
        self.sink = sink
        self.info = info or {}
        self.sessions: dict[int, SessionBridge] = {}
        self._send_lock = asyncio.Lock()
        self._closed = False
        self._info_pending: dict[int, asyncio.Future] = {}
        self.connected_at = asyncio.get_event_loop().time()

    async def send(self, msg_type: int, session_id: int = 0, payload: bytes | str = b"") -> None:
        if self._closed:
            raise ConnectionError("agent connection is closed")
        async with self._send_lock:
            await self.sink.send_raw(protocol.encode(msg_type, session_id, payload))

    def add_session(self, bridge: SessionBridge) -> None:
        self.sessions[bridge.session_id] = bridge

    def get_session(self, session_id: int) -> SessionBridge | None:
        return self.sessions.get(session_id)

    def remove_session(self, session_id: int) -> None:
        self.sessions.pop(session_id, None)

    async def request_sysinfo(self, cmd: str, args: dict[str, Any] | None = None, timeout: float = 10.0) -> dict[str, Any]:
        """Ask the agent for ``info``/``net``/``procs`` data and await the reply."""
        request_id = allocate_session_id()
        future: asyncio.Future = asyncio.get_event_loop().create_future()
        self._info_pending[request_id] = future
        try:
            await self.send(protocol.SYSINFO, request_id, json.dumps({"cmd": cmd, "args": args or {}}))
            return await asyncio.wait_for(future, timeout)
        except asyncio.TimeoutError:
            return {"cmd": cmd, "ok": False, "error": "agent did not respond in time", "data": None}
        finally:
            self._info_pending.pop(request_id, None)

    async def handle_frame(self, raw: bytes) -> None:
        """Dispatch a frame received from the agent."""
        msg_type, session_id, payload = protocol.decode(raw)
        if msg_type == protocol.OPENED:
            bridge = self.get_session(session_id)
            if bridge is not None and bridge.opened is not None and not bridge.opened.done():
                try:
                    bridge.opened.set_result(protocol.decode_json(payload))
                except protocol.ProtocolError:
                    bridge.opened.set_result({"ok": False, "error": "malformed OPENED"})
        elif msg_type == protocol.DATA:
            bridge = self.get_session(session_id)
            if bridge is not None:
                await bridge.feed_client(payload)
        elif msg_type == protocol.CLOSE:
            bridge = self.get_session(session_id)
            if bridge is not None:
                await bridge.on_agent_close(payload)
        elif msg_type == protocol.PING:
            await self.send(protocol.PONG, session_id, b"")
        elif msg_type == protocol.SYSINFO_RES:
            future = self._info_pending.pop(session_id, None)
            if future is not None and not future.done():
                try:
                    future.set_result(protocol.decode_json(payload))
                except protocol.ProtocolError:
                    future.set_result({"cmd": None, "ok": False, "error": "malformed SYSINFO_RES", "data": None})
        # PONG / EXIT / unknown frames are ignored.

    async def shutdown(self, reason: bytes = b"agent disconnected") -> None:
        self._closed = True
        for future in self._info_pending.values():
            if not future.done():
                future.set_result({"cmd": None, "ok": False, "error": "agent disconnected", "data": None})
        self._info_pending.clear()
        bridges = list(self.sessions.values())
        self.sessions.clear()
        for bridge in bridges:
            await bridge.on_agent_close(reason)

    @property
    def closed(self) -> bool:
        return self._closed


class Registry:
    def __init__(self) -> None:
        self._agents: dict[str, AgentConnection] = {}
        self._lock = asyncio.Lock()

    async def add(self, agent: AgentConnection) -> None:
        async with self._lock:
            previous = self._agents.get(agent.server_name)
            self._agents[agent.server_name] = agent
        if previous is not None:
            log.info("agent %s reconnected; evicting old connection", agent.server_name)
            await previous.shutdown(b"replaced by a newer agent connection")

    async def remove(self, agent: AgentConnection) -> None:
        async with self._lock:
            current = self._agents.get(agent.server_name)
            if current is agent:
                del self._agents[agent.server_name]

    def get(self, server_name: str) -> AgentConnection | None:
        return self._agents.get(server_name)

    def is_online(self, server_name: str) -> bool:
        agent = self._agents.get(server_name)
        return agent is not None and not agent.closed

    def online_servers(self) -> list[str]:
        return [name for name, agent in self._agents.items() if not agent.closed]

    def session_counts(self) -> dict[str, int]:
        return {
            name: len(agent.sessions)
            for name, agent in self._agents.items()
            if not agent.closed
        }

    async def disconnect(self, server_name: str, reason: bytes = b"disconnected by operator") -> bool:
        """Drop a live agent connection.  Returns True if one was connected."""
        agent = self._agents.get(server_name)
        if agent is None or agent.closed:
            return False
        await agent.shutdown(reason)
        closer = getattr(agent.sink, "close", None)
        if closer is not None:
            try:
                await closer(1000, "disconnected by operator")
            except Exception:  # pragma: no cover - transport may already be gone
                pass
        await self.remove(agent)
        return True

    async def shutdown(self) -> None:
        for agent in list(self._agents.values()):
            await agent.shutdown(b"relay shutting down")
        self._agents.clear()
