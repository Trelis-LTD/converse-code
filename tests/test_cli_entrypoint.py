"""Exercise the installed console entry point and visible Pi launch contract."""

import os
import socket
import subprocess
import sys

from converse_code.cli import _pi_argv

ENTRY = [sys.executable, "-m", "converse_code.cli"]


def run(args, env_extra=None, timeout=30):
    env = {**os.environ, "CONVERSE_API_KEY": "ck_fake_for_test", **(env_extra or {})}
    return subprocess.run(
        ENTRY + args,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
        check=False,
    )


def test_help_works():
    proc = run(["--help"])
    assert proc.returncode == 0
    assert "voice control for the visible Pi terminal" in proc.stdout
    assert "--pi" in proc.stdout
    assert "--api-url" in proc.stdout


def test_pi_command_launches_visible_tui_with_semantic_extensions():
    argv = _pi_argv("pi --provider openai-codex")
    assert argv[:3] == ["pi", "--provider", "openai-codex"]
    assert "--mode" not in argv
    extensions = [argv[index + 1] for index, value in enumerate(argv) if value == "-e"]
    assert extensions[-2].endswith("pi_bridge.ts")
    assert extensions[-1].endswith("pi_approval.ts")


def test_startup_checks_credentials_before_launching_pi():
    proc = run(["--no-browser", "--port", "0", "--broker-url", "ws://127.0.0.1:1"])
    assert proc.returncode == 1
    assert "Could not reach Converse" in proc.stderr
    assert "Pi was not started" in proc.stderr
    assert "Traceback" not in proc.stderr


def test_port_in_use_reports_cleanly():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(1)
    try:
        proc = run(["--no-browser", "--port", str(sock.getsockname()[1])])
        assert proc.returncode == 1
        assert "Could not start the Converse session server" in proc.stderr
        assert "--port" in proc.stderr
        assert "Traceback" not in proc.stderr
    finally:
        sock.close()
