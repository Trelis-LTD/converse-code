import asyncio

from conftest import append_transcript, finish_turn

from converse_code.tools import ToolRouter

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


async def test_long_task_rejects_bash_mode_prefix(router, fake_driver, fake_sender):
    """A leading '!' is the TUI's raw-shell escape: it bypasses Claude Code's
    permission system, so it must never be injected from the voice path — and
    the guard must see the sanitized text, or a stripped control character
    prefix would smuggle '!' past it."""
    for request in ("!open index.html", "\x01!ls", "\x1b!rm -rf /"):
        await router.handle_tool_call(
            {"id": "c1", "name": "long_task", "args": {"request": request}}
        )
        assert fake_driver.injected == []
        _, content = fake_sender.results[-1]
        assert "not allowed" in content["speak"]
        assert content["data"]["phase"] == "idle"
    assert content["data"]["active_task"] is None


async def test_long_task_redirects_slash_commands(router, fake_driver, fake_sender):
    await router.handle_tool_call(
        {"id": "c1", "name": "long_task", "args": {"request": "/model"}}
    )
    assert fake_driver.injected == []
    _, content = fake_sender.results[0]
    assert "command tool" in content["speak"]


async def test_lagged_transcript_flush_does_not_leak_into_next_turn(router, fake_sender):
    """Turn A's transcript entry can flush to disk after A resolved via the
    hook's text. Turn B must speak B's hook text, not A's late-flushed entry."""
    task = asyncio.create_task(router.handle_tool_call(
        {"id": "a", "name": "long_task", "args": {"request": "make hello.py"}}
    ))
    await asyncio.sleep(0.1)
    await router.on_hook("stop", {
        "transcript_path": str(router.transcript_path),
        "last_assistant_message": "Created hello.py.",
    })
    await task
    # A's entry reaches the transcript only now, during idle.
    append_transcript(router, assistant(text="Created hello.py."))

    task = asyncio.create_task(router.handle_tool_call(
        {"id": "b", "name": "long_task", "args": {"request": "run it"}}
    ))
    await asyncio.sleep(0.1)
    await router.on_hook("stop", {
        "transcript_path": str(router.transcript_path),
        "last_assistant_message": "It prints hello world.",
    })
    await task
    assert fake_sender.results[-1][1]["speak"] == "It prints hello world."


async def test_long_task_resolves_immediately_on_claude_stop_failure(router, fake_sender):
    task = asyncio.create_task(router.handle_tool_call(
        {"id": "c1", "name": "long_task", "args": {"request": "quick thing"}}
    ))
    await asyncio.sleep(0.05)
    await router.on_hook("stop_failure", {
        "error": "authentication_failed",
        "error_details": "Claude session expired",
    })
    await task

    content = fake_sender.results[0][1]
    assert "Claude session expired" in content["speak"]
    assert content["data"]["phase"] == "idle"


async def test_long_task_defers_and_promotes_milestones(router, fake_sender):
    """A starting turn is deferred; a file edit becomes a silent partial result
    (milestones speak, telemetry stays silent — edits stay silent but current)."""
    async def work():
        await asyncio.sleep(0.2)
        append_transcript(router, assistant(tool="Edit", file_path="/p/auth.py"))
        await finish_turn(router, delay=0.3)

    side = asyncio.create_task(work())
    await router.handle_tool_call(
        {"id": "c1", "name": "long_task", "args": {"request": "do a thing"}}
    )
    await side
    assert fake_sender.deferred == [("c1", "cc-test-abc-c1")]
    assert ("c1", {"speak": "Edited auth.py.", "data": {"files": ["auth.py"]},
                   "handle": "cc-test-abc"}, False) in fake_sender.partials


async def test_long_task_resolves_at_deferred_deadline(router, fake_sender):
    router.HOLD_S = 0.3
    await router.handle_tool_call({"id": "c1", "name": "long_task", "args": {"request": "slow task"}})
    _, content = fake_sender.results[0]
    assert "still working" in content["speak"].lower()
    assert content["data"]["phase"] == "working"


