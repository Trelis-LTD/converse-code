import asyncio

from converse_code.bridge import BrowserBridge


async def ignore(_message):
    pass


async def handle(
    bridge, message, *, on_tool_call=ignore, on_tool_cancel=ignore, on_session_end=ignore,
):
    await bridge.handle_browser_message(
        message, on_tool_call=on_tool_call, on_tool_cancel=on_tool_cancel,
        on_session_end=on_session_end,
    )


class FakeTab:
    def __init__(self):
        self.connected = False
        self.frames = []

    async def send(self, frame):
        if not self.connected:
            return False
        self.frames.append(frame)
        return True


async def test_controls_wait_for_ready_and_remain_until_browser_ack():
    tab = FakeTab()
    bridge = BrowserBridge(tab.send)

    await bridge.send_tool_deferred("c1", "code-c1", status_label="Coding task")
    await bridge.send_tool_progress("c1", "running tests")
    assert tab.frames == []

    tab.connected = True
    await handle(bridge, {"type": "local", "event": "bridge_ready"})
    assert [frame["action"] for frame in tab.frames] == ["tool_deferred", "tool_progress"]

    await handle(bridge, {
        "type": "local", "event": "bridge_ack", "seq": tab.frames[0]["seq"],
    })
    await bridge.on_browser_disconnected()
    await handle(bridge, {"type": "local", "event": "bridge_ready"})
    assert [frame["action"] for frame in tab.frames] == [
        "tool_deferred", "tool_progress", "tool_progress",
    ]


async def test_trace_records_replying_partial_delivery_and_ack():
    tab = FakeTab()
    tab.connected = True
    events = []
    bridge = BrowserBridge(
        tab.send,
        trace=lambda source, event, **data: events.append((source, event, data)),
    )
    await handle(bridge, {"type": "local", "event": "bridge_ready"})
    await bridge.send_tool_partial_result(
        "task-1", {"speak": "Ask for approval"}, reply=True,
    )
    partial = tab.frames[0]
    await handle(bridge, {
        "type": "local", "event": "bridge_ack", "seq": partial["seq"],
    })

    assert [(source, event) for source, event, _ in events] == [
        ("browser_bridge", "ready"),
        ("browser_bridge", "control_queued"),
        ("browser_bridge", "control_sent"),
        ("browser_bridge", "control_acknowledged"),
    ]
    assert events[1][2]["action"] == "tool_partial_result"
    assert partial["reply"] is True


async def test_browser_tool_calls_and_cancellation_reach_python_handlers():
    bridge = BrowserBridge(lambda _frame: asyncio.sleep(0, result=True))
    calls, cancels = [], []

    async def on_call(call):
        calls.append(call)

    async def on_cancel(call):
        cancels.append(call)

    await handle(bridge, {
        "type": "local", "event": "tool_call",
        "call": {"id": "c1", "name": "coding_task", "args": {"request": "fix it"}},
    }, on_tool_call=on_call)
    await handle(bridge, {
        "type": "local", "event": "tool_cancel", "call": {"id": "c1"},
    }, on_tool_cancel=on_cancel)

    assert calls[0]["name"] == "coding_task"
    assert cancels == [{"id": "c1"}]


async def test_native_sdk_session_end_reaches_python_handler():
    bridge = BrowserBridge(lambda _frame: asyncio.sleep(0, result=True))
    endings = []

    await handle(
        bridge,
        {"type": "local", "event": "session_end", "code": 1000, "reason": "idle"},
        on_session_end=lambda event: endings.append(event) or asyncio.sleep(0),
    )

    assert endings == [{"code": 1000, "reason": "idle"}]


async def test_malformed_or_abnormal_session_end_does_not_reach_domain():
    bridge = BrowserBridge(lambda _frame: asyncio.sleep(0, result=True))
    endings = []

    async def handler(event):
        endings.append(event)

    for message in (
        {"type": "local", "event": "session_end", "code": 1006, "reason": "lost"},
        {"type": "local", "event": "session_end", "code": "1000", "reason": "idle"},
        {"type": "local", "event": "session_end", "code": 1000, "reason": 7},
    ):
        await handle(bridge, message, on_session_end=handler)

    assert endings == []


async def test_malformed_browser_tool_messages_do_not_enter_the_domain():
    bridge = BrowserBridge(lambda _frame: asyncio.sleep(0, result=True))
    calls, cancels = [], []

    async def on_call(call):
        calls.append(call)

    async def on_cancel(call):
        cancels.append(call)

    for call in (
        {"name": "coding_task", "args": {"request": "fix it"}},
        {"id": "c1", "name": 7, "args": {}},
        {"id": "c1", "name": "coding_task", "args": []},
    ):
        await handle(bridge, {
            "type": "local", "event": "tool_call", "call": call,
        }, on_tool_call=on_call)
    await handle(bridge, {
        "type": "local", "event": "tool_cancel", "call": {"id": 7},
    }, on_tool_cancel=on_cancel)

    assert calls == []
    assert cancels == []
