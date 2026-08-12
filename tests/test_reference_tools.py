import asyncio

from converse_code.agent_tools import AgentToolRouter, manifest
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
        data = {}
        if kind == "model_state":
            data = {
                "provider": "openai-codex",
                "model": "gpt-5.6-sol" if "Sol" in fields["request"] else "gpt-5.6-luna",
                "changed": "Sol" in fields["request"],
                "available": [
                    "openai-codex/gpt-5.6-luna", "openai-codex/gpt-5.6-sol",
                ],
            }
        return CommandReceipt(response, data)

    async def emit(self, event):
        await self.router.on_event(event)


def make_router(pi, sender):
    router = AgentToolRouter(pi, sender, handle="task-reference")
    pi.router = router
    return router

class FakeSender:
    def __init__(self):
        self.timeline = []
        self.deferred = []
        self.progress = []
        self.partials = []
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

    async def send_tool_result(self, call_id, content, **metadata):
        self.timeline.append("result")
        self.results.append((call_id, content, metadata))


def test_manifest_is_a_small_background_tool_reference():
    tools = {tool["name"]: tool for tool in manifest()}
    assert set(tools) == {
        "coding_task", "continue_task", "approval_decision", "pi_model",
    }
    task = tools["coding_task"]
    assert task["deferred"] is True
    assert task["notify_on_complete"] is True
    assert task["status_label"] == "Coding task"
    approval = tools["approval_decision"]
    assert approval["parameters"]["properties"]["decision"]["enum"] == [
        "allow_once", "allow_session", "block",
    ]


async def test_pi_model_reports_only_pi_acknowledged_selected_state():
    pi, sender = FakePi(), FakeSender()
    router = make_router(pi, sender)

    await router.handle_tool_call({
        "id": "model-1", "name": "pi_model", "args": {"request": "Use GPT 5.6 Sol"},
    })

    assert pi.commands == [("model_state", {"request": "Use GPT 5.6 Sol"})]
    assert sender.results[-1] == (
        "model-1",
        {
            "speak": "Pi is now using openai-codex/gpt-5.6-sol.",
            "data": {
                "provider": "openai-codex", "model": "gpt-5.6-sol", "changed": True,
                "available": [
                    "openai-codex/gpt-5.6-luna", "openai-codex/gpt-5.6-sol",
                ],
            },
            "handle": "task-reference",
        },
        {"outcome": "succeeded", "verified": True},
    )


async def test_pi_model_question_returns_authoritative_current_state_without_a_change():
    pi, sender = FakePi(), FakeSender()
    router = make_router(pi, sender)

    await router.handle_tool_call({
        "id": "model-2", "name": "pi_model", "args": {"request": "What model are we using?"},
    })

    assert pi.commands == [("model_state", {"request": "What model are we using?"})]
    assert sender.results[-1][1]["speak"] == (
        "Pi is using openai-codex/gpt-5.6-luna. Available models are "
        "openai-codex/gpt-5.6-luna and openai-codex/gpt-5.6-sol. Which one would you like?"
    )
    assert sender.results[-1][2] == {"outcome": "succeeded", "verified": True}


async def test_unspecified_model_change_returns_available_choices_without_guessing():
    pi, sender = FakePi(), FakeSender()
    router = make_router(pi, sender)

    await router.handle_tool_call({
        "id": "model-3", "name": "pi_model",
        "args": {"request": "Can you change the model to something else?"},
    })

    assert pi.commands == [(
        "model_state", {"request": "Can you change the model to something else?"},
    )]
    assert sender.results[-1][1]["speak"] == (
        "Pi is using openai-codex/gpt-5.6-luna. Available models are "
        "openai-codex/gpt-5.6-luna and openai-codex/gpt-5.6-sol. Which one would you like?"
    )
    assert sender.results[-1][2] == {"outcome": "succeeded", "verified": True}


async def test_pi_approval_is_a_replying_partial_then_resolved_by_an_explicit_decision():
    pi, sender = FakePi(), FakeSender()
    router = make_router(pi, sender)
    running = asyncio.create_task(router.handle_tool_call({
        "id": "task-1", "name": "coding_task", "args": {"request": "Run the tests"},
    }))
    await asyncio.sleep(0)
    await pi.emit({"type": "input_seen", "owner": "bridge", "commandId": "fake-1"})

    await router.on_event({
        "type": "approval_request",
        "approvalId": "approval-7",
        "toolName": "bash",
        "summary": "uv run pytest -q",
    })

    approval = sender.partials[-1]
    assert approval[0] == "task-1"
    assert approval[1]["data"] == {
        "event": "approval_required",
        "approval_id": "approval-7",
        "tool": "bash",
        "summary": "uv run pytest -q",
    }
    assert approval[2] is True
    assert all(choice in approval[1]["speak"] for choice in (
        "allow once", "allow for this session", "block",
    ))
    assert sender.timeline[-1] == "partial"

    await router.handle_tool_call({
        "id": "decision-1",
        "name": "approval_decision",
        "args": {"approval_id": "approval-7", "decision": "allow_once"},
    })

    assert pi.commands[-1] == (
        "approval_response", {"approvalId": "approval-7", "decision": "allow_once"},
    )
    assert sender.results[-1][2] == {"outcome": "succeeded", "verified": True}
    await pi.emit({"type": "message_end", "message": {
        "role": "assistant", "content": "Tests passed.",
    }})
    await pi.emit({"type": "agent_settled"})
    await running


