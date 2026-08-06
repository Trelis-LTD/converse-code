"""A wrapped session that dies on startup must explain itself.

Claude Code wipes the terminal as it leaves its alternate screen, so anything it
printed is gone by the time the user is looking — the screen buffer is the only
surviving copy.
"""

import asyncio
import sys

from converse_code.cli import _report_early_exit
from converse_code.ptyhost import ClaudeHost


async def run_until_exit(argv):
    host = ClaudeHost(argv, attach_terminal=False)
    await host.start()
    await asyncio.wait_for(host.exited.wait(), 10)
    return host


async def test_reports_failed_launch(capsys):
    host = await run_until_exit(["definitely-not-a-real-binary-xyz"])
    assert host.returncode == 127
    _report_early_exit(host, 0.2)
    err = capsys.readouterr().err
    assert "exit code 127" in err
    assert "could not be launched" in err
    assert "--claude" in err


async def test_replays_last_screen_of_a_crashing_session(capsys):
    host = await run_until_exit(
        [sys.executable, "-c", "print('FATAL: could not open config'); raise SystemExit(2)"]
    )
    assert host.returncode == 2
    _report_early_exit(host, 0.3)
    err = capsys.readouterr().err
    assert "exit code 2" in err
    assert "FATAL: could not open config" in err


async def test_silent_immediate_exit_is_called_out(capsys):
    host = await run_until_exit([sys.executable, "-c", "raise SystemExit(3)"])
    _report_early_exit(host, 0.1)
    err = capsys.readouterr().err
    assert "no output at all" in err


async def test_healthy_long_session_reports_nothing(capsys):
    host = await run_until_exit([sys.executable, "-c", "print('bye')"])
    assert host.returncode == 0
    _report_early_exit(host, 120.0)  # clean exit after a real session
    assert capsys.readouterr().err == ""
