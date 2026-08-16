import asyncio

from converse_code.bridge import BrowserBridge


def no_trace(_source, _event, **_data):
    pass


def make_bridge(send):
    return BrowserBridge(send, trace=no_trace)


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
    bridge = make_bridge(tab.send)

    await bridge.send_tool_deferred("c1", "code-c1", status_label="Pi")
    await bridge.send_tool_partial_result(
        "c1",
        {"event": "pi_approval_required"},
        interaction={
            "prompt": "Allow Pi to run bash: pwd?",
            "options": ["Allow once", "Allow for this session", "Block"],
        },
    )
    assert tab.frames == []

    tab.connected = True
    await handle(bridge, {"type": "local", "event": "bridge_ready"})
    assert [frame["action"] for frame in tab.frames] == ["tool_deferred", "tool_partial_result"]
    assert tab.frames[1]["interaction"] == {
        "prompt": "Allow Pi to run bash: pwd?",
        "options": ["Allow once", "Allow for this session", "Block"],
    }

    await handle(bridge, {
        "type": "local", "event": "bridge_ack", "seq": tab.frames[0]["seq"],
    })
    await bridge.on_browser_disconnected()
    await handle(bridge, {"type": "local", "event": "bridge_ready"})
    assert [frame["action"] for frame in tab.frames] == [
        "tool_deferred", "tool_partial_result", "tool_partial_result",
    ]


async def test_browser_tool_calls_and_cancellation_reach_python_handlers():
    bridge = make_bridge(lambda _frame: asyncio.sleep(0, result=True))
    calls, cancels = [], []

    async def on_call(call):
        calls.append(call)

    async def on_cancel(call):
        cancels.append(call)

    await handle(bridge, {
        "type": "local", "event": "tool_call",
        "call": {"id": "c1", "name": "pi_message", "args": {"message": "fix it"}},
    }, on_tool_call=on_call)
    await handle(bridge, {
        "type": "local", "event": "tool_cancel", "call": {"id": "c1"},
    }, on_tool_cancel=on_cancel)

    assert calls[0]["name"] == "pi_message"
    assert cancels == [{"id": "c1"}]


async def test_native_sdk_session_end_reaches_python_handler():
    bridge = make_bridge(lambda _frame: asyncio.sleep(0, result=True))
    endings = []

    await handle(
        bridge,
        {"type": "local", "event": "session_end", "code": 1000, "reason": "idle"},
        on_session_end=lambda event: endings.append(event) or asyncio.sleep(0),
    )

    assert endings == [{"code": 1000, "reason": "idle"}]


async def test_explicit_browser_end_session_reaches_python_handler():
    bridge = make_bridge(lambda _frame: asyncio.sleep(0, result=True))
    endings = []

    await handle(
        bridge,
        {"type": "local", "event": "end_session"},
        on_session_end=lambda event: endings.append(event) or asyncio.sleep(0),
    )

    assert endings == [{"source": "user"}]


async def test_malformed_or_abnormal_session_end_does_not_reach_domain():
    bridge = make_bridge(lambda _frame: asyncio.sleep(0, result=True))
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
    bridge = make_bridge(lambda _frame: asyncio.sleep(0, result=True))
    calls, cancels = [], []

    async def on_call(call):
        calls.append(call)

    async def on_cancel(call):
        cancels.append(call)

    for call in (
        {"name": "pi_message", "args": {"message": "fix it"}},
        {"id": "c1", "name": 7, "args": {}},
        {"id": "c1", "name": "pi_message", "args": []},
    ):
        await handle(bridge, {
            "type": "local", "event": "tool_call", "call": call,
        }, on_tool_call=on_call)
    await handle(bridge, {
        "type": "local", "event": "tool_cancel", "call": {"id": 7},
    }, on_tool_cancel=on_cancel)

    assert calls == []
    assert cancels == []
