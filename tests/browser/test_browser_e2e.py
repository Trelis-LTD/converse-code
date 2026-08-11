"""Behavioral tests through the real shipped page in a real Chromium process."""

import asyncio

import pytest


pytestmark = pytest.mark.browser_e2e


async def wait_for_tab_frame(frames, predicate, timeout=5):
    async with asyncio.timeout(timeout):
        while True:
            for frame in frames:
                if predicate(frame):
                    return frame
            await asyncio.sleep(0.01)


async def test_page_boots_and_terminal_link_opens(browser_page):
    page, errors = browser_page
    assert await page.title() == "Converse Code"
    assert await page.locator("#statusChip").text_content() == "idle"
    assert await page.locator("#micLabel").inner_text() == "Start voice"
    assert errors == []


async def test_typed_turn_uses_canonical_echo_and_streams_reply(browser_page, browser_server):
    page, _ = browser_page
    _, credentials, tab_frames = browser_server

    await page.locator("#textInput").fill("Refactor the parser")
    await page.locator("#textSendBtn").click()

    await page.locator(".entry.you .body").get_by_text("Refactor the parser", exact=True).wait_for()
    await page.locator(".entry.assistant .body").get_by_text(
        "Acknowledged: Refactor the parser", exact=True
    ).wait_for()
    assert await page.locator("#textInput").input_value() == ""
    assert await page.locator("#textForm").get_attribute("aria-busy") == "false"
    assert credentials == ["browser-e2e-session"]
    injected = await page.evaluate("__fakeConverse.clients[0].injected")
    assert len(injected) == 1
    assert injected[0]["text"] == "Refactor the parser"
    assert isinstance(injected[0]["options"]["messageId"], str)
    assert await page.evaluate("__fakeConverse.connectOptions") == [{"noGreeting": True}]
    assert await page.evaluate("__fakeConverse.micStarts") == 0
    assert any(frame.get("event") == "bridge_ready" for frame in tab_frames)


async def test_session_limit_retries_without_duplicate_client(browser_page, browser_server):
    page, _ = browser_page
    _, credentials, _ = browser_server
    await page.evaluate("""() => {
      __converseCodeAdmissionRetryDelays.splice(0,
        __converseCodeAdmissionRetryDelays.length, 1, 1);
      globalThis.__fakeConverse = {connectErrors: [
        {code:'too_many_sessions', detail:'too many concurrent sessions', retryable:true},
        {code:'too_many_sessions', detail:'too many concurrent sessions', retryable:true}
      ]};
    }""")
    await page.locator("#textInput").fill("Wait for a slot")
    await page.locator("#textSendBtn").click()
    await page.locator(".entry.you .body").get_by_text("Wait for a slot", exact=True).wait_for()
    assert await page.evaluate("__fakeConverse.clients.length") == 1
    assert await page.evaluate("__fakeConverse.connectOptions") == [
        {"noGreeting": True}, {"noGreeting": True}, {"noGreeting": True}
    ]
    assert credentials == ["browser-e2e-session"]
    assert await page.locator("#textForm").get_attribute("aria-busy") == "false"


async def test_typed_then_voice_share_capacity_retry(browser_page):
    page, _ = browser_page
    await page.evaluate("""() => {
      __converseCodeAdmissionRetryDelays.splice(0,
        __converseCodeAdmissionRetryDelays.length, 500);
      globalThis.__fakeConverse = {connectErrors: [
        {code:'too_many_sessions', detail:'too many concurrent sessions', retryable:true}
      ]};
    }""")
    await page.locator("#textInput").fill("Shared typed start")
    await page.locator("#textSendBtn").click()
    await page.locator(".entry.banner .body").get_by_text(
        "This account is at its concurrent-session limit — retrying automatically…", exact=True
    ).wait_for()
    await page.locator("#micBtn").click()
    await page.locator("#micLabel").get_by_text("Listening", exact=True).wait_for()
    await page.locator(".entry.you .body").get_by_text("Shared typed start", exact=True).wait_for()
    assert await page.evaluate("__fakeConverse.clients.length") == 1
    assert await page.evaluate("__fakeConverse.connectOptions.length") == 2
    assert await page.evaluate("__fakeConverse.micStarts") == 1
    assert await page.evaluate("__fakeConverse.clients[0].injected.length") == 1


