import asyncio


MENU = [
    " Claude needs your approval",
    " ❯ 1. Yes",
    "   2. No, go back",
    "",
]


def test_manifest_is_prompt_first_with_revisioned_decisions():
    from converse_code.tools import manifest

    tools = {tool["name"]: tool for tool in manifest()}
    assert set(tools) == {
        "long_task", "steer_task", "observe_claude", "change_model",
        "resolve_decision", "end_session",
    }
    assert tools["change_model"]["parameters"]["properties"]["model"]["enum"] == [
        "default", "opus", "fable", "sonnet", "haiku",
    ]
    resolver = tools["resolve_decision"]["parameters"]
    assert resolver["required"] == ["revision", "option"]


def test_menu_state_has_stable_revision(router, fake_driver):
    fake_driver.lines = list(MENU)
    first = router.semantic_state()["ui"]
    second = router.semantic_state()["ui"]

    assert first["kind"] == "menu"
    assert first["revision"] == second["revision"]
    assert first["options"] == ["Yes", "No, go back"]
    assert first["selected"] == "Yes"


def test_identical_menu_gets_new_revision_after_closing(router, fake_driver):
    fake_driver.lines = list(MENU)
    first = router.semantic_state()["ui"]["revision"]
    fake_driver.lines = ["────", "❯", "────"]
    assert router.semantic_state()["ui"] == {"kind": "none"}
    fake_driver.lines = list(MENU)
    second = router.semantic_state()["ui"]["revision"]

    assert second != first


async def test_resolve_decision_requires_matching_revision(
    router, fake_driver, fake_sender,
):
    fake_driver.lines = list(MENU)
    await router.handle_tool_call({
        "id": "c1", "name": "resolve_decision",
        "args": {"revision": "stale", "option": "Yes"},
    })

    assert fake_driver.keys == []
    result = fake_sender.results[-1][1]
    assert result["data"]["last_action"]["effect"] == "stale_decision"
    assert fake_sender.result_metadata[-1] == {"outcome": "failed", "verified": False}


async def test_resolve_decision_requires_exact_option_label(
    router, fake_driver, fake_sender,
):
    fake_driver.lines = list(MENU)
    revision = router.semantic_state()["ui"]["revision"]
    await router.handle_tool_call({
        "id": "c1", "name": "resolve_decision",
        "args": {"revision": revision, "option": "go back"},
    })

    assert fake_driver.keys == []
    assert "exact visible option" in fake_sender.results[-1][1]["speak"]
    assert fake_sender.results[-1][1]["data"]["last_action"]["effect"] == "option_not_found"
    assert fake_sender.result_metadata[-1] == {"outcome": "failed", "verified": False}


async def test_resolve_decision_without_open_decision_is_failed(
    router, fake_driver, fake_sender,
):
    await router.handle_tool_call({
        "id": "c1", "name": "resolve_decision",
        "args": {"revision": "old", "option": "Yes"},
    })

    assert fake_driver.keys == []
    assert fake_sender.results[-1][1]["data"]["last_action"]["effect"] == "no_decision"
    assert fake_sender.result_metadata[-1] == {"outcome": "failed", "verified": False}


async def test_identical_reopened_menu_rejects_old_revision_without_prior_observation(
    router, fake_driver, fake_sender,
):
    fake_driver.lines = list(MENU)
    old_revision = router.semantic_state()["ui"]["revision"]
    # The host saw a new paint, even though callers never observed the intervening close/reopen.
    fake_driver.lines = list(MENU)

    await router.handle_tool_call({
        "id": "c1", "name": "resolve_decision",
        "args": {"revision": old_revision, "option": "Yes"},
    })

    assert fake_driver.keys == []
    assert fake_sender.results[-1][1]["data"]["last_action"]["effect"] == "stale_decision"
    assert fake_sender.result_metadata[-1] == {"outcome": "failed", "verified": False}


