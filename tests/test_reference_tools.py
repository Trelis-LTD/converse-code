import asyncio

from converse_code.agent_tools import AgentToolRouter, manifest


class FakePi:
    def __init__(self):
        self.commands = []
        self.on_event = None
        self.next_id = 1

    async def command(self, kind, **fields):
        self.commands.append((kind, fields))
        response = {"success": True, "id": f"fake-{self.next_id}"}
        self.next_id += 1
        return response

    async def emit(self, event):
        await self.on_event(event)

class FakeSender:
    def __init__(self):
        self.timeline = []
        self.deferred = []
        self.progress = []
        self.partials = []
        self.voice_prompts = []
        self.results = []

    async def send_tool_deferred(self, call_id, handle, status_label=None):
        self.timeline.append("deferred")
        self.deferred.append((call_id, handle, status_label))

    async def send_tool_progress(self, call_id, note):
        self.timeline.append("progress")
        self.progress.append((call_id, note))

    async def send_tool_partial_result(self, call_id, content, reply=False):
        self.timeline.append("partial")
        self.partials.append((call_id, content, reply))

    async def send_voice_prompt(self, prompt_id, text):
        self.timeline.append("voice_prompt")
        self.voice_prompts.append((prompt_id, text))

    async def send_tool_result(self, call_id, content, **metadata):
        self.timeline.append("result")
        self.results.append((call_id, content, metadata))


def test_manifest_is_a_small_background_tool_reference():
    tools = {tool["name"]: tool for tool in manifest()}
    assert set(tools) == {"coding_task", "continue_task", "approval_decision", "end_session"}
    task = tools["coding_task"]
    assert task["deferred"] is True
    assert task["notify_on_complete"] is True
    assert task["status_label"] == "Coding task"
    approval = tools["approval_decision"]
    assert approval["parameters"]["properties"]["decision"]["enum"] == [
        "allow_once", "allow_session", "block",
    ]


async def test_pi_approval_is_spoken_then_resolved_by_an_explicit_voice_decision():
    pi, sender = FakePi(), FakeSender()
    router = AgentToolRouter(pi, sender, handle="task-reference")
    router.phase = "running"
    router.active_call_id = "task-1"
    router._deferred_sent = True

    await router.on_event({
        "type": "approval_request",
        "approvalId": "approval-7",
        "toolName": "bash",
        "summary": "uv run pytest -q",
    })

    assert sender.partials == [(
        "task-1",
        {
            "speak": (
                "Pi wants to run bash: uv run pytest -q. "
                "Ask the user to allow once, allow for this session, or block it."
            ),
            "data": {
                "event": "approval_required",
                "approval_id": "approval-7",
                "tool": "bash",
                "summary": "uv run pytest -q",
            },
            "handle": "task-reference",
        },
        False,
    )]
    assert sender.voice_prompts == [(
        "approval-7",
        (
            "A protected Pi action is waiting for explicit approval. Approval ID: approval-7. "
            "Tool: bash. Target: uv run pytest -q. Ask the user now whether to allow once, "
            "allow for this session, or block. Do not approve it until the user answers."
        ),
    )]

    await router.handle_tool_call({
        "id": "decision-1",
        "name": "approval_decision",
        "args": {"approval_id": "approval-7", "decision": "allow_once"},
    })

    assert pi.commands == [(
        "approval_response",
        {"approvalId": "approval-7", "decision": "allow_once"},
    )]
    assert sender.results[-1][2] == {"outcome": "succeeded", "verified": True}


async def test_approval_decision_fails_closed_without_a_matching_pending_request():
    pi, sender = FakePi(), FakeSender()
    router = AgentToolRouter(pi, sender, handle="task-reference")

    await router.handle_tool_call({
        "id": "decision-1",
        "name": "approval_decision",
        "args": {"approval_id": "stale", "decision": "allow_once"},
    })

    assert pi.commands == []
    assert sender.results[-1][2]["outcome"] == "failed"


