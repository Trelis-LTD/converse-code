import asyncio

from converse_code.agent_tools import AgentToolRouter, manifest


class FakePi:
    def __init__(self):
        self.commands = []
        self.extension_responses = []
        self.on_event = None

    async def command(self, kind, **fields):
        self.commands.append((kind, fields))
        return {"success": True}

    async def emit(self, event):
        await self.on_event(event)

    async def send_extension_response(self, request_id, **fields):
        self.extension_responses.append((request_id, fields))


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
    assert set(tools) == {"coding_task", "continue_task", "end_session"}
    task = tools["coding_task"]
    assert task["deferred"] is True
    assert task["notify_on_complete"] is True
    assert task["status_label"] == "Coding task"


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
    assert sender.results == [(
        "call-1",
        {"speak": "Fixed and tested.", "data": {}, "handle": "task-reference"},
        {"outcome": "succeeded", "verified": False},
    )]


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


async def test_structured_blocking_request_is_a_reply_true_partial():
    pi, sender = FakePi(), FakeSender()
    router = AgentToolRouter(pi, sender, handle="task-reference")
    router.phase = "running"
    router.active_call_id = "call-1"
    router._deferred_sent = True

    await router.on_event({
        "type": "extension_ui_request", "id": "approval-1", "method": "confirm",
        "title": "Run deployment?", "message": "This changes production.",
    })

    call_id, content, reply = sender.partials[-1]
    assert call_id == "call-1"
    assert reply is True
    assert content["data"]["request_id"] == "approval-1"
    assert "Run deployment?" in content["speak"]

    await router.handle_tool_call({
        "id": "call-2", "name": "continue_task", "args": {"request": "yes"},
    })
    assert pi.extension_responses == [("approval-1", {"confirmed": True})]
    assert router.phase == "running"


async def test_select_requires_an_exact_structured_option():
    pi, sender = FakePi(), FakeSender()
    router = AgentToolRouter(pi, sender, handle="task-reference")
    router.phase = "awaiting_input"
    router.active_call_id = "call-1"
    router._deferred_sent = True
    router.pending_ui = {
        "id": "choice-1", "method": "select", "title": "Choose", "options": ["A", "B"],
    }

    await router.handle_tool_call({
        "id": "bad", "name": "continue_task", "args": {"request": "something else"},
    })
    assert pi.extension_responses == []
    assert sender.results[-1][2]["outcome"] == "failed"

    await router.handle_tool_call({
        "id": "good", "name": "continue_task", "args": {"request": "b"},
    })
    assert pi.extension_responses == [("choice-1", {"value": "B"})]


async def test_failed_ui_answer_delivery_returns_a_clean_tool_failure():
    class BrokenPi(FakePi):
        async def send_extension_response(self, request_id, **fields):
            from converse_code.pi_rpc import PiRPCError

            raise PiRPCError("connection closed")

    pi, sender = BrokenPi(), FakeSender()
    router = AgentToolRouter(pi, sender, handle="task-reference")
    router.phase = "awaiting_input"
    router.active_call_id = "call-1"
    router._deferred_sent = True
    router.pending_ui = {
        "id": "choice-1", "method": "select", "title": "Choose", "options": ["A", "B"],
    }

    await router.handle_tool_call({
        "id": "reply-1", "name": "continue_task", "args": {"request": "A"},
    })

    assert sender.results[-1][2] == {"outcome": "failed", "verified": False}
    assert "connection closed" in sender.results[-1][1]["speak"]
    assert router.phase == "awaiting_input"


async def test_events_that_race_prompt_acceptance_still_follow_deferred():
    class EagerPi(FakePi):
        async def command(self, kind, **fields):
            self.commands.append((kind, fields))
            if kind == "prompt":
                await self.emit({"type": "tool_execution_start", "toolName": "edit",
                                 "args": {"path": "raced.py"}})
                await self.emit({"type": "message_end", "message": {
                    "role": "assistant", "content": "Done despite the race.",
                }})
                await self.emit({"type": "agent_settled"})
            return {"success": True}

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
