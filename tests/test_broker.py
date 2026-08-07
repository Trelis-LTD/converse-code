"""BrokerClient against a mock Converse broker (real WebSocket server)."""

import asyncio
import json

import pytest
import websockets

from converse_code.broker import BrokerClient, validate_key
from converse_code.tools import manifest


class MockBroker:
    def __init__(self):
        self.start_frame = None
        self.received = []  # JSON frames after start
        self.audio = []
        self.client_ws = None
        self.got_start = asyncio.Event()

    async def handler(self, ws):
        self.client_ws = ws
        async for msg in ws:
            if isinstance(msg, bytes):
                self.audio.append(msg)
                continue
            data = json.loads(msg)
            if data.get("type") == "auth":
                await ws.send(json.dumps({"type": "ok"} if data["api_key"].startswith("ck_") else {"type": "error"}))
            elif data.get("type") == "start":
                self.start_frame = data
                self.got_start.set()
            else:
                self.received.append(data)


@pytest.fixture
async def mock_broker():
    broker = MockBroker()
    server = await websockets.serve(broker.handler, "127.0.0.1", 0)
    broker.url = f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}"
    yield broker
    server.close()
    await server.wait_closed()


async def test_validate_key(mock_broker):
    assert await validate_key("ck_good", url=mock_broker.url) is True
    assert await validate_key("bad", url=mock_broker.url) is False


async def test_start_frame_and_tool_roundtrip(mock_broker):
    client = BrokerClient("ck_test", session_id="cc-proj-1", tools=manifest(), url=mock_broker.url)

    calls, cancels, extra_json, audio_down = [], [], [], []

    async def on_tool_call(msg):
        calls.append(msg)
        await client.send_tool_progress(msg["id"], "checking things")
        await client.send_tool_result(msg["id"], {"speak": "done", "data": {}, "handle": "cc-proj-1"})

    client.on_tool_call = on_tool_call
    client.on_tool_cancel = lambda m: cancels.append(m) or asyncio.sleep(0)
    client.on_json = lambda m: extra_json.append(m) or asyncio.sleep(0)
    client.on_audio = lambda b: audio_down.append(b) or asyncio.sleep(0)

    await client.connect()
    run_task = asyncio.create_task(client.run())
    await asyncio.wait_for(mock_broker.got_start.wait(), 5)

    # start frame carries auth, 16k audio, and the full tool manifest
    sf = mock_broker.start_frame
    assert sf["api_key"] == "ck_test"
    assert sf["audio"] == {"sr": 16000, "output_encoding": "pcm16"}
    assert [t["name"] for t in sf["mode"]["tools"]] == [t["name"] for t in manifest()]

    # mic audio up
    await client.send_audio(b"\x01\x02" * 320)
    # tool call down -> progress + result up
    await mock_broker.client_ws.send(json.dumps(
        {"type": "tool_call", "id": "t1", "name": "long_task", "args": {"request": "hi"}}
    ))
    await mock_broker.client_ws.send(json.dumps({"type": "tool_cancel", "id": "t2"}))
    # non-tool JSON and TTS audio down are passed through
    await mock_broker.client_ws.send(json.dumps({"type": "asr", "text": "hello", "final": True}))
    await mock_broker.client_ws.send(b"\x00\x00\x80\x3f" * 160)

    await asyncio.sleep(0.3)
    assert [c["id"] for c in calls] == ["t1"]
    types = [(m["type"], m.get("note") or m.get("content", {}).get("speak")) for m in mock_broker.received]
    assert ("tool_progress", "checking things") in types
    assert ("tool_result", "done") in types
    assert [m["id"] for m in cancels] == ["t2"]
    assert len(mock_broker.audio) == 1
    assert extra_json and extra_json[0]["type"] == "asr"
    assert audio_down and len(audio_down[0]) == 640

    await client.send_context("Claude finished.", reply=True)
    await asyncio.sleep(0.05)
    assert {
        "type": "inject_context", "text": "Claude finished.", "role": "context", "reply": True,
    } in mock_broker.received

    await client.close()
    await asyncio.wait_for(run_task, 5)
    assert client.closed.is_set()


async def test_send_after_close_is_noop(mock_broker):
    client = BrokerClient("ck_test", session_id="s", tools=[], url=mock_broker.url)
    await client.connect()
    run_task = asyncio.create_task(client.run())
    await client.close()
    await asyncio.wait_for(run_task, 5)
    await client.send_tool_result("x", {"speak": "late"})  # must not raise