async def test_resolve_decision_does_not_treat_highlight_change_as_completion(
    router, fake_driver, fake_sender,
):
    fake_driver.lines = list(MENU)
    revision = router.semantic_state()["ui"]["revision"]

    await router.handle_tool_call({
        "id": "c1", "name": "resolve_decision",
        "args": {"revision": revision, "option": "No, go back"},
    })
    focused_revision = fake_sender.results[-1][1]["data"]["ui"]["revision"]
    await router.handle_tool_call({
        "id": "c2", "name": "resolve_decision",
        "args": {"revision": focused_revision, "option": "No, go back"},
    })

    assert fake_driver.keys == ["down", "enter"]
    assert fake_sender.results[-1][1]["data"]["last_action"]["effect"] == "selection_unverified"
    assert fake_sender.result_metadata[-1] == {"outcome": "failed", "verified": False}


async def test_resolve_decision_selects_exact_option_and_verifies_transition(
    router, fake_driver, fake_sender,
):
    fake_driver.lines = list(MENU)
    revision = router.semantic_state()["ui"]["revision"]

    await router.handle_tool_call({
        "id": "c1", "name": "resolve_decision",
        "args": {"revision": revision, "option": "No, go back"},
    })
    first = fake_sender.results[-1][1]
    focused_revision = first["data"]["ui"]["revision"]

    assert fake_driver.keys == ["down"]
    assert first["data"]["last_action"]["effect"] == "option_focused"
    assert first["data"]["last_action"]["completed"] is False
    assert fake_sender.result_metadata[-1] == {"outcome": "succeeded", "verified": False}

    async def close_menu():
        while "enter" not in fake_driver.keys:
            await asyncio.sleep(0.01)
        fake_driver.lines = ["────", "❯", "────"]

    side = asyncio.create_task(close_menu())
    await router.handle_tool_call({
        "id": "c2", "name": "resolve_decision",
        "args": {"revision": focused_revision, "option": "No, go back"},
    })
    await side

    assert fake_driver.keys == ["down", "enter"]
    result = fake_sender.results[-1][1]
    assert result["data"]["last_action"]["status"] == "verified"
    assert result["data"]["last_action"]["option"] == "No, go back"
    assert fake_sender.result_metadata[-1] == {"outcome": "succeeded", "verified": True}


async def test_identical_menu_replacement_during_navigation_is_never_submitted(
    router, fake_driver, fake_sender,
):
    fake_driver.lines = list(MENU)
    revision = router.semantic_state()["ui"]["revision"]

    async def replace_after_navigation():
        while "down" not in fake_driver.keys:
            await asyncio.sleep(0.01)
        fake_driver.lines = list(MENU)

    side = asyncio.create_task(replace_after_navigation())
    await router.handle_tool_call({
        "id": "c1", "name": "resolve_decision",
        "args": {"revision": revision, "option": "No, go back"},
    })
    await side

    assert fake_driver.keys == ["down"]
    assert "enter" not in fake_driver.keys
    assert fake_sender.results[-1][1]["data"]["last_action"]["effect"] == "decision_changed"
    assert fake_sender.result_metadata[-1] == {"outcome": "failed", "verified": False}


async def test_resolve_decision_returns_replacement_menu_without_auto_answering(
    router, fake_driver, fake_sender,
):
    fake_driver.lines = list(MENU)
    revision = router.semantic_state()["ui"]["revision"]

    async def replace_menu():
        while "enter" not in fake_driver.keys:
            await asyncio.sleep(0.01)
        fake_driver.lines = [
            " Apply this choice permanently?",
            " ❯ 1. Apply once",
            "   2. Always apply",
            "",
        ]

    side = asyncio.create_task(replace_menu())
    await router.handle_tool_call({
        "id": "c1", "name": "resolve_decision",
        "args": {"revision": revision, "option": "Yes"},
    })
    await side

    assert fake_driver.keys == ["enter"]
    result = fake_sender.results[-1][1]
    assert result["data"]["ui"]["kind"] == "menu"
    assert result["data"]["ui"]["options"] == ["Apply once", "Always apply"]
    assert result["data"]["ui"]["revision"] != revision


