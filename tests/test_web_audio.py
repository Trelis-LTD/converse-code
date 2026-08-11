"""Verify the voice tab's audio wiring by running it in Node.

This executes the vendored SDK codec and resampler against captured wire audio.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

CHECK = Path(__file__).parent / "web_audio_check.mjs"

@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_web_audio_resamplers():
    proc = subprocess.run(
        ["node", str(CHECK)], capture_output=True, text=True, timeout=60
    )
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
    assert "SDK codec: PCM16 both directions: OK" in proc.stdout
    assert "SDK resampler: continuous across chunks: OK" in proc.stdout
