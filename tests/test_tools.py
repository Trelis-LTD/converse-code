import asyncio

from conftest import append_transcript, finish_turn

from converse_code.tools import manifest

MENU_LINES = [
    "  Do you want to proceed?",
    "  ❯ 1. Yes",
    "    2. No, and tell Claude what to do differently (esc)",
    "",
]


def assistant(text=None, tool=None, file_path=None):
    blocks = []
    if tool:
        blocks.append({"type": "tool_use", "name": tool, "input": {"file_path": file_path} if file_path else {}})
    if text:
        blocks.append({"type": "text", "text": text})
    return {"type": "assistant", "message": {"content": blocks}}


def test_manifest_shape():
    tools = manifest()
    names = [t["name"] for t in tools]
    assert names == ["long_task", "command", "select_option", "press_key"]
    long_task = tools[0]
    assert long_task["requires_permission"] is True
    assert long_task["timeout"] == 600  # server ceiling, verified against prod
    assert long_task["status_label"] == "Claude Code task"
    assert "request" in long_task["parameters"]["properties"]


async def test_long_task_completes_on_stop_hook(router, fake_driver, fake_sender):
    append_transcript(
        router,
        assistant(tool="Edit", file_path="/p/auth.py"),
        assistant(text="Fixed the login bug. Tests pass."),
    )
    task = asyncio.create_task(router.handle_tool_call(
        {"type": "tool_call", "id": "c1", "name": "long_task", "args": {"request": "fix the login bug"}}
    ))
    await finish_turn(router)
    await task

    assert fake_driver.injected == ["fix the login bug"]
    call_id, content = fake_sender.results[0]
    assert call_id == "c1"
    assert "Fixed the login bug" in content["speak"]
    assert content["data"]["state"] == "idle"
    assert content["data"]["files"] == ["/p/auth.py"]
    assert content["handle"] == "cc-test-abc"
    assert fake_sender.context == []


async def test_long_task_falls_back_to_hook_message(router, fake_sender):
    """The Stop hook can fire before the transcript flush — the payload's
    last_assistant_message must cover the gap."""
    task = asyncio.create_task(router.handle_tool_call(
        {"id": "c1", "name": "long_task", "args": {"request": "quick thing"}}
    ))
    await asyncio.sleep(0.1)
    await router.on_hook("stop", {
        "transcript_path": str(router.transcript_path),
        "last_assistant_message": "pong",
    })
    await task
    assert fake_sender.results[0][1]["speak"] == "pong"


async def test_long_task_emits_progress_notes(router, fake_sender):
    async def work():
        await asyncio.sleep(0.2)
        append_transcript(router, assistant(tool="Edit", file_path="/p/auth.py"))
        await finish_turn(router, delay=0.3)

    side = asyncio.create_task(work())
    await router.handle_tool_call(
        {"id": "c1", "name": "long_task", "args": {"request": "do a thing"}}
    )
    await side
    assert ("c1", "editing auth.py") in fake_sender.progress


async def test_long_task_resolves_still_working_at_deadline(router, fake_sender):
    router.HOLD_S = 0.3
    await router.handle_tool_call({"id": "c1", "name": "long_task", "args": {"request": "slow task"}})
    _, content = fake_sender.results[0]
    assert "Still working" in content["speak"]
    assert "announced automatically" in content["speak"]
    assert "Call long_task again" not in content["speak"]
    assert content["data"]["state"] == "working"


async def test_long_task_resolves_when_menu_appears(router, fake_driver, fake_sender):
    async def open_menu():
        await asyncio.sleep(0.15)
        fake_driver.lines = MENU_LINES

    side = asyncio.create_task(open_menu())
    await router.handle_tool_call({"id": "c1", "name": "long_task", "args": {"request": "risky task"}})
    await side
    _, content = fake_sender.results[0]
    assert "needs input" in content["speak"]
    assert content["data"]["state"] == "menu"
    assert content["data"]["options"][0] == "Yes"


async def test_long_task_refused_while_menu_open(router, fake_driver, fake_sender):
    fake_driver.lines = MENU_LINES
    await router.handle_tool_call({"id": "c1", "name": "long_task", "args": {"request": "x"}})
    assert fake_driver.injected == []
    _, content = fake_sender.results[0]
    assert "menu" in content["speak"].lower()


async def test_second_long_task_queues(router, fake_driver, fake_sender):
    t1 = asyncio.create_task(router.handle_tool_call(
        {"id": "c1", "name": "long_task", "args": {"request": "first"}}
    ))
    await asyncio.sleep(0.1)
    await router.handle_tool_call({"id": "c2", "name": "long_task", "args": {"request": "second"}})

    queued = next(c for cid, c in fake_sender.results if cid == "c2")
    assert "Queued" in queued["speak"]
    assert queued["data"]["queue"] == ["second"]
    assert fake_driver.injected == ["first", "second"]

    append_transcript(router, assistant(text="First done."))
    await finish_turn(router, delay=0)
    await t1
    assert router.working is True  # queued item is now running
    assert router.queue == []

    await router.on_hook("stop", {"transcript_path": str(router.transcript_path)})
    assert router.working is False