async def test_long_task_refused_while_menu_open(router, fake_driver, fake_sender):
    fake_driver.lines = MENU_LINES
    await router.handle_tool_call({"id": "c1", "name": "long_task", "args": {"request": "x"}})
    assert fake_driver.injected == []
    _, content = fake_sender.results[0]
    assert "menu" in content["speak"].lower()


async def test_long_task_waits_for_matching_prompt_submission_hook(
    fake_driver, fake_sender, tmp_path,
):
    router = ToolRouter(
        fake_driver, fake_sender, handle="cc-test", project_dir=tmp_path,
    )
    router.SUBMIT_ACK_S = 0.05
    router.POLL_S = 0.01
    router.HOLD_S = 0.2

    task = asyncio.create_task(router.handle_tool_call(
        {"id": "c1", "name": "long_task", "args": {"request": "read tests"}}
    ))
    await asyncio.sleep(0.01)
    await router.on_hook("user_prompt_submit", {"prompt": "a different prompt"})
    await asyncio.sleep(0.01)
    assert not task.done()

    await router.on_hook("user_prompt_submit", {"prompt": "read tests"})
    await router.on_hook("stop", {"last_assistant_message": "Done."})
    await task

    assert fake_driver.keys == []
    assert fake_sender.results[0][1]["speak"] == "Done."


async def test_long_task_retries_enter_then_fails_fast_without_ack(
    fake_driver, fake_sender, tmp_path,
):
    router = ToolRouter(
        fake_driver, fake_sender, handle="cc-test", project_dir=tmp_path,
    )
    router.SUBMIT_ACK_S = 0.01
    router.SUBMIT_ATTEMPTS = 3

    await router.handle_tool_call(
        {"id": "c1", "name": "long_task", "args": {"request": "read tests"}}
    )

    assert fake_driver.keys == ["enter", "enter"]
    content = fake_sender.results[0][1]
    assert "couldn't confirm" in content["speak"].lower()
    assert content["data"]["phase"] == "idle"


async def test_second_long_task_requires_explicit_steering(router, fake_driver, fake_sender):
    t1 = asyncio.create_task(router.handle_tool_call(
        {"id": "c1", "name": "long_task", "args": {"request": "first"}}
    ))
    await asyncio.sleep(0.1)
    await router.handle_tool_call({"id": "c2", "name": "long_task", "args": {"request": "second"}})

    rejected = next(c for cid, c in fake_sender.results if cid == "c2")
    assert "steer_task" in rejected["speak"]
    assert fake_driver.injected == ["first"]

    append_transcript(router, assistant(text="First done."))
    await finish_turn(router, delay=0)
    await t1
    assert router.state() == "idle"


async def test_steer_task_adds_guidance_to_current_turn(router, fake_driver, fake_sender):
    task = asyncio.create_task(router.handle_tool_call(
        {"id": "c1", "name": "long_task", "args": {"request": "first"}}
    ))
    await asyncio.sleep(0.1)

    await router.handle_tool_call(
        {"id": "c2", "name": "steer_task", "args": {"request": "also update the docs"}}
    )

    steered = next(c for cid, c in fake_sender.results if cid == "c2")
    assert "current" in steered["speak"].lower()
    assert fake_driver.injected == ["first", "also update the docs"]
    assert router.state() == "working"

    await router.on_hook("stop", {"last_assistant_message": "Code and docs updated."})
    await task
    assert router.state() == "idle"


async def test_steer_task_requires_active_work(router, fake_driver, fake_sender):
    await router.handle_tool_call(
        {"id": "c1", "name": "steer_task", "args": {"request": "also update the docs"}}
    )
    assert fake_driver.injected == []
    assert "long_task" in fake_sender.results[0][1]["speak"]


