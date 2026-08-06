"""ClaudeHost against the scripted fake TUI over a real pty."""

import asyncio
import sys
from pathlib import Path

import pytest

from converse_code.ptyhost import ClaudeHost
from converse_code.screen import detect_menu

FAKE_TUI = str(Path(__file__).parent / "fake_tui.py")


async def wait_for(predicate, timeout=5.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.05)
    return False


@pytest.fixture
async def host():
    h = ClaudeHost([sys.executable, FAKE_TUI], attach_terminal=False)
    await h.start()
    yield h
    if not h.exited.is_set():
        h.inject("exit")
        try:
            await asyncio.wait_for(h.exited.wait(), 5)
        except asyncio.TimeoutError:
            await h.stop()


def screen_text(host):
    return "\n".join(host.snapshot())


async def test_startup_and_echo(host):
    assert await wait_for(lambda: "Welcome to Fake Claude" in screen_text(host))
    host.inject("hello world")
    assert await wait_for(lambda: "echo: hello world" in screen_text(host))


async def test_menu_appears_on_screen_buffer(host):
    assert await wait_for(lambda: "> " in screen_text(host))
    host.inject("menu")
    assert await wait_for(lambda: detect_menu(host.snapshot()) is not None)
    menu = detect_menu(host.snapshot())
    assert menu.options[0] == "Yes"
    assert menu.selected == 0
    assert menu.title == "Do you want to proceed?"


async def test_multiline_injection_is_flattened(host):
    assert await wait_for(lambda: "> " in screen_text(host))
    host.inject("first line\nsecond line")
    assert await wait_for(lambda: "echo: first line second line" in screen_text(host))


async def test_clean_exit(host):
    assert await wait_for(lambda: "> " in screen_text(host))
    host.inject("exit")
    await asyncio.wait_for(host.exited.wait(), 5)
    assert host.returncode == 0
