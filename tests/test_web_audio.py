"""Verify the voice tab's audio math by running it in Node.

The page's resamplers are where crackle comes from: a chunk-boundary
discontinuity is a click, and a dropped fractional remainder drifts the sample
rate. web_audio_check.mjs extracts the real functions from index.html (no
copies) and checks continuity and rate against a synthesized sine.
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
    assert "mic resampler: continuous, no drift: OK" in proc.stdout
    assert "TTS decoder: PCM16 -> Float32 in [-1,1]: OK" in proc.stdout
    assert "playback resampler: continuous across chunks: OK" in proc.stdout
