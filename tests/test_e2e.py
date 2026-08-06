"""Full-loop test: mock Converse broker -> BrokerClient -> ToolRouter -> real
pty running the fake TUI, with the Stop hook arriving over HTTP exactly as
Claude Code's curl hook would send it. Only the real `claude` binary and the
real broker are substituted."""

import asyncio
import json
import sys
from pathlib import Path

import aiohttp
import pytest
import websockets

from converse_code.broker import BrokerClient
from converse_code.localserver import LocalServer
from converse_code.ptyhost import ClaudeHost
from converse_code.tools import ToolRouter, manifest

FAKE_TUI = str(Path(__file__).parent / "fake_tui.py")


async def test_full_loop(tmp_path):
    # -- mock broker ---------------------------------------------------------
    received: list[dict] = []
    client_ws_holder: list = []
    got_start = asyncio.Event()

    async def broker_handler(ws):
        client_ws_holder.append(ws)
        async for msg in ws:
            if isinstance(msg, bytes):
                continue
            data = json.loads(msg)
            if data.get("type") == "start":
                got_start.set()
            else:
                received.append(data)

    ws_server = await websockets.serve(broker_handler, "127.0.0.1", 0)
    broker_url = f"ws://127.0.0.1:{ws_server.sockets[0].getsockname()[1]}"

    # -- the real wiring, minus real claude / real broker ---------------------
    server = LocalServer()
    port = await server.start(port=0)

    host = ClaudeHost([sys.executable, FAKE_TUI], attach_terminal=False)
    await host.start()

    client = BrokerClient("ck_test", session_id="cc-e2e-1", tools=manifest(), url=broker_url)
    router = ToolRouter(host, client, handle="cc-e2e-1", project_dir=tmp_path)
    router.HOLD_S, router.POLL_S, router.SETTLE_S = 5.0, 0.05, 0.2

    transcript = tmp_path / "session.jsonl"
    router.transcript_path = transcript
    transcript.write_text("")

    server.on_hook = router.on_hook
    client.on_tool_call = lambda call: asyncio.ensure_future(router.handle_tool_call(call))
    await client.connect()
    run_task = asyncio.create_task(client.run())
    await asyncio.wait_for(got_start.wait(), 5)
    broker_ws = client_ws_holder[0]

    try:
        # 1. brain sends long_task -> injected into the TUI
        await broker_ws.send(json.dumps(
            {"type": "tool_call", "id": "t1", "name": "long_task", "args": {"request": "hello world"}}
        ))
        deadline = asyncio.get_running_loop().time() + 5
        while "echo: hello world" not in "\n".join(host.snapshot()):
            assert asyncio.get_running_loop().time() < deadline, host.snapshot()
            await asyncio.sleep(0.05)

        # 2. the turn "finishes": transcript written, Stop hook POSTs like curl would
        transcript.write_text(json.dumps({
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": "Echoed the greeting."}]},
        }) + "\n")
        async with aiohttp.ClientSession() as http:
            await http.post(f"http://127.0.0.1:{port}/hook/stop",
                            json={"transcript_path": str(transcript)})

        # 3. terminal result lands at the broker
        deadline = asyncio.get_running_loop().time() + 5
        while not any(m["type"] == "tool_result" for m in received):
            assert asyncio.get_running_loop().time() < deadline, received
            await asyncio.sleep(0.05)
        result = next(m for m in received if m["type"] == "tool_result")
        assert result["id"] == "t1"
        assert result["content"]["speak"] == "Echoed the greeting."
        assert result["content"]["handle"] == "cc-e2e-1"
        assert result["content"]["data"]["state"] == "idle"

        # 4. menu flow end to end: open menu in TUI, brain answers it
        await broker_ws.send(json.dumps(
            {"type": "tool_call", "id": "t2", "name": "long_task", "args": {"request": "menu"}}
        ))
        deadline = asyncio.get_running_loop().time() + 5
        while not any(m["type"] == "tool_result" and m["id"] == "t2" for m in received):
            assert asyncio.get_running_loop().time() < deadline
            await asyncio.sleep(0.05)
        menu_result = next(m for m in received if m["id"] == "t2")
        assert menu_result["content"]["data"]["state"] == "menu"
        assert "Yes" in menu_result["content"]["data"]["options"]

        await broker_ws.send(json.dumps(
            {"type": "tool_call", "id": "t3", "name": "select_option", "args": {"option": "yes"}}
        ))
        deadline = asyncio.get_running_loop().time() + 5
        while not any(m["type"] == "tool_result" and m["id"] == "t3" for m in received):
            assert asyncio.get_running_loop().time() < deadline
            await asyncio.sleep(0.05)
        assert "Chose Yes" in next(m for m in received if m["id"] == "t3")["content"]["speak"]
    finally:
        host.inject("exit")
        await asyncio.wait_for(host.exited.wait(), 5)
        await client.close()
        await asyncio.wait_for(run_task, 5)
        await server.stop()
        ws_server.close()
        await ws_server.wait_closed()
