"""Raw WebSocket client for the Converse broker, per the public protocol docs
(converse.trelis.com/docs/api/websocket/). No SDK — the wire contract is small:

  up:   start frame (audio 16k PCM16 + mode.tools), binary mic frames,
        tool_result / tool_progress / tool_cancel / inject_context,
        client_event (playback_stopped), auth (validation only)
  down: tool_call, binary PCM16 assistant-audio frames (see audio.py — the
        encoding is pinned in the start frame, not inferred), asr / turn /
        text_delta / utterance / done / interrupted / bye (passed through)
"""

import asyncio
import json
import logging
from collections import deque
from typing import Awaitable, Callable

import websockets

from . import audio as audiofmt
from .converse import DEFAULT_WS_URL, validate_key

log = logging.getLogger(__name__)

DEFAULT_URL = DEFAULT_WS_URL


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
        self.on_tool_cancel: Callable[[dict], Awaitable[None]] | None = None
        self.on_json: Callable[[dict], Awaitable[None]] | None = None  # non-tool messages
        self.on_audio: Callable[[bytes], Awaitable[None]] | None = None
        self.closed = asyncio.Event()
        self._ws: websockets.WebSocketClientProtocol | None = None
        self._outbox: deque[dict] = deque()
        self._send_lock = asyncio.Lock()

    async def connect(self, start_frame: dict | None = None) -> None:
        """Open (or re-open) the broker session.

        One client is reused across reconnects — the page's SDK sends a fresh
        start frame whenever the tab reloads or its socket blips. `closed` must be
        reset here: it gates every send, so leaving it set from the previous cycle
        silently swallows every tool result for the rest of the process's life
        while everything still *looks* connected.
        """
        previous = self._ws
        if previous is not None:
            await previous.close()   # no-op if it already closed
        # Cap inbound frames generously (TTS audio chunks are small) rather than
        # disabling the limit — an unbounded cap lets a misbehaving endpoint
        # force arbitrary allocations.
        ws = await websockets.connect(self.url, max_size=4 * 1024 * 1024)
        self._ws = ws
        self.closed.clear()
        if start_frame is not None:
            await ws.send(json.dumps(start_frame))
        else:
            await ws.send(json.dumps({
                "type": "start",
                "session_id": self.session_id,
                "api_key": self.api_key,
                # Pin the downlink encoding rather than relying on the server default,
                # which has changed once already (pcm_f32le -> pcm16).
                "audio": {"sr": audiofmt.SAMPLE_RATE, "output_encoding": audiofmt.OUTPUT_ENCODING},
                "mode": {"kind": "converse", "web_search": False, "tools": self.tools},
                "client": self.client_info,
            }))
        await self._flush_outbox(ws)

    async def run(self) -> None:
        """Receive loop; returns when the connection closes."""
        ws = self._ws
        if ws is None:
            self.closed.set()
            return
        try:
            async for msg in ws:
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
                elif data.get("type") == "tool_cancel" and self.on_tool_cancel:
                    await self.on_tool_cancel(data)
                elif self.on_json:
                    await self.on_json(data)
        except websockets.ConnectionClosed as exc:
            log.info("broker connection closed: %s", exc)
        finally:
            if self._ws is ws:
                self.closed.set()

    async def close(self) -> None:
        if self._ws is not None:
            await self._ws.close()

    # -- senders ------------------------------------------------------------

    async def _flush_outbox(self, ws) -> None:
        async with self._send_lock:
            while self._outbox and self._ws is ws and not self.closed.is_set():
                payload = self._outbox[0]
                try:
                    await ws.send(json.dumps(payload))
                except websockets.ConnectionClosed:
                    self.closed.set()
                    return
                self._outbox.popleft()

    async def _send(self, payload: dict | bytes, *, durable: bool = False) -> None:
        async with self._send_lock:
            ws = self._ws
            if ws is None or self.closed.is_set():
                if durable and isinstance(payload, dict):
                    self._outbox.append(payload)
                return
            try:
                await ws.send(payload if isinstance(payload, bytes) else json.dumps(payload))
            except websockets.ConnectionClosed:
                if durable and isinstance(payload, dict):
                    self._outbox.append(payload)
                if self._ws is ws:
                    self.closed.set()
            return

    async def send_audio(self, pcm16: bytes) -> None:
        await self._send(pcm16)

    async def send_tool_result(self, call_id: str, content: dict) -> None:
        await self._send({"type": "tool_result", "id": call_id, "content": content}, durable=True)

    async def send_tool_progress(self, call_id: str, note: str) -> None:
        await self._send(
            {"type": "tool_progress", "id": call_id, "note": note[:500]}, durable=True
        )

    async def send_tool_deferred(self, call_id: str, handle: str,
                                 status_label: str | None = None) -> None:
        """Detach a call from its voice turn; the terminal result still follows."""
        frame = {"type": "tool_deferred", "id": call_id, "handle": handle}
        if status_label:
            frame["status_label"] = status_label
        await self._send(frame, durable=True)

    async def send_tool_partial_result(self, call_id: str, content: dict,
                                       reply: bool = False) -> None:
        """Structured milestone before the terminal result; reply=True asks the
        brain to announce it now (silently skipped if the floor is occupied)."""
        frame = {"type": "tool_partial_result", "id": call_id, "content": content}
        if reply:
            frame["reply"] = True
        await self._send(frame, durable=True)

    async def send_tool_cancel(self, call_id: str) -> None:
        await self._send({"type": "tool_cancel", "id": call_id}, durable=True)

    async def send_client_event(self, event: str, **fields) -> None:
        await self._send({"type": "client_event", "event": event, **fields})

    async def send_context(self, text: str, role: str = "context", reply: bool = False) -> None:
        """Push host context into the conversation and optionally trigger a reply."""
        await self._send({
            "type": "inject_context",
            "text": text[:2000],
            "role": role,
            "reply": reply,
        }, durable=True)

    async def send_raw(self, payload: dict | bytes) -> None:
        """Relay a frame from the browser's SDK client straight through."""
        await self._send(payload)
