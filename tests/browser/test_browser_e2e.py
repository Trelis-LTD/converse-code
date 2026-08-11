"""The public background-tool sequence through the shipped page and real Chromium."""

import asyncio

import pytest


pytestmark = pytest.mark.browser_e2e


async def wait_for_frame(frames, predicate):
    async with asyncio.timeout(5):
        while not any(predicate(frame) for frame in frames):
            await asyncio.sleep(0.01)


async def open_session(page):
    await page.locator("#text").fill("Start a coding task")
    await page.locator("#send").click()
    await page.locator(".entry.user").wait_for()


async def test_page_drives_text_and_voice_through_one_browser_sdk_client(browser_page):
    page, errors = browser_page
    assert await page.title() == "Converse Code"
    await open_session(page)
    assert await page.evaluate("__fakeConverse.clients.length") == 1
    assert await page.evaluate("__fakeConverse.clients[0].injected[0].text") == "Start a coding task"

    await page.locator("#voice").click()
    await page.locator("#voice").get_by_text("Stop voice", exact=True).wait_for()
    await page.locator("#voice").click()
    await page.locator("#voice").get_by_text("Start voice", exact=True).wait_for()
    assert errors == []


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
    assistant_count = await page.locator(".entry.assistant").count()

    await server.send_json_to_tab({
        "type": "local", "event": "bridge_control", "seq": 2,
        "action": "tool_partial_result", "id": "task-1",
        "content": {"speak": "Updated app.py"}, "reply": False,
    })
    await wait_for_frame(frames, lambda frame: frame.get("seq") == 2)
    assert await page.locator(".entry.assistant").count() == assistant_count

    await server.send_json_to_tab({
        "type": "local", "event": "bridge_control", "seq": 3,
        "action": "tool_partial_result", "id": "task-1",
        "content": {"speak": "The tests now pass"}, "reply": True,
    })
    await page.locator(".entry.assistant").get_by_text("The tests now pass", exact=True).wait_for()

    await server.send_json_to_tab({
        "type": "local", "event": "bridge_control", "seq": 4,
        "action": "tool_result", "id": "task-1", "content": {"speak": "Finished"},
        "outcome": "succeeded", "verified": False,
    })
    await wait_for_frame(frames, lambda frame: frame.get("seq") == 4)
    assert await page.locator("#status").inner_text() == "idle"

    calls = await page.evaluate("__fakeConverse.clients[0].bridgeCalls")
    assert [call["action"] for call in calls] == [
        "tool_deferred", "tool_partial_result", "tool_partial_result", "tool_result",
    ]
    assert calls[1]["options"]["reply"] is False
    assert calls[2]["options"]["reply"] is True
    assert await page.locator(".entry.assistant").count() == 2  # typed reply + spoken partial


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
