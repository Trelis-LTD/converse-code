import asyncio

import pytest

from support import wait_until

from converse_code.pi_tui import PiTUIBridge, PiTUIBridgeError


class FakeWire:
    def __init__(self):
        self.frames = []

    async def send(self, frame):
        self.frames.append(frame)
        return True

    async def sent(self, count=1):
        await wait_until(
            lambda: len(self.frames) >= count,
            describe=lambda: f"{len(self.frames)} frame(s) on the wire, wanted {count}",
        )
        return self.frames[-1]


async def test_commands_are_id_correlated_and_events_are_forwarded():
    wire = FakeWire()
    bridge = PiTUIBridge(wire.send)
    bridge.connect()

    pending = asyncio.create_task(bridge.command("prompt", message="Fix it"))
    sent = await wire.sent()
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
    command_id = (await wire.sent())["id"]

    await bridge.handle_message({
        "type": "response", "id": command_id, "success": "yes",
    })
    assert not pending.done()
    await bridge.handle_message({
        "type": "response", "id": command_id, "success": True,
    })
    assert await pending == command_id


async def test_disconnect_fails_pending_command_instead_of_claiming_acceptance():
    wire = FakeWire()
    bridge = PiTUIBridge(wire.send)
    bridge.connect()
    pending = asyncio.create_task(bridge.command("steer", message="Also test it"))
    await wire.sent()
    bridge.disconnect()
    with pytest.raises(PiTUIBridgeError, match="disconnected"):
        await pending

async def test_disconnect_and_connection_replacement_emit_ownership_breaks():
    wire = FakeWire()
    bridge = PiTUIBridge(wire.send)
    first = bridge.connect()
    pending = asyncio.create_task(bridge.command("prompt", message="Fix it"))
    await wire.sent()
    replaced = bridge.connect()
    with pytest.raises(PiTUIBridgeError, match="replaced"):
        await pending
    disconnected = bridge.disconnect()

    assert first is None
    assert replaced == {"type": "bridge_replaced"}
    assert disconnected == {"type": "bridge_disconnect"}
