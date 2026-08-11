import asyncio

import pytest

from converse_code.pi_tui import PiTUIBridge, PiTUIBridgeError


class FakeWire:
    def __init__(self):
        self.frames = []

    async def send(self, frame):
        self.frames.append(frame)
        return True


async def test_commands_are_id_correlated_and_events_are_forwarded():
    wire = FakeWire()
    events = []
    bridge = PiTUIBridge(wire.send, on_event=lambda event: events.append(event))
    await bridge.set_connected(True)

    pending = asyncio.create_task(bridge.command("prompt", message="Fix it"))
    await asyncio.sleep(0)
    sent = wire.frames[-1]
    assert sent["type"] == "prompt"
    assert sent["message"] == "Fix it"

    await bridge.handle_message({"type": "agent_start"})
    await bridge.handle_message({"type": "response", "id": sent["id"], "success": True})
    assert await pending == {"type": "response", "id": sent["id"], "success": True}
    assert events == [{"type": "agent_start"}]


async def test_disconnect_fails_pending_command_instead_of_claiming_acceptance():
    bridge = PiTUIBridge(FakeWire().send, timeout=1)
    await bridge.set_connected(True)
    pending = asyncio.create_task(bridge.command("steer", message="Also test it"))
    await asyncio.sleep(0)
    await bridge.set_connected(False)
    with pytest.raises(PiTUIBridgeError, match="disconnected"):
        await pending


async def test_command_fails_closed_when_visible_pi_is_not_connected():
    bridge = PiTUIBridge(FakeWire().send, connect_timeout=0.01)
    with pytest.raises(PiTUIBridgeError, match="visible Pi terminal"):
        await bridge.command("prompt", message="Fix it")


async def test_disconnect_and_connection_replacement_emit_ownership_breaks():
    events = []
    bridge = PiTUIBridge(FakeWire().send, on_event=lambda event: events.append(event))
    await bridge.set_connected(True)
    await bridge.set_connected(True)
    await bridge.set_connected(False)

    assert events == [{"type": "bridge_replaced"}, {"type": "bridge_disconnect"}]