async def test_approval_decision_fails_closed_without_a_matching_pending_request():
    pi, sender = FakePi(), FakeSender()
    router = make_router(pi, sender)

    await router.handle_tool_call({
        "id": "decision-1",
        "name": "approval_decision",
        "args": {"approval_id": "stale", "decision": "allow_once"},
    })

    assert pi.commands == []
    assert sender.results[-1][2]["outcome"] == "failed"


async def test_non_string_tool_arguments_are_rejected_before_reaching_pi():
    pi, sender = FakePi(), FakeSender()
    router = make_router(pi, sender)

    running = asyncio.create_task(router.handle_tool_call({
        "id": "call-1", "name": "coding_task", "args": {"request": ["Fix it"]},
    }))
    await asyncio.sleep(0)
    if pi.commands:
        await pi.emit({"type": "input_seen", "owner": "bridge", "commandId": "fake-1"})
        await pi.emit({"type": "agent_settled"})
    await running

    assert pi.commands == []
    assert sender.results[-1][2]["outcome"] == "failed"


async def test_task_backgrounds_then_emits_silent_and_spoken_partials_and_final_result():
    pi, sender = FakePi(), FakeSender()
    router = make_router(pi, sender)
    running = asyncio.create_task(router.handle_tool_call({
        "id": "call-1", "name": "coding_task", "args": {"request": "Fix it"},
    }))
    await asyncio.sleep(0)
    assert pi.commands == [("prompt", {"message": "Fix it"})]
    assert sender.deferred == [("call-1", "task-reference", "Coding task")]

    await pi.emit({"type": "input_seen", "owner": "bridge", "commandId": "fake-1"})
    await pi.emit({
        "type": "tool_execution_start", "toolCallId": "read-1", "toolName": "read",
        "args": {"path": "app.py"},
    })
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

    assert sender.progress == [("call-1", "Pi is preparing to use read: app.py.")]
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


async def test_unrelated_terminal_input_fails_closed_and_cannot_supply_final_evidence():
    pi, sender = FakePi(), FakeSender()
    router = make_router(pi, sender)
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
    router = make_router(pi, sender)
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
        router = make_router(pi, sender)
        running = asyncio.create_task(router.handle_tool_call({
            "id": "call-1", "name": "coding_task", "args": {"request": "Fix it"},
        }))
        await asyncio.sleep(0)
        await pi.emit(event)
        await running
        assert sender.results[-1][2]["outcome"] == "failed"


async def test_settled_without_bridge_owned_input_is_not_success():
    pi, sender = FakePi(), FakeSender()
    router = make_router(pi, sender)
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
    router = make_router(pi, sender)
    running = asyncio.create_task(router.handle_tool_call({
        "id": "call-1", "name": "coding_task", "args": {"request": "Update the code"},
    }))
    await asyncio.sleep(0)
    await pi.emit({"type": "input_seen", "owner": "bridge", "commandId": "fake-1"})

    await router.handle_tool_call({
        "id": "call-2", "name": "continue_task",
        "args": {"request": "Also update the docs"},
    })

    assert pi.commands[-1] == ("steer", {"message": "Also update the docs"})
    assert sender.results[-1][2] == {"outcome": "succeeded", "verified": True}
    await pi.emit({"type": "message_end", "message": {
        "role": "assistant", "content": "Updated both.",
    }})
    await pi.emit({"type": "agent_settled"})
    await running


async def test_cancelled_task_uses_the_browser_sdk_outcome_spelling():
    pi, sender = FakePi(), FakeSender()
    router = make_router(pi, sender)
    running = asyncio.create_task(router.handle_tool_call({
        "id": "call-1", "name": "coding_task", "args": {"request": "Do work"},
    }))
    await asyncio.sleep(0)
    await router.handle_tool_cancel({"id": "call-1"})
    await pi.emit({"type": "agent_settled"})
    await running

    assert pi.commands[-1] == ("abort", {})
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
                response = f"fake-{self.next_id}"
                self.next_id += 1
                return CommandReceipt(response, {})

    pi, sender = EagerPi(), FakeSender()
    router = make_router(pi, sender)
    await router.handle_tool_call({
        "id": "call-1", "name": "coding_task", "args": {"request": "Do work"},
    })

    assert sender.timeline.index("deferred") < sender.timeline.index("partial")
    assert sender.timeline.index("deferred") < sender.timeline.index("result")


async def test_pi_process_exit_resolves_active_task_as_failed():
    pi, sender = FakePi(), FakeSender()
    router = make_router(pi, sender)
    running = asyncio.create_task(router.handle_tool_call({
        "id": "call-1", "name": "coding_task", "args": {"request": "Do work"},
    }))
    await asyncio.sleep(0)
    await pi.emit({"type": "process_exit", "status": 7})
    await running

    assert sender.results[-1][2] == {"outcome": "failed", "verified": False}
    assert "status 7" in sender.results[-1][1]["speak"]
