import asyncio

from converse_code.bridge import BrowserBridge


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
    await bridge.handle_browser_message({"type": "local", "event": "bridge_ready"})
    assert [frame["action"] for frame in tab.frames] == ["tool_deferred", "tool_progress"]

    await bridge.handle_browser_message({
        "type": "local", "event": "bridge_ack", "seq": tab.frames[0]["seq"],
    })


async def test_unacked_control_is_replayed_after_tab_reconnect():
    tab = FakeTab()
    tab.connected = True
    bridge = BrowserBridge(tab.send)
    await bridge.handle_browser_message({"type": "local", "event": "bridge_ready"})
    await bridge.send_tool_result("c1", {"speak": "done"})
    first = tab.frames[-1]

    await bridge.on_browser_disconnected()
    await bridge.handle_browser_message({"type": "local", "event": "bridge_ready"})

    assert tab.frames[-1] == first


async def test_tool_result_maps_router_verification_to_sdk_outcome():
    tab = FakeTab()
    tab.connected = True
    bridge = BrowserBridge(tab.send)
    await bridge.handle_browser_message({"type": "local", "event": "bridge_ready"})

    await bridge.send_tool_result("ok", {"speak": "done"},
                                  outcome="succeeded", verified=True)
    assert tab.frames[-1]["outcome"] == "succeeded"
    assert tab.frames[-1]["verified"] is True

    await bridge.send_tool_result("bad", {"speak": "failed"},
                                  outcome="failed", verified=False)
    assert tab.frames[-1]["outcome"] == "failed"
    assert tab.frames[-1]["verified"] is False


async def test_partial_result_maps_reply_to_public_browser_sdk_action():
    tab = FakeTab()
    tab.connected = True
    bridge = BrowserBridge(tab.send)
    await bridge.handle_browser_message({"type": "local", "event": "bridge_ready"})

    await bridge.send_tool_partial_result(
        "c1", {"speak": "Tests pass"}, reply=True,
    )

    partial = tab.frames[0]
    assert partial["action"] == "tool_partial_result"
    assert partial["reply"] is True


async def test_voice_prompt_is_an_acknowledged_replayable_browser_control():
    tab = FakeTab()
    tab.connected = True
    bridge = BrowserBridge(tab.send)
    await bridge.handle_browser_message({"type": "local", "event": "bridge_ready"})

    await bridge.send_voice_prompt("approval-7", "Ask for explicit approval")

    prompt = tab.frames[0]
    assert prompt["action"] == "voice_prompt"
    assert prompt["prompt_id"] == "approval-7"
    assert prompt["text"] == "Ask for explicit approval"


async def test_trace_records_control_delivery_ack_and_browser_prompt_milestones():
    tab = FakeTab()
    tab.connected = True
    events = []
    bridge = BrowserBridge(
        tab.send,
        trace=lambda source, event, **data: events.append((source, event, data)),
    )
    await bridge.handle_browser_message({"type": "local", "event": "bridge_ready"})
    await bridge.send_voice_prompt("approval-7", "Ask for approval")
    prompt = tab.frames[0]
    await bridge.handle_browser_message({
        "type": "local", "event": "debug_trace",
        "name": "voice_prompt_injection_result",
        "data": {"prompt_id": "approval-7", "accepted": True},
    })
    await bridge.handle_browser_message({
        "type": "local", "event": "bridge_ack", "seq": prompt["seq"],
    })

    assert [(source, event) for source, event, _ in events] == [
        ("browser_bridge", "ready"),
        ("browser_bridge", "control_queued"),
        ("browser_bridge", "control_sent"),
        ("browser", "voice_prompt_injection_result"),
        ("browser_bridge", "control_acknowledged"),
    ]
    assert events[1][2]["action"] == "voice_prompt"
    assert events[3][2] == {"prompt_id": "approval-7", "accepted": True}


async def test_browser_tool_calls_and_cancellation_reach_python_handlers():
    bridge = BrowserBridge(lambda _frame: asyncio.sleep(0, result=True))
    calls, cancels = [], []
    bridge.on_tool_call = lambda call: calls.append(call) or asyncio.sleep(0)
    bridge.on_tool_cancel = lambda call: cancels.append(call) or asyncio.sleep(0)

    await bridge.handle_browser_message({
        "type": "local", "event": "tool_call",
        "call": {"id": "c1", "name": "coding_task", "args": {"request": "fix it"}},
    })
    await bridge.handle_browser_message({
        "type": "local", "event": "tool_cancel", "call": {"id": "c1"},
    })

    assert calls[0]["name"] == "coding_task"
    assert cancels == [{"id": "c1"}]
