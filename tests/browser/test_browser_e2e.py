"""The public background-tool sequence through the shipped page and real Chromium."""

import asyncio

import pytest

pytestmark = pytest.mark.browser_e2e


async def wait_for_frame(frames, predicate):
    async with asyncio.timeout(5):
        while not any(predicate(frame) for frame in frames):
            await asyncio.sleep(0.01)


async def open_session(page):
    await page.locator("#voice").click()
    await page.locator("#voice").get_by_text("Stop voice", exact=True).wait_for()


async def test_page_is_a_voice_only_remote_for_the_visible_pi_terminal(browser_page):
    page, errors = browser_page
    assert await page.title() == "Converse Code"
    assert await page.locator("#text").count() == 0
    assert await page.locator("#send").count() == 0
    assert await page.locator("#log").count() == 1
    await open_session(page)
    assert await page.evaluate("__fakeConverse.clients.length") == 1
    instructions = await page.evaluate("__fakeConverse.clients[0].options.mode.instructions")
    assert "For every question or request that depends on that host state" in instructions
    assert "visible Pi terminal" in instructions
    assert "first action must be coding_task" in instructions
    await page.locator("#voice").click()
    await page.locator("#voice").get_by_text("Start voice", exact=True).wait_for()
    assert errors == []


async def test_page_streams_user_speech_assistant_text_and_tool_activity(browser_page):
    page, _ = browser_page
    await open_session(page)

    await page.evaluate("""__fakeConverse.clients[0].emit({
      type:'asr', text:'List the files', final:true, turn_id:'user-1'
    })""")
    await page.evaluate("""__fakeConverse.clients[0].emit({type:'turn',turn_id:'reply-1'});
      __fakeConverse.clients[0].emit({type:'text_delta',delta:'I will inspect ',turn_id:'reply-1'});
      __fakeConverse.clients[0].emit({type:'text_delta',delta:'the repository.',turn_id:'reply-1'});
      __fakeConverse.clients[0].emit({type:'done',turn_id:'reply-1'});""")
    await page.evaluate("""__fakeConverse.clients[0].emit({
      type:'tool_call', id:'task-1', name:'coding_task', args:{request:'List the files'}
    })""")

    assert await page.locator(".entry.user").get_by_text("List the files", exact=True).count() == 1
    assert await page.locator(".entry.assistant").get_by_text(
        "I will inspect the repository.", exact=True,
    ).count() == 1
    assert await page.locator(".entry.activity").get_by_text(
        "Requested coding_task", exact=True,
    ).count() == 1


async def test_approval_prompt_waits_for_active_reply_then_forces_a_voiced_turn(
    browser_page, browser_server,
):
    page, _ = browser_page
    server, _, frames = browser_server
    await open_session(page)
    await page.evaluate("__fakeConverse.clients[0].emit({type:'turn',turn_id:'busy-reply'})")

    await server.send_json_to_tab({
        "type": "local", "event": "bridge_control", "seq": 20,
        "action": "voice_prompt", "prompt_id": "approval-7",
        "text": "Ask now whether to allow once, allow for this session, or block.",
    })
    await page.wait_for_timeout(50)
    assert await page.evaluate("__fakeConverse.clients[0].injections.length") == 0
    assert not any(frame.get("seq") == 20 for frame in frames)

    await page.evaluate("__fakeConverse.clients[0].emit({type:'done',turn_id:'busy-reply'})")
    await wait_for_frame(frames, lambda frame: frame.get("seq") == 20)
    injection = await page.evaluate("__fakeConverse.clients[0].injections[0]")
    assert injection["text"].startswith("Ask now")
    assert injection["options"] == {
        "role": "context", "reply": True, "messageId": "converse-approval-approval-7",
    }
    assert await page.locator(".entry.assistant").get_by_text(
        "Would you like to allow that action once, for this session, or block it?", exact=True,
    ).count() == 1


async def test_approval_prompt_retries_after_broker_reports_a_late_reply_race(
    browser_page, browser_server,
):
    page, _ = browser_page
    server, _, frames = browser_server
    await open_session(page)
    await page.evaluate("__fakeConverse.retryInjectionOnce=true")

    await server.send_json_to_tab({
        "type": "local", "event": "bridge_control", "seq": 21,
        "action": "voice_prompt", "prompt_id": "approval-race",
        "text": "Ask the user for explicit approval now.",
    })
    await wait_for_frame(frames, lambda frame: frame.get("seq") == 21)

    injections = await page.evaluate("__fakeConverse.clients[0].injections")
    assert len(injections) == 2
    assert injections[0]["options"]["messageId"] == injections[1]["options"]["messageId"]
    assert await page.locator(".entry.assistant").get_by_text(
        "Would you like to allow that action once, for this session, or block it?", exact=True,
    ).count() == 1