async def test_voice_then_typed_share_capacity_retry(browser_page):
    page, _ = browser_page
    await page.evaluate("""() => {
      __converseCodeAdmissionRetryDelays.splice(0,
        __converseCodeAdmissionRetryDelays.length, 500);
      globalThis.__fakeConverse = {connectErrors: [
        {code:'too_many_sessions', detail:'too many concurrent sessions', retryable:true}
      ]};
    }""")
    await page.locator("#micBtn").click()
    await page.locator("#micLabel").get_by_text("Waiting for capacity…", exact=True).wait_for()
    await page.locator("#textInput").fill("Shared voice start")
    await page.locator("#textSendBtn").click()
    await page.locator("#micLabel").get_by_text("Listening", exact=True).wait_for()
    await page.locator(".entry.you .body").get_by_text("Shared voice start", exact=True).wait_for()
    assert await page.evaluate("__fakeConverse.clients.length") == 1
    assert await page.evaluate("__fakeConverse.connectOptions.length") == 2
    assert await page.evaluate("__fakeConverse.micStarts") == 1
    assert await page.evaluate("__fakeConverse.clients[0].injected.length") == 1


async def test_cancel_voice_during_capacity_backoff_opens_no_session(browser_page):
    page, _ = browser_page
    await page.evaluate("""() => {
      __converseCodeAdmissionRetryDelays.splice(0,
        __converseCodeAdmissionRetryDelays.length, 500);
      globalThis.__fakeConverse = {connectErrors: [
        {code:'too_many_sessions', detail:'too many concurrent sessions', retryable:true}
      ]};
    }""")
    await page.locator("#micBtn").click()
    await page.locator("#micLabel").get_by_text("Waiting for capacity…", exact=True).wait_for()
    await page.locator("#micBtn").click()
    await page.locator(".entry.banner .body").get_by_text(
        "Voice connection canceled.", exact=True
    ).wait_for()
    await page.wait_for_timeout(650)
    assert await page.evaluate("__fakeConverse.connectOptions.length") == 1
    assert await page.evaluate("__fakeConverse.micStarts") == 0
    assert await page.locator("#micLabel").inner_text() == "Start voice"


async def test_fake_microphone_mute_stop_and_text_reuse_one_session(browser_page, browser_server):
    page, _ = browser_page
    _, credentials, _ = browser_server

    await page.locator("#micBtn").click()
    await page.locator("#micLabel").get_by_text("Listening", exact=True).wait_for()
    assert await page.evaluate("__fakeConverse.micStarts") == 1
    assert await page.evaluate(
        "__fakeConverse.clients[0].stream.getAudioTracks()[0].readyState"
    ) == "live"

    await page.locator("#muteBtn").click()
    assert await page.locator("#micLabel").inner_text() == "Muted"
    assert await page.evaluate("__fakeConverse.micEnabled") == [False]
    await page.locator("#muteBtn").click()
    assert await page.evaluate("__fakeConverse.micEnabled") == [False, True]

    await page.locator("#micBtn").click()
    await page.locator(".entry.banner .body").get_by_text(
        "Voice input stopped — the Converse session remains available for text.", exact=True
    ).wait_for()
    assert await page.evaluate("__fakeConverse.micStops") == 1

    await page.locator("#textInput").fill("Continue over text")
    await page.locator("#textSendBtn").click()
    await page.locator(".entry.you .body").get_by_text("Continue over text", exact=True).wait_for()
    assert credentials == ["browser-e2e-session"]
    assert await page.evaluate("__fakeConverse.clients.length") == 1


async def test_local_bridge_updates_screen_and_acknowledges_control(browser_page, browser_server):
    page, _ = browser_page
    server, _, tab_frames = browser_server

    await page.locator("#textInput").fill("Open the session")
    await page.locator("#textSendBtn").click()
    await page.locator(".entry.you").wait_for()

    assert await server.send_json_to_tab(
        {"type": "local", "event": "prompt_accepted", "text": "Prompt reached Claude Code"}
    )
    await page.locator(".entry.inject .body").get_by_text(
        "Prompt reached Claude Code", exact=True
    ).wait_for()
    assert await page.locator(".entry.inject .label").inner_text() == "→ ACCEPTED BY CLAUDE CODE"

    assert await server.send_json_to_tab(
        {
            "type": "local", "event": "bridge_control", "seq": 7,
            "action": "tool_progress", "id": "call-1", "note": "Running tests",
        }
    )
    await wait_for_tab_frame(
        tab_frames, lambda frame: frame.get("event") == "bridge_ack" and frame.get("seq") == 7
    )
    assert await page.evaluate("__fakeConverse.clients[0].bridgeCalls") == [
        {"action": "tool_progress", "id": "call-1", "note": "Running tests"}
    ]


