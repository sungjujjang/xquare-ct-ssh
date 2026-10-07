"""End to end test: SSH client -> relay PTY -> WebSocket -> agent PTY -> shell.

This is the test that proves the transport is a raw byte stream and not an
``exec_command`` RPC: it drives an interactive bash, checks ANSI passthrough,
terminal resize propagation and control characters (Ctrl+C).
"""

import asyncio
import os
import shutil
import tempfile

import pytest

pytest.importorskip("asyncssh")
import asyncssh  # noqa: E402

from agent.agent import Agent, AgentConfig  # noqa: E402
from relay.config import RelayConfig  # noqa: E402
from relay.db import RegistryDB  # noqa: E402
from relay.registry import Registry  # noqa: E402
from relay.ssh_server import start_ssh_server  # noqa: E402
from relay.ws_server import start_ws_server  # noqa: E402

BASH = shutil.which("bash") or shutil.which("sh")
STTY = shutil.which("stty")


class Expect:
    """Sequential byte matcher that keeps unconsumed data for the next match."""

    def __init__(self, stream):
        self.stream = stream
        self.buf = b""

    async def until(self, marker: bytes, timeout: float = 15.0) -> bytes:
        async def _run() -> None:
            while marker not in self.buf:
                chunk = await self.stream.read(65536)
                if not chunk:
                    raise AssertionError(
                        f"stream closed while waiting for {marker!r}; got {self.buf!r}"
                    )
                self.buf += chunk
            end = self.buf.index(marker) + len(marker)
            self.buf = self.buf[end:]

        await asyncio.wait_for(_run(), timeout)
        return self.buf


async def _scenario() -> None:
    tmp = tempfile.mkdtemp(prefix="ctssh-")
    ssh_listener = ws_server = None
    try:
        config = RelayConfig()
        config.ssh_host = "127.0.0.1"
        config.ssh_port = 0
        config.ws_host = "127.0.0.1"
        config.ws_port = 0
        config.host_key = os.path.join(tmp, "host_key")
        config.authorized_keys = None
        config.db_path = os.path.join(tmp, "relay.db")
        config.open_timeout = 10

        db = RegistryDB(config.db_path)
        db.init_schema()
        db.add_relay_user("admin", "adminpass")
        db.add_server("server-001", "opsecret", token="testtoken")

        registry = Registry()
        ssh_listener = await start_ssh_server(db, registry, config)
        ws_server = await start_ws_server(db, registry, config)
        ssh_port = ssh_listener.get_port()
        ws_port = ws_server.sockets[0].getsockname()[1]

        agent = Agent(
            AgentConfig(
                relay_url=f"ws://127.0.0.1:{ws_port}/agent",
                server_id="server-001",
                token="testtoken",
                shell=BASH,
            )
        )
        agent_task = asyncio.ensure_future(agent.run())
        try:
            for _ in range(200):
                if registry.is_online("server-001"):
                    break
                await asyncio.sleep(0.05)
            assert registry.is_online("server-001"), "agent never registered"

            async with asyncssh.connect(
                "127.0.0.1",
                port=ssh_port,
                username="admin",
                password="adminpass",
                known_hosts=None,
            ) as conn:
                proc = await conn.create_process(
                    term_type="xterm-256color", term_size=(100, 30), encoding=None
                )
                out = Expect(proc.stdout)

                await out.until(b"C2>")
                # Ctrl+L clears the C2 screen
                proc.stdin.write(b"\x0c")
                await out.until(b"\x1b[2J\x1b[H")
                proc.stdin.write(b"login server-001 opsecret\n")
                await out.until(b"Connected to server-001")
                await out.until(b"$")

                # plain command echoed back through the real shell
                proc.stdin.write(b"echo HELLO_E2E_MARKER\n")
                await out.until(b"HELLO_E2E_MARKER")

                # `clear` runs for real and the shell survives it
                proc.stdin.write(b"clear; echo CLEARED_OK\n")
                await out.until(b"CLEARED_OK")

                # long output streams through without being buffered up front
                proc.stdin.write(b"seq 1 5000\n")
                await out.until(b"5000", timeout=10)

                # ANSI colour escapes pass through untouched
                proc.stdin.write(b"printf '\\033[31mRED_MARKER\\033[0m\\n'\n")
                await out.until(b"\x1b[31mRED_MARKER\x1b[0m")

                # terminal resize is propagated to the remote PTY
                if STTY:
                    proc.stdin.write(b"stty size\n")
                    await out.until(b"30 100")
                    proc.change_terminal_size(120, 40)
                    await asyncio.sleep(0.4)
                    proc.stdin.write(b"stty size\n")
                    await out.until(b"40 120")

                # Ctrl+C interrupts a foreground process
                proc.stdin.write(b"sleep 5\n")
                await asyncio.sleep(0.3)
                proc.stdin.write(b"\x03")
                await asyncio.sleep(0.2)
                proc.stdin.write(b"echo AFTER_CTRL_C\n")
                await out.until(b"AFTER_CTRL_C", timeout=3.0)

                # exiting the remote shell returns to the C2 CLI
                proc.stdin.write(b"exit\n")
                await out.until(b"disconnected from remote shell")
                await out.until(b"C2>")
                proc.stdin.write(b"exit\n")
                await asyncio.sleep(0.2)
        finally:
            agent.stop()
            agent_task.cancel()
            try:
                await agent_task
            except asyncio.CancelledError:
                pass
    finally:
        if ssh_listener is not None:
            ssh_listener.close()
            await ssh_listener.wait_closed()
        if ws_server is not None:
            ws_server.close()
            await ws_server.wait_closed()
        shutil.rmtree(tmp, ignore_errors=True)


def test_end_to_end_interactive_shell() -> None:
    if BASH is None:
        pytest.skip("no shell available")
    asyncio.run(_scenario())
