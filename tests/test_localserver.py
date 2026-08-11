import asyncio
import json

import aiohttp
import pytest

from converse_code.localserver import LocalServer


@pytest.fixture
async def server():
    instance = LocalServer(token="test-token")
    await instance.start(port=0)
    try:
        yield instance
    finally:
        await instance.stop()


def base(server):
    return f"http://127.0.0.1:{server.port}"


async def test_page_is_token_gated_and_not_cached(server):
    async with aiohttp.ClientSession() as session:
        async with session.get(f"{base(server)}/") as denied:
            assert denied.status == 403
        async with session.get(f"{base(server)}/?t=test-token") as response:
            assert response.status == 200
            assert "no-store" in response.headers["Cache-Control"]


async def test_credential_endpoint_is_origin_gated_and_delegated(server):
    requested = []
    server.on_session_credential = lambda session_id: (
        requested.append(session_id) or asyncio.sleep(0, result={
            "api_key": "scoped", "session_id": session_id, "expires_in": 600, "tools": [],
        })
    )
    url = f"{base(server)}/session-credential?t=test-token"
    async with aiohttp.ClientSession() as session:
        async with session.post(
            url, json={"session_id": "session-1"}, headers={"Origin": "https://evil.example"},
        ) as denied:
            assert denied.status == 403
        async with session.post(
            url, json={"session_id": "session-1"},
            headers={"Origin": f"http://127.0.0.1:{server.port}"},
        ) as response:
            assert response.status == 201
            assert (await response.json())["api_key"] == "scoped"
    assert requested == ["session-1"]


async def test_websocket_is_gated_and_relays_both_directions(server):
    received = []
    server.on_tab_json = lambda frame: received.append(frame) or asyncio.sleep(0)
    async with aiohttp.ClientSession() as session:
        with pytest.raises(aiohttp.WSServerHandshakeError):
            await session.ws_connect(f"{base(server)}/ws")
        async with session.ws_connect(
            f"{base(server)}/ws?t=test-token",
            headers={"Origin": f"http://127.0.0.1:{server.port}"},
        ) as websocket:
            await websocket.send_json({"type": "local", "event": "bridge_ready"})
            await asyncio.sleep(0.01)
            assert received[-1]["event"] == "bridge_ready"
            assert await server.send_json_to_tab({"type": "local", "event": "ping"})
            assert json.loads((await websocket.receive()).data)["event"] == "ping"


async def test_send_without_connected_page_returns_false(server):
    assert await server.send_json_to_tab({"type": "local"}) is False