async def test_task_backgrounds_then_emits_silent_and_spoken_partials_and_final_result():
    pi, sender = FakePi(), FakeSender()
    router = AgentToolRouter(pi, sender, handle="task-reference")
    pi.on_event = router.on_event

    running = asyncio.create_task(router.handle_tool_call({
        "id": "call-1", "name": "coding_task", "args": {"request": "Fix it"},
    }))
    await asyncio.sleep(0)
    assert pi.commands == [("prompt", {"message": "Fix it"})]
    assert sender.deferred == [("call-1", "task-reference", "Coding task")]

    await pi.emit({"type": "input_seen", "owner": "bridge", "commandId": "fake-1"})
    await pi.emit({
        "type": "tool_execution_start", "toolCallId": "edit-1", "toolName": "edit",
        "args": {"path": "app.py"},
    })
    await pi.emit({
        "type": "tool_execution_start", "toolCallId": "bash-1", "toolName": "bash",
        "args": {"command": "uv run pytest -q"},
    })
    await pi.emit({"type": "message_end", "message": {
        "role": "assistant", "content": [{"type": "text", "text": "Fixed and tested."}],
    }})
    await pi.emit({"type": "agent_settled"})
    await running

    assert sender.partials[0][2] is False
    assert "app.py" in sender.partials[0][1]["speak"]
    assert sender.partials[1][2] is True
    assert "tests" in sender.partials[1][1]["speak"].lower()
    assert sender.partials[1][1]["data"]["command"] == "uv run pytest -q"
    assert sender.results == [(
        "call-1",
        {"speak": "Fixed and tested.", "data": {}, "handle": "task-reference"},
        {"outcome": "succeeded", "verified": False},
    )]


async def test_read_progress_identifies_the_target_instead_of_repeating_generic_chatter():
    pi, sender = FakePi(), FakeSender()
    router = AgentToolRouter(pi, sender, handle="task-reference")
    router.phase = "running"
    router.active_call_id = "task-1"
    router._deferred_sent = True

    await router.on_event({
        "type": "tool_execution_start", "toolCallId": "read-1", "toolName": "read",
        "args": {"path": "index.html"},
    })

    assert sender.progress == [("task-1", "Pi is preparing to use read: index.html.")]


async def test_unrelated_terminal_input_fails_closed_and_cannot_supply_final_evidence():
    pi, sender = FakePi(), FakeSender()
    router = AgentToolRouter(pi, sender, handle="task-reference")
    running = asyncio.create_task(router.handle_tool_call({
        "id": "call-1", "name": "coding_task", "args": {"request": "Fix it"},
    }))
    await asyncio.sleep(0)
    await pi.emit({"type": "input_seen", "owner": "interactive", "text": "Unrelated work"})
    await pi.emit({"type": "message_end", "message": {
        "role": "assistant", "content": "Unrelated result",
    }})
    await pi.emit({"type": "agent_settled"})
    await running

    assert sender.results[-1][2]["outcome"] == "failed"
    assert "unrelated" in sender.results[-1][1]["speak"].lower()
    assert "Unrelated result" not in sender.results[-1][1]["speak"]


async def test_bridge_input_with_wrong_command_id_cannot_claim_task_ownership():
    pi, sender = FakePi(), FakeSender()
    router = AgentToolRouter(pi, sender, handle="task-reference")
    running = asyncio.create_task(router.handle_tool_call({
        "id": "call-1", "name": "coding_task", "args": {"request": "Fix it"},
    }))
    await asyncio.sleep(0)
    await pi.emit({"type": "input_seen", "owner": "bridge", "commandId": "stale-command"})
    await running

    assert sender.results[-1][2]["outcome"] == "failed"
    assert "did not match" in sender.results[-1][1]["speak"]