async def test_server_tool_cancel_interrupts_matching_task(router, fake_driver, fake_sender):
    t1 = asyncio.create_task(router.handle_tool_call(
        {"id": "c1", "name": "long_task", "args": {"request": "long thing"}}
    ))
    await asyncio.sleep(0.1)
    await router.handle_tool_cancel({"type": "tool_cancel", "id": "c1"})
    await t1

    assert "escape" in fake_driver.keys
    assert fake_sender.results == []  # Converse already discarded the canceled call
    assert router.state() == "canceling"
    await router.on_hook("stop", {"prompt_id": "test-prompt-1"})
    assert router.state() == "idle"


async def test_server_tool_cancel_ignores_an_unrelated_call(router, fake_driver):
    t1 = asyncio.create_task(router.handle_tool_call(
        {"id": "c1", "name": "long_task", "args": {"request": "long thing"}}
    ))
    await asyncio.sleep(0.1)
    await router.handle_tool_cancel({"type": "tool_cancel", "id": "someone-else"})
    assert fake_driver.keys == []
    await router.handle_tool_cancel({"type": "tool_cancel", "id": "c1"})
    await t1


async def test_cancel_is_ignored_once_completed_result_is_being_sent(
    router, fake_driver, fake_sender,
):
    sending = asyncio.Event()
    release = asyncio.Event()
    original_send = fake_sender.send_tool_result

    async def blocked_send(call_id, content):
        sending.set()
        await release.wait()
        await original_send(call_id, content)

    fake_sender.send_tool_result = blocked_send
    task = asyncio.create_task(router.handle_tool_call(
        {"id": "c1", "name": "long_task", "args": {"request": "quick task"}}
    ))
    await asyncio.sleep(0.1)
    await router.on_hook("stop", {"last_assistant_message": "Done."})
    await sending.wait()

    await router.handle_tool_cancel({"type": "tool_cancel", "id": "c1"})
    release.set()
    await task

    assert fake_driver.keys == []
    assert fake_sender.results[0][1]["speak"] == "Done."


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


async def test_prompt_correlated_cancel_blocks_new_ui_work_and_ignores_late_stop(
    router, fake_driver, fake_sender,
):
    fake_driver.auto_ack = False
    task = asyncio.create_task(router.handle_tool_call(
        {"id": "old", "name": "long_task", "args": {"request": "open the game"}}
    ))
    await asyncio.sleep(0.05)
    await router.on_hook("user_prompt_submit", {
        "prompt": "open the game", "prompt_id": "prompt-old",
    })
    await router.handle_tool_cancel({"type": "tool_cancel", "id": "old"})
    await task

    assert router.state() == "canceling"
    await router.handle_tool_call(
        {"id": "model", "name": "command", "args": {"command": "/model"}}
    )
    assert fake_driver.injected == ["open the game"]
    assert "still stopping" in fake_sender.results[-1][1]["speak"].lower()

    await router.on_hook("stop", {
        "prompt_id": "prompt-old", "last_assistant_message": "The game is open.",
    })
    assert router.state() == "idle"
    assert fake_sender.context == []

    next_task = asyncio.create_task(router.handle_tool_call(
        {"id": "new", "name": "long_task", "args": {"request": "check the model"}}
    ))
    await asyncio.sleep(0.05)
    await router.on_hook("user_prompt_submit", {
        "prompt": "check the model", "prompt_id": "prompt-new",
    })
    # A duplicate/late Stop from the canceled episode must not complete the new one.
    await router.on_hook("stop", {
        "prompt_id": "prompt-old", "last_assistant_message": "The game is up and running.",
    })
    assert not next_task.done()
    assert router.state() == "working"
    assert router.semantic_state()["last_action"]["action"] == "long_task"

    await router.on_hook("stop", {
        "prompt_id": "prompt-new", "last_assistant_message": "The model is Fable.",
    })
    await next_task
    assert fake_sender.results[-1][1]["speak"] == "The model is Fable."


