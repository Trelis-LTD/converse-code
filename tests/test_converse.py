import asyncio
import json

import pytest
import websockets
from aiohttp import web

from converse_code.converse import CredentialError, mint_session_credential, validate_key


async def test_validate_key_uses_non_billable_auth_frame():
    async def handler(ws):
        request = json.loads(await ws.recv())
        accepted = request.get("api_key") == "ck_good"
        await ws.send(json.dumps({"type": "ok" if accepted else "error"}))

    server = await websockets.serve(handler, "127.0.0.1", 0)
    url = f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}"
    try:
        assert await validate_key("ck_good", url=url) is True
        assert await validate_key("bad", url=url) is False
    finally:
        server.close()
        await server.wait_closed()


async def test_mint_session_credential_keeps_persistent_key_server_side():
    seen = {}

    async def issue(request):
        seen["authorization"] = request.headers.get("Authorization")
        seen["body"] = await request.json()
        return web.json_response({
            "api_key": "csk_scoped", "session_id": seen["body"]["session_id"],
            "expires_in": 600,
        }, status=201)

    app = web.Application()
    app.router.add_post("/api/v1/session-keys", issue)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    base = f"http://127.0.0.1:{runner.addresses[0][1]}"
    try:
        credential = await mint_session_credential("ck_persistent", "browser-session", base)
    finally:
        await runner.cleanup()

    assert seen == {
        "authorization": "Bearer ck_persistent",
        "body": {"session_id": "browser-session"},
    }
    assert credential == {
        "api_key": "csk_scoped", "session_id": "browser-session", "expires_in": 600,
    }


async def test_mint_session_credential_rejects_bad_upstream_response():
    async def issue(_request):
        return web.json_response({"error": "unauthorized"}, status=401)

    app = web.Application()
    app.router.add_post("/api/v1/session-keys", issue)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    base = f"http://127.0.0.1:{runner.addresses[0][1]}"
    try:
        with pytest.raises(CredentialError, match="401"):
            await mint_session_credential("bad", "browser-session", base)
    finally:
        await runner.cleanup()
