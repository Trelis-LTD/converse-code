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
    bridge = PiTUIBridge(wire.send)
    assert bridge.connect() is None

    pending = asyncio.create_task(bridge.command("prompt", message="Fix it"))
    await asyncio.sleep(0)
    sent = wire.frames[-1]
    assert sent["type"] == "prompt"
    assert sent["message"] == "Fix it"

    event = await bridge.handle_message({"type": "agent_start"})
    await bridge.handle_message({
        "type": "response", "id": sent["id"], "success": True,
        "provider": "openai-codex", "model": "gpt-5.6-sol",
    })
    assert await pending == sent["id"]
    assert event == {"type": "agent_start"}


async def test_malformed_acknowledgement_cannot_prove_command_acceptance():
    wire = FakeWire()
    bridge = PiTUIBridge(wire.send)
    bridge.connect()
    pending = asyncio.create_task(bridge.command("prompt", message="Fix it"))
    await asyncio.sleep(0)
    command_id = wire.frames[-1]["id"]

    await bridge.handle_message({
        "type": "response", "id": command_id, "success": "yes",
    })
    assert not pending.done()
    await bridge.handle_message({
        "type": "response", "id": command_id, "success": True,
    })
    assert await pending == command_id


async def test_disconnect_fails_pending_command_instead_of_claiming_acceptance():
    bridge = PiTUIBridge(FakeWire().send)
    bridge.connect()
    pending = asyncio.create_task(bridge.command("steer", message="Also test it"))
    await asyncio.sleep(0)
    bridge.disconnect()
    with pytest.raises(PiTUIBridgeError, match="disconnected"):
        await pending

async def test_disconnect_and_connection_replacement_emit_ownership_breaks():
    bridge = PiTUIBridge(FakeWire().send)
    first = bridge.connect()
    pending = asyncio.create_task(bridge.command("prompt", message="Fix it"))
    await asyncio.sleep(0)
    replaced = bridge.connect()
    with pytest.raises(PiTUIBridgeError, match="replaced"):
        await pending
    disconnected = bridge.disconnect()

    assert first is None
    assert replaced == {"type": "bridge_replaced"}
    assert disconnected == {"type": "bridge_disconnect"}
