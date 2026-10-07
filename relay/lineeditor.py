"""A small but real line editor for the C2 interactive menu.

It is deliberately hand written rather than using :mod:`readline` because the
input arrives from an SSH channel as arbitrary bytes and we must keep full
control over ANSI output.  It supports the editing keys an operator expects:

* printable input, UTF-8 aware
* Enter, Backspace
* Ctrl+C (cancel line), Ctrl+D (EOF), Ctrl+L (clear screen)
* Ctrl+A / Ctrl+E (home/end), Ctrl+U / Ctrl+K (kill line), Ctrl+W (kill word)
* Left/Right/Home/End/Delete arrow keys
* Up/Down command history
* Tab completion (delegated to a callback)

The editor never blocks: :meth:`LineEditor.feed` takes a chunk of bytes and
returns the list of completed command lines found in it.
"""

from __future__ import annotations

import codecs
from typing import Callable

ESC = "\x1b"

# control characters
_CTRL_A = "\x01"
_CTRL_C = "\x03"
_CTRL_D = "\x04"
_CTRL_E = "\x05"
_CTRL_K = "\x0b"
_CTRL_L = "\x0c"
_CTRL_U = "\x15"
_CTRL_W = "\x17"
_BACKSPACE = "\x7f"
_BS = "\x08"
_CR = "\r"
_LF = "\n"
_TAB = "\t"

Completer = Callable[[str, int], list[str]]


