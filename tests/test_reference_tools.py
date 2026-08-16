import asyncio

from converse_code.agent_tools import PiControlRouter, manifest
from converse_code.pi_tui import CommandReceipt


class FakePi:
    def __init__(self):
        self.commands = []
        self.router = None
        self.next_id = 1

    async def command(self, kind, **fields):
        self.commands.append((kind, fields))
        response = f"fake-{self.next_id}"
        self.next_id += 1
        return CommandReceipt(response, {})

    async def emit(self, event):
        await self.router.on_event(event)


class FakeSender:
    def __init__(self):
        self.timeline = []
        self.deferred = []
        self.partials = []
        self.results = []

    async def send_tool_deferred(self, call_id, handle, status_label=None):
        self.timeline.append("deferred")
        self.deferred.append((call_id, handle, status_label))

    async def send_tool_partial_result(self, call_id, content, *, interaction=None):
        self.timeline.append("partial")
        self.partials.append((call_id, content, interaction))

    async def send_tool_result(self, call_id, content, **metadata):
        self.timeline.append("result")
        self.results.append((call_id, content, metadata))


def make_router(pi, sender):
    router = PiControlRouter(pi, sender, handle="pi-turn")
    pi.router = router
    return router


def task_call(call_id="message-1", message="Fix it"):
    return {"id": call_id, "name": "pi_request", "args": {"user_request": message}}


async def start_task(pi, sender, message="Fix it"):
    router = make_router(pi, sender)
    running = asyncio.create_task(router.handle_tool_call(task_call(message=message)))
    await asyncio.sleep(0)
    await pi.emit({"type": "input_seen", "owner": "bridge", "commandId": "fake-1"})
    return router, running


def test_manifest_exposes_only_human_equivalent_pi_controls():
    tools = {tool["name"]: tool for tool in manifest()}
    assert set(tools) == {"pi_request", "pi_approval", "pi_cancel"}
    assert tools["pi_request"]["deferred"] is True
    assert tools["pi_request"]["notify_on_complete"] is True
    assert set(tools["pi_request"]["parameters"]["properties"]) == {"user_request"}
    assert tools["pi_approval"]["parameters"]["properties"]["decision"]["enum"] == [
        "allow_once", "allow_session", "block",
    ]


async def test_idle_pi_message_owns_one_deferred_pi_episode_until_settlement():
    pi, sender = FakePi(), FakeSender()
    router, running = await start_task(pi, sender, "Open the game")

    assert pi.commands == [("prompt", {"message": "Open the game"})]
    assert sender.deferred == [("message-1", "pi-turn", "Pi")]

    await pi.emit({"type": "message_end", "message": {
        "role": "assistant", "content": [{"type": "text", "text": "Opened the game."}],
    }})
    await pi.emit({"type": "agent_settled"})
    await running

    assert sender.results == [(
        "message-1",
        {"event": "pi_settled", "pi_response": "Opened the game.", "handle": "pi-turn"},
        {"outcome": "succeeded", "verified": False},
    )]
    assert router.active_call_id is None


async def test_pi_request_while_working_is_immediate_steering_not_another_background_job():
    pi, sender = FakePi(), FakeSender()
    router, running = await start_task(pi, sender, "Build the game")

    await router.handle_tool_call(task_call("message-2", "Make it an airplane game"))

    assert pi.commands[-1] == ("steer", {"message": "Make it an airplane game"})
    assert len(sender.deferred) == 1
    assert sender.results[-1] == (
        "message-2",
        {"event": "pi_message_delivered", "mode": "steer", "task_status": "running"},
        {"outcome": "succeeded", "verified": True},
    )
    await pi.emit({"type": "message_end", "message": {
        "role": "assistant", "content": "Built the airplane game.",
    }})
    await pi.emit({"type": "agent_settled"})
    await running


async def test_approval_surfaces_as_a_queued_user_interaction():
    pi, sender = FakePi(), FakeSender()
    router, running = await start_task(pi, sender)

    await pi.emit({
        "type": "approval_request", "approvalId": "approval-7",
        "toolName": "bash", "summary": "uv run pytest -q",
    })

    assert sender.partials[-1] == (
        "message-1",
        {
            "event": "pi_approval_required", "approval_id": "approval-7",
            "tool": "bash", "summary": "uv run pytest -q",
            "decisions": ["allow_once", "allow_session", "block"],
            "handle": "pi-turn",
        },
        {
            "prompt": "Allow Pi to run bash: uv run pytest -q?",
            "options": ["Allow once", "Allow for this session", "Block"],
        },
    )
    assert "speak" not in sender.partials[-1][1]

    await router.handle_tool_call({
        "id": "approval-call", "name": "pi_approval",
        "args": {"approval_id": "approval-7", "decision": "allow_once"},
    })
    assert pi.commands[-1] == (
        "approval_response", {"approvalId": "approval-7", "decision": "allow_once"},
    )
    assert sender.results[-1] == (
        "approval-call",
        {"event": "pi_approval_delivered", "decision": "allow_once", "task_status": "running"},
        {"outcome": "succeeded", "verified": True},
    )
    await pi.emit({"type": "message_end", "message": {"role": "assistant", "content": "Done"}})
    await pi.emit({"type": "agent_settled"})
    await running


