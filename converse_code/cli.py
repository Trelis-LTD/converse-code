"""Entry point: `converse-code` wraps `claude` in the current terminal and
connects the session to Converse. `converse-code login` stores the API key."""

import argparse
import asyncio
import getpass
import logging
import os
import sys
import tempfile
import webbrowser
from pathlib import Path

from . import broker as brokermod
from . import config, hooks, tools
from .localserver import LocalServer
from .ptyhost import ClaudeHost

log = logging.getLogger(__name__)
DEFAULT_PORT = 8737


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
    port = await server.start(port=args.port)
    url = f"http://127.0.0.1:{port}"

    scratch = tempfile.mkdtemp(prefix="converse-code-")
    settings_path = hooks.write_settings(scratch, port)
    claude_argv = args.claude.split() + ["--settings", str(settings_path)]
    host = ClaudeHost(claude_argv, attach_terminal=not args.headless)

    handle = _session_handle()
    client = brokermod.BrokerClient(
        api_key, session_id=handle, tools=tools.manifest(), url=args.broker_url,
        client_info={"capabilities": []},
    )
    try:
        await client.connect()
    except Exception as exc:
        await server.stop()
        print(f"Could not connect to Converse ({exc}). Claude Code was not started.", file=sys.stderr)
        return 1

    router = tools.ToolRouter(host, client, handle=handle)
    router.on_status = server.send_json_to_tab
    server.on_hook = router.on_hook
    server.on_tab_audio = client.send_audio

    async def on_tab_json(msg: dict) -> None:
        if msg.get("type") == "playback_stopped":
            await client.send_client_event(
                "playback_stopped",
                remaining_ms=int(msg.get("remaining_ms", 0)),
                discarded_ms=int(msg.get("discarded_ms", 0)),
                barge_seq=int(msg.get("barge_seq", 0)),
            )

    server.on_tab_json = on_tab_json
    client.on_json = server.send_json_to_tab
    client.on_audio = server.send_audio_to_tab
    client.on_tool_call = lambda call: _spawn_tool(router, call)

    print(f"Converse Code — voice tab: {url}   (session: {handle})")
    if not args.no_browser:
        webbrowser.open(url)

    broker_task = asyncio.create_task(client.run())
    await host.start()
    try:
        await host.exited.wait()
    finally:
        host.restore_terminal()
        broker_task.cancel()
        await client.close()
        await server.stop()
    return host.returncode or 0


_tool_tasks: set[asyncio.Task] = set()


async def _spawn_tool(router: tools.ToolRouter, call: dict) -> None:
    # Don't block the broker receive loop while long_task holds open.
    task = asyncio.create_task(router.handle_tool_call(call))
    _tool_tasks.add(task)
    task.add_done_callback(_tool_tasks.discard)


def main() -> None:
    parser = argparse.ArgumentParser(prog="converse-code", description="Talk to Claude Code by voice via Converse.")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="local port for the voice tab")
    parser.add_argument("--claude", default=os.environ.get("CONVERSE_CODE_CLAUDE_CMD", "claude"),
                        help="command used to launch Claude Code")
    parser.add_argument("--broker-url", default=os.environ.get("CONVERSE_URL", brokermod.DEFAULT_URL))
    parser.add_argument("--no-browser", action="store_true", help="don't auto-open the voice tab")
    parser.add_argument("--headless", action="store_true", help=argparse.SUPPRESS)  # tests only
    sub = parser.add_subparsers(dest="cmd")
    sub.add_parser("login", help="store and validate your Converse API key")
    args = parser.parse_args()

    logging.basicConfig(level=logging.WARNING, stream=sys.stderr)
    if args.cmd == "login":
        raise SystemExit(asyncio.run(_login(args.broker_url)))
    raise SystemExit(asyncio.run(_run(args)))


if __name__ == "__main__":
    main()
