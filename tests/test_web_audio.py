"""Verify the voice tab's audio wiring by running it in Node.

Audio is now the vendored @trelis/converse SDK's job. This asserts the page
still delegates to it (rather than growing a hand-rolled implementation again),
that the SDK's codec expectations match what converse_code/audio.py feeds it,
and that its resampler stays seam-continuous on real captured speech.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

CHECK = Path(__file__).parent / "web_audio_check.mjs"
PAGE = Path(__file__).parents[1] / "converse_code" / "web" / "index.html"


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_web_audio_resamplers():
    proc = subprocess.run(
        ["node", str(CHECK)], capture_output=True, text=True, timeout=60
    )
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
    assert "page uses the SDK ConverseClient: OK" in proc.stdout
    assert "SDK codec: PCM16 both directions: OK" in proc.stdout
    assert "SDK resampler: continuous across chunks: OK" in proc.stdout


def test_voice_session_pins_classic_and_explains_retryable_pipeline_errors():
    page = PAGE.read_text()

    assert 'voice: "classic"' in page
    assert 'ev.detail ||' in page
    assert "ev.retryable" in page
    assert "Click the mic to reconnect" in page


def test_voice_session_closes_after_its_final_reply():
    page = PAGE.read_text()

    assert 'msg.event === "end_session"' in page
    assert "endAfterReply = true" in page
    assert "finishEndSession" in page
