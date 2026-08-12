"""ID-correlated semantic control channel to a visible Pi TUI extension."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass


class PiTUIBridgeError(RuntimeError):
    pass


@dataclass(frozen=True)
class CommandReceipt:
    command_id: str
    data: dict[str, object]


FrameSender = Callable[[dict], Awaitable[bool]]
ACK_TIMEOUT = 15
CONNECT_TIMEOUT = 15


class PiTUIBridge:
    def __init__(self, send: FrameSender) -> None:
        self.send = send
        self.connected = asyncio.Event()
        self._next_id = 1
        self._pending: dict[str, asyncio.Future] = {}

    def connect(self) -> dict | None:
        replaced = self.connected.is_set()
        if replaced:
            self._fail_pending(PiTUIBridgeError("the visible Pi terminal was replaced"))
        self.connected.set()
        return {"type": "bridge_replaced"} if replaced else None

    def disconnect(self) -> dict:
        self.connected.clear()
        self._fail_pending(PiTUIBridgeError("the visible Pi terminal disconnected"))
        return {"type": "bridge_disconnect"}

    def _fail_pending(self, error: PiTUIBridgeError) -> None:
        for future in tuple(self._pending.values()):
            if not future.done():
                future.set_exception(error)

    async def command(self, kind: str, **fields) -> CommandReceipt:
        try:
            await asyncio.wait_for(self.connected.wait(), timeout=CONNECT_TIMEOUT)
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
                data = await asyncio.wait_for(future, timeout=ACK_TIMEOUT)
            except TimeoutError as exc:
                raise PiTUIBridgeError(f"Pi did not acknowledge {kind}") from exc
        finally:
            self._pending.pop(request_id, None)
        return CommandReceipt(request_id, data)

    async def handle_message(self, frame: dict) -> dict | None:
        if (
            frame.get("type") == "response"
            and isinstance(frame.get("id"), str)
            and type(frame.get("success")) is bool
        ):
            future = self._pending.get(frame["id"])
            if future is not None and not future.done():
                if frame["success"]:
                    future.set_result({
                        key: value for key, value in frame.items()
                        if key not in {"type", "id", "success"}
                    })
                else:
                    error = frame.get("error")
                    future.set_exception(PiTUIBridgeError(
                        error if isinstance(error, str) and error else "Pi rejected the command"
                    ))
            return None
        return frame
