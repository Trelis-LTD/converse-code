"""ID-correlated semantic control channel to a visible Pi TUI extension."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable


class PiTUIBridgeError(RuntimeError):
    pass


EventHandler = Callable[[dict], Awaitable[None] | None]
FrameSender = Callable[[dict], Awaitable[bool]]


class PiTUIBridge:
    def __init__(
        self,
        send: FrameSender,
        *,
        on_event: EventHandler | None = None,
        timeout: float = 15,
        connect_timeout: float = 15,
    ) -> None:
        self.send = send
        self.on_event = on_event
        self.timeout = timeout
        self.connect_timeout = connect_timeout
        self.connected = asyncio.Event()
        self._next_id = 1
        self._pending: dict[str, asyncio.Future] = {}

    async def set_connected(self, connected: bool) -> None:
        if connected:
            if self.connected.is_set():
                await self._emit({"type": "bridge_replaced"})
            self.connected.set()
            return
        self.connected.clear()
        error = PiTUIBridgeError("the visible Pi terminal disconnected")
        for future in tuple(self._pending.values()):
            if not future.done():
                future.set_exception(error)
        await self._emit({"type": "bridge_disconnect"})

    async def command(self, kind: str, **fields) -> dict:
        try:
            await asyncio.wait_for(self.connected.wait(), timeout=self.connect_timeout)
        except TimeoutError as exc:
            raise PiTUIBridgeError("the visible Pi terminal is not connected") from exc
        request_id = f"converse-{self._next_id}"
        self._next_id += 1
        future = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        try:
            if not await self.send({"id": request_id, "type": kind, **fields}):
                raise PiTUIBridgeError("the visible Pi terminal disconnected")
            try:
                response = await asyncio.wait_for(future, timeout=self.timeout)
            except TimeoutError as exc:
                raise PiTUIBridgeError(f"Pi did not acknowledge {kind}") from exc
        finally:
            self._pending.pop(request_id, None)
        if not response.get("success"):
            raise PiTUIBridgeError(str(response.get("error") or f"Pi rejected {kind}"))
        return response

    async def handle_message(self, frame: dict) -> None:
        if frame.get("type") == "response" and isinstance(frame.get("id"), str):
            future = self._pending.get(frame["id"])
            if future is not None and not future.done():
                future.set_result(frame)
            return
        await self._emit(frame)

    async def _emit(self, frame: dict) -> None:
        if self.on_event is None:
            return
        result = self.on_event(frame)
        if inspect.isawaitable(result):
            await result
