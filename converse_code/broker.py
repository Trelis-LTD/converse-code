"""Raw WebSocket client for the Converse broker, per the public protocol docs
(converse.trelis.com/docs/api/websocket/). No SDK — the wire contract is small:

  up:   start frame (audio 16k PCM16 + mode.tools), binary mic frames,
        tool_result / tool_progress / tool_partial_result / tool_cancel,
        client_event (playback_stopped), auth (validation only)
  down: tool_call, binary PCM16 assistant-audio frames (see audio.py — the
        encoding is pinned in the start frame, not inferred), asr / turn /
        text_delta / utterance / done / interrupted / bye (passed through)
"""

import asyncio
import json
import logging
from typing import Awaitable, Callable

import websockets

from . import audio as audiofmt

log = logging.getLogger(__name__)

DEFAULT_URL = "wss://converse.trelis.com/ws"


class AuthError(Exception):
    pass


async def validate_key(api_key: str, url: str = DEFAULT_URL) -> bool:
    """Check a key with the non-billable auth frame."""
    async with websockets.connect(url) as ws:
        await ws.send(json.dumps({"type": "auth", "api_key": api_key}))
        reply = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
        return reply.get("type") == "ok"


class BrokerClient:
    def __init__(
        self,
        api_key: str,
        session_id: str,
        tools: list[dict],
        url: str = DEFAULT_URL,
        client_info: dict | None = None,
    ):
        self.api_key = api_key
        self.session_id = session_id
        self.tools = tools
        self.url = url
        self.client_info = client_info or {}
        self.on_tool_call: Callable[[dict], Awaitable[None]] | None = None
        self.on_json: Callable[[dict], Awaitable[None]] | None = None  # non-tool messages
        self.on_audio: Callable[[bytes], Awaitable[None]] | None = None
        self.closed = asyncio.Event()
        self._ws: websockets.WebSocketClientProtocol | None = None

    async def connect(self, start_frame: dict | None = None) -> None:
        """Open (or re-open) the broker session.

        One client is reused across reconnects — the page's SDK sends a fresh
        start frame whenever the tab reloads or its socket blips. `closed` must be
        reset here: it gates every send, so leaving it set from the previous cycle
        silently swallows every tool result for the rest of the process's life
        while everything still *looks* connected.
        """
        if self._ws is not None:
            await self._ws.close()   # no-op if it already closed
        self.closed.clear()
        # Cap inbound frames generously (TTS audio chunks are small) rather than
        # disabling the limit — an unbounded cap lets a misbehaving endpoint
        # force arbitrary allocations.
        self._ws = await websockets.connect(self.url, max_size=4 * 1024 * 1024)
        if start_frame is not None:
            await self._ws.send(json.dumps(start_frame))
            return
        await self._ws.send(json.dumps({
            "type": "start",
            "session_id": self.session_id,
            "api_key": self.api_key,
            # Pin the downlink encoding rather than relying on the server default,
            # which has changed once already (pcm_f32le -> pcm16).
            "audio": {"sr": audiofmt.SAMPLE_RATE, "output_encoding": audiofmt.OUTPUT_ENCODING},
            "mode": {"kind": "converse", "web_search": False, "tools": self.tools},
            "client": self.client_info,
        }))

    async def run(self) -> None:
        """Receive loop; returns when the connection closes."""
        try:
            async for msg in self._ws:
                if isinstance(msg, bytes):
                    if self.on_audio:
                        await self.on_audio(msg)
                    continue
                try:
                    data = json.loads(msg)
                except json.JSONDecodeError:
                    log.warning("bad JSON from broker: %.100s", msg)
                    continue
                if data.get("type") == "tool_call" and self.on_tool_call:
                    await self.on_tool_call(data)
                elif self.on_json:
                    await self.on_json(data)
        except websockets.ConnectionClosed as exc:
            log.info("broker connection closed: %s", exc)
        finally:
            self.closed.set()

    async def close(self) -> None:
        if self._ws is not None:
            await self._ws.close()

    # -- senders (all no-ops once closed; callers shouldn't have to care) ----

    async def _send(self, payload: dict | bytes) -> None:
        if self._ws is None or self.closed.is_set():
            return
        try:
            await self._ws.send(payload if isinstance(payload, bytes) else json.dumps(payload))
        except websockets.ConnectionClosed:
            self.closed.set()

    async def send_audio(self, pcm16: bytes) -> None:
        await self._send(pcm16)

    async def send_tool_result(self, call_id: str, content: dict) -> None:
        await self._send({"type": "tool_result", "id": call_id, "content": content})

    async def send_tool_partial_result(self, call_id: str, content: dict, reply: bool = False) -> None:
        await self._send({"type": "tool_partial_result", "id": call_id, "content": content, "reply": reply})

    async def send_tool_progress(self, call_id: str, note: str) -> None:
        await self._send({"type": "tool_progress", "id": call_id, "note": note[:500]})

    async def send_tool_cancel(self, call_id: str) -> None:
        await self._send({"type": "tool_cancel", "id": call_id})

    async def send_client_event(self, event: str, **fields) -> None:
        await self._send({"type": "client_event", "event": event, **fields})

    async def send_raw(self, payload: dict | bytes) -> None:
        """Relay a frame from the browser's SDK client straight through."""
        await self._send(payload)
