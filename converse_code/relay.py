"""Relaying the browser SDK's session to the broker.

The page runs the SDK's own ConverseClient — the same code path as the Converse
playground — but the tools are answered by this process, and the protocol carries
audio and tool declarations on one socket. So the page's start frame is rewritten
on its way out: the real API key goes in (never handed to the browser), the
session id becomes ours, and the tool manifest is attached.
"""

from . import audio as audiofmt


def rewrite_start_frame(
    page_frame: dict, api_key: str, session_id: str, tools: list[dict]
) -> dict:
    """Turn the page's start frame into ours, preserving what it chose."""
    frame = dict(page_frame)   # callers gate on is_start(), so "type" is already right
    frame["api_key"] = api_key
    frame["session_id"] = session_id

    mode = dict(frame.get("mode") or {})
    mode.setdefault("kind", "converse")
    mode["tools"] = tools
    frame["mode"] = mode

    audio = dict(frame.get("audio") or {})
    audio.setdefault("sr", audiofmt.SAMPLE_RATE)
    # Pin the downlink encoding: the server default changed once (pcm_f32le ->
    # pcm16) and a mismatch is silent noise, not an error.
    audio["output_encoding"] = audiofmt.OUTPUT_ENCODING
    frame["audio"] = audio
    return frame


def is_start(msg: dict) -> bool:
    return msg.get("type") == "start"
