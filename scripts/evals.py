#!/usr/bin/env python3
"""Run non-overlapping Converse Code validation stages exactly once."""

import argparse
import subprocess
import sys


STAGES = {
    "quick": [sys.executable, "-m", "pytest", "-q"],
    "browser": [sys.executable, "scripts/browser_e2e.py"],
    "audio": [sys.executable, "-u", "scripts/audio_loopback_probe.py"],
    "live-sdk": [sys.executable, "-u", "scripts/smoke_agent_sdk.py"],
    "live-pty": [sys.executable, "-u", "scripts/smoke_real_claude.py"],
    "live-converse": [sys.executable, "-u", "scripts/eval_voice_text.py"],
}
PROFILES = {
    "quick": ["quick"],
    "pr": ["quick", "browser"],
    "release": ["quick", "browser", "live-pty"],
    "extended": [
        "quick", "browser", "live-pty", "live-converse", "live-sdk", "audio",
    ],
}


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run distinct validation layers without repeating scenarios."
    )
    parser.add_argument("--profile", choices=PROFILES, default="quick")
    parser.add_argument("--stage", action="append", choices=STAGES, dest="stages")
    args = parser.parse_args()
    stages = list(dict.fromkeys(args.stages or PROFILES[args.profile]))

    for index, stage in enumerate(stages, 1):
        command = STAGES[stage]
        print(f"\n[{index}/{len(stages)}] {stage}: {' '.join(command)}", flush=True)
        completed = subprocess.run(command, check=False)
        if completed.returncode:
            print(f"{stage}: FAIL ({completed.returncode})", flush=True)
            return completed.returncode
        print(f"{stage}: PASS", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
