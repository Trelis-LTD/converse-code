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

    await bridge.send_tool_deferred("c1", "cc-c1", status_label="Claude Code task")
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


async def test_context_and_partial_result_map_to_public_browser_sdk_actions():
    tab = FakeTab()
    tab.connected = True
    bridge = BrowserBridge(tab.send)
    await bridge.handle_browser_message({"type": "local", "event": "bridge_ready"})

    await bridge.send_context("Claude finished.", role="context", reply=True)
    await bridge.send_tool_partial_result(
        "c1", {"speak": "Tests pass"}, reply=True,
    )

    context, partial = tab.frames
    assert context["action"] == "inject_context"
    assert context["text"] == "Claude finished."
    assert context["role"] == "context"
    assert context["reply"] is True
    assert partial["action"] == "tool_partial_result"
    assert partial["reply"] is True


async def test_browser_tool_calls_and_cancellation_reach_python_handlers():
    bridge = BrowserBridge(lambda _frame: asyncio.sleep(0, result=True))
    calls, cancels = [], []
    bridge.on_tool_call = lambda call: calls.append(call) or asyncio.sleep(0)
    bridge.on_tool_cancel = lambda call: cancels.append(call) or asyncio.sleep(0)

    await bridge.handle_browser_message({
        "type": "local", "event": "tool_call",
        "call": {"id": "c1", "name": "long_task", "args": {"request": "fix it"}},
    })
    await bridge.handle_browser_message({
        "type": "local", "event": "tool_cancel", "call": {"id": "c1"},
    })

    assert calls[0]["name"] == "long_task"
    assert cancels == [{"id": "c1"}]
