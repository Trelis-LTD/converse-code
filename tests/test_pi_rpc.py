import asyncio
import json
import sys

import pytest

from converse_code.pi_rpc import PiRPC, PiRPCError


FAKE_PI = __file__.replace("test_pi_rpc.py", "fake_pi_rpc.py")


async def test_rpc_correlates_responses_and_streams_events():
    events = []
    client = PiRPC([sys.executable, FAKE_PI], on_event=events.append)
    await client.start()
    try:
        response = await client.command("get_state")
        assert response["data"]["isStreaming"] is False

        await client.command("prompt", message="Fix the tests")
        await asyncio.wait_for(client.settled.wait(), timeout=1)
        assert [event["type"] for event in events] == [
            "agent_start", "tool_execution_start", "message_end", "agent_settled",
        ]
    finally:
        await client.stop()


async def test_rpc_reports_command_failure():
    client = PiRPC([sys.executable, FAKE_PI])
    await client.start()
    try:
        with pytest.raises(PiRPCError, match="not streaming"):
            await client.command("abort")
    finally:
        await client.stop()


async def test_rpc_fails_pending_commands_when_process_exits():
    client = PiRPC([sys.executable, FAKE_PI, "--exit-on-command"])
    await client.start()
    with pytest.raises(PiRPCError, match="exited"):
        await client.command("get_state")


def test_rpc_writes_strict_single_line_json():
    payload = PiRPC.encode_command("request-1", "prompt", message="line one\nline two")
    assert payload.endswith(b"\n")
    assert payload.count(b"\n") == 1
    assert json.loads(payload) == {
        "id": "request-1", "type": "prompt", "message": "line one\nline two",
    }
