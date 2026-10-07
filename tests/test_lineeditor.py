from relay.lineeditor import LineEditor


class Harness:
    def __init__(self, completer=None):
        self.output = ""
        self.editor = LineEditor("C2> ", self._emit, completer)
        self.editor.render_prompt()

    def _emit(self, text):
        self.output += text

    def send(self, text):
        return self.editor.feed(text.encode("utf-8"))


def test_simple_line():
    h = Harness()
    assert h.send("hello") == []
    assert h.send("\r") == ["hello"]
    assert h.editor.buffer == []


def test_backspace():
    h = Harness()
    h.send("hellp")
    h.send("\x7f")  # backspace
    completed = h.send("o\r")
    assert completed == ["hello"]


def test_ctrl_u_kills_line():
    h = Harness()
    h.send("garbage")
    h.send("\x15")  # ctrl-u
    assert h.editor.buffer == []
    assert h.send("login x\r") == ["login x"]


def test_history_navigation():
    h = Harness()
    h.send("first\r")
    h.send("second\r")
    h.editor.add_history("first")
    h.editor.add_history("second")
    h.send("\x1b[A")  # up
    assert "".join(h.editor.buffer) == "second"
    h.send("\x1b[A")  # up again
    assert "".join(h.editor.buffer) == "first"
    h.send("\x1b[B")  # down
    assert "".join(h.editor.buffer) == "second"


def test_cursor_movement_and_insert():
    h = Harness()
    h.send("hlo")
    h.send("\x1b[D")  # left
    h.send("\x1b[D")  # left
    h.send("el")
    h.send("\x05")  # ctrl-e (end)
    completed = h.send("!\r")
    assert completed == ["hello!"]


def test_ctrl_d_sets_eof_on_empty_line():
    h = Harness()
    h.send("\x04")
    assert h.editor.eof is True


def test_tab_completion_single_match():
    h = Harness(completer=lambda line, cursor: ["login"])
    h.send("log\t")
    assert "".join(h.editor.buffer) == "login"


def test_tab_completion_multiple_shows_candidates():
    h = Harness(completer=lambda line, cursor: ["help", "helper"])
    h.send("\t")
    assert "help" in h.output and "helper" in h.output


def test_utf8_multibyte():
    h = Harness()
    h.send("로그인")
    assert "".join(h.editor.buffer) == "로그인"


def test_ctrl_c_resets_line():
    h = Harness()
    h.send("partial")
    h.send("\x03")
    assert h.editor.buffer == []
    assert "^C" in h.output
