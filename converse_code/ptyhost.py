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
from collections import deque

import pyte


class _Screen(pyte.Screen):
    def report_device_status(self, *args, **kwargs):
        # Claude Code sends private DSR queries (CSI ? 6 n) that pyte's
        # handler doesn't accept; we render read-only, so ignore them.
        pass

MAX_SCREEN_CSI_BYTES = 4096

class _ScreenByteFilter:
    """Drop terminal queries that pyte renders as visible text.

    Claude's TUI uses DCS-wrapped tmux queries and kitty keyboard-protocol
    negotiation. Real terminals consume them, but pyte does not implement
    those extensions and can paint their final bytes into the virtual screen.
    Keep the raw stream untouched for the user's terminal; only sanitize the
    copy used for snapshots and menu detection.
    """

    def __init__(self):
        self._state = "normal"
        self._sequence = bytearray()
        self.keyboard_flags = 0
        self.application_cursor_keys = False

    def feed(self, data: bytes) -> bytes:
        output = bytearray()
        for byte in data:
            if self._state == "normal":
                if byte == 0x1B:
                    self._state = "escape"
                    self._sequence = bytearray((byte,))
                elif byte == 0x90:  # 8-bit DCS
                    self._state = "dcs"
                else:
                    output.append(byte)
            elif self._state == "escape":
                if byte == ord("P"):
                    self._state = "dcs"
                    self._sequence.clear()
                elif byte == ord("["):
                    self._state = "csi"
                    self._sequence.append(byte)
                else:
                    self._sequence.append(byte)
                    output.extend(self._sequence)
                    self._sequence.clear()
                    self._state = "normal"
            elif self._state == "csi":
                self._sequence.append(byte)
                if len(self._sequence) > MAX_SCREEN_CSI_BYTES:
                    self._sequence.clear()
                    self._state = "normal"
                elif 0x40 <= byte <= 0x7E:
                    params = self._sequence[2:-1]
                    if params == b"?1" and byte in (ord("h"), ord("l")):
                        self.application_cursor_keys = byte == ord("h")
                    keyboard_control = (
                        byte == ord("u") and params[:1] in (b"<", b">")
                    )
                    if keyboard_control:
                        if params[:1] == b">":
                            try:
                                self.keyboard_flags = int(params[1:].split(b";", 1)[0] or b"0")
                            except ValueError:
                                self.keyboard_flags = 0
                        else:
                            self.keyboard_flags = 0
                    else:
                        output.extend(self._sequence)
                    self._sequence.clear()
                    self._state = "normal"
            elif self._state == "dcs":
                if byte == 0x1B:
                    self._state = "dcs_escape"
                elif byte == 0x9C:  # 8-bit ST
                    self._state = "normal"
                elif byte in (0x18, 0x1A):  # CAN/SUB abort a control string
                    self._state = "normal"
            elif self._state == "dcs_escape":
                self._state = (
                    "normal" if byte == ord("\\") or byte in (0x18, 0x1A) else "dcs"
                )
        return bytes(output)


MAX_INJECT_CHARS = 8000
INJECT_SUBMIT_DELAY_S = 0.05

STOP_SIGNAL_TIMEOUT_S = 2.0

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
    "ctrl-u": b"\x15",
    "s": b"s",
}


