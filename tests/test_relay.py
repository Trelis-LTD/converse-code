"""Start-frame rewriting for the browser SDK relay.

This is the seam between the page's SDK client (which owns audio) and this
process (which owns the tools). Getting it wrong means either no tools, a leaked
API key, or a silently wrong audio format.
"""

from converse_code.relay import is_start, rewrite_start_frame
from converse_code.tools import manifest

PAGE_FRAME = {
    "type": "start",
    "session_id": "browser-generated-uuid",
    "api_key": "relayed-by-converse-code",
    "audio": {"sr": 16000},
    "mode": {"kind": "converse", "web_search": False},
    "client": {"timezone": "Europe/Dublin"},
}


def rewritten():
    return rewrite_start_frame(PAGE_FRAME, "ck_real_secret", "cc-proj-abc", manifest())


def test_real_key_and_our_session_id_replace_the_pages():
    frame = rewritten()
    assert frame["api_key"] == "ck_real_secret"
    assert frame["session_id"] == "cc-proj-abc"


def test_tool_manifest_is_attached():
    frame = rewritten()
    assert [t["name"] for t in frame["mode"]["tools"]] == [t["name"] for t in manifest()]
    assert frame["mode"]["kind"] == "converse"
    assert frame["mode"]["web_search"] is False  # the page's own choices survive


def test_downlink_encoding_is_pinned_and_rate_preserved():
    frame = rewritten()
    assert frame["audio"] == {"sr": 16000, "output_encoding": "pcm16"}


def test_page_fields_we_do_not_own_are_preserved():
    frame = rewritten()
    assert frame["client"] == {"timezone": "Europe/Dublin"}


def test_the_pages_frame_is_not_mutated():
    """The page's dict must not be edited in place — it would leak the real key
    back into anything that logged or reused it."""
    before = dict(PAGE_FRAME)
    rewritten()
    assert PAGE_FRAME == before
    assert PAGE_FRAME["api_key"] == "relayed-by-converse-code"


def test_missing_mode_and_audio_get_sane_defaults():
    frame = rewrite_start_frame({"type": "start"}, "ck_x", "s1", manifest())
    assert frame["mode"]["kind"] == "converse"
    assert frame["audio"]["sr"] == 16000
    assert frame["audio"]["output_encoding"] == "pcm16"


def test_is_start_only_matches_the_start_frame():
    assert is_start({"type": "start"})
    assert not is_start({"type": "tool_result"})
    assert not is_start({})
