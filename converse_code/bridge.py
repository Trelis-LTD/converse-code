"""Acknowledged controls between the Converse Browser SDK and a local tool host."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, NamedTuple


@dataclass
class Connected:
    sent: set[int] = field(default_factory=set)


class ToolCall(NamedTuple):
    call_id: str
    name: str
    arguments: dict


class BrowserBridge:
    """Keep outbound controls until browser JavaScript confirms SDK delivery."""

    def __init__(
        self,
        send_to_tab: Callable[[dict], Awaitable[bool]],
        trace: Callable[..., None],
    ) -> None:
        self._send_to_tab = send_to_tab
        self._pending: dict[int, dict] = {}
        self._next_seq = 1
        self._delivery: Connected | None = None
        self._lock = asyncio.Lock()
        self._trace_callback = trace

    def _trace(self, event: str, **data: Any) -> None:
        self._trace_callback("browser_bridge", event, **data)

    async def on_browser_disconnected(self) -> None:
        self._trace("disconnected")
        async with self._lock:
            self._delivery = None

    async def handle_browser_message(
        self,
        message: dict,
        *,
        on_tool_call: Callable[[ToolCall], Awaitable[None]],
        on_tool_cancel: Callable[[str], Awaitable[None]],
        on_session_end: Callable[[], Awaitable[None]],
    ) -> None:
        if message.get("type") != "local":
            return
        event = message.get("event")
        if event == "bridge_ready":
            self._trace("ready")
            async with self._lock:
                self._delivery = Connected()
            await self._flush()
        elif event == "bridge_ack":
            seq = message.get("seq")
            if isinstance(seq, int):
                frame = self._pending.get(seq, {})
                self._trace(
                    "control_acknowledged", seq=seq, action=frame.get("action"),
                )
                async with self._lock:
                    self._pending.pop(seq, None)
                    if self._delivery is not None:
                        self._delivery.sent.discard(seq)
        elif event == "tool_call":
            call = message.get("call")
            if isinstance(call, dict):
                call_id, name, arguments = call.get("id"), call.get("name"), call.get("args")
                if (
                    isinstance(call_id, str) and call_id
                    and isinstance(name, str) and name
                    and isinstance(arguments, dict)
                ):
                    await on_tool_call(ToolCall(call_id, name, arguments))
        elif event == "tool_cancel":
            call = message.get("call")
            call_id = call.get("id") if isinstance(call, dict) else None
            if isinstance(call_id, str) and call_id:
                await on_tool_cancel(call_id)
        elif event == "session_end":
            code, reason = message.get("code"), message.get("reason")
            if type(code) is int and code == 1000 and isinstance(reason, str):
                self._trace("session_end", code=code, reason=reason)
                await on_session_end()
        elif event == "end_session":
            self._trace("session_end", source="user")
            await on_session_end()
        elif event == "debug_trace":
            name = message.get("name")
            data = message.get("data")
            if isinstance(name, str) and isinstance(data, dict):
                self._trace_callback("browser", name, **data)

    async def _control(self, action: str, **fields) -> None:
        async with self._lock:
            seq = self._next_seq
            self._next_seq += 1
            self._pending[seq] = {
                "type": "local", "event": "bridge_control",
                "seq": seq, "action": action, **fields,
            }
            self._trace("control_queued", seq=seq, action=action, fields=fields)
        await self._flush()

    async def _flush(self) -> None:
        async with self._lock:
            delivery = self._delivery
            if delivery is None:
                return
            for seq, frame in self._pending.items():
                if seq in delivery.sent:
                    continue
                if not await self._send_to_tab(frame):
                    self._trace("control_send_failed", seq=seq, action=frame.get("action"))
                    self._delivery = None
                    return
                delivery.sent.add(seq)
                self._trace("control_sent", seq=seq, action=frame.get("action"))

    async def send_tool_result(
        self, call_id: str, content: dict, *,
        outcome: str = "unknown", verified: bool = False,
    ) -> None:
        await self._control(
            "tool_result", id=call_id, content=content,
            outcome=outcome, verified=verified,
        )

    async def send_tool_deferred(
        self, call_id: str, handle: str, status_label: str | None = None,
    ) -> None:
        fields = {"id": call_id, "handle": handle}
        if status_label:
            fields["status_label"] = status_label
        await self._control("tool_deferred", **fields)

    async def send_tool_partial_result(
        self,
        call_id: str,
        content: dict,
        *,
        interaction: dict | None = None,
    ) -> None:
        fields = {"id": call_id, "content": content}
        if interaction is not None:
            fields["interaction"] = interaction
        await self._control("tool_partial_result", **fields)