async def test_session_shutdown_or_bridge_disconnect_settles_active_task_as_failed():
    for event in ({"type": "session_shutdown"}, {"type": "bridge_disconnect"}):
        pi, sender = FakePi(), FakeSender()
        router = AgentToolRouter(pi, sender, handle="task-reference")
        running = asyncio.create_task(router.handle_tool_call({
            "id": "call-1", "name": "coding_task", "args": {"request": "Fix it"},
        }))
        await asyncio.sleep(0)
        await pi.emit(event)
        await running
        assert sender.results[-1][2]["outcome"] == "failed"


async def test_settled_without_bridge_owned_input_is_not_success():
    pi, sender = FakePi(), FakeSender()
    router = AgentToolRouter(pi, sender, handle="task-reference")
    running = asyncio.create_task(router.handle_tool_call({
        "id": "call-1", "name": "coding_task", "args": {"request": "Fix it"},
    }))
    await asyncio.sleep(0)
    await pi.emit({"type": "agent_settled"})
    await running
    assert sender.results[-1][2]["outcome"] == "failed"
    assert "attributed safely" in sender.results[-1][1]["speak"].lower()


async def test_continue_task_steers_active_work():
    pi, sender = FakePi(), FakeSender()
    router = AgentToolRouter(pi, sender, handle="task-reference")
    router.phase = "running"
    router.active_call_id = "call-1"

    await router.handle_tool_call({
        "id": "call-2", "name": "continue_task",
        "args": {"request": "Also update the docs"},
    })

    assert pi.commands == [("steer", {"message": "Also update the docs"})]
    assert sender.results[-1][2] == {"outcome": "succeeded", "verified": True}


async def test_cancel_aborts_the_active_pi_run():
    pi, sender = FakePi(), FakeSender()
    router = AgentToolRouter(pi, sender, handle="task-reference")
    router.phase = "running"
    router.active_call_id = "call-1"

    await router.handle_tool_cancel({"id": "call-1"})

    assert pi.commands == [("abort", {})]


async def test_cancelled_task_uses_the_browser_sdk_outcome_spelling():
    pi, sender = FakePi(), FakeSender()
    router = AgentToolRouter(pi, sender, handle="task-reference")
    pi.on_event = router.on_event
    running = asyncio.create_task(router.handle_tool_call({
        "id": "call-1", "name": "coding_task", "args": {"request": "Do work"},
    }))
    await asyncio.sleep(0)
    await router.handle_tool_cancel({"id": "call-1"})
    await pi.emit({"type": "agent_settled"})
    await running

    assert sender.results[-1][2] == {"outcome": "cancelled", "verified": False}


async def test_events_that_race_prompt_acceptance_still_follow_deferred():
    class EagerPi(FakePi):
        async def command(self, kind, **fields):
            self.commands.append((kind, fields))
            if kind == "prompt":
                await self.emit({
                    "type": "input_seen", "owner": "bridge", "commandId": "fake-1",
                })
                await self.emit({"type": "tool_execution_start", "toolName": "edit",
                                 "args": {"path": "raced.py"}})
                await self.emit({"type": "message_end", "message": {
                    "role": "assistant", "content": "Done despite the race.",
                }})
                await self.emit({"type": "agent_settled"})
            response = {"success": True, "id": f"fake-{self.next_id}"}
            self.next_id += 1
            return response

    pi, sender = EagerPi(), FakeSender()
    router = AgentToolRouter(pi, sender, handle="task-reference")
    await router.handle_tool_call({
        "id": "call-1", "name": "coding_task", "args": {"request": "Do work"},
    })

    assert sender.timeline == ["deferred", "partial", "result"]


async def test_pi_process_exit_resolves_active_task_as_failed():
    pi, sender = FakePi(), FakeSender()
    router = AgentToolRouter(pi, sender, handle="task-reference")
    running = asyncio.create_task(router.handle_tool_call({
        "id": "call-1", "name": "coding_task", "args": {"request": "Do work"},
    }))
    await asyncio.sleep(0)
    await pi.emit({"type": "process_exit", "status": 7})
    await running

    assert sender.results[-1][2] == {"outcome": "failed", "verified": False}
    assert "status 7" in sender.results[-1][1]["speak"]
