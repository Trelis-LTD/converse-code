#!/usr/bin/env python3
"""Generate deterministic mic audio and run the real-Chromium browser suite."""

from __future__ import annotations

import math
import os
import struct
import subprocess
import sys
import wave
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WAV_PATH = ROOT / "tests" / "browser" / "microphone.wav"


def write_microphone_fixture() -> None:
    sample_rate = 48_000
    duration_s = 2
    amplitude = 0.15 * 32767
    with wave.open(str(WAV_PATH), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(sample_rate)
        for index in range(sample_rate * duration_s):
            value = amplitude * (
                math.sin(2 * math.pi * 440 * index / sample_rate)
                + 0.35 * math.sin(2 * math.pi * 880 * index / sample_rate)
            ) / 1.35
            output.writeframesraw(struct.pack("<h", round(value)))


def main() -> int:
    write_microphone_fixture()
    env = dict(os.environ)
    env["PYTHONUNBUFFERED"] = "1"
    env["CONVERSE_CODE_BROWSER_E2E"] = "1"
    command = [sys.executable, "-m", "pytest", "-q", "-m", "browser_e2e", *sys.argv[1:]]
    try:
        return subprocess.run(command, cwd=ROOT, env=env, check=False).returncode
    finally:
        WAV_PATH.unlink(missing_ok=True)


if __name__ == "__main__":
    raise SystemExit(main())
