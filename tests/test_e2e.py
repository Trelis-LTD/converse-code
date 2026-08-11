"""Full local loop: browser tool event -> bridge -> router -> fake Claude PTY.

Converse itself is substituted. The direct Browser SDK owns that remote socket;
this test exercises everything Converse Code still owns after the cutover.
"""

import asyncio
import json
import sys
from pathlib import Path

import aiohttp

from converse_code.bridge import BrowserBridge
from converse_code.localserver import LocalServer
from converse_code.ptyhost import ClaudeHost
from converse_code.tools import ToolRouter

FAKE_TUI = str(Path(__file__).parent / "fake_tui.py")


async def test_full_local_tool_loop(tmp_path):
    server = LocalServer(token="e2e-token")
    await server.start(port=0)
    host = ClaudeHost([sys.executable, FAKE_TUI], attach_terminal=False)
    await host.start()

    bridge = BrowserBridge(server.send_json_to_tab)
    router = ToolRouter(host, bridge, handle="cc-e2e-1", project_dir=tmp_path)
    router.HOLD_S, router.POLL_S, router.SETTLE_S = 5.0, 0.05, 0.2
    transcript = tmp_path / "session.jsonl"
    router.transcript_path = transcript
    transcript.write_text("")
    server.on_hook = router.on_hook
    server.on_tab_json = bridge.handle_browser_message
    server.on_tab_closed = bridge.on_browser_disconnected

    tasks = set()

    async def on_tool_call(call):
        task = asyncio.create_task(router.handle_tool_call(call))
        tasks.add(task)
        task.add_done_callback(tasks.discard)

    bridge.on_tool_call = on_tool_call
    bridge.on_tool_cancel = router.handle_tool_cancel

    origin = {"Origin": f"http://127.0.0.1:{server.port}"}
    ws_url = f"http://127.0.0.1:{server.port}/ws?t=e2e-token"

    async def receive_action(ws, action, call_id):
        deadline = asyncio.get_running_loop().time() + 5
        while True:
            assert asyncio.get_running_loop().time() < deadline
            message = await ws.receive(timeout=5)
            frame = json.loads(message.data)
            if frame.get("event") == "bridge_control":
                await ws.send_json({
                    "type": "local", "event": "bridge_ack", "seq": frame["seq"],
                })
                if frame.get("action") == action and frame.get("id") == call_id:
                    return frame

    try:
        async with aiohttp.ClientSession() as http:
            async with http.ws_connect(ws_url, headers=origin) as ws:
                await ws.send_json({"type": "local", "event": "bridge_ready"})

                await ws.send_json({
                    "type": "local", "event": "tool_call",
                    "call": {
                        "id": "t1", "name": "long_task",
                        "args": {"request": "hello world"},
                    },
                })
                deadline = asyncio.get_running_loop().time() + 5
                while "echo: hello world" not in "\n".join(host.snapshot()):
                    assert asyncio.get_running_loop().time() < deadline, host.snapshot()
                    await asyncio.sleep(0.05)
                await http.post(server.hook_url("user_prompt_submit"), json={
                    "prompt": "hello world", "prompt_id": "prompt-t1",
                })
                deferred = await receive_action(ws, "tool_deferred", "t1")
                assert deferred["handle"].endswith("-t1")

                transcript.write_text(json.dumps({
                    "type": "assistant",
                    "message": {"content": [{"type": "text", "text": "Echoed."}]},
                }) + "\n")
                await http.post(
                    server.hook_url("stop"), json={"transcript_path": str(transcript)},
                )
                result = await receive_action(ws, "tool_result", "t1")
                assert result["content"]["speak"] == "Echoed."
                assert result["content"]["data"]["phase"] == "idle"

                await ws.send_json({
                    "type": "local", "event": "tool_call",
                    "call": {"id": "t2", "name": "long_task", "args": {"request": "menu"}},
                })
                deadline = asyncio.get_running_loop().time() + 5
                while router.menu() is None:
                    assert asyncio.get_running_loop().time() < deadline, host.snapshot()
                    await asyncio.sleep(0.05)
                await http.post(server.hook_url("user_prompt_submit"), json={
                    "prompt": "menu", "prompt_id": "prompt-t2",
                })
                await receive_action(ws, "tool_deferred", "t2")
                partial = await receive_action(ws, "tool_partial_result", "t2")
                assert partial["reply"] is True
                assert "needs input" in partial["content"]["speak"]

                await ws.send_json({
                    "type": "local", "event": "tool_call",
                    "call": {"id": "t3", "name": "select_option", "args": {"option": "yes"}},
                })
                selected = await receive_action(ws, "tool_result", "t3")
                assert "Chose Yes" in selected["content"]["speak"]

                await http.post(server.hook_url("stop"), json={
                    "transcript_path": str(transcript),
                    "last_assistant_message": "Menu answered.",
                })
                result = await receive_action(ws, "tool_result", "t2")
                assert result["content"]["speak"] == "Menu answered."
    finally:
        host.inject("exit")
        await asyncio.wait_for(host.exited.wait(), 5)
        await server.stop()
