"""Local HTTP + WebSocket server. Three channels:

  /ws     status only — Claude Code's state/queue and the text injected into the
          terminal (JSON, this process -> page)
  /proxy  the Converse protocol, relayed between the page's SDK client and the
          broker (JSON + binary audio, both directions)
  /hook   Claude Code lifecycle hooks POST their payloads here

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
        self.on_tab_json: Callable[[dict], Awaitable[None]] | None = None
        self.on_hook: Callable[[str, dict], Awaitable[None]] | None = None
        # The page's SDK client speaks the Converse protocol over /proxy; the
        # process relays it to the broker, adding the tool manifest. Keeping this
        # separate from /ws means converse-code's own status messages never enter
        # the SDK's message stream.
        self.on_proxy_json: Callable[[dict], Awaitable[None]] | None = None
        self.on_proxy_audio: Callable[[bytes], Awaitable[None]] | None = None
        self.on_proxy_closed: Callable[[], Awaitable[None]] | None = None
        self._proxy_ws: web.WebSocketResponse | None = None
        self._tab: web.WebSocketResponse | None = None
        self._runner: web.AppRunner | None = None
        self._host = "127.0.0.1"
        self.port: int | None = None

    async def start(self, port: int = 0, host: str = "127.0.0.1") -> int:
        self._host = host
        app = web.Application()
        app.router.add_get("/", self._index)
        app.router.add_get("/assistant-transcript.js", self._assistant_transcript)
        app.router.add_get("/ws", self._ws)
        app.router.add_get("/proxy", self._proxy)
        app.router.add_get("/vendor/converse/{name}", self._vendor)
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
        for sock in (self._tab, self._proxy_ws):
            if sock is not None and not sock.closed:
                await sock.close()
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

    # -- handlers ----------------------------------------------------------

    # A cached page is indistinguishable from a fixed one, which has already cost
    # a debugging round — never let the browser keep these.
    NO_STORE = {"Cache-Control": "no-store, must-revalidate", "Pragma": "no-cache"}

    async def _index(self, request: web.Request) -> web.StreamResponse:
        if not self._authorized(request):
            return web.Response(status=403, text="missing or bad token")
        return web.FileResponse(WEB_DIR / "index.html", headers=self.NO_STORE)

    async def _assistant_transcript(self, _request: web.Request) -> web.StreamResponse:
        return web.FileResponse(
            WEB_DIR / "assistant-transcript.js",
            headers={**self.NO_STORE, "Content-Type": "application/javascript"},
        )

    async def _vendor(self, request: web.Request) -> web.StreamResponse:
        """Serve the vendored @trelis/converse SDK modules to the page.

        Deliberately not token-gated: an ES module's own static imports can't
        carry a query string, and these files are public npm code containing no
        secrets. The token still guards the page, the socket and the hook.
        """
        name = request.match_info["name"]
        if not name.endswith(".js") or "/" in name or ".." in name:
            return web.Response(status=404, text="not found")
        path = WEB_DIR / "vendor" / "converse" / name
        if not path.is_file():
            return web.Response(status=404, text="not found")
        return web.FileResponse(
            path, headers={**self.NO_STORE, "Content-Type": "application/javascript"}
        )

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
            if msg.type == WSMsgType.TEXT and self.on_tab_json:
                try:
                    await self.on_tab_json(json.loads(msg.data))
                except json.JSONDecodeError:
                    log.warning("bad JSON from tab: %.100s", msg.data)
        if self._tab is ws:
            self._tab = None
        return ws

    async def send_json_to_proxy(self, msg: dict) -> None:
        if self._proxy_ws is not None and not self._proxy_ws.closed:
            try:
                await self._proxy_ws.send_str(json.dumps(msg))
            except ConnectionError:
                pass

    async def send_audio_to_proxy(self, data: bytes) -> None:
        if self._proxy_ws is not None and not self._proxy_ws.closed:
            try:
                await self._proxy_ws.send_bytes(data)
            except ConnectionError:
                pass

    async def _proxy(self, request: web.Request) -> web.StreamResponse:
        if not self._authorized(request) or not self._same_origin(request):
            return web.Response(status=403, text="forbidden")
        ws = web.WebSocketResponse(heartbeat=30, max_msg_size=4 * 1024 * 1024)
        await ws.prepare(request)
        if self._proxy_ws is not None and not self._proxy_ws.closed:
            await self._proxy_ws.close()
        self._proxy_ws = ws
        log.info("SDK client connected")
        async for msg in ws:
            if msg.type == WSMsgType.BINARY and self.on_proxy_audio:
                await self.on_proxy_audio(msg.data)
            elif msg.type == WSMsgType.TEXT and self.on_proxy_json:
                try:
                    await self.on_proxy_json(json.loads(msg.data))
                except json.JSONDecodeError:
                    log.warning("bad JSON from SDK client: %.100s", msg.data)
        if self._proxy_ws is ws:
            self._proxy_ws = None
        if self.on_proxy_closed:
            await self.on_proxy_closed()
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
        # Native Claude Code HTTP hooks expect a JSON response body. An empty
        # object means "observed, no decision" and lets processing continue.
        return web.json_response({})