async def test_active_task_announces_revisioned_decision(
    router, fake_driver, fake_sender,
):
    task = asyncio.create_task(router.handle_tool_call({
        "id": "c1", "name": "long_task", "args": {"request": "do the work"},
    }))
    while not fake_sender.deferred:
        await asyncio.sleep(0.01)
    fake_driver.lines = list(MENU)
    revision = router.semantic_state()["ui"]["revision"]
    deadline = asyncio.get_running_loop().time() + 1
    while not fake_sender.partials:
        assert asyncio.get_running_loop().time() < deadline
        await asyncio.sleep(0.01)

    partial = fake_sender.partials[-1][1]["speak"]
    assert revision in partial
    assert "resolve_decision" in partial

    fake_driver.lines = ["────", "❯", "────"]
    await router.on_hook("stop", {
        "prompt_id": "test-prompt-1", "last_assistant_message": "Done.",
    })
    await task


async def test_permission_notification_includes_current_decision_revision(
    router, fake_driver, fake_sender,
):
    fake_driver.lines = list(MENU)
    revision = router.semantic_state()["ui"]["revision"]
    await router.on_hook("permission_request", {"tool_name": "Bash"})
    await asyncio.sleep(router.SETTLE_S * 2)

    text, role, reply = fake_sender.context[-1]
    assert revision in text
    assert "resolve_decision" in text
    assert role == "context"
    assert reply is True


async def test_model_change_is_sent_as_natural_language_task(
    router, fake_driver, fake_sender,
):
    request = "Switch your host model to Opus and verify the actual model."

    task = asyncio.create_task(router.handle_tool_call({
        "id": "c1", "name": "long_task", "args": {"request": request},
    }))
    await asyncio.sleep(0.05)
    await router.on_hook("stop", {
        "prompt_id": "test-prompt-1",
        "last_assistant_message": "Switched the host model to Opus.",
    })
    await task

    assert fake_driver.injected == [request]


async def test_change_model_uses_documented_command_and_current_header(
    router, fake_driver, fake_sender,
):
    fake_driver.lines = ["Opus 5 · Claude", "❯"]

    async def show_changed_header():
        while fake_driver.injected != ["/model sonnet"]:
            await asyncio.sleep(0.01)
        fake_driver.lines = ["Sonnet 5 · Claude", "❯"]

    side = asyncio.create_task(show_changed_header())
    await router.handle_tool_call({
        "id": "model", "name": "change_model", "args": {"model": "sonnet"},
    })
    await side

    action = fake_sender.results[-1][1]["data"]["last_action"]
    assert fake_driver.injected == ["/model sonnet"]
    assert action["status"] == "verified"
    assert action["to"] == "sonnet"
    assert action["evidence"] == {"kind": "current_model_header", "model": "sonnet"}
    assert fake_sender.result_metadata[-1] == {"outcome": "succeeded", "verified": True}


async def test_change_model_confirms_matching_cached_context_prompt(
    router, fake_driver, fake_sender,
):
    fake_driver.lines = ["Opus 5 · Claude", "❯"]

    async def advance_ui():
        while fake_driver.injected != ["/model sonnet"]:
            await asyncio.sleep(0.01)
        fake_driver.lines = [
            " Switch model?",
            " Your next response will be slower and use more tokens",
            " This conversation is cached for the current model.",
            " Switching to Sonnet 5 means the full history gets re-read on your next message.",
            " ❯ 1. Yes, switch to Sonnet 5", "   2. No, go back", "",
        ]
        while "enter" not in fake_driver.keys:
            await asyncio.sleep(0.01)
        fake_driver.lines = ["Sonnet 5 · Claude", "❯"]

    side = asyncio.create_task(advance_ui())
    await router.handle_tool_call({
        "id": "model", "name": "change_model", "args": {"model": "sonnet"},
    })
    await side

    assert fake_driver.keys == ["enter"]
    assert fake_sender.results[-1][1]["data"]["last_action"]["status"] == "verified"


