"""A reused BrokerClient must still work after a reconnect.

The page's SDK client sends a fresh start frame whenever the tab reloads or its
socket blips, and converse-code reuses one BrokerClient for the process. A
regression here is silent and total: Claude Code keeps running tools, but no
result ever reaches the voice brain, so every call hangs to its 600s timeout.
Nothing in the unit tests could see that, so this drives a real socket through
two full cycles.
"""

import asyncio
import json

import pytest
import websockets

from converse_code.broker import BrokerClient
from converse_code.relay import rewrite_start_frame
from converse_code.tools import manifest


class FakeBroker:
    def __init__(self):
        self.connections = 0
        self.frames: list[list[dict]] = []      # JSON per connection
        self.start_frames: list[dict] = []

    async def handler(self, ws):
        self.connections += 1
        received: list[dict] = []
        self.frames.append(received)
        async for msg in ws:
            if isinstance(msg, bytes):
                continue
            data = json.loads(msg)
            if data.get("type") == "start":
                self.start_frames.append(data)
            else:
                received.append(data)


@pytest.fixture
async def broker():
    fake = FakeBroker()
    server = await websockets.serve(fake.handler, "127.0.0.1", 0)
    fake.url = f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}"
    yield fake
    server.close()
    await server.wait_closed()


async def one_cycle(client: BrokerClient, broker: FakeBroker, result_text: str):
    """Connect as the page's SDK would, send a tool result, then drop the socket
    the way a tab reload does."""
    frame = rewrite_start_frame(
        {"type": "start", "session_id": "browser-uuid", "api_key": "placeholder"},
        "ck_real", "cc-handle", manifest(),
    )
    await client.connect(start_frame=frame)
    run = asyncio.create_task(client.run())
    await asyncio.sleep(0.1)
    await client.send_tool_result("call-1", {"speak": result_text})
    await asyncio.sleep(0.1)
    await client.close()
    await asyncio.wait_for(run, 5)


async def test_tool_results_still_reach_the_broker_after_a_reload(broker):
    client = BrokerClient("ck_real", session_id="cc-handle", tools=manifest(), url=broker.url)

    await one_cycle(client, broker, "first session")
    await one_cycle(client, broker, "after reload")

    assert broker.connections == 2, "the second start frame should open a new session"
    spoken = [
        msg["content"]["speak"]
        for conn in broker.frames
        for msg in conn
        if msg.get("type") == "tool_result"
    ]
    # The regression drops the second one silently while everything looks fine.
    assert spoken == ["first session", "after reload"]


async def test_each_cycle_sends_a_start_frame_with_the_tools(broker):
    client = BrokerClient("ck_real", session_id="cc-handle", tools=manifest(), url=broker.url)
    await one_cycle(client, broker, "one")
    await one_cycle(client, broker, "two")

    assert len(broker.start_frames) == 2
    for frame in broker.start_frames:
        assert frame["api_key"] == "ck_real"
        assert [t["name"] for t in frame["mode"]["tools"]] == [t["name"] for t in manifest()]


async def test_closed_is_reset_by_connect(broker):
    client = BrokerClient("ck_real", session_id="s", tools=[], url=broker.url)
    await client.connect(start_frame={"type": "start"})
    run = asyncio.create_task(client.run())
    await client.close()
    await asyncio.wait_for(run, 5)
    assert client.closed.is_set()

    await client.connect(start_frame={"type": "start"})
    run = asyncio.create_task(client.run())
    assert not client.closed.is_set(), "a fresh connection must not look closed"
    await client.close()
    await asyncio.wait_for(run, 5)
