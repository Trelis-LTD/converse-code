"""JSONL control channel for running Claude Code without a terminal UI.

The channel is deliberately stdio rather than another localhost socket: only
the process that launched Converse Code can control it, and every event is easy
to consume from a shell, editor, or another agent. stdout is protocol-only;
diagnostics continue to use stderr/the normal log.
"""

import asyncio
import json
import os
import sys
from typing import TextIO


MAX_CONTROL_LINE_BYTES = 64 * 1024


class JsonLineBridge:
    """ToolRouter sender that writes one durable, machine-readable event per line."""

    def __init__(self, stream: TextIO):
        self.stream = stream
        self._lock = asyncio.Lock()

    async def emit(self, event: dict) -> None:
        async with self._lock:
            self.stream.write(json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n")
            self.stream.flush()

    async def send_tool_result(
        self, call_id: str, content: dict, *,
        outcome: str = "unknown", verified: bool = False,
    ) -> None:
        await self.emit({
            "type": "tool_result", "id": call_id, "content": content,
            "outcome": outcome, "verified": verified,
        })

    async def send_tool_progress(self, call_id: str, note: str) -> None:
        await self.emit({"type": "tool_progress", "id": call_id, "note": note})

    async def send_tool_deferred(
        self, call_id: str, handle: str, status_label: str | None = None,
    ) -> None:
        event = {"type": "tool_deferred", "id": call_id, "handle": handle}
        if status_label:
            event["status_label"] = status_label
        await self.emit(event)

    async def send_tool_partial_result(
        self, call_id: str, content: dict, reply: bool = False,
    ) -> None:
        await self.emit({
            "type": "tool_partial_result", "id": call_id,
            "content": content, "reply": reply,
        })

    async def send_tool_cancel(self, call_id: str) -> None:
        await self.emit({"type": "tool_cancel", "id": call_id})

    async def send_context(self, text: str, role: str = "context", reply: bool = False) -> None:
        await self.emit({"type": "inject_context", "text": text, "role": role, "reply": reply})


class HeadlessController:
    """Dispatch JSONL requests to a ToolRouter and expose screen/state snapshots."""

    def __init__(self, router, driver, bridge: JsonLineBridge):
        self.router = router
        self.driver = driver
        self.bridge = bridge
        self.stopping = asyncio.Event()
        self._tasks: dict[str, asyncio.Task] = {}
        self._task_names: dict[str, str] = {}
        self._started: set[str] = set()

    def observation(self) -> dict:
        return {
            **self.router.semantic_state(),
            "screen": [line.rstrip() for line in self.driver.snapshot()],
            "last_response": self.router.last_assistant_text or None,
            "transcript_path": (
                str(self.router.transcript_path) if self.router.transcript_path else None
            ),
        }

    async def status_event(self, event: dict) -> None:
        data = {key: value for key, value in event.items() if key not in {"type", "event"}}
        event_type = event.get("event") or "status"
        await self.bridge.emit({
            "type": event_type, **data,
            "last_response": self.router.last_assistant_text or None,
        })

    async def handle(self, request: dict) -> None:
        request_id = request.get("id")
        kind = request.get("type")
        if kind == "screen_snapshot":
            await self.bridge.emit({
                "type": "screen_snapshot", "id": request_id, "data": self.observation(),
            })
            return
        if kind == "tool_cancel":
            if not isinstance(request_id, str) or not request_id:
                await self._error(request_id, "tool_cancel requires a non-empty id")
                return
            task = self._tasks.get(request_id)
            if task is None:
                await self._error(request_id, "tool_cancel id is not active")
                return
            if request_id not in self._started:
                task.cancel()
            elif self._task_names.get(request_id) != "long_task":
                await self._error(request_id, "only long_task supports active cancellation")
                return
            else:
                await self.router.handle_tool_cancel({"type": "tool_cancel", "id": request_id})
            await self.bridge.send_tool_cancel(request_id)
            return
        if kind == "shutdown":
            await self.bridge.emit({"type": "shutdown", "id": request_id})
            self.stopping.set()
            return
        if kind != "tool_call":
            await self._error(request_id, f"unknown or missing request type: {kind}")
            return

        name = request.get("name")
        if not isinstance(request_id, str) or not request_id:
            await self._error(request_id, "tool_call requires a non-empty string id")
            return
        if request_id in self._tasks:
            await self._error(request_id, "tool_call id is already active")
            return
        if not isinstance(name, str) or not name:
            await self._error(request_id, "tool_call requires a non-empty tool name")
            return
        if name == "end_session":
            await self._error(request_id, "end_session is unavailable in headless mode; use shutdown")
            return
        args = request.get("args", {})
        if not isinstance(args, dict):
            await self._error(request_id, "tool_call args must be an object")
            return
        call = {"type": "tool_call", "id": request_id, "name": name, "args": args}
        task = asyncio.create_task(self._run_tool_call(call))
        self._tasks[request_id] = task
        self._task_names[request_id] = name
        task.add_done_callback(lambda done, cid=request_id: self._task_done(cid, done))

    async def read(self, reader: asyncio.StreamReader) -> None:
        while not self.stopping.is_set():
            try:
                line = await reader.readline()
            except ValueError:
                await self._error(None, "control line exceeds 64 KiB")
                continue
            if not line:
                self.stopping.set()
                return
            try:
                request = json.loads(line)
            except (UnicodeDecodeError, json.JSONDecodeError):
                await self._error(None, "invalid JSON")
                continue
            if not isinstance(request, dict):
                await self._error(None, "request must be a JSON object")
                continue
            await self.handle(request)

    async def _run_tool_call(self, call: dict) -> None:
        call_id = call["id"]
        self._started.add(call_id)
        try:
            await self.router.handle_tool_call(call)
        finally:
            self._started.discard(call_id)

    async def _error(self, request_id, detail: str) -> None:
        await self.bridge.emit({"type": "control_error", "id": request_id, "detail": detail})

    def _task_done(self, call_id: str, task: asyncio.Task) -> None:
        if self._tasks.get(call_id) is task:
            self._tasks.pop(call_id, None)
            self._task_names.pop(call_id, None)
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            asyncio.create_task(self._error(call_id, f"tool task failed: {error}"))

    async def cancel_tasks(self) -> None:
        tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)


