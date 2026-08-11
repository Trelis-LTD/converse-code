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
    assert await page.evaluate("__fakeConverse.clients[0].injected") == [
        {"text": "Refactor the parser", "options": {"role": "user", "reply": True}}
    ]
    assert await page.evaluate("__fakeConverse.connectOptions") == [{"noGreeting": True}]
    assert await page.evaluate("__fakeConverse.micStarts") == 0
    assert any(frame.get("event") == "bridge_ready" for frame in tab_frames)


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
        {"type": "local", "event": "injected", "text": "Prompt reached Claude Code"}
    )
    await page.locator(".entry.inject .body").get_by_text(
        "Prompt reached Claude Code", exact=True
    ).wait_for()

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
    await page.evaluate("__fakeConverse.autoReply = false")
    await page.locator("#textInput").fill("Keep this typed turn")
    await page.locator("#textSendBtn").click()
    await page.wait_for_function("__fakeConverse.clients[0]?.injected.length === 1")

    await page.evaluate("__fakeConverse.clients[0].emit({type:'asr', text:'Keep this typed turn', final:false, turn_id:'voice-1'})")
    assert await page.locator("#textForm").get_attribute("aria-busy") == "true"
    assert await page.locator("#textInput").input_value() == "Keep this typed turn"

    await page.evaluate("__fakeConverse.clients[0].emit({type:'asr', text:'A voice turn', final:true, turn_id:'voice-1'})")
    await page.evaluate("__fakeConverse.clients[0].emit({type:'asr', text:'Keep this typed turn', final:true, turn_id:'typed-1'})")
    await page.locator(".entry.you .body").get_by_text("A voice turn", exact=True).wait_for()
    await page.locator(".entry.you .body").get_by_text("Keep this typed turn", exact=True).wait_for()

    rows = await page.locator(".entry.you .body").all_inner_texts()
    assert rows == ["A voice turn", "Keep this typed turn"]


async def test_rapid_second_submit_is_not_delivered(browser_page):
    page, _ = browser_page
    await page.evaluate("__fakeConverse.autoReply = false")
    await page.locator("#textInput").fill("First turn")
    await page.locator("#textSendBtn").click()
    await page.wait_for_function("__fakeConverse.clients[0]?.injected.length === 1")
    await page.evaluate("textInput.disabled=false; textInput.value='Second turn'; textForm.dispatchEvent(new Event('submit', {cancelable:true}))")
    await page.wait_for_timeout(50)
    injected = await page.evaluate("__fakeConverse.clients[0].injected")
    assert [entry["text"] for entry in injected] == ["First turn"]
    await page.evaluate("__fakeConverse.clients[0].emit({type:'asr', text:'First turn', final:true, turn_id:'typed-1'})")
    await page.wait_for_function("textForm.getAttribute('aria-busy') === 'false'")

async def test_default_browser_timers_timeout_and_unlock_without_illegal_invocation(browser_page):
    page, _ = browser_page
    result = await page.evaluate(
        """() => new Promise((resolve) => {
          const events = {busy: [], errors: []};
          const controller = new TypedTurnController({
            send: async () => {},
            setBusy: value => events.busy.push(value),
            clearInput: () => { events.cleared = true; },
            showError: value => events.errors.push(value),
          }, {timeoutMs: 10});
          controller.submit("timer probe");
          setTimeout(() => resolve({
            busy: events.busy,
            errors: events.errors,
            pending: controller.pending,
          }), 30);
        })"""
    )
    assert result["busy"] == [True, False]
    assert result["pending"] is None
    assert result["errors"] == [
        "Typed turn was not acknowledged. It remains in the input so you can retry."
    ]
