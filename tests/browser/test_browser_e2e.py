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
    await page.locator("#voice").get_by_text("Mute microphone", exact=True).wait_for()


async def test_page_is_a_voice_only_remote_for_the_visible_pi_terminal(browser_page):
    page, errors = browser_page
    assert await page.locator("#text").count() == 0
    assert await page.locator("#send").count() == 0
    assert await page.locator("#log").count() == 1
    await open_session(page)
    assert await page.evaluate("__fakeConverse.clients.length") == 1
    assert errors == []


async def test_end_session_closes_voice_and_requests_host_shutdown(
    browser_page, browser_server,
):
    page, _ = browser_page
    _, _, frames = browser_server
    await open_session(page)

    await page.locator("#end-session").click()

    await wait_for_frame(frames, lambda frame: frame.get("event") == "end_session")
    assert await page.locator("#status").inner_text() == "ended"
    assert await page.locator("#voice").is_disabled()
    assert await page.locator("#end-session").is_disabled()
    assert await page.evaluate("__fakeConverse.clients[0].closed") is True


async def test_browser_renders_and_traces_sdk_mic_lifecycle(
    browser_page, browser_server,
):
    page, _ = browser_page
    _, _, frames = browser_server

    await open_session(page)
    await wait_for_frame(
        frames,
        lambda frame: frame.get("event") == "debug_trace"
        and frame.get("name") == "mic_state"
        and frame.get("data", {}).get("state") == "warming_up",
    )
    await wait_for_frame(
        frames,
        lambda frame: frame.get("event") == "debug_trace"
        and frame.get("name") == "mic_state"
        and frame.get("data", {}).get("state") == "listening",
    )
    assert await page.locator("#status").inner_text() == "listening"
    await page.locator("#voice").click()
    await wait_for_frame(
        frames,
        lambda frame: frame.get("event") == "debug_trace"
        and frame.get("name") == "mic_state"
        and frame.get("data") == {"state": "muted"},
    )
    await page.locator("#voice").click()
    assert await page.evaluate("__fakeConverse.clients[0].micEnabled") is True