async def test_change_of_course_blocks_pending_approval_before_steering():
    pi, sender = FakePi(), FakeSender()
    router, running = await start_task(pi, sender, "Open the game")
    await pi.emit({
        "type": "approval_request", "approvalId": "approval-1",
        "toolName": "bash", "summary": "open index.html",
    })

    await router.handle_tool_call(task_call("message-2", "Use a local server instead"))

    assert pi.commands[-2:] == [
        ("approval_response", {"approvalId": "approval-1", "decision": "block"}),
        ("steer", {"message": "Use a local server instead"}),
    ]
    await router.handle_tool_call({
        "id": "stale", "name": "pi_approval",
        "args": {"approval_id": "approval-1", "decision": "allow_once"},
    })
    assert sender.results[-1][2]["outcome"] == "failed"
    await pi.emit({"type": "message_end", "message": {"role": "assistant", "content": "Done"}})
    await pi.emit({"type": "agent_settled"})
    await running


async def test_pi_cancel_aborts_only_the_active_pi_turn():
    pi, sender = FakePi(), FakeSender()
    router, running = await start_task(pi, sender)

    await router.handle_tool_call({"id": "cancel-1", "name": "pi_cancel", "args": {}})

    assert pi.commands[-1] == ("abort", {})
    assert sender.results[-1] == (
        "cancel-1", {"event": "pi_cancel_requested", "task_status": "cancelling"},
        {"outcome": "succeeded", "verified": True},
    )
    await pi.emit({"type": "agent_settled"})
    await running
    assert sender.results[-1][0] == "message-1"
    assert sender.results[-1][2]["outcome"] == "cancelled"


async def test_tool_activity_is_forwarded_as_generic_structured_partial():
    pi, sender = FakePi(), FakeSender()
    _, running = await start_task(pi, sender)

    await pi.emit({
        "type": "tool_execution_start", "toolCallId": "tool-1",
        "toolName": "custom_tool", "args": {"thing": "value"},
    })

    assert sender.partials[-1] == (
        "message-1",
        {
            "event": "pi_tool_started", "tool": "custom_tool",
            "arguments": {"thing": "value"}, "handle": "pi-turn",
        },
        None,
    )
    await pi.emit({"type": "message_end", "message": {"role": "assistant", "content": "Done"}})
    await pi.emit({"type": "agent_settled"})
    await running


async def test_malformed_arguments_and_stale_approvals_fail_before_side_effects():
    pi, sender = FakePi(), FakeSender()
    router = make_router(pi, sender)

    await router.handle_tool_call({
        "id": "bad-message", "name": "pi_request", "args": {"user_request": ["Fix it"]},
    })
    await router.handle_tool_call({
        "id": "bad-approval", "name": "pi_approval",
        "args": {"approval_id": "missing", "decision": "allow_once"},
    })

    assert pi.commands == []
    assert [result[2]["outcome"] for result in sender.results] == ["failed", "failed"]


async def test_unrelated_terminal_input_and_disconnect_fail_the_owned_episode_closed():
    for event in (
        {"type": "input_seen", "owner": "interactive"},
        {"type": "bridge_disconnect"},
        {"type": "process_exit", "status": 7},
    ):
        pi, sender = FakePi(), FakeSender()
        _, running = await start_task(pi, sender)
        await pi.emit(event)
        await running
        assert sender.results[-1][2]["outcome"] == "failed"


async def test_events_racing_prompt_acknowledgement_are_delivered_after_deferred():
    class EagerPi(FakePi):
        async def command(self, kind, **fields):
            self.commands.append((kind, fields))
            if kind == "prompt":
                await self.emit({"type": "input_seen", "owner": "bridge", "commandId": "fake-1"})
                await self.emit({
                    "type": "tool_execution_start", "toolName": "edit",
                    "args": {"path": "raced.py"},
                })
                await self.emit({
                    "type": "message_end", "message": {
                        "role": "assistant", "content": "Done despite the race.",
                    },
                })
                await self.emit({"type": "agent_settled"})
                return CommandReceipt("fake-1", {})
            return await super().command(kind, **fields)

    pi, sender = EagerPi(), FakeSender()
    router = make_router(pi, sender)
    await router.handle_tool_call(task_call())

    assert sender.timeline.index("deferred") < sender.timeline.index("partial")
    assert sender.timeline.index("deferred") < sender.timeline.index("result")
