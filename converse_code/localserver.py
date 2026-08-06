"""Local HTTP + WebSocket server: serves the browser tab, relays audio and
captions to/from it, and receives Claude Code hook POSTs."""

import json
import logging
from pathlib import Path
from typing import Awaitable, Callable

from aiohttp import WSMsgType, web

log = logging.getLogger(__name__)
WEB_DIR = Path(__file__).parent / "web"


class LocalServer:
    """One browser tab at a time (last connection wins)."""

    def __init__(self) -> None:
        self.on_tab_audio: Callable[[bytes], Awaitable[None]] | None = None
        self.on_tab_json: Callable[[dict], Awaitable[None]] | None = None
        self.on_hook: Callable[[str, dict], Awaitable[None]] | None = None
        self._tab: web.WebSocketResponse | None = None
        self._runner: web.AppRunner | None = None
        self.port: int | None = None

    async def start(self, port: int = 0, host: str = "127.0.0.1") -> int:
        app = web.Application()
        app.router.add_get("/", self._index)
        app.router.add_get("/ws", self._ws)
        app.router.add_post("/hook/{event}", self._hook)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, host, port)
        await site.start()
        self.port = site._server.sockets[0].getsockname()[1]
        return self.port

    async def stop(self) -> None:
        if self._tab is not None and not self._tab.closed:
            await self._tab.close()
        if self._runner:
            await self._runner.cleanup()

    # -- outbound to the tab ---------------------------------------------

    async def send_json_to_tab(self, msg: dict) -> None:
        if self._tab is not None and not self._tab.closed:
            try:
                await self._tab.send_str(json.dumps(msg))
            except ConnectionError:
                pass

    async def send_audio_to_tab(self, data: bytes) -> None:
        if self._tab is not None and not self._tab.closed:
            try:
                await self._tab.send_bytes(data)
            except ConnectionError:
                pass

    # -- handlers ----------------------------------------------------------

    async def _index(self, _request: web.Request) -> web.FileResponse:
        return web.FileResponse(WEB_DIR / "index.html")

    async def _ws(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse(heartbeat=30)
        await ws.prepare(request)
        if self._tab is not None and not self._tab.closed:
            await self._tab.close()
        self._tab = ws
        log.info("browser tab connected")
        async for msg in ws:
            if msg.type == WSMsgType.BINARY and self.on_tab_audio:
                await self.on_tab_audio(msg.data)
            elif msg.type == WSMsgType.TEXT and self.on_tab_json:
                try:
                    await self.on_tab_json(json.loads(msg.data))
                except json.JSONDecodeError:
                    log.warning("bad JSON from tab: %.100s", msg.data)
        if self._tab is ws:
            self._tab = None
        return ws

    async def _hook(self, request: web.Request) -> web.Response:
        try:
            payload = await request.json()
        except json.JSONDecodeError:
            payload = {}
        if self.on_hook:
            await self.on_hook(request.match_info["event"], payload)
        return web.Response(text="ok")
