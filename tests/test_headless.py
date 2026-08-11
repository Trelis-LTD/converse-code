import asyncio
import io
import json
import os
import tempfile

from converse_code.headless import (
    MAX_CONTROL_LINE_BYTES,
    HeadlessController,
    JsonLineBridge,
    NonBlockingLineReader,
)


class FakeDriver:
    def snapshot(self):
        return ["Claude Code", "❯", ""]


class FakeRouter:
    def __init__(self, bridge):
        self.bridge = bridge
        self.last_assistant_text = "Full response text."
        self.transcript_path = None
        self.calls = []
        self.cancels = []

    def _status_data(self):
        return {"state": "idle", "phase": "idle", "active_task": None}

    async def handle_tool_call(self, call):
        self.calls.append(call)
        await self.bridge.send_tool_result(call["id"], {"speak": "Done."})

    async def handle_tool_cancel(self, call):
        self.cancels.append(call)

class SlowRouter(FakeRouter):
    def __init__(self, bridge):
        super().__init__(bridge)
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def handle_tool_call(self, call):
        self.calls.append(call)
        self.started.set()
        await self.release.wait()


def events(stream):
    return [json.loads(line) for line in stream.getvalue().splitlines()]


async def test_screen_snapshot_exposes_state_screen_and_full_response():
    stream = io.StringIO()
    bridge = JsonLineBridge(stream)
    router = FakeRouter(bridge)
    controller = HeadlessController(router, FakeDriver(), bridge)

    await controller.handle({"type": "screen_snapshot", "id": "s1"})

    event = events(stream)[0]
    assert event["type"] == "screen_snapshot"
    assert event["id"] == "s1"
    assert event["data"]["state"] == "idle"
    assert event["data"]["screen"][:2] == ["Claude Code", "❯"]
    assert event["data"]["last_response"] == "Full response text."


async def test_tool_calls_and_active_cancellation_are_dispatched():
    stream = io.StringIO()
    bridge = JsonLineBridge(stream)
    router = SlowRouter(bridge)
    controller = HeadlessController(router, FakeDriver(), bridge)

    await controller.handle({
        "type": "tool_call", "id": "t1", "name": "long_task", "args": {},
    })
    await router.started.wait()
    await controller.handle({"type": "tool_cancel", "id": "t1"})

    assert router.calls[0]["name"] == "long_task"
    assert router.cancels == [{"type": "tool_cancel", "id": "t1"}]
    assert events(stream) == [{"type": "tool_cancel", "id": "t1"}]
    await controller.cancel_tasks()


async def test_active_non_long_task_cancellation_is_rejected():
    stream = io.StringIO()
    bridge = JsonLineBridge(stream)
    router = SlowRouter(bridge)
    controller = HeadlessController(router, FakeDriver(), bridge)

    await controller.handle({
        "type": "tool_call", "id": "t1", "name": "observe_claude", "args": {},
    })
    await router.started.wait()
    await controller.handle({"type": "tool_cancel", "id": "t1"})

    assert router.cancels == []
    assert events(stream) == [{
        "type": "control_error", "id": "t1",
        "detail": "only long_task supports active cancellation",
    }]
    await controller.cancel_tasks()

async def test_buffered_immediate_cancel_prevents_tool_start():
    stream = io.StringIO()
    bridge = JsonLineBridge(stream)
    router = FakeRouter(bridge)
    controller = HeadlessController(router, FakeDriver(), bridge)
    reader = asyncio.StreamReader()
    reader.feed_data(
        b'{"type":"tool_call","id":"t1","name":"long_task","args":{}}\n'
        b'{"type":"tool_cancel","id":"t1"}\n'
        b'{"type":"shutdown"}\n'
    )
    reader.feed_eof()

    await controller.read(reader)
    await asyncio.sleep(0)

    assert router.calls == []
    assert {"type": "tool_cancel", "id": "t1"} in events(stream)


async def test_status_and_context_use_canonical_event_names():
    stream = io.StringIO()
    bridge = JsonLineBridge(stream)
    router = FakeRouter(bridge)
    controller = HeadlessController(router, FakeDriver(), bridge)

    await controller.status_event({"type": "local", "event": "status", "state": "idle"})
    await controller.status_event({"type": "local", "event": "injected", "text": "do work"})
    await bridge.send_context("finished", reply=True)

    assert events(stream) == [
        {"type": "status", "state": "idle", "last_response": "Full response text."},
        {"type": "injected", "text": "do work", "last_response": "Full response text."},
        {"type": "inject_context", "text": "finished", "role": "context", "reply": True},
    ]


async def test_jsonl_reader_reports_bad_input_and_shutdown():
    stream = io.StringIO()
    bridge = JsonLineBridge(stream)
    controller = HeadlessController(FakeRouter(bridge), FakeDriver(), bridge)
    reader = asyncio.StreamReader()
    reader.feed_data(b"not-json\n{\"type\":\"shutdown\",\"id\":\"bye\"}\n")
    reader.feed_eof()

    await controller.read(reader)

    output = events(stream)
    assert output[0]["type"] == "control_error"
    assert output[1] == {"type": "shutdown", "id": "bye"}
    assert controller.stopping.is_set()


async def test_oversized_line_is_discarded_without_losing_next_frame():
    with tempfile.TemporaryFile() as stream:
        stream.write(b"x" * (MAX_CONTROL_LINE_BYTES + 1) + b'\n{"type":"shutdown"}\n')
        stream.seek(0)
        reader = NonBlockingLineReader(stream.fileno())
        try:
            try:
                await reader.readline()
            except ValueError:
                pass
            assert await reader.readline() == b'{"type":"shutdown"}\n'
        finally:
            reader.close()


def test_nonblocking_reader_restores_inherited_blocking_mode():
    read_fd, write_fd = os.pipe()
    try:
        assert os.get_blocking(read_fd) is True
        reader = NonBlockingLineReader(read_fd)
        assert os.get_blocking(read_fd) is False
        reader.close()
        assert os.get_blocking(read_fd) is True
    finally:
        os.close(read_fd)
        os.close(write_fd)
