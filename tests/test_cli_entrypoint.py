"""Run the real console-script entry point as a subprocess.

The unit tests exercise modules directly, which meant `main()` itself — argument
parsing, logging setup, startup ordering — had no coverage at all, and a crash
there takes down every invocation before anything useful happens.
"""

import subprocess
import sys

import pytest

from converse_code.cli import _pi_argv, _require_pi_model
from converse_code.pi_rpc import PiRPCError

ENTRY = [sys.executable, "-m", "converse_code.cli"]


def run(args, env_extra=None, timeout=30):
    import os

    env = {**os.environ, "CONVERSE_API_KEY": "ck_fake_for_test", **(env_extra or {})}
    return subprocess.run(ENTRY + args, capture_output=True, text=True, timeout=timeout, env=env)


def test_help_works():
    proc = run(["--help"])
    assert proc.returncode == 0
    assert "background-tool reference using Pi and Codex" in proc.stdout
    assert "--pi" in proc.stdout
    assert "--api-url" in proc.stdout


def test_pi_command_always_loads_the_structured_approval_gate():
    argv = _pi_argv("pi --mode rpc --provider openai-codex")
    assert argv[:4] == ["pi", "--mode", "rpc", "--provider"]
    assert argv[-2] == "-e"
    assert argv[-1].endswith("pi_approval.ts")


def test_pi_startup_fails_fast_when_codex_login_is_missing():
    with pytest.raises(PiRPCError, match="/login"):
        _require_pi_model({"data": {"model": {"id": "unknown"}}})
    _require_pi_model({"data": {"model": {"id": "gpt-5.6-codex"}}})


def test_startup_checks_credentials_before_launching_pi():
    """Exercises the whole startup path: logging setup, key load, credential
    check. The browser's direct socket is opened later, so an unreachable broker
    must still fail fast here with a clear
    message rather than a traceback or a silent start."""
    proc = run(["--no-browser", "--port", "0", "--broker-url", "ws://127.0.0.1:1"])
    assert proc.returncode == 1
    assert "Could not reach Converse" in proc.stderr
    assert "Pi was not started" in proc.stderr
    assert "Traceback" not in proc.stderr


def test_port_in_use_reports_cleanly():
    """A second instance on the same port must explain itself, not traceback."""
    import socket

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(1)
    busy_port = sock.getsockname()[1]
    try:
        proc = run(["--no-browser", "--port", str(busy_port)])
        assert proc.returncode == 1
        assert "Could not start the Converse session server" in proc.stderr
        assert "--port" in proc.stderr
        assert "Traceback" not in proc.stderr
    finally:
        sock.close()
