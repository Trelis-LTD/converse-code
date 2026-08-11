"""Small, strict JSONL client for Pi's documented RPC mode."""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path

log = logging.getLogger(__name__)


class PiRPCError(RuntimeError):
    pass


EventHandler = Callable[[dict], Awaitable[None] | None]


class PiRPC:
    """Own one Pi process and correlate command responses with streamed events."""

    def __init__(
        self,
        argv: list[str],
        *,
        cwd: str | Path | None = None,
        on_event: EventHandler | None = None,
    ) -> None:
        self.argv = argv
        self.cwd = str(cwd) if cwd is not None else None
        self.on_event = on_event
        self.process: asyncio.subprocess.Process | None = None
        self.settled = asyncio.Event()
        self.settled.set()
        self._pending: dict[str, asyncio.Future] = {}
        self._next_id = 1
        self._write_lock = asyncio.Lock()
        self._reader_task: asyncio.Task | None = None
        self._stderr_task: asyncio.Task | None = None

    @staticmethod
    def encode_command(request_id: str, kind: str, **fields) -> bytes:
        payload = {"id": request_id, "type": kind, **fields}
        return (json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n").encode()

    async def start(self) -> None:
        if self.process is not None:
            return
        try:
            self.process = await asyncio.create_subprocess_exec(
                *self.argv,
                cwd=self.cwd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            raise PiRPCError(
                f"Could not launch {self.argv[0]!r}. Install Pi and authenticate with `pi /login`."
            ) from exc
        self._reader_task = asyncio.create_task(self._read_stdout())
        self._stderr_task = asyncio.create_task(self._read_stderr())

    async def command(self, kind: str, **fields) -> dict:
        process = self.process
        if process is None or process.stdin is None:
            raise PiRPCError("Pi is not running")
        if process.returncode is not None:
            raise PiRPCError(f"Pi exited with status {process.returncode}")
        request_id = f"converse-{self._next_id}"
        self._next_id += 1
        future = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        if kind == "prompt":
            self.settled.clear()
        try:
            async with self._write_lock:
                process.stdin.write(self.encode_command(request_id, kind, **fields))
                await process.stdin.drain()
            response = await future
        finally:
            self._pending.pop(request_id, None)
        if not response.get("success"):
            raise PiRPCError(str(response.get("error") or f"Pi rejected {kind}"))
        return response

    async def send_extension_response(self, request_id: str, **fields) -> None:
        process = self.process
        if process is None or process.stdin is None or process.returncode is not None:
            raise PiRPCError("Pi is not running")
        payload = {"type": "extension_ui_response", "id": request_id, **fields}
        encoded = (json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n").encode()
        async with self._write_lock:
            process.stdin.write(encoded)
            await process.stdin.drain()

    async def stop(self) -> None:
        process = self.process
        if process is None:
            return
        if process.returncode is None:
            if process.stdin is not None:
                process.stdin.close()
            try:
                await asyncio.wait_for(process.wait(), timeout=2)
            except asyncio.TimeoutError:
                process.terminate()
                await process.wait()
        for task in (self._reader_task, self._stderr_task):
            if task is not None and not task.done():
                task.cancel()
        await asyncio.gather(
            *(task for task in (self._reader_task, self._stderr_task) if task is not None),
            return_exceptions=True,
        )
        self.process = None

    async def _read_stdout(self) -> None:
        assert self.process is not None and self.process.stdout is not None
        try:
            while line := await self.process.stdout.readline():
                try:
                    frame = json.loads(line)
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    await self._fail(PiRPCError(f"Pi emitted invalid JSON: {exc}"))
                    continue
                if frame.get("type") == "response" and isinstance(frame.get("id"), str):
                    future = self._pending.get(frame["id"])
                    if future is not None and not future.done():
                        future.set_result(frame)
                    continue
                if frame.get("type") == "agent_settled":
                    self.settled.set()
                if self.on_event is not None:
                    result = self.on_event(frame)
                    if inspect.isawaitable(result):
                        await result
        finally:
            status = await self.process.wait()
            await self._fail(PiRPCError(f"Pi exited with status {status}"))
            if self.on_event is not None:
                result = self.on_event({"type": "process_exit", "status": status})
                if inspect.isawaitable(result):
                    await result

    async def _read_stderr(self) -> None:
        assert self.process is not None and self.process.stderr is not None
        while line := await self.process.stderr.readline():
            log.warning("pi: %s", line.decode(errors="replace").rstrip())

    async def _fail(self, error: PiRPCError) -> None:
        for future in tuple(self._pending.values()):
            if not future.done():
                future.set_exception(error)