async def test_local_bridge_retains_control_until_transport_reconnects(
    browser_page, browser_server,
):
    page, _ = browser_page
    server, _, tab_frames = browser_server
    await page.locator("#textInput").fill("Open the session")
    await page.locator("#textSendBtn").click()
    await page.locator(".entry.you").wait_for()

    await page.evaluate("__fakeConverse.transportLive = false")
    assert await server.send_json_to_tab({
        "type": "local", "event": "bridge_control", "seq": 8,
        "action": "tool_progress", "id": "call-2", "note": "Still working",
    })
    await page.wait_for_timeout(100)
    assert not any(
        frame.get("event") == "bridge_ack" and frame.get("seq") == 8
        for frame in tab_frames
    )

    await page.evaluate("""
      __fakeConverse.transportLive = true;
      __fakeConverse.clients[0].emit({type: 'reconnected'});
    """)
    await wait_for_tab_frame(
        tab_frames, lambda frame: frame.get("event") == "bridge_ack" and frame.get("seq") == 8
    )
    calls = await page.evaluate("__fakeConverse.clients[0].bridgeCalls")
    assert [call for call in calls if call["id"] == "call-2"] == [
        {"action": "tool_progress", "id": "call-2", "note": "Still working"},
    ]


async def test_bridge_flush_replays_control_queued_during_awaited_injection(
    browser_page, browser_server,
):
    page, _ = browser_page
    server, _, tab_frames = browser_server
    await page.locator("#textInput").fill("Open the session")
    await page.locator("#textSendBtn").click()
    await page.locator(".entry.you").wait_for()
    await page.evaluate("__fakeConverse.deferBridgeAck = true")

    assert await server.send_json_to_tab({
        "type": "local", "event": "bridge_control", "seq": 9,
        "action": "inject_context", "text": "Deferred context",
        "role": "context", "reply": False,
    })
    await page.wait_for_function("typeof __fakeConverse.resolveBridgeAck === 'function'")
    assert await server.send_json_to_tab({
        "type": "local", "event": "bridge_control", "seq": 10,
        "action": "tool_progress", "id": "call-3", "note": "Queued mid-flush",
    })
    await page.evaluate("__fakeConverse.clients[0].emit({type: 'reconnected'})")
    await page.wait_for_timeout(50)
    assert not any(
        frame.get("event") == "bridge_ack" and frame.get("seq") in {9, 10}
        for frame in tab_frames
    )

    # Reject the in-flight injection after reconnected fired. The active flush catches and
    # breaks, so only the requested follow-up flush can replay seq 9 and reach seq 10.
    await page.evaluate("__fakeConverse.rejectBridgeAck()")
    for seq in (9, 10):
        await wait_for_tab_frame(
            tab_frames,
            lambda frame, seq=seq: frame.get("event") == "bridge_ack"
            and frame.get("seq") == seq,
        )
    calls = await page.evaluate("__fakeConverse.clients[0].bridgeCalls")
    assert [call for call in calls if call.get("id") == "call-3"] == [
        {"action": "tool_progress", "id": "call-3", "note": "Queued mid-flush"}
    ]


async def test_cancel_during_connection_never_opens_microphone(browser_page):
    page, _ = browser_page
    await page.evaluate("__fakeConverse.deferConnect = true")
    await page.locator("#micBtn").click()
    await page.locator("#micLabel").get_by_text("Connecting…", exact=True).wait_for()
    await page.wait_for_function("typeof __fakeConverse.resolveConnect === 'function'")
    await page.locator("#micBtn").click()
    await page.evaluate("__fakeConverse.resolveConnect()")
    await page.wait_for_timeout(100)
    assert await page.evaluate("__fakeConverse.micStarts") == 0
    assert await page.locator("#micLabel").inner_text() == "Start voice"


