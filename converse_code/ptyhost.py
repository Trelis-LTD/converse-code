"""Transparent pty wrapper around the interactive `claude` CLI.

The dev's terminal stays fully interactive — keystrokes pass straight through
and the TUI renders normally. On the way through, output bytes also feed an
in-process pyte terminal emulator so the tool router can snapshot the rendered
screen (menu detection), and the router can inject keystrokes that are
indistinguishable from typing.
"""

import asyncio
import fcntl
import os
import pty
import signal
import struct
import sys
import termios
import tty

import pyte


class _Screen(pyte.Screen):
    def report_device_status(self, *args, **kwargs):
        # Claude Code sends private DSR queries (CSI ? 6 n) that pyte's
        # handler doesn't accept; we render read-only, so ignore them.
        pass


MAX_INJECT_CHARS = 8000


def sanitize(text: str) -> str:
    """Collapse newlines to spaces and drop control characters (incl. ESC)."""
    flat = text.replace("\r", "\n").replace("\n", " ")
    stripped = "".join(
        ch for ch in flat
        if ch == "\t" or (ord(ch) >= 0x20 and ord(ch) != 0x7F and not 0x80 <= ord(ch) <= 0x9F)
    )
    return " ".join(stripped.split())[:MAX_INJECT_CHARS]


KEYMAP = {
    "escape": b"\x1b",
    "enter": b"\r",
    "up": b"\x1b[A",
    "down": b"\x1b[B",
    "left": b"\x1b[D",
    "right": b"\x1b[C",
    "tab": b"\t",
    "shift-tab": b"\x1b[Z",
    "ctrl-c": b"\x03",
}


class ClaudeHost:
    def __init__(self, argv: list[str], env: dict | None = None, attach_terminal: bool = True,
                 cols: int = 120, rows: int = 40):
        self.argv = argv
        self.env = env
        self.attach_terminal = attach_terminal
        self._pid: int | None = None
        self._master_fd: int | None = None
        self._saved_termios = None
        self._stdin_was_blocking: bool | None = None
        self._screen = _Screen(cols, rows)
        self._stream = pyte.ByteStream(self._screen)
        self.exited = asyncio.Event()
        self.returncode: int | None = None

    # -- lifecycle -----------------------------------------------------------

    async def start(self) -> None:
        if self.attach_terminal:
            cols, rows = os.get_terminal_size()
            self._screen.resize(rows, cols)
        pid, master_fd = pty.fork()
        if pid == 0:  # child
            env = dict(os.environ if self.env is None else self.env)
            # If converse-code itself was launched from inside a Claude Code
            # session, the inherited child-session marker makes the wrapped
            # claude disable transcript saving — which our output path needs.
            # The wrapped claude is a fresh top-level session; drop the markers.
            env.pop("CLAUDE_CODE_CHILD_SESSION", None)
            env.pop("CLAUDECODE", None)
            try:
                os.execvpe(self.argv[0], self.argv, env)
            except OSError:
                os._exit(127)
        self._pid, self._master_fd = pid, master_fd
        os.set_blocking(master_fd, False)

        loop = asyncio.get_running_loop()
        loop.add_reader(master_fd, self._on_child_output)
        if self.attach_terminal:
            self._saved_termios = termios.tcgetattr(sys.stdin.fileno())
            self._stdin_was_blocking = os.get_blocking(sys.stdin.fileno())
            tty.setraw(sys.stdin.fileno())
            os.set_blocking(sys.stdin.fileno(), False)
            loop.add_reader(sys.stdin.fileno(), self._on_terminal_input)
            signal.signal(signal.SIGWINCH, lambda *_: self._sync_winsize())
            self._sync_winsize()

    def _sync_winsize(self) -> None:
        cols, rows = os.get_terminal_size()
        fcntl.ioctl(self._master_fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
        self._screen.resize(rows, cols)

    def _on_child_output(self) -> None:
        try:
            data = os.read(self._master_fd, 65536)
        except (BlockingIOError, InterruptedError):
            return
        except OSError:
            data = b""
        if not data:
            self._finish()
            return
        self._stream.feed(data)
        if self.attach_terminal:
            os.write(sys.stdout.fileno(), data)

    def _on_terminal_input(self) -> None:
        try:
            data = os.read(sys.stdin.fileno(), 4096)
        except (BlockingIOError, InterruptedError):
            return
        if data:
            os.write(self._master_fd, data)

    def _finish(self) -> None:
        loop = asyncio.get_running_loop()
        loop.remove_reader(self._master_fd)
        if self.attach_terminal:
            loop.remove_reader(sys.stdin.fileno())
        if self._pid:
            try:
                _, status = os.waitpid(self._pid, 0)
                self.returncode = os.waitstatus_to_exitcode(status)
            except ChildProcessError:
                self.returncode = -1
        if self._master_fd is not None:
            try:
                os.close(self._master_fd)
            except OSError:
                pass
            self._master_fd = None
        self.restore_terminal()
        self.exited.set()

    def restore_terminal(self) -> None:
        """Undo everything start() did to the dev's terminal — including stdin's
        blocking mode, or the shell we return to sees spurious EAGAIN reads."""
        if self._saved_termios is not None:
            termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, self._saved_termios)
            self._saved_termios = None
        if self._stdin_was_blocking is not None:
            os.set_blocking(sys.stdin.fileno(), self._stdin_was_blocking)
            self._stdin_was_blocking = None

    async def stop(self) -> None:
        if self._pid and not self.exited.is_set():
            try:
                os.kill(self._pid, signal.SIGHUP)
            except ProcessLookupError:
                pass
        await self.exited.wait()

    # -- injection & snapshots -----------------------------------------------

    def inject(self, text: str) -> None:
        """Type an instruction into Claude Code and submit it.

        Text arrives from the far side of the WebSocket and is written to the
        dev's real controlling terminal, so control bytes are stripped first:
        an embedded ESC sequence would otherwise be a terminal-escape injection
        (prompt spoofing, OSC-52 clipboard writes) against the dev's emulator.
        """
        self._write(sanitize(text).encode() + b"\r")

    def send_key(self, name: str) -> None:
        self._write(KEYMAP[name])

    def _write(self, data: bytes) -> None:
        if self._master_fd is None:
            raise OSError("Claude Code session has exited")
        os.write(self._master_fd, data)

    def snapshot(self) -> list[str]:
        """Current rendered screen, one string per row."""
        return list(self._screen.display)
