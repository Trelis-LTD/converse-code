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


async def test_serves_assistant_transcript_script_without_cache(server):
    async with aiohttp.ClientSession() as session:
        async with session.get(f"{base(server)}/assistant-transcript.js") as resp:
            assert resp.status == 200
            assert resp.content_type == "application/javascript"
            assert "no-store" in resp.headers["Cache-Control"]
            assert "AssistantTranscript" in await resp.text()


async def test_serves_typed_turn_controller_without_cache(server):
    async with aiohttp.ClientSession() as session:
        async with session.get(f"{base(server)}/typed-turn.js") as resp:
            assert resp.status == 200
            assert resp.content_type == "application/javascript"
            assert "no-store" in resp.headers["Cache-Control"]
            assert "TypedTurnController" in await resp.text()


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


async def test_session_credential_requires_token_and_local_origin():
    s = LocalServer(token="tok123")
    await s.start(port=0)
    try:
        url = f"http://127.0.0.1:{s.port}/session-credential"
        async with aiohttp.ClientSession() as session:
            for bad in (url, f"{url}?t=wrong"):
                async with session.post(bad, json={"session_id": "s1"}) as response:
                    assert response.status == 403
            async with session.post(
                f"{url}?t=tok123", json={"session_id": "s1"},
                headers={"Origin": "https://evil.example"},
            ) as response:
                assert response.status == 403
    finally:
        await s.stop()


async def test_session_credential_is_minted_by_python(server):
    requested = []
    server.on_session_credential = lambda sid: requested.append(sid) or asyncio.sleep(0, result={
        "api_key": "csk_scoped", "session_id": sid, "expires_in": 600, "tools": [],
    })
    async with aiohttp.ClientSession() as session:
        async with session.post(
            f"http://127.0.0.1:{server.port}/session-credential?t=tok123",
            headers={"Origin": f"http://127.0.0.1:{server.port}"},
            json={"session_id": "browser-session"},
        ) as response:
            assert response.status == 201
            assert (await response.json())["api_key"] == "csk_scoped"
    assert requested == ["browser-session"]


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

        # hook endpoint (as Claude Code's native HTTP hook calls it)
        async with session.post(
            server.hook_url("stop"),
            json={"transcript_path": "/tmp/t.jsonl", "session_id": "s1"},
        ) as resp:
            assert resp.status == 200
    assert hooks == [("stop", {"transcript_path": "/tmp/t.jsonl", "session_id": "s1"})]


async def test_urls_carry_token(server):
    assert "t=tok123" in server.url
    assert server.hook_url("stop").endswith("/hook/stop?t=tok123")


async def test_send_without_a_connected_page_reports_false(server):
    assert await server.send_json_to_tab({"type": "x"}) is False