async def test_cancel_during_microphone_acquisition_releases_track(browser_page):
    page, _ = browser_page
    await page.evaluate("__fakeConverse.deferMic = true")
    await page.locator("#micBtn").click()
    await page.wait_for_function("typeof __fakeConverse.resolveMic === 'function'")
    assert await page.evaluate("__fakeConverse.micStarts") == 1
    await page.locator("#micBtn").click()
    await page.evaluate("__fakeConverse.resolveMic()")
    await page.wait_for_timeout(100)
    assert await page.locator("#micLabel").inner_text() == "Start voice"
    assert await page.evaluate("__fakeConverse.clients[0].stream") is None
    assert await page.evaluate("__fakeConverse.micStops") >= 2


async def test_voice_partial_cannot_ack_or_overwrite_pending_typed_turn(browser_page):
    page, _ = browser_page
    await page.evaluate("__fakeConverse.autoReply = false; __fakeConverse.deferTextAck = true")
    await page.locator("#textInput").fill("Keep this typed turn")
    await page.locator("#textSendBtn").click()
    await page.wait_for_function("__fakeConverse.clients[0]?.injected.length === 1")

    await page.evaluate("__fakeConverse.clients[0].emit({type:'asr', text:'Keep this typed turn', final:false, turn_id:'voice-1'})")
    assert await page.locator("#textForm").get_attribute("aria-busy") == "true"
    assert await page.locator("#textInput").input_value() == "Keep this typed turn"

    await page.evaluate("__fakeConverse.clients[0].emit({type:'asr', text:'A voice turn', final:true, turn_id:'voice-1', input_source:'voice'})")
    await page.evaluate("__fakeConverse.resolveTextAck()")
    await page.wait_for_function("textForm.getAttribute('aria-busy') === 'false'")
    message_id = await page.evaluate("__fakeConverse.clients[0].injected[0].options.messageId")
    await page.evaluate("([id]) => __fakeConverse.clients[0].emit({type:'asr', text:'Keep this typed turn', final:true, turn_id:'typed-1', message_id:id, input_source:'text'})", [message_id])
    await page.locator(".entry.you .body").get_by_text("A voice turn", exact=True).wait_for()
    await page.locator(".entry.you .body").get_by_text("Keep this typed turn", exact=True).wait_for()

    rows = await page.locator(".entry.you .body").all_inner_texts()
    assert rows == ["A voice turn", "Keep this typed turn"]


async def test_rapid_second_submit_is_not_delivered(browser_page):
    page, _ = browser_page
    await page.evaluate("__fakeConverse.autoReply = false; __fakeConverse.deferTextAck = true")
    await page.locator("#textInput").fill("First turn")
    await page.locator("#textSendBtn").click()
    await page.wait_for_function("__fakeConverse.clients[0]?.injected.length === 1")
    await page.evaluate("textInput.disabled=false; textInput.value='Second turn'; textForm.dispatchEvent(new Event('submit', {cancelable:true}))")
    await page.wait_for_timeout(50)
    injected = await page.evaluate("__fakeConverse.clients[0].injected")
    assert [entry["text"] for entry in injected] == ["First turn"]
    await page.evaluate("__fakeConverse.resolveTextAck()")
    await page.wait_for_function("textForm.getAttribute('aria-busy') === 'false'")

async def test_broker_rejection_retains_input_and_unlocks(browser_page):
    page, _ = browser_page
    result = await page.evaluate(
        """async () => {
          const events = {busy: [], errors: []};
          const controller = new TypedTurnController({
            send: async (_text, messageId) => ({
              type: 'inject_context_ack', message_id: messageId,
              accepted: false, reason: 'session busy',
            }),
            setBusy: value => events.busy.push(value),
            clearInput: () => { events.cleared = true; },
            showError: value => events.errors.push(value),
          }, {messageId: () => 'rejected-browser-1'});
          const accepted = await controller.submit('rejection probe');
          return {accepted, busy: events.busy, errors: events.errors,
            pending: controller.pending, cleared: !!events.cleared};
        }"""
    )
    assert result["accepted"] is False
    assert result["busy"] == [True, False]
    assert result["pending"] is None
    assert result["cleared"] is False
    assert "session busy" in result["errors"][0]
