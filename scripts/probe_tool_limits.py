"""Does production accept a tool timeout above the old 120s ceiling yet?

The 0.6.0 changelog says the server-side cap rises to 600s, deployed separately.
Rather than guessing, declare a manifest at 600s and see whether the session is
accepted (`ready`) or rejected/clamped.
"""

import asyncio
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import websockets

from converse_code import config
from converse_code.broker import DEFAULT_URL
from converse_code.tools import manifest


async def try_timeout(seconds: int) -> str:
    tools = manifest()
    for t in tools:
        if t["name"] == "long_task":
            t["timeout"] = seconds
    frame = {
        "type": "start",
        "session_id": f"cc-probe-{os.urandom(3).hex()}",
        "api_key": config.get_api_key(),
        "audio": {"sr": 16000, "output_encoding": "pcm16"},
        "mode": {"kind": "converse", "web_search": False, "tools": tools},
        "client": {"capabilities": []},
    }
    async with websockets.connect(DEFAULT_URL, max_size=4 * 1024 * 1024) as ws:
        await ws.send(json.dumps(frame))
        try:
            while True:
                raw = await asyncio.wait_for(ws.recv(), timeout=10)
                if isinstance(raw, bytes):
                    continue
                msg = json.loads(raw)
                if msg.get("type") == "ready":
                    tools_echo = msg.get("tools") or msg.get("mode", {}).get("tools")
                    return f"ACCEPTED (ready){f'; echo={tools_echo}' if tools_echo else ''}"
                if msg.get("type") in ("bye", "error"):
                    return f"REJECTED: {msg}"
        except asyncio.TimeoutError:
            return "no ready/bye within 10s"


async def main() -> None:
    for seconds in (120, 600, 900):
        try:
            print(f"timeout={seconds}s -> {await try_timeout(seconds)}")
        except Exception as exc:
            print(f"timeout={seconds}s -> connection error: {exc}")
        await asyncio.sleep(1)


asyncio.run(main())
