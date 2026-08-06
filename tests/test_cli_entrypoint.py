"""Run the real console-script entry point as a subprocess.

The unit tests exercise modules directly, which meant `main()` itself — argument
parsing, logging setup, startup ordering — had no coverage at all, and a crash
there takes down every invocation before anything useful happens.
"""

import subprocess
import sys

import pytest

from converse_code.cli import _configure_logging

ENTRY = [sys.executable, "-m", "converse_code.cli"]


def run(args, env_extra=None, timeout=30):
    import os

    env = {**os.environ, "CONVERSE_API_KEY": "ck_fake_for_test", **(env_extra or {})}
    return subprocess.run(ENTRY + args, capture_output=True, text=True, timeout=timeout, env=env)


def test_help_works():
    proc = run(["--help"])
    assert proc.returncode == 0
    assert "Talk to Claude Code by voice" in proc.stdout


def test_startup_reaches_broker_connect_and_fails_cleanly():
    """Exercises the whole startup path: logging setup, key load, local server,
    broker connect. An unreachable broker must produce a clear message, not a
    traceback."""
    proc = run(["--no-browser", "--headless", "--broker-url", "ws://127.0.0.1:1"])
    assert proc.returncode == 1
    assert "Could not connect to Converse" in proc.stderr
    assert "Claude Code was not started" in proc.stderr
    assert "Traceback" not in proc.stderr


@pytest.mark.parametrize("owns_terminal", [True, False])
def test_configure_logging_accepts_both_modes(owns_terminal):
    """basicConfig rejects filename and stream together — even as None."""
    _configure_logging(owns_terminal=owns_terminal)
