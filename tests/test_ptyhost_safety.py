"""Injection sanitization and terminal-state restoration."""

import asyncio
import os
import sys
from pathlib import Path

import pytest

from converse_code.ptyhost import INJECT_SUBMIT_DELAY_S, MAX_INJECT_CHARS, ClaudeHost, sanitize

FAKE_TUI = str(Path(__file__).parent / "fake_tui.py")


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


def test_sanitize_caps_length():
    assert len(sanitize("x" * (MAX_INJECT_CHARS + 500))) == MAX_INJECT_CHARS


async def test_inject_separates_text_from_submit_keystroke():
    host = ClaudeHost(["unused"], attach_terminal=False)
    writes = []
    host._master_fd = 1
    host._write = writes.append

    host.inject("read the test file")

    assert writes == [b"read the test file"]
    await asyncio.sleep(INJECT_SUBMIT_DELAY_S * 2)
    assert writes == [b"read the test file", b"\r"]


async def test_concurrent_injections_are_serialized():
    host = ClaudeHost(["unused"], attach_terminal=False)
    writes = []
    host._master_fd = 1
    host._write = writes.append

    host.inject("first")
    host.inject("second")

    await asyncio.sleep(INJECT_SUBMIT_DELAY_S * 5)
    assert writes == [b"first", b"\r", b"second", b"\r"]


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
