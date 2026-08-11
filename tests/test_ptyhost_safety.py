"""Injection sanitization and terminal-state restoration."""

import asyncio
import os
import sys
from pathlib import Path

import pytest

from converse_code import ptyhost as ptymod
from converse_code.ptyhost import (
    INJECT_SUBMIT_DELAY_S,
    MAX_INJECT_CHARS,
    ClaudeHost,
    _ScreenByteFilter,
    sanitize,
)

FAKE_TUI = str(Path(__file__).parent / "fake_tui.py")
IGNORE_SIGNALS = str(Path(__file__).parent / "ignore_signals.py")


def test_sanitize_strips_escape_sequences():
    """Text from the wire reaches the dev's real terminal — ESC must not."""
    # ESC and BEL go; the printable payload is left as harmless literal text.
    assert sanitize("\x1b]0;pwned\x07hello") == "]0;pwnedhello"
    assert "\x1b" not in sanitize("\x1b[31mred\x1b[0m")
    assert sanitize("a\x00b\x07c\x7f") == "abc"
    assert sanitize("\x9bmalicious") == "malicious"  # C1 CSI


def test_sanitize_flattens_newlines_and_collapses_space():
    assert sanitize("first line\nsecond\r\nthird") == "first line second third"
    assert sanitize("  padded   out  ") == "padded out"


def test_sanitize_keeps_ordinary_and_unicode_text():
    assert sanitize("fix auth.py --flag 'x' → done") == "fix auth.py --flag 'x' → done"


def test_screen_filter_drops_claude_terminal_queries_across_chunks():
    filter_ = _ScreenByteFilter()
    chunks = [
        b"before\x1b[<",
        b"u\x1b[>1u\x1bPtmux;\x1b\x1b]11;?\x07\x1b",
        b"\\after\x1b[31mred\x1b[0m",
    ]

    assert b"".join(filter_.feed(chunk) for chunk in chunks) == (
        b"beforeafter\x1b[31mred\x1b[0m"
    )

def test_screen_filter_tracks_kitty_all_keys_mode():
    filter_ = _ScreenByteFilter()
    assert filter_.feed(b"\x1b[>9u") == b""
    assert filter_.keyboard_flags == 9
    assert filter_.feed(b"\x1b[<u") == b""
    assert filter_.keyboard_flags == 0


def test_send_key_encodes_plain_shortcut_in_kitty_all_keys_mode():
    host = ClaudeHost(["unused"], attach_terminal=False)
    writes = []
    host._master_fd = 1
    host._write = writes.append

    host.send_key("s")
    host.send_key("escape")
    host._screen_filter.feed(b"\x1b[>9u")
    host.send_key("s")
    host.send_key("escape")
    host.send_key("up")
    host.send_key("down")
    host._screen_filter.feed(b"\x1b[?1h")
    host.send_key("up")
    host.send_key("down")
    host._screen_filter.feed(b"\x1b[?1l")

    assert writes == [
        b"s", b"\x1b", b"\x1b[115u", b"\x1b",
        b"\x1b[A", b"\x1b[B", b"\x1bOA", b"\x1bOB",
    ]
    assert host._screen_filter.application_cursor_keys is False


def test_sanitize_caps_length():
    assert len(sanitize("x" * (MAX_INJECT_CHARS + 500))) == MAX_INJECT_CHARS


def test_screen_filter_recovers_from_aborted_dcs_and_oversized_csi():
    filter_ = _ScreenByteFilter()
    assert filter_.feed(b"before\x1bPunfinished") == b"before"
    assert filter_.feed(b"\x18after") == b"after"

    filter_ = _ScreenByteFilter()
    assert filter_.feed(b"\x1b[") == b""
    recovered = filter_.feed(b"1" * 5000 + b"visible")
    assert recovered.endswith(b"visible")
    assert filter_._state == "normal"
    assert filter_._sequence == b""




async def test_inject_separates_text_from_submit_keystroke():
    host = ClaudeHost(["unused"], attach_terminal=False)
    writes = []
    host._master_fd = 1
    host._write = writes.append

    host.inject("read the test file")

    assert writes == [b"read the test file"]
    await asyncio.sleep(INJECT_SUBMIT_DELAY_S * 2)
    assert writes == [b"read the test file", b"\r"]


async def test_inject_command_dismisses_autocomplete_then_submits_once():
    host = ClaudeHost(["unused"], attach_terminal=False)
    writes = []
    host._master_fd = 1
    host._write = writes.append

    host.inject_command("/model", submit_delay_s=INJECT_SUBMIT_DELAY_S)

    assert writes == [b"/model"]
    await asyncio.sleep(INJECT_SUBMIT_DELAY_S * 5)
    assert writes == [b"/model", b"\x1b", b"\r"]


