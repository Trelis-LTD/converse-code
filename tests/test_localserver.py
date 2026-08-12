import asyncio
import json

import aiohttp
import pytest

from converse_code.localserver import LocalServer, ServerHandlers


@pytest.fixture
async def server():
    received = {"tab": [], "pi": [], "credentials": []}

    async def ignore():
        pass

    async def tab_message(frame):
        received["tab"].append(frame)

    async def pi_message(frame):
        received["pi"].append(frame)

    async def credential(session_id):
        received["credentials"].append(session_id)
        return {
            "api_key": "scoped", "session_id": session_id, "expires_in": 600, "tools": [],
        }

    instance = LocalServer(ServerHandlers(
        tab_message=tab_message, tab_closed=ignore,
        pi_message=pi_message, pi_connected=ignore, pi_closed=ignore,
        session_credential=credential,
    ))
    await instance.start(port=0)
    try:
        yield instance, received
    finally:
        await instance.stop()


def base(server):
    return f"http://127.0.0.1:{server.port}"


async def test_page_is_token_gated_and_not_cached(server):
    server, _ = server
    async with aiohttp.ClientSession() as session:
        async with session.get(f"{base(server)}/") as denied:
            assert denied.status == 403
        async with session.get(server.url) as response:
            assert response.status == 200
            assert "no-store" in response.headers["Cache-Control"]


async def test_credential_endpoint_is_origin_gated_and_delegated(server):
    server, received = server
    url = f"{base(server)}/session-credential?t={server.token}"
    async with aiohttp.ClientSession() as session:
        async with session.post(
            url, json={"session_id": "session-1"}, headers={"Origin": "https://evil.example"},
        ) as denied:
            assert denied.status == 403
        async with session.post(
            url, json={"session_id": "session-1"},
            headers={"Origin": "http://127.0.0.1:1"},
        ) as denied:
            assert denied.status == 403
        async with session.post(
            url, json={"session_id": "session-1"},
            headers={"Origin": f"http://127.0.0.1:{server.port}"},
        ) as response:
            assert response.status == 201
            assert (await response.json())["api_key"] == "scoped"
    assert received["credentials"] == ["session-1"]


async def test_websocket_is_gated_and_relays_both_directions(server):
    server, received = server
    async with aiohttp.ClientSession() as session:
        with pytest.raises(aiohttp.WSServerHandshakeError):
            await session.ws_connect(f"{base(server)}/ws")
        async with session.ws_connect(
            f"{base(server)}/ws?t={server.token}",
            headers={"Origin": f"http://127.0.0.1:{server.port}"},
        ) as websocket:
            await websocket.send_json({"type": "local", "event": "bridge_ready"})
            await asyncio.sleep(0.01)
            assert received["tab"][-1]["event"] == "bridge_ready"
            assert await server.send_json_to_tab({"type": "local", "event": "ping"})
            assert json.loads((await websocket.receive()).data)["event"] == "ping"

async def test_pi_extension_socket_is_separate_authenticated_and_bidirectional(server):
    server, received = server
    async with aiohttp.ClientSession() as session:
        with pytest.raises(aiohttp.WSServerHandshakeError):
            await session.ws_connect(f"{base(server)}/pi")
        async with session.ws_connect(f"{base(server)}/pi?t={server.token}") as websocket:
            await websocket.send_json({"type": "agent_start"})
            await asyncio.sleep(0.01)
            assert received["pi"] == [{"type": "agent_start"}]
            assert await server.send_json_to_pi({"id": "command-1", "type": "prompt"})
            assert json.loads((await websocket.receive()).data) == {
                "id": "command-1", "type": "prompt",
            }
    assert await server.send_json_to_pi({"type": "prompt"}) is False