async def test_browser_renders_sdk_mic_recovery_without_owning_retry(
    browser_page, browser_server,
):
    page, _ = browser_page
    _, _, frames = browser_server
    await open_session(page)

    await page.evaluate("""__fakeConverse.clients[0].emit({
      type:'recovering',code:'capture_stalled',attempt:1,next_attempt:2
    })""")
    await wait_for_frame(
        frames,
        lambda frame: frame.get("event") == "debug_trace"
        and frame.get("name") == "mic_state"
        and frame.get("data", {}).get("state") == "recovering",
    )
    assert await page.locator("#status").inner_text() == "recovering"


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
      type:'tool_call', id:'task-1', name:'pi_request', args:{user_request:'List the files'}
    })""")

    assert await page.locator(".entry.user").get_by_text("List the files", exact=True).count() == 1
    assert await page.locator(".entry.assistant").get_by_text(
        "I will inspect the repository.", exact=True,
    ).count() == 1
    assert await page.locator(".entry.activity").get_by_text(
        "Requested pi_request", exact=True,
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


async def test_interaction_partial_is_sent_while_a_voice_turn_is_active(
    browser_page, browser_server,
):
    page, _ = browser_page
    server, _, frames = browser_server
    await open_session(page)
    await page.evaluate("__fakeConverse.clients[0].emit({type:'turn',turn_id:'busy-reply'})")

    await server.send_json_to_tab({
        "type": "local", "event": "bridge_control", "seq": 20,
        "action": "tool_partial_result", "id": "task-1",
        "interaction": {
            "prompt": "Allow Pi to run bash: pwd?",
            "options": ["Allow once", "Allow for this session", "Block"],
        },
        "content": {
            "event": "pi_approval_required", "approval_id": "approval-1",
            "tool": "bash", "summary": "pwd",
            "decisions": ["allow_once", "allow_session", "block"],
        },
    })
    await wait_for_frame(frames, lambda frame: frame.get("seq") == 20)
    calls = await page.evaluate("__fakeConverse.clients[0].bridgeCalls")
    assert calls == [{
        "action": "tool_partial_result", "id": "task-1",
        "content": {
            "event": "pi_approval_required", "approval_id": "approval-1",
            "tool": "bash", "summary": "pwd",
            "decisions": ["allow_once", "allow_session", "block"],
        },
        "options": {"interaction": {
            "prompt": "Allow Pi to run bash: pwd?",
            "options": ["Allow once", "Allow for this session", "Block"],
        }},
    }]


async def test_interaction_narration_lifecycle_is_visible_and_traced(
    browser_page, browser_server,
):
    page, _ = browser_page
    _, _, frames = browser_server
    await open_session(page)

    await page.evaluate("""__fakeConverse.clients[0].emit({
      type:'tool_job_narration',state:'queued',job_ids:['task-1'],kind:'interaction'
    });
    __fakeConverse.clients[0].emit({
      type:'tool_job_narration',state:'started',job_ids:['task-1'],kind:'interaction'
    });""")

    await wait_for_frame(
        frames,
        lambda frame: frame.get("event") == "debug_trace"
        and frame.get("name") == "tool_job_narration"
        and frame.get("data", {}).get("state") == "started",
    )
    assert await page.locator(".entry.activity").all_text_contents() == [
        "Approval prompt queued", "Approval prompt started",
    ]


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


async def test_background_status_tracks_the_deferred_task_not_unrelated_results(
    browser_page, browser_server,
):
    page, _ = browser_page
    server, _, frames = browser_server
    await open_session(page)

    await server.send_json_to_tab({
        "type": "local", "event": "bridge_control", "seq": 1,
        "action": "tool_deferred", "id": "task-1", "handle": "code-1",
        "status_label": "Pi",
    })
    await wait_for_frame(frames, lambda frame: frame.get("seq") == 1)
    assert await page.locator("#status").inner_text() == "working"
    assert await page.locator(".entry.activity").get_by_text(
        "Pi accepted in the background", exact=True,
    ).count() == 1

    await server.send_json_to_tab({
        "type": "local", "event": "bridge_control", "seq": 10,
        "action": "tool_result", "id": "approval-call-1",
        "content": {
            "event": "pi_approval_delivered", "decision": "allow_once",
            "task_status": "running",
        },
        "outcome": "succeeded", "verified": True,
    })
    await wait_for_frame(frames, lambda frame: frame.get("seq") == 10)
    assert await page.locator("#status").inner_text() == "working"

    await server.send_json_to_tab({
        "type": "local", "event": "bridge_control", "seq": 2,
        "action": "tool_partial_result", "id": "task-1",
        "content": {
            "event": "pi_tool_started", "tool": "edit",
            "arguments": {"path": "app.py"},
        },
    })
    await wait_for_frame(frames, lambda frame: frame.get("seq") == 2)
    assert await page.locator(".entry.activity").get_by_text("edit", exact=True).count() == 1

    await server.send_json_to_tab({
        "type": "local", "event": "bridge_control", "seq": 4,
        "action": "tool_result", "id": "task-1",
        "content": {"event": "pi_settled", "message": "Finished"},
        "outcome": "succeeded", "verified": False,
    })
    await wait_for_frame(frames, lambda frame: frame.get("seq") == 4)
    assert await page.locator("#status").inner_text() == "idle"
    assert await page.locator(".entry.activity").get_by_text("Finished", exact=True).count() == 1


async def test_activity_rows_distinguish_approval_and_unverified_pi_evidence(
    browser_page, browser_server,
):
    page, _ = browser_page
    server, _, frames = browser_server
    await open_session(page)

    await server.send_json_to_tab({
        "type": "local", "event": "bridge_control", "seq": 30,
        "action": "tool_partial_result", "id": "task-1",
        "interaction": {
            "prompt": "Allow Pi to run bash: pwd?",
            "options": ["Allow once", "Allow for this session", "Block"],
        },
        "content": {
            "event": "pi_approval_required", "approval_id": "approval-1",
            "tool": "bash", "summary": "pwd",
            "decisions": ["allow_once", "allow_session", "block"],
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
        "content": {
            "event": "pi_settled",
            "message": "Opened the airplane game in your default browser.",
        },
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
        "action": "tool_partial_result", "id": "task-1",
        "content": {"event": "pi_tool_started", "tool": "read", "arguments": {}},
    })
    await page.wait_for_timeout(50)
    assert not any(frame.get("seq") == 9 for frame in frames)
    await page.evaluate("__fakeConverse.transportLive=true;__fakeConverse.clients[0].emit({type:'reconnected'})")
    await wait_for_frame(frames, lambda frame: frame.get("seq") == 9)
    calls = await page.evaluate("__fakeConverse.clients[0].bridgeCalls")
    assert calls[-1] == {
        "action": "tool_partial_result", "id": "task-1",
        "content": {"event": "pi_tool_started", "tool": "read", "arguments": {}},
        "options": {},
    }