async def test_cancel_can_settle_from_verified_idle_ui_without_stop_hook(
    router, fake_driver,
):
    fake_driver.auto_ack = False
    router.CANCEL_POLL_S = 0.01
    router.CANCEL_GRACE_S = 0.01
    router.CANCEL_IDLE_SAMPLES = 2
    router.CANCEL_RETRY_S = 0.01
    task = asyncio.create_task(router.handle_tool_call(
        {"id": "old", "name": "long_task", "args": {"request": "long task"}}
    ))
    await asyncio.sleep(0.02)
    await router.on_hook("user_prompt_submit", {
        "prompt": "long task", "prompt_id": "prompt-old",
    })
    fake_driver.lines = ["✻ Working…", "esc to interrupt"]
    await router.handle_tool_cancel({"type": "tool_cancel", "id": "old"})
    await task
    assert router.state() == "canceling"
    deadline = asyncio.get_running_loop().time() + 1
    while fake_driver.keys.count("escape") < 2 and asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(0.01)
    assert fake_driver.keys.count("escape") >= 2

    fake_driver.lines = [
        "────────────────────────", "❯", "────────────────────────",
    ]
    deadline = asyncio.get_running_loop().time() + 1
    while router.state() != "idle" and asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(0.01)

    assert router.state() == "idle"
    action = router.semantic_state()["last_action"]
    assert action["action"] == "cancel_task"
    assert action["status"] == "verified"


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
    assert reply is False  # telemetry, not a milestone: current, but silent


async def test_permission_hook_wakes_voice_for_terminal_typed_menu(
    router, fake_driver, fake_sender,
):
    fake_driver.lines = MENU_LINES

    await router.on_hook("permission_request", {"tool_name": "Bash"})
    await asyncio.sleep(router.SETTLE_S * 2)

    assert fake_sender.context
    text, role, reply = fake_sender.context[0]
    assert "permission" in text.lower()
    assert "Yes" in text
    assert role == "context"
    assert reply is True


async def test_permission_hook_does_not_announce_auto_mode_denial(router, fake_sender):
    await router.on_hook("permission_request", {"tool_name": "Bash"})
    await asyncio.sleep(router.SETTLE_S * 2)

    assert fake_sender.context == []


async def test_stop_failure_wakes_voice_for_terminal_typed_work(router, fake_sender):
    await router.on_hook("stop_failure", {
        "error": "rate_limit",
        "error_details": "Try again later",
    })

    assert fake_sender.context
    assert "Try again later" in fake_sender.context[0][0]
    assert fake_sender.context[0][2] is True


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


async def test_command_model_redirects_to_atomic_tool_without_touching_picker(
    router, fake_driver, fake_sender,
):
    await router.handle_tool_call({"id": "c1", "name": "command", "args": {"command": "/model"}})
    assert fake_driver.injected == []
    assert fake_driver.keys == []
    _, content = fake_sender.results[0]
    assert "set_model" in content["speak"]
    assert content["data"]["phase"] == "idle"
    assert content["data"]["last_action"] is None


async def test_observe_claude_returns_authoritative_menu_state(
    router, fake_driver, fake_sender,
):
    fake_driver.lines = [" Select model:", " ❯ Fable ✔", "   Sonnet", "   Haiku", ""]
    await router.handle_tool_call({"id": "c1", "name": "observe_claude", "args": {}})

    content = fake_sender.results[0][1]
    assert "model picker" in content["speak"].lower()
    assert content["data"]["phase"] == "awaiting_input"
    assert content["data"]["ui"] == {
        "kind": "model_picker",
        "title": "Select model:",
        "options": ["Fable ✔", "Sonnet", "Haiku"],
        "selected": "Fable ✔",
    }


