import asyncio

from support import wait_until

from converse_code.bridge import (
    BrowserBridge,
    InteractionUpdateAck,
    ToolCall,
)


async def ignore(_message):
    pass


def ignore_trace(_source, _event, **_data):
    pass


async def handle(
    bridge, message, *, on_tool_call=ignore, on_tool_cancel=ignore,
    on_deferred_resume=ignore, on_cancelled_interactions=ignore, on_session_end=ignore,
):
    await bridge.handle_browser_message(
        message, on_tool_call=on_tool_call, on_tool_cancel=on_tool_cancel,
        on_deferred_resume=on_deferred_resume,
        on_cancelled_interactions=on_cancelled_interactions,
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
    records = []
    bridge = BrowserBridge(
        tab.send, trace=lambda source, event, **data: records.append((source, event, data)),
    )

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
    acknowledged = [
        data for source, event, data in records
        if source == "browser_bridge" and event == "control_acknowledged"
    ]
    assert [record["seq"] for record in acknowledged] == [tab.frames[0]["seq"]]
    await bridge.on_browser_disconnected()
    await handle(bridge, {"type": "local", "event": "bridge_ready"})
    assert [frame["action"] for frame in tab.frames] == [
        "tool_deferred", "tool_partial_result", "tool_partial_result",
    ]


async def test_interaction_close_waits_for_the_sdk_ack_and_returns_its_outcome():
    tab = FakeTab()
    tab.connected = True
    bridge = BrowserBridge(tab.send, ignore_trace)
    await handle(bridge, {"type": "local", "event": "bridge_ready"})

    closing = asyncio.create_task(bridge.send_tool_interaction_update(
        "task-1", "approval-1", "superseded", note="The user changed course.",
    ))
    await wait_until(
        lambda: bool(tab.frames), describe=lambda: "the close never reached the browser",
    )
    frame = tab.frames[-1]
    assert frame == {
        "type": "local", "event": "bridge_control", "seq": frame["seq"],
        "action": "tool_interaction_update", "id": "task-1",
        "interaction_id": "approval-1", "state": "superseded",
        "note": "The user changed course.",
    }
    assert not closing.done()

    await handle(bridge, {
        "type": "local", "event": "bridge_ack", "seq": frame["seq"],
        "detail": {
            "type": "tool_interaction_update_ack", "id": "task-1",
            "interaction_id": "approval-1", "state": "cancelled",
            "applied": True, "reason": None,
        },
    })
    assert not closing.done()

    await handle(bridge, {
        "type": "local", "event": "bridge_ack", "seq": frame["seq"],
        "detail": {
            "type": "tool_interaction_update_ack", "id": "task-1",
            "interaction_id": "approval-1", "state": "superseded",
            "applied": True, "reason": None,
        },
    })

    assert await closing == InteractionUpdateAck(True, None)


async def test_browser_interaction_and_resume_events_reach_typed_handlers():
    bridge = BrowserBridge(lambda _frame: asyncio.sleep(0, result=True), ignore_trace)
    resumed, cancelled = [], []

    await handle(
        bridge,
        {"type": "local", "event": "tool_deferred_resume", "handle": "pi-turn"},
        on_deferred_resume=lambda handle: resumed.append(handle) or asyncio.sleep(0),
    )
    await handle(
        bridge,
        {
            "type": "local", "event": "interaction_cancelled",
            "interaction_ids": ["approval-1", "approval-2"],
        },
        on_cancelled_interactions=(
            lambda ids: cancelled.append(ids) or asyncio.sleep(0)
        ),
    )

    assert resumed == ["pi-turn"]
    assert cancelled == [("approval-1", "approval-2")]


async def test_browser_tool_calls_and_cancellation_reach_python_handlers():
    bridge = BrowserBridge(lambda _frame: asyncio.sleep(0, result=True), ignore_trace)
    calls, cancels = [], []

    async def on_call(call):
        calls.append(call)

    async def on_cancel(call):
        cancels.append(call)

    await handle(bridge, {
        "type": "local", "event": "tool_call",
        "call": {"id": "c1", "name": "pi_request", "args": {"user_request": "fix it"}},
    }, on_tool_call=on_call)
    await handle(bridge, {
        "type": "local", "event": "tool_cancel", "call": {"id": "c1"},
    }, on_tool_cancel=on_cancel)

    assert calls == [ToolCall("c1", "pi_request", {"user_request": "fix it"})]
    assert cancels == ["c1"]


async def test_native_sdk_session_end_reaches_python_handler():
    bridge = BrowserBridge(lambda _frame: asyncio.sleep(0, result=True), ignore_trace)
    endings = []

    await handle(
        bridge,
        {"type": "local", "event": "session_end", "code": 1000, "reason": "idle"},
        on_session_end=lambda: endings.append("ended") or asyncio.sleep(0),
    )

    assert endings == ["ended"]


async def test_explicit_browser_end_session_reaches_python_handler():
    bridge = BrowserBridge(lambda _frame: asyncio.sleep(0, result=True), ignore_trace)
    endings = []

    await handle(
        bridge,
        {"type": "local", "event": "end_session"},
        on_session_end=lambda: endings.append("ended") or asyncio.sleep(0),
    )

    assert endings == ["ended"]


async def test_malformed_or_abnormal_session_end_does_not_reach_domain():
    bridge = BrowserBridge(lambda _frame: asyncio.sleep(0, result=True), ignore_trace)
    endings = []

    async def handler():
        endings.append("ended")

    for message in (
        {"type": "local", "event": "session_end", "code": 1006, "reason": "lost"},
        {"type": "local", "event": "session_end", "code": "1000", "reason": "idle"},
        {"type": "local", "event": "session_end", "code": 1000, "reason": 7},
    ):
        await handle(bridge, message, on_session_end=handler)

    assert endings == []


async def test_malformed_browser_tool_messages_do_not_enter_the_domain():
    bridge = BrowserBridge(lambda _frame: asyncio.sleep(0, result=True), ignore_trace)
    calls, cancels = [], []

    async def on_call(call):
        calls.append(call)

    async def on_cancel(call):
        cancels.append(call)

    for call in (
        {"name": "pi_request", "args": {"user_request": "fix it"}},
        {"id": "c1", "name": 7, "args": {}},
        {"id": "c1", "name": "pi_request", "args": []},
    ):
        await handle(bridge, {
            "type": "local", "event": "tool_call", "call": call,
        }, on_tool_call=on_call)
    await handle(bridge, {
        "type": "local", "event": "tool_cancel", "call": {"id": 7},
    }, on_tool_cancel=on_cancel)

    assert calls == []
    assert cancels == []
