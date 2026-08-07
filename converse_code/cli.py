"""Entry point: `converse-code` wraps `claude` in the current terminal and
connects the session to Converse. `converse-code login` stores the API key."""

import argparse
import asyncio
import getpass
import logging
import os
import shlex
import shutil
import sys
import tempfile
import webbrowser
from pathlib import Path

from . import broker as brokermod
from . import config, hooks, relay, selftest, tools
from .record import WavRecorder
from .localserver import LocalServer
from .ptyhost import ClaudeHost

log = logging.getLogger(__name__)
DEFAULT_PORT = 8737
DEFAULT_CLAUDE_CMD = "claude --permission-mode auto"


def _session_handle() -> str:
    project = "".join(c if c.isalnum() else "-" for c in Path.cwd().name)[:32].strip("-") or "project"
    return f"cc-{project}-{os.urandom(3).hex()}"


def _ensure_api_key() -> str | None:
    key = config.get_api_key()
    if key:
        return key
    print("No Converse API key found. Get one from the converse.trelis.com dashboard.")
    key = getpass.getpass("Paste your API key (ck_…): ").strip()
    if not key:
        return None
    config.save_api_key(key)
    print(f"Saved to {config.CONFIG_PATH}.")
    return key


async def _login(url: str) -> int:
    key = getpass.getpass("Paste your Converse API key (ck_…): ").strip()
    if not key:
        print("No key given.")
        return 1
    if await brokermod.validate_key(key, url=url):
        config.save_api_key(key)
        print(f"Key is valid. Saved to {config.CONFIG_PATH}.")
        return 0
    print("That key was not accepted by the Converse API.")
    return 1


async def _run(args) -> int:
    api_key = _ensure_api_key()
    if not api_key:
        print("Cannot start without an API key. Run: converse-code login", file=sys.stderr)
        return 1

    server = LocalServer()
    try:
        await server.start(port=args.port)
    except OSError as exc:
        print(
            f"Could not start the voice tab server on port {args.port}: {exc}\n"
            "Another converse-code may already be running — stop it, or pass "
            "--port <other> (or --port 0 to pick a free one).",
            file=sys.stderr,
        )
        return 1
    url = server.url

    # The broker connection is now opened lazily, when the page's SDK client
    # sends its start frame — so check credentials up front with the free
    # (non-billable) auth frame rather than letting a bad key surface minutes
    # later as a banner in the browser.
    try:
        if not await brokermod.validate_key(api_key, url=args.broker_url):
            await server.stop()
            print("Converse rejected that API key. Run: converse-code login", file=sys.stderr)
            return 1
    except Exception as exc:
        await server.stop()
        print(f"Could not reach Converse ({exc}). Claude Code was not started.", file=sys.stderr)
        return 1

    scratch = tempfile.mkdtemp(prefix="converse-code-")
    # --settings loads *additional* settings, so the dev's own hooks/config stay.
    settings_path = hooks.write_settings(
        scratch,
        server.hook_url("stop"),
        server.hook_url("user_prompt_submit"),
        server.hook_url("permission_request"),
        server.hook_url("stop_failure"),
    )
    claude_argv = shlex.split(args.claude) + ["--settings", str(settings_path)]
    host = ClaudeHost(claude_argv, attach_terminal=not args.headless)

    handle = _session_handle()
    client = brokermod.BrokerClient(
        api_key, session_id=handle, tools=tools.manifest(), url=args.broker_url,
        client_info={"capabilities": []},
    )
    router = tools.ToolRouter(host, client, handle=handle, verify_submissions=True)
    router.on_status = server.send_json_to_tab
    server.on_hook = router.on_hook

    # The browser runs the SDK's own ConverseClient — the same code path as the
    # Converse playground — and this process relays its socket to the broker,
    # substituting the real API key and adding the tool manifest. Hand-driving the
    # SDK's audio pieces instead produced a run of composition bugs (missing echo
    # canceller, missing scheduler pump, wrong frame ordering); the client already
    # solves all of that, so it owns the audio and this owns the tools.
    broker_task: asyncio.Task | None = None

    async def on_proxy_json(msg: dict) -> None:
        nonlocal broker_task
        if relay.is_start(msg):
            frame = relay.rewrite_start_frame(msg, api_key, handle, tools.manifest())
            try:
                await client.connect(start_frame=frame)
            except Exception as exc:
                log.error("broker connect failed: %s", exc)
                await server.send_json_to_proxy(
                    {"type": "bye", "code": 1011, "reason": f"could not reach Converse: {exc}"}
                )
                return
            broker_task = asyncio.create_task(client.run())
            return
        await client.send_raw(msg)

    async def on_proxy_closed() -> None:
        await client.close()

    server.on_proxy_json = on_proxy_json
    server.on_proxy_audio = client.send_raw
    server.on_proxy_closed = on_proxy_closed

    async def to_page(msg: dict) -> None:
        await server.send_json_to_proxy(msg)

    client.on_json = to_page
    recorder: WavRecorder | None = None
    if args.record_audio:
        rec_path = Path(tempfile.gettempdir()) / f"converse-code-downlink-{os.getpid()}.wav"
        recorder = WavRecorder(rec_path)
        print(f"Recording assistant audio to: {rec_path}")
        client.on_audio = lambda frame: _record_and_relay_audio(recorder, server, frame)
    else:
        client.on_audio = server.send_audio_to_proxy
    client.on_tool_call = lambda call: _spawn_tool(router, call)
    client.on_tool_cancel = router.handle_tool_cancel

    print(f"Converse Code — voice tab: {url}   (session: {handle})")
    print(f"Logs: {LOG_PATH}")
    if not args.no_browser:
        webbrowser.open(url)
    started_at = asyncio.get_running_loop().time()
    await host.start()
    try:
        await host.exited.wait()
    finally:
        host.restore_terminal()
        _report_early_exit(host, asyncio.get_running_loop().time() - started_at)
        if broker_task is not None:
            broker_task.cancel()
        await client.close()
        await server.stop()
        shutil.rmtree(scratch, ignore_errors=True)
        if recorder is not None:
            recorder.close()
            print(f"\nRecorded {recorder.seconds:.1f}s of assistant audio: {recorder.path}")
            print("Play it: if it sounds clean, the problem is the browser or the "
                  "audio device, not the session.")
    return host.returncode or 0


