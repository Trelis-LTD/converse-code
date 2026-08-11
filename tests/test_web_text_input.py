from pathlib import Path


PAGE = (Path(__file__).parents[1] / "converse_code" / "web" / "index.html").read_text()


def test_browser_has_typed_turn_composer():
    assert 'id="textForm"' in PAGE
    assert 'id="textInput"' in PAGE
    assert 'maxlength="2000"' in PAGE
    assert 'sendText(text, {messageId: messageId})' in PAGE


def test_typed_session_suppresses_greeting_and_reuses_canonical_asr_entry():
    assert 'startSession({noGreeting: true})' in PAGE
    assert 'connect({noGreeting: !!options.noGreeting})' in PAGE
    assert "new UserTurnRenderer" in PAGE
    assert "userTurns.handle(ev, userText, isFinal(ev))" in PAGE
