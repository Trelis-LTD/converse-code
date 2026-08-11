#!/usr/bin/env python3
"""Prove that a real Chromium process can emit recordable audio on this Linux host."""

from __future__ import annotations

import argparse
import asyncio
import math
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import wave
from pathlib import Path

from playwright.async_api import async_playwright


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = ROOT / "test-results" / "browser-audio" / "loopback.wav"


def require(command: str) -> str:
    path = shutil.which(command)
    if not path:
        raise SystemExit(f"missing {command}; install PulseAudio utilities and Xvfb")
    return path

def start_recorder(raw_path: Path, env: dict[str, str]):
    raw_output = raw_path.open("wb")
    try:
        recorder = subprocess.Popen(
            [
                "parec", "--raw", "--device=converse_code_test.monitor",
                "--format=s16le", "--rate=48000", "--channels=2",
            ],
            env=env, stdout=raw_output, stderr=subprocess.PIPE,
        )
    except BaseException:
        raw_output.close()
        raise
    return recorder, raw_output


async def play_tone(env: dict[str, str]) -> None:
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(
            headless=False,
            env=env,
            args=["--autoplay-policy=no-user-gesture-required"],
        )
        try:
            page = await browser.new_page()
            await page.set_content("<title>Converse Code audio loopback probe</title>")
            await page.evaluate(
                """async () => {
                  const context = new AudioContext({sampleRate: 48000});
                  const oscillator = new OscillatorNode(context, {frequency: 880});
                  const gain = new GainNode(context, {gain: 0.2});
                  oscillator.connect(gain).connect(context.destination);
                  oscillator.start();
                  await new Promise(resolve => setTimeout(resolve, 1500));
                  oscillator.stop();
                  await context.close();
                }"""
            )
        finally:
            await browser.close()


async def run_probe(output: Path) -> None:
    require("pulseaudio")
    require("pactl")
    require("parec")
    with tempfile.TemporaryDirectory(prefix="converse-code-audio-") as temp_name:
        temp = Path(temp_name)
        runtime = temp / "runtime"
        runtime.mkdir(mode=0o700)
        raw_path = temp / "capture.raw"
        pulse_log = temp / "pulseaudio.log"
        env = dict(os.environ)
        env["XDG_RUNTIME_DIR"] = str(runtime)
        env["PULSE_SERVER"] = f"unix:{runtime}/pulse/native"
        env["PULSE_SINK"] = "converse_code_test"

        pulse = [
            "pulseaudio", "--daemonize=yes", "--fail=yes", "--exit-idle-time=-1",
            f"--log-target=file:{pulse_log}", "-n",
            "--load=module-native-protocol-unix",
            "--load=module-null-sink sink_name=converse_code_test rate=48000 channels=2",
        ]
        subprocess.run(pulse, env=env, check=True)
        recorder = None
        try:
            for _ in range(50):
                ready = subprocess.run(
                    ["pactl", "info"], env=env, stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL, check=False,
                )
                if ready.returncode == 0:
                    break
                await asyncio.sleep(0.1)
            else:
                raise RuntimeError(f"private PulseAudio server did not start: {pulse_log.read_text()}")

            recorder, raw_output = start_recorder(raw_path, env)
            try:
                await play_tone(env)
            finally:
                if recorder.poll() is None:
                    recorder.terminate()
                try:
                    recorder.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    recorder.kill()
                    recorder.wait(timeout=5)
                finally:
                    raw_output.close()
        finally:
            subprocess.run(
                ["pulseaudio", "--kill"], env=env, check=False,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )

        raw = raw_path.read_bytes()
        samples = struct.unpack(f"<{len(raw) // 2}h", raw[: len(raw) // 2 * 2])
        rms = math.sqrt(sum(sample * sample for sample in samples) / max(len(samples), 1))
        if len(samples) < 48_000 or rms < 500:
            raise RuntimeError(
                f"browser output was missing or silent: samples={len(samples)}, rms={rms:.1f}"
            )
        output.parent.mkdir(parents=True, exist_ok=True)
        with wave.open(str(output), "wb") as wav:
            wav.setnchannels(2)
            wav.setsampwidth(2)
            wav.setframerate(48_000)
            wav.writeframes(raw)
        print(f"audio loopback passed: samples={len(samples)}, rms={rms:.1f}, output={output}", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--inside-xvfb", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if not os.environ.get("DISPLAY") and not args.inside_xvfb:
        require("xvfb-run")
        command = [
            "xvfb-run", "-a", sys.executable, "-u", __file__,
            "--inside-xvfb", "--output", str(args.output),
        ]
        return subprocess.run(command, cwd=ROOT, check=False).returncode
    asyncio.run(run_probe(args.output.resolve()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