async def test_set_model_reports_success_only_after_reopening_and_verifying(
    router, fake_driver, fake_sender,
):
    fake_driver.lines = [" > ", ""]

    async def advance_model_ui():
        while fake_driver.injected != ["/model"]:
            await asyncio.sleep(0.01)
        fake_driver.lines = [" Select model:", " ❯ Fable ✔", "   Sonnet", "   Haiku", ""]
        while fake_driver.keys.count("enter") < 1:
            await asyncio.sleep(0.01)
        fake_driver.lines = [
            " This conversation is cached for the current model.",
            " ❯ 1. Yes, switch to Haiku 4.5",
            "   2. No, go back",
            "",
        ]
        while fake_driver.keys.count("enter") < 2:
            await asyncio.sleep(0.01)
        fake_driver.lines = [" > ", ""]
        while fake_driver.injected != ["/model", "/model"]:
            await asyncio.sleep(0.01)
        fake_driver.lines = [" Select model:", "   Fable", "   Sonnet", " ❯ Haiku ✔", ""]
        while "escape" not in fake_driver.keys:
            await asyncio.sleep(0.01)
        fake_driver.lines = [" > ", ""]

    side = asyncio.create_task(advance_model_ui())
    await router.handle_tool_call(
        {"id": "c1", "name": "set_model", "args": {"model": "haiku"}}
    )
    await side

    content = fake_sender.results[0][1]
    assert "verified" in content["speak"].lower()
    assert content["data"]["last_action"]["status"] == "verified"
    assert content["data"]["last_action"]["from"] == "fable"
    assert content["data"]["last_action"]["to"] == "haiku"
    assert content["data"]["phase"] == "idle"
    assert fake_driver.keys[-1] == "escape"


async def test_set_model_uses_claude_confirmation_without_reopening_picker(
    router, fake_driver, fake_sender,
):
    fake_driver.lines = [" > ", ""]

    async def advance_model_ui():
        while fake_driver.injected != ["/model"]:
            await asyncio.sleep(0.01)
        fake_driver.lines = [" Select model:", " ❯ Opus ✔", "   Sonnet", ""]
        while fake_driver.keys.count("enter") < 1:
            await asyncio.sleep(0.01)
        fake_driver.lines = [
            " This conversation is cached for the current model.",
            " ❯ 1. Yes, switch to Sonnet 5",
            "   2. No, go back",
            "",
        ]
        while fake_driver.keys.count("enter") < 2:
            await asyncio.sleep(0.01)
        fake_driver.lines = [
            "❯ /model",
            "  ⎿  Set model to Sonnet 5 and saved as your default for new sessions",
            "",
            " > ",
        ]

    side = asyncio.create_task(advance_model_ui())
    await router.handle_tool_call(
        {"id": "c1", "name": "set_model", "args": {"model": "sonnet"}}
    )
    await side

    content = fake_sender.results[0][1]
    assert content["data"]["last_action"] == {
        "action": "set_model", "status": "verified", "effect": "model_changed",
        "completed": True, "from": "opus", "to": "sonnet",
    }
    assert fake_driver.injected == ["/model"]
    assert "escape" not in fake_driver.keys


async def test_set_model_does_not_claim_unverified_change(router, fake_driver, fake_sender):
    fake_driver.lines = [" Select model:", " ❯ Fable ✔", "   Haiku", ""]

    async def fail_to_change():
        while fake_driver.keys.count("enter") < 1:
            await asyncio.sleep(0.01)
        fake_driver.lines = [" > ", ""]
        while fake_driver.injected != ["/model"]:
            await asyncio.sleep(0.01)
        fake_driver.lines = [" Select model:", " ❯ Fable ✔", "   Haiku", ""]
        while "escape" not in fake_driver.keys:
            await asyncio.sleep(0.01)
        fake_driver.lines = [" > ", ""]

    side = asyncio.create_task(fail_to_change())
    await router.handle_tool_call(
        {"id": "c1", "name": "set_model", "args": {"model": "haiku"}}
    )
    await side

    content = fake_sender.results[0][1]
    assert "couldn't verify" in content["speak"].lower()
    assert content["data"]["last_action"]["status"] == "failed"
    assert content["data"]["last_action"]["completed"] is False


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


