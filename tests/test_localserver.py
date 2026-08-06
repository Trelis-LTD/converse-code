"""LocalServer: static page, tab WebSocket relay, hook endpoint."""

import asyncio
import json

import aiohttp
import pytest

from converse_code.localserver import LocalServer


@pytest.fixture
async def server():
    s = LocalServer()
    await s.start(port=0)
    yield s
    await s.stop()


async def test_serves_index(server):
    async with aiohttp.ClientSession() as session:
        async with session.get(f"http://127.0.0.1:{server.port}/") as resp:
            assert resp.status == 200
            body = await resp.text()
            assert "Converse Code" in body


async def test_tab_ws_relay_and_hook(server):
    tab_audio, tab_json, hooks = [], [], []

    async def on_audio(b):
        tab_audio.append(b)

    async def on_json(m):
        tab_json.append(m)

    async def on_hook(event, payload):
        hooks.append((event, payload))

    server.on_tab_audio = on_audio
    server.on_tab_json = on_json
    server.on_hook = on_hook

    async with aiohttp.ClientSession() as session:
        async with session.ws_connect(f"http://127.0.0.1:{server.port}/ws") as ws:
            # tab -> server
            await ws.send_bytes(b"\x00\x01" * 100)
            await ws.send_str(json.dumps({"type": "playback_stopped", "remaining_ms": 5, "barge_seq": 1}))
            await asyncio.sleep(0.1)
            assert len(tab_audio[0]) == 200
            assert tab_json[0]["type"] == "playback_stopped"

            # server -> tab
            await server.send_json_to_tab({"type": "asr", "text": "hi"})
            await server.send_audio_to_tab(b"\x00\x00\x80\x3f" * 4)
            msg1 = await ws.receive(timeout=5)
            msg2 = await ws.receive(timeout=5)
            payloads = {msg1.type: msg1.data, msg2.type: msg2.data}
            assert json.loads(payloads[aiohttp.WSMsgType.TEXT])["type"] == "asr"
            assert len(payloads[aiohttp.WSMsgType.BINARY]) == 16

        # hook endpoint (as the curl in the Stop hook would call it)
        async with session.post(
            f"http://127.0.0.1:{server.port}/hook/stop",
            json={"transcript_path": "/tmp/t.jsonl", "session_id": "s1"},
        ) as resp:
            assert resp.status == 200
    assert hooks == [("stop", {"transcript_path": "/tmp/t.jsonl", "session_id": "s1"})]


async def test_sends_without_tab_are_noops(server):
    await server.send_json_to_tab({"type": "x"})
    await server.send_audio_to_tab(b"\x00")
