"""LocalServer: static page, tab WebSocket relay, hook endpoint, and the token
/ origin gate that keeps other web pages and local processes out."""

import asyncio
import json

import aiohttp
import pytest

from converse_code.localserver import LocalServer


@pytest.fixture
async def server():
    s = LocalServer(token="tok123")
    await s.start(port=0)
    yield s
    await s.stop()


def base(server):
    return f"http://127.0.0.1:{server.port}"


async def test_serves_index_with_token(server):
    async with aiohttp.ClientSession() as session:
        async with session.get(f"{base(server)}/?t=tok123") as resp:
            assert resp.status == 200
            assert "Converse Code" in await resp.text()


async def test_index_requires_token(server):
    async with aiohttp.ClientSession() as session:
        for url in (f"{base(server)}/", f"{base(server)}/?t=wrong"):
            async with session.get(url) as resp:
                assert resp.status == 403


async def test_hook_requires_token(server):
    """A forged hook would put attacker-chosen words in Claude's mouth."""
    hooks = []
    server.on_hook = lambda e, p: hooks.append((e, p)) or asyncio.sleep(0)
    async with aiohttp.ClientSession() as session:
        async with session.post(f"{base(server)}/hook/stop", json={"x": 1}) as resp:
            assert resp.status == 403
        async with session.post(f"{base(server)}/hook/stop?t=wrong", json={"x": 1}) as resp:
            assert resp.status == 403
    assert hooks == []


async def test_ws_requires_token(server):
    async with aiohttp.ClientSession() as session:
        with pytest.raises(aiohttp.WSServerHandshakeError) as exc:
            async with session.ws_connect(f"{base(server)}/ws"):
                pass
        assert exc.value.status == 403


async def test_ws_rejects_foreign_origin(server):
    """Browsers don't apply same-origin policy to WebSockets, so the server must."""
    async with aiohttp.ClientSession() as session:
        with pytest.raises(aiohttp.WSServerHandshakeError) as exc:
            async with session.ws_connect(
                f"{base(server)}/ws?t=tok123", headers={"Origin": "https://evil.example"}
            ):
                pass
        assert exc.value.status == 403


async def test_proxy_requires_token_and_local_origin():
    """The SDK client's socket carries the whole session — it needs the same gate
    as the status channel, or any page could open a billable session."""
    s = LocalServer(token="tok123")
    await s.start(port=0)
    try:
        url = f"http://127.0.0.1:{s.port}/proxy"
        async with aiohttp.ClientSession() as session:
            for bad in (url, f"{url}?t=wrong"):
                with pytest.raises(aiohttp.WSServerHandshakeError) as exc:
                    async with session.ws_connect(bad):
                        pass
                assert exc.value.status == 403
            with pytest.raises(aiohttp.WSServerHandshakeError) as exc:
                async with session.ws_connect(f"{url}?t=tok123",
                                              headers={"Origin": "https://evil.example"}):
                    pass
            assert exc.value.status == 403
    finally:
        await s.stop()


async def test_proxy_relays_both_directions(server):
    """JSON and binary must pass through untouched in both directions."""
    from_page_json, from_page_audio, closed = [], [], []
    server.on_proxy_json = lambda m: from_page_json.append(m) or asyncio.sleep(0)
    server.on_proxy_audio = lambda b: from_page_audio.append(b) or asyncio.sleep(0)
    server.on_proxy_closed = lambda: closed.append(True) or asyncio.sleep(0)

    async with aiohttp.ClientSession() as session:
        async with session.ws_connect(
            f"http://127.0.0.1:{server.port}/proxy?t=tok123",
            headers={"Origin": f"http://127.0.0.1:{server.port}"},
        ) as ws:
            await ws.send_str(json.dumps({"type": "start", "session_id": "x"}))
            await ws.send_bytes(b"\x01\x02" * 160)
            await asyncio.sleep(0.1)
            assert from_page_json[0]["type"] == "start"
            assert len(from_page_audio[0]) == 320

            await server.send_json_to_proxy({"type": "asr", "text": "hi"})
            await server.send_audio_to_proxy(b"\x00\x01" * 320)
            first = await ws.receive(timeout=5)
            second = await ws.receive(timeout=5)
            got = {first.type: first.data, second.type: second.data}
            assert json.loads(got[aiohttp.WSMsgType.TEXT])["type"] == "asr"
            assert len(got[aiohttp.WSMsgType.BINARY]) == 640
    await asyncio.sleep(0.1)
    assert closed == [True]


async def test_tab_ws_relay_and_hook(server):
    tab_json, hooks = [], []

    async def on_json(m):
        tab_json.append(m)

    async def on_hook(event, payload):
        hooks.append((event, payload))

    server.on_tab_json = on_json
    server.on_hook = on_hook

    async with aiohttp.ClientSession() as session:
        async with session.ws_connect(
            f"{base(server)}/ws?t=tok123", headers={"Origin": f"http://127.0.0.1:{server.port}"}
        ) as ws:
            # tab -> server (status channel is JSON only)
            await ws.send_str(json.dumps({"type": "hello"}))
            await asyncio.sleep(0.1)
            assert tab_json[0]["type"] == "hello"

            # server -> tab
            await server.send_json_to_tab({"type": "local", "event": "status", "state": "idle"})
            msg = await ws.receive(timeout=5)
            assert json.loads(msg.data)["event"] == "status"

        # hook endpoint (as the curl in the Stop hook would call it)
        async with session.post(
            server.hook_url("stop"),
            json={"transcript_path": "/tmp/t.jsonl", "session_id": "s1"},
        ) as resp:
            assert resp.status == 200
    assert hooks == [("stop", {"transcript_path": "/tmp/t.jsonl", "session_id": "s1"})]


async def test_urls_carry_token(server):
    assert "t=tok123" in server.url
    assert server.hook_url("stop").endswith("/hook/stop?t=tok123")


async def test_sends_without_a_connected_page_are_noops(server):
    await server.send_json_to_tab({"type": "x"})
    await server.send_json_to_proxy({"type": "x"})
    await server.send_audio_to_proxy(b"\x00")
