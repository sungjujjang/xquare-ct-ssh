"""PTY backends used by the internal agent.

An agent session is a *real* pseudo terminal running the user's shell.  On
POSIX systems this is :func:`pty.fork`; on Windows it is ConPTY (via the
optional ``pywinpty`` package).  Both backends expose the same tiny surface::

    pty.write(bytes)          # keystrokes into the shell
    await pty.read() -> bytes # shell output (b"" == EOF)
    pty.resize(cols, rows)    # TIOCSWINSZ / ConPTY resize
    pty.close()
"""

from __future__ import annotations

import asyncio
import os
import shutil
import signal
import struct
import sys
from typing import Mapping, Sequence


def default_shell() -> str:
    if sys.platform == "win32":
        for candidate in ("pwsh.exe", "powershell.exe", "cmd.exe"):
            found = shutil.which(candidate)
            if found:
                return found
        return "cmd.exe"
    shell = os.environ.get("SHELL")
    if shell and os.path.exists(shell):
        return shell
    for candidate in ("/bin/bash", "/usr/bin/bash", "/bin/sh"):
        if os.path.exists(candidate):
            return candidate
    return "/bin/sh"


class UnixPty:
    """POSIX pseudo terminal backed by a master fd registered with the loop."""

    HIGH_WATER = 256
    LOW_WATER = 64
    READ_SIZE = 65536

    def __init__(
        self,
        argv: Sequence[str],
        cols: int,
        rows: int,
        env: Mapping[str, str],
        cwd: str | None = None,
    ):
        import fcntl
        import pty
        import termios

        self._fcntl = fcntl
        self._termios = termios
        self._loop = asyncio.get_running_loop()
        self._queue: asyncio.Queue[bytes | None] = asyncio.Queue()
        self._outbuf = b""
        self._reader_registered = False
        self._writer_registered = False
        self._eof = False
        self._closed = False

        pid, fd = pty.fork()
        if pid == 0:  # pragma: no cover - child never returns
            try:
                if cwd:
                    os.chdir(cwd)
                signal.signal(signal.SIGINT, signal.SIG_DFL)
                signal.signal(signal.SIGTERM, signal.SIG_DFL)
                os.execvpe(argv[0], list(argv), dict(env))
            except BaseException:
                os._exit(127)

        self.pid = pid
        self.fd = fd
        self.resize(cols, rows)
        os.set_blocking(fd, False)
        self._loop.add_reader(fd, self._on_readable)
        self._reader_registered = True

    # -- reading -----------------------------------------------------------
    def _on_readable(self) -> None:
        if self._closed:
            return
        try:
            data = os.read(self.fd, self.READ_SIZE)
        except BlockingIOError:
            return
        except OSError:
            data = b""
        if data:
            self._queue.put_nowait(data)
            if self._queue.qsize() >= self.HIGH_WATER:
                self._pause_reader()
        else:
            self._pause_reader()
            self._eof = True
            self._queue.put_nowait(None)

    def _pause_reader(self) -> None:
        if self._reader_registered:
            self._loop.remove_reader(self.fd)
            self._reader_registered = False

    def _resume_reader(self) -> None:
        if not self._reader_registered and not self._eof and not self._closed:
            self._loop.add_reader(self.fd, self._on_readable)
            self._reader_registered = True

    async def read(self) -> bytes:
        item = await self._queue.get()
        if item is None:
            return b""
        if not self._reader_registered and self._queue.qsize() <= self.LOW_WATER:
            self._resume_reader()
        return item

    # -- writing -----------------------------------------------------------
    def write(self, data: bytes) -> None:
        if self._closed or not data:
            return
        self._outbuf += data
        self._flush()

    def _flush(self) -> None:
        while self._outbuf:
            try:
                written = os.write(self.fd, self._outbuf)
            except BlockingIOError:
                if not self._writer_registered:
                    self._loop.add_writer(self.fd, self._on_writable)
                    self._writer_registered = True
                return
            except OSError:
                self.close()
                return
            if written <= 0:
                return
            self._outbuf = self._outbuf[written:]
        if self._writer_registered:
            self._loop.remove_writer(self.fd)
            self._writer_registered = False

    def _on_writable(self) -> None:
        self._flush()

    # -- resize / lifecycle ------------------------------------------------
    def resize(self, cols: int, rows: int) -> None:
        if self._closed:
            return
        try:
            winsize = struct.pack("HHHH", max(rows, 1), max(cols, 1), 0, 0)
            self._fcntl.ioctl(self.fd, self._termios.TIOCSWINSZ, winsize)
        except OSError:
            pass

    @property
    def eof(self) -> bool:
        return self._eof

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._pause_reader()
        if self._writer_registered:
            self._loop.remove_writer(self.fd)
            self._writer_registered = False
        try:
            os.close(self.fd)
        except OSError:
            pass
        self._signal_child()

    def _signal_child(self) -> None:
        for sig in (signal.SIGHUP, signal.SIGTERM):
            try:
                os.kill(self.pid, sig)
            except (ProcessLookupError, PermissionError):
                break
        try:
            asyncio.ensure_future(self._reap())
        except RuntimeError:  # pragma: no cover - loop already closed
            pass

    async def _reap(self) -> None:
        for _ in range(50):
            try:
                reaped, _status = os.waitpid(self.pid, os.WNOHANG)
            except ChildProcessError:
                return
            if reaped == self.pid:
                return
            await asyncio.sleep(0.02)
        try:
            os.kill(self.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass


class WindowsPty:
    """Windows ConPTY backend (requires the optional ``pywinpty`` package)."""

    READ_SIZE = 65536
    LOW_WATER = 64
    HIGH_WATER = 256

    def __init__(
        self,
        argv: Sequence[str],
        cols: int,
        rows: int,
        env: Mapping[str, str],
        cwd: str | None = None,
    ):
        try:
            import winpty  # type: ignore
        except ImportError as exc:  # pragma: no cover - windows only
            raise RuntimeError(
                "Windows support requires pywinpty (pip install pywinpty)"
            ) from exc

        self._loop = asyncio.get_running_loop()
        self._queue: asyncio.Queue[bytes | None] = asyncio.Queue()
        self._closed = False
        self._eof = False
        self._process = winpty.PtyProcess.spawn(
            list(argv),
            dimensions=(max(rows, 1), max(cols, 1)),
            env=dict(env),
            cwd=cwd,
        )
        self._loop.run_in_executor(None, self._blocking_reader)

    def _blocking_reader(self) -> None:  # pragma: no cover - windows only
        while not self._closed:
            try:
                data = self._process.read(self.READ_SIZE)
            except EOFError:
                break
            except Exception:
                break
            if not data:
                break
            self._loop.call_soon_threadsafe(self._queue.put_nowait, data)
            if self._queue.qsize() >= self.HIGH_WATER:
                # crude backpressure: sleep while the consumer catches up
                import time

                while self._queue.qsize() > self.LOW_WATER and not self._closed:
                    time.sleep(0.005)
        self._eof = True
        self._loop.call_soon_threadsafe(self._queue.put_nowait, None)

    async def read(self) -> bytes:
        item = await self._queue.get()
        if item is None:
            return b""
        return item

    def write(self, data: bytes) -> None:
        if self._closed or not data:
            return
        try:
            self._process.write(data.decode("utf-8", "replace"))
        except Exception:  # pragma: no cover - windows only
            self.close()

    def resize(self, cols: int, rows: int) -> None:
        try:
            self._process.setwinsize(max(rows, 1), max(cols, 1))
        except Exception:  # pragma: no cover - windows only
            pass

    @property
    def eof(self) -> bool:
        return self._eof

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._process.terminate(force=True)
        except Exception:  # pragma: no cover - windows only
            pass


def create_pty(
    shell: str,
    cols: int,
    rows: int,
    env: Mapping[str, str],
    cwd: str | None = None,
):
    argv: list[str] = [shell]
    if sys.platform == "win32":
        return WindowsPty(argv, cols, rows, env, cwd)
    return UnixPty(argv, cols, rows, env, cwd)