async def test_server_tool_cancel_interrupts_matching_task(router, fake_driver, fake_sender):
    t1 = asyncio.create_task(router.handle_tool_call(
        {"id": "c1", "name": "long_task", "args": {"request": "long thing"}}
    ))
    await asyncio.sleep(0.1)
    await router.handle_tool_cancel({"type": "tool_cancel", "id": "c1"})
    await t1

    assert "escape" in fake_driver.keys
    assert fake_sender.results == []  # Converse already discarded the canceled call
    assert router.working is False


async def test_server_tool_cancel_ignores_an_unrelated_call(router, fake_driver):
    t1 = asyncio.create_task(router.handle_tool_call(
        {"id": "c1", "name": "long_task", "args": {"request": "long thing"}}
    ))
    await asyncio.sleep(0.1)
    await router.handle_tool_cancel({"type": "tool_cancel", "id": "someone-else"})
    assert fake_driver.keys == []
    await router.handle_tool_cancel({"type": "tool_cancel", "id": "c1"})
    await t1


async def test_canceled_turn_does_not_suppress_the_next_terminal_completion(
    router, fake_sender,
):
    task = asyncio.create_task(router.handle_tool_call(
        {"id": "c1", "name": "long_task", "args": {"request": "cancel me"}}
    ))
    await asyncio.sleep(0.1)
    await router.handle_tool_cancel({"type": "tool_cancel", "id": "c1"})
    await task
    await router.on_hook("stop", {"last_assistant_message": "Canceled."})
    assert fake_sender.context == []

    await router.on_hook("stop", {"last_assistant_message": "A later typed task finished."})
    assert len(fake_sender.context) == 1
    assert "later typed task" in fake_sender.context[0][0]


async def test_stop_hook_wakes_voice_for_terminal_typed_work(router, fake_sender):
    await router.on_hook("stop", {
        "transcript_path": str(router.transcript_path),
        "last_assistant_message": "Updated the parser and all tests pass.",
    })

    assert len(fake_sender.context) == 1
    text, role, reply = fake_sender.context[0]
    assert "Updated the parser" in text
    assert "entered directly in the terminal" in text
    assert role == "context"
    assert reply is True


async def test_terminal_completion_does_not_reuse_stale_voice_result(router, fake_sender):
    router.last_assistant_text = "Old voice-started result."
    append_transcript(router, assistant(text="New terminal result."))

    await router.on_hook("stop", {"transcript_path": str(router.transcript_path)})

    assert "New terminal result" in fake_sender.context[0][0]
    assert "Old voice-started result" not in fake_sender.context[0][0]


async def test_completion_after_tool_deadline_wakes_voice(router, fake_sender):
    router.HOLD_S = 0.1
    await router.handle_tool_call(
        {"id": "c1", "name": "long_task", "args": {"request": "slow task"}}
    )
    assert fake_sender.context == []

    await router.on_hook("stop", {
        "transcript_path": str(router.transcript_path),
        "last_assistant_message": "The slow task is complete.",
    })

    assert fake_sender.context and fake_sender.context[0][2] is True


async def test_command_reports_menu(router, fake_driver, fake_sender):
    async def show_menu():
        await asyncio.sleep(0.02)
        fake_driver.lines = [" Select model:", " ❯ Sonnet", "   Opus", ""]

    side = asyncio.create_task(show_menu())
    await router.handle_tool_call({"id": "c1", "name": "command", "args": {"command": "/model"}})
    await side
    assert fake_driver.injected == ["/model"]
    _, content = fake_sender.results[0]
    assert "Sonnet" in content["speak"] and "menu" in content["speak"]


async def test_command_requires_slash(router, fake_sender):
    await router.handle_tool_call({"id": "c1", "name": "command", "args": {"command": "clear"}})
    assert "slash" in fake_sender.results[0][1]["speak"]


async def test_select_option_arrow_math(router, fake_driver, fake_sender):
    fake_driver.lines = [" Select model:", " ❯ Opus", "   Sonnet", "   Haiku", ""]

    async def close_menu():
        await asyncio.sleep(0.02)
        fake_driver.lines = [" > ", ""]

    side = asyncio.create_task(close_menu())
    await router.handle_tool_call({"id": "c1", "name": "select_option", "args": {"option": "haiku"}})
    await side
    assert fake_driver.keys == ["down", "down", "enter"]
    assert "Chose Haiku" in fake_sender.results[0][1]["speak"]


async def test_select_option_without_menu(router, fake_sender):
    await router.handle_tool_call({"id": "c1", "name": "select_option", "args": {"option": "yes"}})
    assert "no menu" in fake_sender.results[0][1]["speak"].lower()


async def test_press_key(router, fake_driver, fake_sender):
    await router.handle_tool_call({"id": "c1", "name": "press_key", "args": {"key": "ctrl-c"}})
    assert fake_driver.keys == ["ctrl-c"]
    await router.handle_tool_call({"id": "c2", "name": "press_key", "args": {"key": "bogus"}})
    assert "Unknown key" in fake_sender.results[1][1]["speak"]


async def test_handler_exception_still_resolves(router, fake_driver, fake_sender):
    fake_driver.inject = None  # force a TypeError inside the handler
    await router.handle_tool_call({"id": "c1", "name": "long_task", "args": {"request": "x"}})
    _, content = fake_sender.results[0]
    assert "went wrong" in content["speak"]