class NonBlockingLineReader:
    """Cancelable line reader for pipes, terminals, and redirected regular files."""

    def __init__(self, fd: int):
        self.fd = fd
        self.buffer = bytearray()
        self.eof = False
        self.discarding = False
        self._was_blocking = os.get_blocking(fd)
        os.set_blocking(fd, False)

    def close(self) -> None:
        os.set_blocking(self.fd, self._was_blocking)

    async def readline(self) -> bytes:
        while True:
            newline = self.buffer.find(b"\n")
            if self.discarding:
                if newline >= 0:
                    del self.buffer[: newline + 1]
                    self.discarding = False
                    continue
                self.buffer.clear()
            elif newline >= 0:
                line = bytes(self.buffer[: newline + 1])
                del self.buffer[: newline + 1]
                if len(line) > MAX_CONTROL_LINE_BYTES:
                    raise ValueError("control line too long")
                return line
            if self.eof:
                line = bytes(self.buffer)
                self.buffer.clear()
                return line
            if not self.discarding and len(self.buffer) > MAX_CONTROL_LINE_BYTES:
                self.buffer.clear()
                self.discarding = True
                raise ValueError("control line too long")
            try:
                chunk = os.read(self.fd, 16 * 1024)
            except BlockingIOError:
                await asyncio.sleep(0.02)
                continue
            if chunk:
                self.buffer.extend(chunk)
            else:
                self.eof = True


async def stdin_reader() -> NonBlockingLineReader:
    """Attach a cancelable non-blocking reader to stdin."""
    return NonBlockingLineReader(sys.stdin.fileno())