async def test_change_model_does_not_auto_confirm_non_cached_prompt(
    router, fake_driver, fake_sender,
):
    fake_driver.lines = ["Opus 5 · Claude", "❯"]

    async def show_unrelated_confirmation():
        while fake_driver.injected != ["/model sonnet"]:
            await asyncio.sleep(0.01)
        fake_driver.lines = [
            " Save this as your default model?",
            " ❯ 1. Yes, save Sonnet", "   2. No, go back", "",
        ]

    side = asyncio.create_task(show_unrelated_confirmation())
    await router.handle_tool_call({
        "id": "model", "name": "change_model", "args": {"model": "sonnet"},
    })
    await side

    assert fake_driver.keys == []
    assert fake_sender.results[-1][1]["data"]["last_action"]["effect"] == "decision_required"
    assert fake_sender.result_metadata[-1] == {"outcome": "succeeded", "verified": False}


async def test_change_model_ignores_stale_cached_proof_above_current_prompt(
    router, fake_driver, fake_sender,
):
    fake_driver.lines = ["Opus 5 · Claude", "❯"]

    async def show_current_persistent_prompt_after_stale_text():
        while fake_driver.injected != ["/model sonnet"]:
            await asyncio.sleep(0.01)
        fake_driver.lines = [
            " Switch model?",
            " This conversation is cached for the current model.",
            " Switching means the full history gets re-read.",
            "────────────────",
            " Save this as your default model?",
            " ❯ 1. Yes, save Sonnet", "   2. No, go back", "",
        ]

    side = asyncio.create_task(show_current_persistent_prompt_after_stale_text())
    await router.handle_tool_call({
        "id": "model", "name": "change_model", "args": {"model": "sonnet"},
    })
    await side

    assert fake_driver.keys == []
    assert fake_sender.results[-1][1]["data"]["last_action"]["effect"] == "decision_required"


async def test_change_model_does_not_verify_selected_picker_as_header(
    router, fake_driver, fake_sender,
):
    fake_driver.lines = ["Opus 5 · Claude", "❯"]

    async def advance_to_picker():
        while fake_driver.injected != ["/model sonnet"]:
            await asyncio.sleep(0.01)
        fake_driver.lines = [
            " Switch model?",
            " Your next response will be slower and use more tokens",
            " This conversation is cached for the current model.",
            " Switching to Sonnet 5 means the full history gets re-read on your next message.",
            " ❯ 1. Yes, switch to Sonnet 5", "   2. No, go back", "",
        ]
        while "enter" not in fake_driver.keys:
            await asyncio.sleep(0.01)
        fake_driver.lines = [
            " Select model:", " ❯ Sonnet ✔", "   Opus", "",
        ]

    side = asyncio.create_task(advance_to_picker())
    await router.handle_tool_call({
        "id": "model", "name": "change_model", "args": {"model": "sonnet"},
    })
    await side

    action = fake_sender.results[-1][1]["data"]["last_action"]
    assert action["status"] != "verified"
    assert fake_sender.result_metadata[-1]["verified"] is False


async def test_change_model_rejects_legacy_id_without_typing(
    router, fake_driver, fake_sender,
):
    await router.handle_tool_call({
        "id": "model", "name": "change_model",
        "args": {"model": "claude-3-5-sonnet-20241022"},
    })

    assert fake_driver.injected == []
    assert fake_sender.results[-1][1]["data"]["last_action"]["effect"] == "model_not_supported"
    assert fake_sender.result_metadata[-1] == {"outcome": "failed", "verified": False}
