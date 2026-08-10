"""Text-only end-to-end eval of the voice control loop.

Simulates transcribed voice by injecting user text turns into a REAL Converse
broker session (real Gemini brain, real tool protocol) and routes the resulting
tool calls through a REAL ToolRouter into a REAL `claude` CLI in a throwaway
project directory. No audio is sent and no browser is opened; assistant speech
comes back as text events, so tool-call correctness and voiceover wording can
both be checked from a terminal.

Costs a small amount of Converse and Claude usage. Run manually:

    uv run scripts/eval_voice_text.py
"""

import asyncio
import json
import os
import sys
import tempfile
import time
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

from converse_code import config  # noqa: E402
from converse_code.broker import BrokerClient, DEFAULT_URL
from converse_code.hooks import write_settings
from converse_code.localserver import LocalServer
from converse_code.ptyhost import ClaudeHost
from converse_code.tools import ToolRouter, manifest

QUIET_S = 4.0          # a turn is over when events stop this long and no tool is running
VOICE_RED_FLAGS = ("*", "#", "`", "http", "](")

SCENARIOS = [
    {"id": "greeting", "say": "Hello! Just checking you can hear me okay.",
     "expect_tools": [], "timeout": 45},
    {"id": "observe-model",
     "say": "Inspect Claude Code's actual UI state and tell me which model is selected. Do not guess.",
     "expect_tools_any": ["observe_claude", "command"],
     "expect_spoken_any": ["default", "opus", "fable", "sonnet", "haiku"], "timeout": 60},
    {"id": "change-model",
     "say": "Change Claude Code's model to Sonnet and verify that it actually changed.",
     "expect_tools": ["set_model"], "timeout": 90},
    {"id": "challenge-model",
     "say": "I don't believe the model changed. Inspect the actual Claude Code state again.",
     "expect_tools_any": ["observe_claude", "command"],
     "expect_spoken_any": ["sonnet"], "timeout": 60},
    {"id": "build-file", "say": "Could you create a Python file called hello.py that prints hello world?",
     "expect_tools": ["long_task"], "file": "hello.py", "timeout": 240},
    {"id": "run-it", "say": "Great, can you run it and tell me what it prints?",
     "expect_tools": ["long_task"], "timeout": 240},
    # Regression for the session where "open it up" produced narration and no
    # tool call: a non-coding verb must still be piped through to Claude Code.
    {"id": "open-it", "say": "Can you open that file up for me?",
     "expect_tools": ["long_task"], "timeout": 240},
    {"id": "no-action", "say": "Sounds good, thanks.",
     "expect_tools": [], "timeout": 45},
]


class Recorder:
    """Everything that comes down the socket, timestamped, for report + dump."""

    def __init__(self):
        self.events: list[dict] = []
        self.audio_bytes = 0
        self.last_event = time.monotonic()

    def note(self, kind: str, payload: dict | None = None) -> None:
        self.last_event = time.monotonic()
        self.events.append({"t": round(time.time(), 3), "kind": kind, **(payload or {})})

    def spoken_text(self, since: int) -> str:
        """Assistant speech since event index `since`, preferring complete
        utterances over raw deltas when both are present."""
        utterances, deltas = [], []
        for ev in self.events[since:]:
            if ev["kind"] == "utterance":
                utterances.append(str(ev.get("text") or ""))
            elif ev["kind"] == "text_delta":
                deltas.append(str(ev.get("text") or ev.get("delta") or ""))
        return " ".join(t for t in utterances if t) or "".join(deltas)

    def tool_calls(self, since: int) -> list[str]:
        return [ev["name"] for ev in self.events[since:] if ev["kind"] == "tool_call"]


async def run_turn(client, rec: Recorder, pending: set, sc: dict) -> tuple[int, bool]:
    """Send one simulated transcribed turn; return (window_start, finished).

    The broker queues injected turns behind in-flight speech, so wall-clock
    windows misattribute events. Anchor the window to the broker's ASR echo of
    this exact text, then close it after a `done` with no tool running and the
    stream quiet."""
    mark = len(rec.events)
    await client.send_context(sc["say"], role="user", reply=True)
    deadline = time.monotonic() + sc["timeout"]
    probe = sc["say"][:24].lower()
    start = None
    sent_at = time.monotonic()
    resent = False
    while start is None:
        if time.monotonic() > deadline or client.closed.is_set():
            return mark, False  # never acknowledged — session stalled or closed
        # A turn injected while a reply is in flight is rejected with an
        # inject_rejected nack — resend once the stream goes quiet, as a real
        # user would repeat themselves. Keep a 20s blind retry as backstop.
        nacked = any(ev["kind"] == "error" and ev.get("code") == "inject_rejected"
                     for ev in rec.events[mark:])
        waited = time.monotonic() - sent_at
        if not resent and ((nacked and time.monotonic() - rec.last_event > 2) or waited > 20):
            print(f"(turn not accepted after {waited:.0f}s — repeating it)")
            await client.send_context(sc["say"], role="user", reply=True)
            resent = True
        await asyncio.sleep(0.3)
        start = next((i for i, ev in enumerate(rec.events[mark:], mark)
                      if ev["kind"] == "asr" and probe in str(ev.get("text", "")).lower()),
                     None)
    while True:
        done = any(ev["kind"] == "done" for ev in rec.events[start:])
        if client.closed.is_set():
            # A graceful sign-off (brain END_CALL) closes the socket right
            # after the goodbye's done — that's a finished turn, not a stall.
            return start, done
        if time.monotonic() > deadline:
            return start, False
        await asyncio.sleep(0.5)
        quiet = time.monotonic() - rec.last_event
        if done and quiet > QUIET_S and not pending:
            return start, True


