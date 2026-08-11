#!/usr/bin/env python3
"""Open one bounded production Converse session, report admission, and close it."""

import asyncio
import json
import os
import uuid

import websockets

from converse_code import config, converse


async def main() -> int:
    api_key = config.get_api_key()
    if not api_key:
        raise SystemExit("No configured Converse API key")
    session_id = f"cc-admission-probe-{uuid.uuid4().hex[:10]}"
    credential = await converse.mint_session_credential(api_key, session_id)
    async with websockets.connect(converse.DEFAULT_WS_URL, max_size=4 * 1024 * 1024) as ws:
        await ws.send(json.dumps({
            "type": "start",
            "session_id": session_id,
            "api_key": credential["api_key"],
            "audio": {"sr": 16000, "output_encoding": "pcm16"},
            "mode": {"kind": "converse", "greeting": False, "web_search": False, "tools": []},
        }))
        frame = json.loads(await asyncio.wait_for(ws.recv(), timeout=20))
        safe = {key: frame.get(key) for key in ("type", "code", "detail") if key in frame}
        print(json.dumps(safe, sort_keys=True), flush=True)
        return 0 if frame.get("type") == "ready" else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