async def _record_and_relay_audio(recorder: WavRecorder, server: LocalServer, frame: bytes) -> None:
    """Record the same downlink frame that is handed to the browser page."""
    recorder.add(frame)
    await server.send_audio_to_proxy(frame)


LOG_PATH = Path(tempfile.gettempdir()) / "converse-code.log"
EARLY_EXIT_S = 5.0


def _configure_logging(owns_terminal: bool) -> None:
    """Claude Code owns the terminal, so log records written to stderr would be
    painted into the middle of its TUI — write them to a file in that case.

    basicConfig rejects `filename` and `stream` together even when one is None,
    so only ever pass one.
    """
    if owns_terminal:
        logging.basicConfig(level=logging.WARNING, filename=str(LOG_PATH))
    else:
        logging.basicConfig(level=logging.WARNING, stream=sys.stderr)


def _report_early_exit(host: ClaudeHost, elapsed: float) -> None:
    """Explain a Claude Code session that died on startup.

    Claude Code restores the terminal from its alternate screen as it exits,
    which erases whatever it printed — so a failed launch otherwise looks like
    `converse-code` silently doing nothing. Our screen buffer still holds the
    final frame, so replay it.
    """
    if host.returncode == 0 and elapsed >= EARLY_EXIT_S:
        return
    lines = [l.rstrip() for l in host.snapshot() if l.strip()]
    print(
        f"\nClaude Code exited after {elapsed:.1f}s (exit code {host.returncode}).",
        file=sys.stderr,
    )
    if host.returncode == 127:
        print(
            "The `claude` command could not be launched — check it's on your PATH, "
            "or pass --claude with the right command.",
            file=sys.stderr,
        )
    if lines:
        print("Its last screen before exiting:", file=sys.stderr)
        for line in lines[-15:]:
            print(f"  {line}", file=sys.stderr)
    else:
        print("It produced no output at all.", file=sys.stderr)


_tool_tasks: set[asyncio.Task] = set()


async def _spawn_tool(router: tools.ToolRouter, call: dict) -> None:
    # Don't block the broker receive loop while long_task holds open.
    task = asyncio.create_task(router.handle_tool_call(call))
    _tool_tasks.add(task)
    task.add_done_callback(_tool_tasks.discard)


def main() -> None:
    parser = argparse.ArgumentParser(prog="converse-code", description="Talk to Claude Code by voice via Converse.")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="local port for the voice tab")
    parser.add_argument("--claude", default=os.environ.get("CONVERSE_CODE_CLAUDE_CMD", DEFAULT_CLAUDE_CMD),
                        help="command used to launch Claude Code")
    parser.add_argument("--broker-url", default=os.environ.get("CONVERSE_URL", brokermod.DEFAULT_URL))
    parser.add_argument("--no-browser", action="store_true", help="don't auto-open the voice tab")
    parser.add_argument("--record-audio", action="store_true",
                        help="save the assistant audio relayed to the page as a WAV (for diagnosing playback)")
    parser.add_argument("--headless", action="store_true", help=argparse.SUPPRESS)  # tests only
    sub = parser.add_subparsers(dest="cmd")
    sub.add_parser("login", help="store and validate your Converse API key")
    sub.add_parser("selftest", help="check the audio path end to end, without a browser")
    args = parser.parse_args()

    _configure_logging(owns_terminal=not (args.cmd in ("login", "selftest") or args.headless))
    if args.cmd == "login":
        raise SystemExit(asyncio.run(_login(args.broker_url)))
    if args.cmd == "selftest":
        raise SystemExit(asyncio.run(selftest.run(args.broker_url)))
    raise SystemExit(asyncio.run(_run(args)))


if __name__ == "__main__":
    main()
