"""End-to-end coverage for the public headless JSONL protocol."""

import json
import os
import subprocess
import sys
from pathlib import Path


ENTRY = [sys.executable, "-m", "converse_code.cli"]
FAKE_TUI = Path(__file__).parent / "fake_tui.py"


def test_headless_protocol_does_not_connect_to_converse():
    proc = subprocess.run(
        ENTRY + [
            "--headless", "--port", "0", "--broker-url", "ws://127.0.0.1:1",
            "--claude", f"{sys.executable} {FAKE_TUI}",
        ],
        input=(
            '{"type":"screen_snapshot","id":"s1"}\n'
            '{"type":"shutdown","id":"bye"}\n'
        ),
        capture_output=True,
        text=True,
        timeout=15,
        env={key: value for key, value in os.environ.items() if key != "CONVERSE_API_KEY"},
    )

    assert proc.returncode == 0, proc.stderr
    output = [json.loads(line) for line in proc.stdout.splitlines()]
    assert output[0]["type"] == "ready"
    assert output[0]["protocol"] == "converse-code-headless-v1"
    assert "end_session" not in output[0]["tools"]
    snapshot = next(event for event in output if event["type"] == "screen_snapshot")
    assert snapshot["id"] == "s1"
    assert isinstance(snapshot["data"]["screen"], list)
    assert output[-1] == {"type": "shutdown", "id": "bye"}