class ClaudeHost:
    def __init__(self, argv: list[str], env: dict | None = None, attach_terminal: bool = True):
        self.argv = argv
        self.env = env
        self.attach_terminal = attach_terminal
        self._pid: int | None = None
        self._master_fd: int | None = None
        self._saved_termios = None
        self._stdin_was_blocking: bool | None = None
        self._screen = _Screen(120, 40)
        self._stream = pyte.ByteStream(self._screen)
        self.screen_revision = 0
        self._screen_filter = _ScreenByteFilter()
        self.exited = asyncio.Event()
        self.returncode: int | None = None
        self._injection_queue: deque[tuple[bytes, float, bool, bytes]] = deque()
        self._dismiss_autocomplete = False
        self._command_suffix = b""
        self._injecting = False
        self._pending_write = bytearray()
        self._writer_registered = False
        self._pending_output = bytearray()
        self._output_writer_registered = False

    # -- lifecycle -----------------------------------------------------------

    async def start(self) -> None:
        if self.attach_terminal:
            cols, rows = os.get_terminal_size()
            self._screen.resize(rows, cols)
        else:
            cols, rows = self._screen.columns, self._screen.lines
        pid, master_fd = pty.fork()
        if pid == 0:  # child
            # Set the slave PTY size before exec, so Claude's very first TUI
            # paint sees the real dimensions. Waiting for the parent's later
            # SIGWINCH can leave the initial screen clipped until a relaunch.
            fcntl.ioctl(
                0, termios.TIOCSWINSZ,
                struct.pack("HHHH", rows, cols, 0, 0),
            )
            env = dict(os.environ if self.env is None else self.env)
            # If converse-code itself was launched from inside a Claude Code
            # session, the inherited child-session marker makes the wrapped
            # claude disable transcript saving — which our output path needs.
            # The wrapped claude is a fresh top-level session; drop the markers.
            env.pop("CLAUDE_CODE_CHILD_SESSION", None)
            env.pop("CLAUDECODE", None)
            if not self.attach_terminal:
                # A detached child owns a plain PTY, even when converse-code was
                # launched from tmux. Inheriting tmux identity makes Claude emit
                # focus/keyboard sequences for a terminal that is not present.
                env.pop("TMUX", None)
                env.pop("TMUX_PANE", None)
                env["TERM"] = "xterm-256color"
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
        screen_data = self._screen_filter.feed(data)
        if screen_data:
            self._stream.feed(screen_data)
            # A revision is an observation boundary, not a hash of visible text. Claude can
            # close and reopen an identical modal between polls; callers must not be able to
            # replay approval from the earlier instance just because both renders look alike.
            self.screen_revision += 1
        if self.attach_terminal:
            self._pending_output.extend(data)
            self._flush_terminal_output()

    def _flush_terminal_output(self) -> None:
        """Forward a complete Claude paint even when stdout accepts only part.

        Large initial TUI frames can be split by a terminal write. Dropping the
        unwritten suffix also drops ANSI cursor/layout commands, leaving a
        randomly malformed screen until Claude happens to repaint it.
        """
        fd = sys.stdout.fileno()
        while self._pending_output:
            try:
                written = os.write(fd, self._pending_output)
            except InterruptedError:
                continue
            except BlockingIOError:
                if not self._output_writer_registered:
                    asyncio.get_running_loop().add_writer(fd, self._flush_terminal_output)
                    self._output_writer_registered = True
                return
            except OSError:
                self._pending_output.clear()
                break
            if written <= 0:
                if not self._output_writer_registered:
                    asyncio.get_running_loop().add_writer(fd, self._flush_terminal_output)
                    self._output_writer_registered = True
                return
            del self._pending_output[:written]
        if self._output_writer_registered:
            asyncio.get_running_loop().remove_writer(fd)
            self._output_writer_registered = False

    def _on_terminal_input(self) -> None:
        try:
            data = os.read(sys.stdin.fileno(), 4096)
        except (BlockingIOError, InterruptedError):
            return
        if data:
            try:
                self._write(data)
            except OSError:
                pass

    def _finish(self) -> None:
        loop = asyncio.get_running_loop()
        loop.remove_reader(self._master_fd)
        if self._writer_registered:
            loop.remove_writer(self._master_fd)
            self._writer_registered = False
        if self._output_writer_registered:
            loop.remove_writer(sys.stdout.fileno())
            self._output_writer_registered = False
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
        self._pending_write.clear()
        self._pending_output.clear()
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
        if not self._pid or self.exited.is_set():
            return
        for sig in (signal.SIGHUP, signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(self._pid, sig)
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(self.exited.wait(), STOP_SIGNAL_TIMEOUT_S)
                return
            except asyncio.TimeoutError:
                continue
        raise TimeoutError(
            "Claude Code did not exit after SIGHUP, SIGTERM, and SIGKILL"
        )

    # -- injection & snapshots -----------------------------------------------

    def inject(self, text: str, submit_delay_s: float = INJECT_SUBMIT_DELAY_S) -> None:
        """Type an instruction into Claude Code and submit it.

        Text arrives from the far side of the WebSocket and is written to the
        dev's real controlling terminal, so control bytes are stripped first:
        an embedded ESC sequence would otherwise be a terminal-escape injection
        (prompt spoofing, OSC-52 clipboard writes) against the dev's emulator.
        """
        if self._master_fd is None:
            raise OSError("Claude Code session has exited")
        delay = max(INJECT_SUBMIT_DELAY_S, min(float(submit_delay_s), 2.0))
        self._injection_queue.append((sanitize(text).encode(), delay, False, b""))
        if not self._injecting:
            self._start_next_injection()

    def inject_command(self, text: str, submit_delay_s: float = 0.4) -> None:
        """Submit a slash command once, after dismissing its autocomplete.

        For argument commands, type the command name first, dismiss the command-name popup, then
        type the argument. Escaping after the full string can clear it; pressing Enter before
        dismissing can select an autocomplete row instead of executing the command.
        """
        delay = max(INJECT_SUBMIT_DELAY_S, min(float(submit_delay_s), 2.0))
        cleaned = sanitize(text)
        payload, separator, argument = cleaned.partition(" ")
        suffix = (separator + argument).encode() if separator else b""
        self._injection_queue.append((payload.encode(), delay, True, suffix))
        if not self._injecting:
            self._start_next_injection()

    def _start_next_injection(self) -> None:
        if self._master_fd is None or not self._injection_queue:
            self._injecting = False
            self._injection_queue.clear()
            return
        self._injecting = True
        (
            payload, submit_delay_s, self._dismiss_autocomplete, self._command_suffix,
        ) = self._injection_queue.popleft()
        self._write(payload)
        asyncio.get_running_loop().call_later(submit_delay_s, self._submit_injection)

    def _submit_injection(self) -> None:
        if self._dismiss_autocomplete:
            self._dismiss_autocomplete = False
            try:
                self._write(KEYMAP["escape"])
            except OSError:
                self._finish_injection()
                return
            asyncio.get_running_loop().call_later(0.1, self._submit_after_autocomplete)
            return
        self._submit_after_autocomplete()

    def _submit_after_autocomplete(self) -> None:
        if self._master_fd is None:
            self._injecting = False
            self._injection_queue.clear()
            return
        try:
            if self._command_suffix:
                suffix, self._command_suffix = self._command_suffix, b""
                self._write(suffix)
                asyncio.get_running_loop().call_later(0.1, self._submit_after_autocomplete)
                return
            self._write(b"\r")
        except OSError:
            self._injecting = False
            self._injection_queue.clear()
            return
        self._finish_injection()

    def _finish_injection(self) -> None:
        self._injecting = False
        if self._injection_queue:
            asyncio.get_running_loop().call_later(INJECT_SUBMIT_DELAY_S, self._start_next_injection)

    def send_key(self, name: str) -> None:
        data = KEYMAP[name]
        # Kitty keyboard protocol flag 8 asks the terminal to encode even plain
        # printable keys as CSI-u. Claude 2.1.227 uses this for single-key modal
        # choices such as the model-scope `s` shortcut.
        flags = self._screen_filter.keyboard_flags
        if name in {"up", "down"} and self._screen_filter.application_cursor_keys:
            data = b"\x1bOA" if name == "up" else b"\x1bOB"
        elif name == "s" and flags & 8:
            data = b"\x1b[115u"
        self._write(data)

    def _write(self, data: bytes) -> None:
        if self._master_fd is None:
            raise OSError("Claude Code session has exited")
        self._pending_write.extend(data)
        self._flush_writes()

    def _flush_writes(self) -> None:
        if self._master_fd is None:
            self._pending_write.clear()
            return
        while self._pending_write:
            try:
                written = os.write(self._master_fd, self._pending_write)
            except InterruptedError:
                continue
            except BlockingIOError:
                if not self._writer_registered:
                    asyncio.get_running_loop().add_writer(self._master_fd, self._flush_writes)
                    self._writer_registered = True
                return
            if written <= 0:
                if not self._writer_registered:
                    asyncio.get_running_loop().add_writer(self._master_fd, self._flush_writes)
                    self._writer_registered = True
                return
            del self._pending_write[:written]
        if self._writer_registered:
            asyncio.get_running_loop().remove_writer(self._master_fd)
            self._writer_registered = False

    def snapshot(self) -> list[str]:
        """Current rendered screen, one string per row."""
        return list(self._screen.display)
