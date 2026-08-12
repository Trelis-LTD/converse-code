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
    assert await page.locator("h1").inner_text() == "Converse Code"
    assert await page.locator("#text").count() == 0
    assert await page.locator("#send").count() == 0
    assert await page.locator("#log").count() == 1
    await open_session(page)
    assert await page.evaluate("__fakeConverse.clients.length") == 1
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

    styles = await page.evaluate("""() => {
      const user=getComputedStyle(document.querySelector('.entry.user'));
      const assistant=getComputedStyle(document.querySelector('.entry.assistant'));
      return {
        userBackground:user.backgroundColor,
        assistantBackground:assistant.backgroundColor,
        userBorder:user.borderColor,
        assistantBorder:assistant.borderColor,
      };
    }""")
    assert styles["userBackground"] != styles["assistantBackground"]
    assert styles["userBorder"] != styles["assistantBorder"]
    assert await page.locator(".entry.user").get_attribute("data-label") == "You"
    assert await page.locator(".entry.assistant").get_attribute("data-label") == "Converse Code"
    assert await page.locator(".entry.user").get_attribute("aria-label") == "You"
    assert await page.locator(".entry.assistant").get_attribute("aria-label") == "Converse Code"


async def test_transcript_follows_new_rows_and_streaming_text_to_the_bottom(browser_page):
    page, _ = browser_page
    await open_session(page)

    await page.evaluate("""for(let index=0;index<40;index++){
      __fakeConverse.clients[0].emit({
        type:'asr',text:`User message ${index}`,final:true,turn_id:`user-${index}`
      });
    }""")
    await page.wait_for_timeout(50)
    after_rows = await page.locator("#log").evaluate(
        "el => ({top:el.scrollTop,max:el.scrollHeight-el.clientHeight})",
    )
    assert after_rows["max"] > 0
    assert after_rows["max"] - after_rows["top"] <= 1

    await page.evaluate("""__fakeConverse.clients[0].emit({type:'turn',turn_id:'long-reply'});
      for(let index=0;index<80;index++){
        __fakeConverse.clients[0].emit({
          type:'text_delta',delta:`Streaming line ${index}.\\n`,turn_id:'long-reply'
        });
      }""")
    await page.wait_for_timeout(50)
    after_stream = await page.locator("#log").evaluate(
        "el => ({top:el.scrollTop,max:el.scrollHeight-el.clientHeight})",
    )
    assert after_stream["max"] > after_rows["max"]
    assert after_stream["max"] - after_stream["top"] <= 1


async def test_asr_without_a_final_flag_is_recorded_without_local_end_routing(
    browser_page, browser_server,
):
    page, _ = browser_page
    _, _, frames = browser_server
    await open_session(page)

    await page.evaluate("""__fakeConverse.clients[0].emit({
      type:'asr',text:'End the session',turn_id:'user-end'
    })""")
    await wait_for_frame(
        frames,
        lambda frame: frame.get("event") == "debug_trace"
        and frame.get("name") == "user_transcript",
    )

    transcript = next(frame for frame in frames if frame.get("name") == "user_transcript")
    assert transcript["data"] == {"turn_id": "user-end", "text": "End the session"}

    assert not any(frame.get("event") == "session_end" for frame in frames)


async def test_sdk_session_end_is_forwarded_to_the_host(browser_page, browser_server):
    page, _ = browser_page
    _, _, frames = browser_server
    await open_session(page)

    await page.evaluate("""__fakeConverse.clients[0].emit({
      type:'session_end',code:1000,reason:'idle'
    })""")
    await wait_for_frame(frames, lambda frame: frame.get("event") == "session_end")

    ended = next(frame for frame in frames if frame.get("event") == "session_end")
    assert ended == {
        "type": "local", "event": "session_end", "code": 1000, "reason": "idle",
    }
    assert await page.locator("#status").inner_text() == "ended"
    assert await page.locator("#voice").inner_text() == "Session ended"
    assert await page.locator("#voice").is_disabled()
    assert await page.locator("#voice").get_attribute("aria-pressed") == "false"
    assert await page.evaluate("__fakeConverse.clients[0].stream") is None


async def test_late_interrupted_utterance_updates_one_assistant_row(browser_page):
    page, _ = browser_page
    await open_session(page)

    await page.evaluate("""__fakeConverse.clients[0].emit({type:'turn',turn_id:'reply-1'});
      __fakeConverse.clients[0].emit({type:'text_delta',delta:'Hello there.',turn_id:'reply-1'});
      __fakeConverse.clients[0].emit({type:'interrupted',turn_id:'reply-1'});
      __fakeConverse.clients[0].emit({
        type:'utterance',text:'Hello there. [interrupted]',turn_id:'reply-1'
      });""")

    assert await page.locator(".entry.assistant").count() == 1
    assert await page.locator(".entry.assistant").inner_text() == "Hello there. [interrupted]"