async def test_deferred_silent_partial_reply_partial_and_completion(browser_page, browser_server):
    page, _ = browser_page
    server, _, frames = browser_server
    await open_session(page)

    await server.send_json_to_tab({
        "type": "local", "event": "bridge_control", "seq": 1,
        "action": "tool_deferred", "id": "task-1", "handle": "code-1",
        "status_label": "Coding task",
    })
    await wait_for_frame(frames, lambda frame: frame.get("seq") == 1)
    assert await page.locator("#status").inner_text() == "working"
    assert await page.locator(".entry.activity").get_by_text(
        "Coding task accepted in the background", exact=True,
    ).count() == 1

    await server.send_json_to_tab({
        "type": "local", "event": "bridge_control", "seq": 10,
        "action": "tool_result", "id": "approval-call-1",
        "content": {"speak": "Allowed that action once."},
        "outcome": "succeeded", "verified": True,
    })
    await wait_for_frame(frames, lambda frame: frame.get("seq") == 10)
    assert await page.locator("#status").inner_text() == "working"

    await server.send_json_to_tab({
        "type": "local", "event": "bridge_control", "seq": 2,
        "action": "tool_partial_result", "id": "task-1",
        "content": {"speak": "Updated app.py"}, "reply": False,
    })
    await wait_for_frame(frames, lambda frame: frame.get("seq") == 2)
    assert await page.locator(".entry.activity").get_by_text("Updated app.py", exact=True).count() == 1

    await server.send_json_to_tab({
        "type": "local", "event": "bridge_control", "seq": 3,
        "action": "tool_partial_result", "id": "task-1",
        "content": {"speak": "The tests now pass"}, "reply": True,
    })
    await wait_for_frame(frames, lambda frame: frame.get("seq") == 3)

    await server.send_json_to_tab({
        "type": "local", "event": "bridge_control", "seq": 4,
        "action": "tool_result", "id": "task-1", "content": {"speak": "Finished"},
        "outcome": "succeeded", "verified": False,
    })
    await wait_for_frame(frames, lambda frame: frame.get("seq") == 4)
    assert await page.locator("#status").inner_text() == "idle"
    assert await page.locator(".entry.activity").get_by_text("Finished", exact=True).count() == 1

    calls = await page.evaluate("__fakeConverse.clients[0].bridgeCalls")
    assert [call["action"] for call in calls] == [
        "tool_deferred", "tool_result", "tool_partial_result", "tool_partial_result", "tool_result",
    ]
    assert calls[2]["options"]["reply"] is False
    assert calls[3]["options"]["reply"] is True


async def test_sdk_cancellation_reaches_the_local_host(browser_page, browser_server):
    page, _ = browser_page
    _, _, frames = browser_server
    await open_session(page)
    await page.evaluate("__fakeConverse.clients[0].emit({type:'tool_cancel',id:'task-1'})")
    await wait_for_frame(
        frames,
        lambda frame: frame.get("event") == "tool_cancel"
        and frame.get("call", {}).get("id") == "task-1",
    )


async def test_unacknowledged_control_replays_after_sdk_reconnect(browser_page, browser_server):
    page, _ = browser_page
    server, _, frames = browser_server
    await open_session(page)
    await page.evaluate("__fakeConverse.transportLive=false")
    await server.send_json_to_tab({
        "type": "local", "event": "bridge_control", "seq": 9,
        "action": "tool_progress", "id": "task-1", "note": "Still working",
    })
    await page.wait_for_timeout(50)
    assert not any(frame.get("seq") == 9 for frame in frames)
    await page.evaluate("__fakeConverse.transportLive=true;__fakeConverse.clients[0].emit({type:'reconnected'})")
    await wait_for_frame(frames, lambda frame: frame.get("seq") == 9)
    calls = await page.evaluate("__fakeConverse.clients[0].bridgeCalls")
    assert calls[-1] == {"action": "tool_progress", "id": "task-1", "note": "Still working"}