class LineEditor:
    def __init__(self, prompt: str, emit: Callable[[str], None], completer: Completer | None = None):
        self.prompt = prompt
        self._emit = emit
        self._completer = completer
        self._decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self.buffer: list[str] = []
        self.cursor = 0
        self.history: list[str] = []
        self._history_index: int | None = None
        self._saved_line: str = ""
        self.eof = False
        self.secret = False

        # escape parsing state
        self._esc: str | None = None

    # -- public api --------------------------------------------------------
    def set_prompt(self, prompt: str) -> None:
        self.prompt = prompt

    def reset(self) -> None:
        self.buffer = []
        self.cursor = 0
        self._history_index = None
        self._saved_line = ""
        self._esc = None

    def render_prompt(self) -> None:
        self._emit(self.prompt + self._visible_buffer())

    def feed(self, data: bytes) -> list[str]:
        text = self._decoder.decode(data)
        completed: list[str] = []
        for ch in text:
            self._feed_char(ch, completed)
        return completed

    def add_history(self, line: str) -> None:
        if line and (not self.history or self.history[-1] != line):
            self.history.append(line)

    # -- input handling ----------------------------------------------------
    def _feed_char(self, ch: str, completed: list[str]) -> None:
        if self._esc is not None:
            self._feed_escape(ch, completed)
            return

        if ch == ESC:
            self._esc = ESC
            return
        if ch in (_CR, _LF):
            self._emit("\r\n")
            line = "".join(self.buffer)
            self.reset()
            completed.append(line)
            return
        if ch in (_BACKSPACE, _BS):
            self._backspace()
            return
        if ch == _CTRL_C:
            self._emit("^C\r\n")
            self.reset()
            self.render_prompt()
            return
        if ch == _CTRL_D:
            if not self.buffer:
                self.eof = True
            return
        if ch == _CTRL_L:
            self._emit("\x1b[2J\x1b[H")
            self._redraw()
            return
        if ch == _CTRL_A:
            self.cursor = 0
            self._redraw()
            return
        if ch == _CTRL_E:
            self.cursor = len(self.buffer)
            self._redraw()
            return
        if ch == _CTRL_U:
            del self.buffer[: self.cursor]
            self.cursor = 0
            self._redraw()
            return
        if ch == _CTRL_K:
            del self.buffer[self.cursor:]
            self._redraw()
            return
        if ch == _CTRL_W:
            self._kill_word()
            return
        if ch == _TAB:
            self._complete(completed)
            return
        if ch < " ":
            # unknown control character - ignore
            return
        self._insert(ch)

    def _feed_escape(self, ch: str, completed: list[str]) -> None:
        assert self._esc is not None
        self._esc += ch
        seq = self._esc
        # CSI sequences: ESC [ ... <final 0x40-0x7e>
        if seq.startswith(ESC + "["):
            if len(seq) < 3:
                return
            final = seq[-1]
            if "@" <= final <= "~":
                self._handle_csi(seq[2:-1], final)
                self._esc = None
            return
        # SS3 sequences: ESC O <char>
        if seq.startswith(ESC + "O"):
            if len(seq) < 3:
                return
            self._handle_ss3(seq[2])
            self._esc = None
            return
        # Bare ESC followed by something else - drop it.
        self._esc = None

    def _handle_ss3(self, final: str) -> None:
        if final == "A":
            self._history_prev()
        elif final == "B":
            self._history_next()
        elif final == "C":
            self._move_right()
        elif final == "D":
            self._move_left()
        elif final == "H":
            self.cursor = 0
            self._redraw()
        elif final == "F":
            self.cursor = len(self.buffer)
            self._redraw()

    def _handle_csi(self, params: str, final: str) -> None:
        if final == "A":
            self._history_prev()
        elif final == "B":
            self._history_next()
        elif final == "C":
            self._move_right()
        elif final == "D":
            self._move_left()
        elif final == "H":
            self.cursor = 0
            self._redraw()
        elif final == "F":
            self.cursor = len(self.buffer)
            self._redraw()
        elif final == "~":
            code = params.split(";")[0]
            if code in ("1", "7"):
                self.cursor = 0
                self._redraw()
            elif code in ("4", "8"):
                self.cursor = len(self.buffer)
                self._redraw()
            elif code == "3":

                self._delete()
            # 200~ / 201~ are bracketed-paste markers - ignore.
        # other finals (R cursor report, Z shift-tab, ...) are ignored

    # -- editing primitives ------------------------------------------------
    def _insert(self, ch: str) -> None:
        self.buffer.insert(self.cursor, ch)
        self.cursor += 1
        if self.secret:
            self._redraw()
        else:
            self._emit(ch)

    def _backspace(self) -> None:
        if self.cursor == 0:
            return
        del self.buffer[self.cursor - 1]
        self.cursor -= 1
        if self.secret:
            self._redraw()
        else:
            self._emit("\b \b")

    def _delete(self) -> None:
        if self.cursor >= len(self.buffer):
            return
        del self.buffer[self.cursor]
        self._redraw()

    def _move_left(self) -> None:
        if self.cursor > 0:
            self.cursor -= 1
            self._emit("\x1b[D")

    def _move_right(self) -> None:
        if self.cursor < len(self.buffer):
            self.cursor += 1
            self._emit("\x1b[C")

    def _kill_word(self) -> None:
        while self.cursor > 0 and self.buffer[self.cursor - 1] == " ":
            del self.buffer[self.cursor - 1]
            self.cursor -= 1
        while self.cursor > 0 and self.buffer[self.cursor - 1] != " ":
            del self.buffer[self.cursor - 1]
            self.cursor -= 1
        self._redraw()

    # -- history -----------------------------------------------------------
    def _history_prev(self) -> None:
        if not self.history:
            return
        if self._history_index is None:
            self._saved_line = "".join(self.buffer)
            self._history_index = len(self.history) - 1
        elif self._history_index > 0:
            self._history_index -= 1
        text = self.history[self._history_index]
        self._set_line(text)

    def _history_next(self) -> None:
        if self._history_index is None:
            return
        if self._history_index < len(self.history) - 1:
            self._history_index += 1
            text = self.history[self._history_index]
        else:
            self._history_index = None
            text = self._saved_line
        self._set_line(text)

    def _set_line(self, text: str) -> None:
        self.buffer = list(text)
        self.cursor = len(self.buffer)
        self._redraw()

    # -- completion --------------------------------------------------------
    def _complete(self, completed: list[str]) -> None:
        if self._completer is None:
            return
        line = "".join(self.buffer)
        matches = self._completer(line, self.cursor)
        if not matches:
            return
        if len(matches) == 1:
            prefix_len = self._current_word_start()
            self.buffer[prefix_len : self.cursor] = list(matches[0])
            self.cursor = prefix_len + len(matches[0])
            self._redraw()
            return
        # multiple candidates: show them and redraw the line
        self._emit("\r\n")
        self._emit("  ".join(matches) + "\r\n")
        self._redraw()

    def _current_word_start(self) -> int:
        start = 0
        for i in range(self.cursor - 1, -1, -1):
            if self.buffer[i] == " ":
                start = i + 1
                break
        return start

    # -- rendering ---------------------------------------------------------
    def _visible_buffer(self) -> str:
        if self.secret:
            return "*" * len(self.buffer)
        return "".join(self.buffer)

    def _redraw(self) -> None:
        self._emit("\r\x1b[K" + self.prompt + self._visible_buffer())
        if not self.secret:
            tail = len(self.buffer) - self.cursor
            if tail > 0:
                self._emit(f"\x1b[{tail}D")