async def test_late_distinct_utterance_cannot_overwrite_an_earlier_matching_turn(browser_page):
    page, _ = browser_page
    await open_session(page)

    await page.evaluate("""__fakeConverse.clients[0].emit({type:'turn',turn_id:'reply-1'});
      __fakeConverse.clients[0].emit({type:'text_delta',delta:'Hello',turn_id:'reply-1'});
      __fakeConverse.clients[0].emit({type:'interrupted',turn_id:'reply-1'});
      __fakeConverse.clients[0].emit({type:'turn',turn_id:'reply-2'});
      __fakeConverse.clients[0].emit({type:'text_delta',delta:'Hello',turn_id:'reply-2'});
      __fakeConverse.clients[0].emit({type:'interrupted',turn_id:'reply-2'});
      __fakeConverse.clients[0].emit({
        type:'utterance',text:'Hello again [interrupted]',turn_id:'reply-2'
      });""")

    assert await page.locator(".entry.assistant").count() == 2
    assert await page.locator(".entry.assistant").all_text_contents() == [
        "Hello", "Hello again [interrupted]",
    ]


async def test_replying_partial_waits_until_the_current_voice_turn_finishes(
    browser_page, browser_server,
):
    page, _ = browser_page
    server, _, frames = browser_server
    await open_session(page)
    await page.evaluate("__fakeConverse.clients[0].emit({type:'turn',turn_id:'busy-reply'})")

    await server.send_json_to_tab({
        "type": "local", "event": "bridge_control", "seq": 20,
        "action": "tool_partial_result", "id": "task-1", "reply": True,
        "content": {"speak": "Would you like to allow once, for this session, or block?"},
    })
    await page.wait_for_timeout(50)
    assert await page.evaluate("__fakeConverse.clients[0].bridgeCalls.length") == 0
    assert not any(frame.get("seq") == 20 for frame in frames)

    await page.evaluate("__fakeConverse.clients[0].emit({type:'done',turn_id:'busy-reply'})")
    await wait_for_frame(frames, lambda frame: frame.get("seq") == 20)
    calls = await page.evaluate("__fakeConverse.clients[0].bridgeCalls")
    assert calls == [{
        "action": "tool_partial_result", "id": "task-1",
        "content": {"speak": "Would you like to allow once, for this session, or block?"},
        "options": {"reply": True},
    }]
    assert await page.locator(".entry.assistant").get_by_text(
        "Would you like to allow once, for this session, or block?", exact=True,
    ).count() == 1


async def test_trace_captures_assistant_text_at_interruption(browser_page, browser_server):
    page, _ = browser_page
    _, _, frames = browser_server
    await open_session(page)

    await page.evaluate("""__fakeConverse.clients[0].emit({type:'turn',turn_id:'reply-1'});
      __fakeConverse.clients[0].emit({
        type:'text_delta',delta:'Would you like to allow that?',turn_id:'reply-1'
      });
      __fakeConverse.clients[0].emit({type:'interrupted',turn_id:'reply-1'});""")
    await wait_for_frame(
        frames,
        lambda frame: frame.get("event") == "debug_trace"
        and frame.get("name") == "voice_turn"
        and frame.get("data", {}).get("type") == "interrupted",
    )

    terminal = next(
        frame for frame in frames
        if frame.get("event") == "debug_trace"
        and frame.get("name") == "voice_turn"
        and frame.get("data", {}).get("type") == "interrupted"
    )
    assert terminal["data"] == {
        "type": "interrupted", "turn_id": "reply-1",
        "text": "Would you like to allow that?",
    }


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
    task_calls = [call for call in calls if call.get("id") == "task-1"]
    silent = next(call for call in task_calls if call.get("content", {}).get("speak") == (
        "Updated app.py"
    ))
    spoken = next(call for call in task_calls if call.get("content", {}).get("speak") == (
        "The tests now pass"
    ))
    assert silent["options"]["reply"] is False
    assert spoken["options"]["reply"] is True
    assert task_calls[-1]["action"] == "tool_result"


async def test_activity_rows_distinguish_approval_and_unverified_pi_evidence(
    browser_page, browser_server,
):
    page, _ = browser_page
    server, _, frames = browser_server
    await open_session(page)

    await server.send_json_to_tab({
        "type": "local", "event": "bridge_control", "seq": 30,
        "action": "tool_partial_result", "id": "task-1", "reply": True,
        "content": {
            "speak": "Pi wants to run bash: pwd. Ask the user to allow once, allow for this "
                     "session, or block it.",
            "data": {
                "event": "approval_required", "tool": "bash", "summary": "pwd",
            },
        },
    })
    await wait_for_frame(frames, lambda frame: frame.get("seq") == 30)
    assert await page.locator(".entry.activity").get_by_text(
        "bash: pwd", exact=True,
    ).count() == 1
    assert await page.locator(".entry.activity").last.get_attribute("data-label") == (
        "Approval required"
    )

    await server.send_json_to_tab({
        "type": "local", "event": "bridge_control", "seq": 31,
        "action": "tool_result", "id": "task-1",
        "content": {"speak": "Opened the airplane game in your default browser."},
        "outcome": "succeeded", "verified": False,
    })
    await wait_for_frame(frames, lambda frame: frame.get("seq") == 31)
    assert await page.locator(".entry.activity").last.inner_text() == (
        "Opened the airplane game in your default browser."
    )
    assert await page.locator(".entry.activity").last.get_attribute("data-label") == "Pi reported"


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