async def main() -> int:
    sys.stdout.reconfigure(line_buffering=True)
    api_key = config.get_api_key()
    if not api_key:
        print("No Converse API key configured. Run: converse-code login", file=sys.stderr)
        return 1
    broker_url = os.environ.get("CONVERSE_URL", DEFAULT_URL)

    original_dir = Path.cwd()
    project_dir = Path(tempfile.mkdtemp(prefix="cc-eval-project-"))
    os.chdir(project_dir)
    server = LocalServer()
    await server.start(port=0)
    settings = write_settings(
        tempfile.mkdtemp(prefix="cc-eval-"),
        server.hook_url("stop"),
        server.hook_url("user_prompt_submit"),
        server.hook_url("permission_request"),
        server.hook_url("stop_failure"),
    )
    host = ClaudeHost(
        ["claude", "--permission-mode", "auto", "--settings", str(settings)],
        attach_terminal=False,
    )
    await host.start()

    rec = Recorder()
    handle = f"cc-eval-{os.urandom(3).hex()}"
    client = BrokerClient(api_key, session_id=handle, tools=manifest(), url=broker_url,
                          client_info={"capabilities": []})
    router = ToolRouter(host, client, handle=handle, project_dir=project_dir,
                        verify_submissions=True)
    server.on_hook = router.on_hook

    pending: set[asyncio.Task] = set()

    async def on_tool_call(call: dict) -> None:
        rec.note("tool_call", {"name": call.get("name"), "args": call.get("args")})
        task = asyncio.create_task(router.handle_tool_call(call))
        pending.add(task)
        task.add_done_callback(pending.discard)

    async def on_tool_cancel(call: dict) -> None:
        rec.note("tool_cancel", {"id": call.get("id")})
        await router.handle_tool_cancel(call)

    async def on_json(msg: dict) -> None:
        rec.note(msg.get("type") or "unknown", {k: v for k, v in msg.items() if k != "type"})

    async def on_audio(frame: bytes) -> None:
        rec.audio_bytes += len(frame)

    client.on_tool_call = on_tool_call
    client.on_tool_cancel = on_tool_cancel
    client.on_json = on_json
    client.on_audio = on_audio

    results = []
    try:
        await asyncio.sleep(6)  # let the TUI boot
        menu = router.menu()
        if menu:  # fresh directory trust dialog
            await router.handle_tool_call({"id": "trust", "name": "select_option",
                                           "args": {"option": "yes"}})
            await asyncio.sleep(3)
        print(f"claude ready in {project_dir} (state: {router.state()})")

        await client.connect()
        broker_task = asyncio.create_task(client.run())
        await asyncio.sleep(2)

        for sc in SCENARIOS:
            print(f"\n=== {sc['id']}: {sc['say']!r}")
            start, finished = await run_turn(client, rec, pending, sc)
            called = rec.tool_calls(start)
            spoken = rec.spoken_text(start)
            expected = sc.get("expect_tools", [])
            expected_any = sc.get("expect_tools_any", [])
            if expected_any:
                tool_calls_ok = any(name in expected_any for name in called)
            else:
                tool_calls_ok = called == expected or (
                    bool(expected) and [c for c in called if c in expected] == expected
                )
            checks = {"turn_finished": finished, "tool_calls": tool_calls_ok}
            if sc.get("file"):
                checks["file_exists"] = (project_dir / sc["file"]).exists()
            if sc.get("expect_spoken_any"):
                checks["spoken_state"] = any(
                    token in spoken.lower() for token in sc["expect_spoken_any"]
                )
            flags = [f for f in VOICE_RED_FLAGS if f in spoken]
            checks["voice_clean"] = not flags
            ok = all(checks.values())
            results.append((sc["id"], ok, checks, called, spoken, flags))
            expected_display = expected or expected_any or "none"
            print(f"tools called: {called or 'none'}   expected: {expected_display}")
            print(f"assistant said: {spoken.strip() or '(no text captured)'}")
            print(f"checks: {checks}  -> {'PASS' if ok else 'FAIL'}")
            if client.closed.is_set():
                print("broker connection closed mid-eval; stopping.")
                break

        dump = Path(tempfile.gettempdir()) / f"cc-eval-events-{os.getpid()}.jsonl"
        dump.write_text("\n".join(json.dumps(e) for e in rec.events), encoding="utf-8")
        print(f"\nfull event dump: {dump}")
        print(f"assistant audio received: {rec.audio_bytes / 32000:.1f}s")
        failed = [r for r in results if not r[1]]
        print(f"\nEVAL: {len(results) - len(failed)}/{len(results)} scenarios passed"
              + (f" — FAILED: {[r[0] for r in failed]}" if failed else ""))
        return 1 if failed or not results else 0
    finally:
        try:
            await client.close()
        except Exception:
            pass
        host.inject("/exit")
        try:
            await asyncio.wait_for(host.exited.wait(), 15)
        except asyncio.TimeoutError:
            await host.stop()
        await server.stop()
        os.chdir(original_dir)


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