async def test_inject_argument_command_dismisses_before_typing_argument():
    host = ClaudeHost(["unused"], attach_terminal=False)
    writes = []
    host._master_fd = 1
    host._write = writes.append

    host.inject_command("/model sonnet", submit_delay_s=INJECT_SUBMIT_DELAY_S)

    assert writes == [b"/model"]
    await asyncio.sleep(INJECT_SUBMIT_DELAY_S * 7)
    assert writes == [b"/model", b"\x1b", b" sonnet", b"\r"]


async def test_concurrent_injections_are_serialized():
    host = ClaudeHost(["unused"], attach_terminal=False)
    writes = []
    host._master_fd = 1
    host._write = writes.append

    host.inject("first")
    host.inject("second")

    await asyncio.sleep(INJECT_SUBMIT_DELAY_S * 5)
    assert writes == [b"first", b"\r", b"second", b"\r"]


async def test_concurrent_injections_arrive_as_complete_prompts():
    host = ClaudeHost([sys.executable, FAKE_TUI], attach_terminal=False)
    await host.start()
    try:
        host.inject("first")
        host.inject("second")
        deadline = asyncio.get_running_loop().time() + 5
        while "echo: second" not in "\n".join(host.snapshot()):
            assert asyncio.get_running_loop().time() < deadline
            await asyncio.sleep(0.05)
        screen = "\n".join(host.snapshot())
        assert "echo: first" in screen
        assert "echo: second" in screen
    finally:
        host.inject("exit")
        await asyncio.wait_for(host.exited.wait(), 5)


async def test_inject_writes_sanitized_bytes(tmp_path):
    host = ClaudeHost([sys.executable, FAKE_TUI], attach_terminal=False)
    await host.start()
    try:
        host.inject("\x1b[31mhello\x1b[0m")
        deadline = 0
        while deadline < 100 and "echo:" not in "\n".join(host.snapshot()):
            import asyncio

            await asyncio.sleep(0.05)
            deadline += 1
        text = "\n".join(host.snapshot())
        assert "echo: [31mhello[0m" in text
        assert "\x1b" not in text
    finally:
        host.inject("exit")
        import asyncio

        await asyncio.wait_for(host.exited.wait(), 5)


async def test_write_after_exit_raises_not_crashes(tmp_path):
    host = ClaudeHost([sys.executable, FAKE_TUI], attach_terminal=False)
    await host.start()
    host.inject("exit")
    import asyncio

    await asyncio.wait_for(host.exited.wait(), 5)
    with pytest.raises(OSError):
        host.inject("too late")


async def test_stop_before_start_is_a_noop():
    host = ClaudeHost(["unused"], attach_terminal=False)
    await host.stop()
    assert host._pid is None


async def test_stop_escalates_for_child_ignoring_hup_and_term(monkeypatch):
    monkeypatch.setattr(ptymod, "STOP_SIGNAL_TIMEOUT_S", 0.1)
    host = ClaudeHost([sys.executable, IGNORE_SIGNALS], attach_terminal=False)
    await host.start()
    deadline = asyncio.get_running_loop().time() + 3
    while "ignoring signals" not in "\n".join(host.snapshot()):
        assert asyncio.get_running_loop().time() < deadline
        await asyncio.sleep(0.02)

    await asyncio.wait_for(host.stop(), 2)
    assert host.exited.is_set()
    assert host.returncode is not None


def test_restore_terminal_restores_stdin_blocking_mode():
    """A pty wrapper that leaves stdin non-blocking breaks the parent shell."""
    host = ClaudeHost(["true"], attach_terminal=False)
    r, w = os.pipe()
    try:
        host._stdin_was_blocking = True
        real_stdin = sys.stdin
        sys.stdin = os.fdopen(r)
        os.set_blocking(sys.stdin.fileno(), False)
        assert os.get_blocking(sys.stdin.fileno()) is False
        host.restore_terminal()
        assert os.get_blocking(sys.stdin.fileno()) is True
        sys.stdin.close()
        sys.stdin = real_stdin
    finally:
        os.close(w)


def test_write_retries_partial_nonblocking_pty_writes(monkeypatch):
    host = ClaudeHost(["unused"], attach_terminal=False)
    host._master_fd = 123
    calls = []

    def partial_write(fd, data):
        calls.append((fd, bytes(data)))
        return min(2, len(data))

    monkeypatch.setattr(os, "write", partial_write)
    host._write(b"abcdef")

    assert calls == [(123, b"abcdef"), (123, b"cdef"), (123, b"ef")]


def test_child_output_retries_partial_terminal_writes(monkeypatch):
    """A large initial Claude paint must reach the terminal in full."""
    host = ClaudeHost(["unused"], attach_terminal=True)
    host._master_fd = 123
    writes = []

    monkeypatch.setattr(os, "read", lambda _fd, _size: b"abcdef")

    def partial_write(fd, data):
        writes.append((fd, bytes(data)))
        return min(2, len(data))

    monkeypatch.setattr(os, "write", partial_write)
    host._on_child_output()

    assert [data for _fd, data in writes] == [b"abcdef", b"cdef", b"ef"]
