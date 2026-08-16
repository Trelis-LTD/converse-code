#!/usr/bin/env python3
"""Generate deterministic mic audio and run the real-Chromium browser suite."""

from __future__ import annotations

import math
import os
import struct
import subprocess
import sys
import tempfile
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))
from support import node_typescript_support  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
def write_microphone_fixture(path: Path) -> None:
    sample_rate = 48_000
    duration_s = 2
    amplitude = 0.15 * 32767
    with wave.open(str(path), "wb") as output:
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
    capable, detail = node_typescript_support()
    if not capable:
        # The ladder is where the Pi extension contract is guaranteed to run red-on-failure --
        # it must not silently degrade the way an underequipped `pytest` invocation may skip.
        print(f"node >= 22.18 (or 23.6+) is required: the Pi extension contract check runs "
              f"here; {detail}", file=sys.stderr)
        return 1
    contract = subprocess.run(["node", "tests/pi_extensions_check.mjs"], cwd=ROOT, check=False)
    if contract.returncode != 0:
        return contract.returncode
    with tempfile.TemporaryDirectory(prefix="converse-code-browser-") as directory:
        wav_path = Path(directory) / "microphone.wav"
        write_microphone_fixture(wav_path)
        env = dict(os.environ)
        env["PYTHONUNBUFFERED"] = "1"
        env["CONVERSE_CODE_BROWSER_E2E"] = "1"
        env["CONVERSE_CODE_TEST_WAV"] = str(wav_path)
        command = [sys.executable, "-m", "pytest", "-q", "-m", "browser_e2e", *sys.argv[1:]]
        return subprocess.run(command, cwd=ROOT, env=env, check=False).returncode


if __name__ == "__main__":
    raise SystemExit(main())
