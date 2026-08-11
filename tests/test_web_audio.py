"""Verify the session page's voice-input wiring by running it in Node.

This executes the vendored SDK codec and resampler against captured wire audio.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

CHECK = Path(__file__).parent / "web_audio_check.mjs"
CONTROL_CHECK = Path(__file__).parent / "web_sdk_control_check.mjs"
PAGE = Path(__file__).parents[1] / "converse_code" / "web" / "index.html"


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_web_audio_resamplers():
    proc = subprocess.run(
        ["node", str(CHECK)], capture_output=True, text=True, timeout=60
    )
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
    assert "SDK codec: PCM16 both directions: OK" in proc.stdout
    assert "SDK resampler: continuous across chunks: OK" in proc.stdout


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_vendored_sdk_reports_tool_control_delivery():
    proc = subprocess.run(
        ["node", str(CONTROL_CHECK)], capture_output=True, text=True, timeout=30
    )
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
    assert "SDK tool controls expose transport delivery: OK" in proc.stdout


def test_voice_session_pins_classic_and_explains_retryable_pipeline_errors():
    page = PAGE.read_text()

    assert "@trelis/converse SDK 0.12.3" in page
    assert 'voice: "classic"' in page
    assert 'ev.detail ||' in page
    assert "ev.retryable" in page
    assert "Use voice or text to reconnect" in page


def test_voice_session_closes_after_its_final_reply():
    page = PAGE.read_text()

    assert 'msg.event === "end_session"' in page
    assert "endAfterReply = true" in page
    assert "finishEndSession" in page


def test_stop_is_a_clean_session_end_and_mute_is_independent():
    page = PAGE.read_text()

    assert 'id="muteBtn"' in page
    assert ">Mute mic</button>" in page
    assert "#muteBtn:disabled{opacity:.62" in page
    assert "setMicEnabled" in page
    assert "stopVoiceInput" in page
    assert "client.stopMic()" in page
    assert "sessionGeneration" in page
    assert 'case "session_end"' in page


def test_browser_connects_directly_with_scoped_credentials_and_resume_state():
    page = PAGE.read_text()

    assert '"wss://converse.trelis.com/ws"' in page
    assert "/session-credential" in page
    assert "/proxy" not in page
    assert "injectContext" in page
    assert "sendToolResult" in page
    assert "sendToolDeferred" in page
    assert "exportResumeState" in page
    assert "importResumeState" in page
    assert 'addEventListener("resume_state"' in page
    assert "sessionStorage" in page
