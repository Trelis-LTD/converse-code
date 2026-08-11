"""Acknowledged tool/control bridge between the Browser SDK and Claude Code."""

import asyncio
from collections import OrderedDict
from typing import Awaitable, Callable


class BrowserBridge:
    """Keep outbound controls until browser JavaScript confirms SDK delivery."""

    def __init__(self, send_to_tab: Callable[[dict], Awaitable[bool]]) -> None:
        self._send_to_tab = send_to_tab
        self._pending: OrderedDict[int, dict] = OrderedDict()
        self._sent: set[int] = set()
        self._next_seq = 1
        self._ready = False
        self._lock = asyncio.Lock()
        self.on_tool_call: Callable[[dict], Awaitable[None]] | None = None
        self.on_tool_cancel: Callable[[dict], Awaitable[None]] | None = None

    async def on_browser_disconnected(self) -> None:
        async with self._lock:
            self._ready = False
            self._sent.clear()

    async def handle_browser_message(self, message: dict) -> None:
        if message.get("type") != "local":
            return
        event = message.get("event")
        if event == "bridge_ready":
            async with self._lock:
                self._ready = True
                self._sent.clear()
            await self._flush()
        elif event in {"bridge_ack", "bridge_reject"}:
            seq = message.get("seq")
            if isinstance(seq, int):
                async with self._lock:
                    self._pending.pop(seq, None)
                    self._sent.discard(seq)
        elif event == "tool_call" and self.on_tool_call:
            call = message.get("call")
            if isinstance(call, dict):
                await self.on_tool_call(call)
        elif event == "tool_cancel" and self.on_tool_cancel:
            call = message.get("call")
            if isinstance(call, dict):
                await self.on_tool_cancel(call)

    async def _control(self, action: str, **fields) -> None:
        async with self._lock:
            seq = self._next_seq
            self._next_seq += 1
            self._pending[seq] = {
                "type": "local", "event": "bridge_control",
                "seq": seq, "action": action, **fields,
            }
        await self._flush()

    async def _flush(self) -> None:
        async with self._lock:
            if not self._ready:
                return
            for seq, frame in self._pending.items():
                if seq in self._sent:
                    continue
                if not await self._send_to_tab(frame):
                    self._ready = False
                    self._sent.clear()
                    return
                self._sent.add(seq)

    async def send_tool_result(
        self, call_id: str, content: dict, *,
        outcome: str = "unknown", verified: bool = False,
    ) -> None:
        await self._control(
            "tool_result", id=call_id, content=content,
            outcome=outcome, verified=verified,
        )

    async def send_tool_progress(self, call_id: str, note: str) -> None:
        await self._control("tool_progress", id=call_id, note=note[:500])

    async def send_tool_deferred(
        self, call_id: str, handle: str, status_label: str | None = None,
    ) -> None:
        fields = {"id": call_id, "handle": handle}
        if status_label:
            fields["status_label"] = status_label
        await self._control("tool_deferred", **fields)

    async def send_tool_partial_result(
        self, call_id: str, content: dict, reply: bool = False,
    ) -> None:
        await self._control("tool_partial_result", id=call_id, content=content, reply=reply)

    async def send_context(
        self, text: str, role: str = "context", reply: bool = False,
    ) -> None:
        await self._control(
            "inject_context", text=text[:2000], role=role, reply=reply,
        )
