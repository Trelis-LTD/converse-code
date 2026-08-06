"""Local HTTP + WebSocket server: serves the browser tab, relays audio and
captions to/from it, and receives Claude Code hook POSTs.

Everything here is reachable from any web page the dev happens to have open —
browsers don't apply same-origin policy to WebSockets, and a simple-content-type
POST needs no preflight. So both endpoints require a per-run secret token
(`?t=…`), the page is served only to a request carrying it, and WebSocket
upgrades additionally must come from our own origin. Without this, a background
ad frame could evict the real tab, listen to the conversation, or forge a
"Claude finished" hook whose text gets spoken to the dev.
"""

import hmac
import json
import logging
import secrets
from pathlib import Path
from typing import Awaitable, Callable
from urllib.parse import urlparse

from aiohttp import WSMsgType, web

log = logging.getLogger(__name__)
WEB_DIR = Path(__file__).parent / "web"


class LocalServer:
    """One browser tab at a time (last authenticated connection wins)."""

    def __init__(self, token: str | None = None) -> None:
        self.token = token or secrets.token_urlsafe(24)
        self.on_tab_audio: Callable[[bytes], Awaitable[None]] | None = None
        self.on_tab_json: Callable[[dict], Awaitable[None]] | None = None
        self.on_hook: Callable[[str, dict], Awaitable[None]] | None = None
        self._tab: web.WebSocketResponse | None = None
        self._runner: web.AppRunner | None = None
        self._host = "127.0.0.1"
        self.port: int | None = None

    async def start(self, port: int = 0, host: str = "127.0.0.1") -> int:
        self._host = host
        app = web.Application()
        app.router.add_get("/", self._index)
        app.router.add_get("/ws", self._ws)
        app.router.add_post("/hook/{event}", self._hook)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, host, port)
        await site.start()
        self.port = self._runner.addresses[0][1]
        return self.port

    @property
    def url(self) -> str:
        return f"http://{self._host}:{self.port}/?t={self.token}"

    def hook_url(self, event: str) -> str:
        return f"http://{self._host}:{self.port}/hook/{event}?t={self.token}"

    async def stop(self) -> None:
        if self._tab is not None and not self._tab.closed:
            await self._tab.close()
        if self._runner:
            await self._runner.cleanup()

    # -- auth --------------------------------------------------------------

    def _authorized(self, request: web.Request) -> bool:
        supplied = request.query.get("t") or request.headers.get("X-Converse-Code-Token", "")
        return hmac.compare_digest(supplied, self.token)

    def _same_origin(self, request: web.Request) -> bool:
        """WebSocket upgrades must come from the page we served (or a non-browser
        client, which sends no Origin at all)."""
        origin = request.headers.get("Origin")
        if origin is None:
            return True
        host = urlparse(origin).hostname
        return host in ("127.0.0.1", "localhost", "::1")

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

    async def _index(self, request: web.Request) -> web.StreamResponse:
        if not self._authorized(request):
            return web.Response(status=403, text="missing or bad token")
        return web.FileResponse(WEB_DIR / "index.html")

    async def _ws(self, request: web.Request) -> web.StreamResponse:
        if not self._authorized(request):
            # Usually a stale tab from a previous run retrying with an old token,
            # not an attack — hence debug, not warning.
            log.debug("rejected websocket with bad token")
            return web.Response(status=403, text="stale or missing token — reopen the printed URL")
        if not self._same_origin(request):
            log.warning("rejected websocket from foreign origin %r", request.headers.get("Origin"))
            return web.Response(status=403, text="forbidden")
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
        if not self._authorized(request):
            log.warning("rejected unauthenticated hook POST")
            return web.Response(status=403, text="forbidden")
        try:
            payload = await request.json()
        except (json.JSONDecodeError, ValueError):
            payload = {}
        if self.on_hook:
            await self.on_hook(request.match_info["event"], payload)
        return web.Response(text="ok")