async def test_selecting_model_confirms_second_phase(router, fake_driver, fake_sender):
    """Claude Code 2.1.226 asks for a second confirmation when a model change
    affects speed/token use. The explicit model selection authorizes that exact
    confirmation; leaving it open makes the next voice action one phase behind."""
    fake_driver.lines = [" Select model:", " ❯ Opus", "   Sonnet", "   Haiku", ""]

    async def advance_menus():
        while fake_driver.keys.count("enter") < 1:
            await asyncio.sleep(0.01)
        fake_driver.lines = [
            # The screen parser intentionally uses the line immediately above
            # the first option as its title; Claude renders explanatory copy
            # there, not the top-level "Switch model?" heading.
            " This conversation is cached for the current model.",
            " ❯ 1. Yes, switch to Haiku 4.5",
            "   2. No, go back",
            "",
        ]
        while fake_driver.keys.count("enter") < 2:
            await asyncio.sleep(0.01)
        fake_driver.lines = [" > ", ""]

    side = asyncio.create_task(advance_menus())
    await router.handle_tool_call(
        {"id": "c1", "name": "select_option", "args": {"option": "haiku"}}
    )
    await side

    assert fake_driver.keys == ["down", "down", "enter", "enter"]
    result = fake_sender.results[0][1]
    assert "confirmed" in result["speak"].lower()
    assert result["data"]["phase"] == "idle"


async def test_select_option_without_menu(router, fake_sender):
    await router.handle_tool_call({"id": "c1", "name": "select_option", "args": {"option": "yes"}})
    assert "no menu" in fake_sender.results[0][1]["speak"].lower()


async def test_press_key(router, fake_driver, fake_sender):
    await router.handle_tool_call({"id": "c1", "name": "press_key", "args": {"key": "ctrl-c"}})
    assert fake_driver.keys == ["ctrl-c"]
    await router.handle_tool_call({"id": "c2", "name": "press_key", "args": {"key": "bogus"}})
    assert "Unknown key" in fake_sender.results[1][1]["speak"]


async def test_end_session_arms_browser_close_after_goodbye(router, fake_sender):
    events = []
    router.on_status = lambda event: events.append(event) or asyncio.sleep(0)

    await router.handle_tool_call({"id": "c1", "name": "end_session", "args": {}})

    assert events[0] == {"type": "local", "event": "end_session"}
    assert "converse session" in fake_sender.results[0][1]["speak"].lower()


async def test_cancel_before_prompt_id_remains_non_idle_until_ui_settles(
    router, fake_driver, fake_sender,
):
    fake_driver.auto_ack = False
    router.SUBMIT_ACK_S = 1
    router.CANCEL_GRACE_S = 0.01
    router.CANCEL_POLL_S = 0.01
    router.CANCEL_IDLE_SAMPLES = 2
    task = asyncio.create_task(router.handle_tool_call({
        "id": "old", "name": "long_task", "args": {"request": "slow task"},
    }))
    await asyncio.sleep(0.02)
    await router.handle_tool_cancel({"id": "old"})

    assert router.state() == "canceling"
    await router.handle_tool_call({
        "id": "new", "name": "command", "args": {"command": "/clear"},
    })
    assert "still stopping" in fake_sender.results[-1][1]["speak"]

    fake_driver.lines = ["────────────────", "❯", "────────────────"]
    await task
    deadline = asyncio.get_running_loop().time() + 1
    while router.state() != "idle":
        assert asyncio.get_running_loop().time() < deadline
        await asyncio.sleep(0.01)


async def test_driver_exit_during_submission_returns_router_to_idle(
    router, fake_driver, fake_sender,
):
    def exited(_text):
        raise OSError("Claude Code session has exited")

    fake_driver.inject = exited
    await router.handle_tool_call({
        "id": "c1", "name": "long_task", "args": {"request": "do work"},
    })

    result = fake_sender.results[-1][1]
    assert "went wrong" in result["speak"]
    assert result["data"]["phase"] == "idle"
    assert result["data"]["active_task"] is None


async def test_malformed_tool_calls_are_rejected_without_entering_handlers(
    router, fake_driver, fake_sender,
):
    await router.handle_tool_call({"id": "bad-name", "name": [], "args": {}})
    await router.handle_tool_call({"id": "bad-args", "name": "long_task", "args": []})
    await router.handle_tool_call({"id": [], "name": "long_task", "args": {}})

    assert fake_driver.injected == []
    assert [call_id for call_id, _ in fake_sender.results] == ["bad-name", "bad-args"]
    assert all("malformed" in result["speak"] for _, result in fake_sender.results)
