"""Use the Converse browser as voice control for a visible Pi terminal."""

from __future__ import annotations

import argparse
import asyncio
import getpass
import logging
import os
import shlex
import sys
import webbrowser
from pathlib import Path

from . import agent_tools, config, converse
from .agent_tools import AgentToolRouter
from .bridge import BrowserBridge
from .localserver import LocalServer
from .pi_tui import PiTUIBridge, PiTUIBridgeError

DEFAULT_PORT = 8737
DEFAULT_PI_CMD = "pi --provider openai-codex"


def _session_handle() -> str:
    project = "".join(c if c.isalnum() else "-" for c in Path.cwd().name)[:32].strip("-")
    return f"code-{project or 'project'}-{os.urandom(3).hex()}"


def _ensure_api_key() -> str | None:
    key = config.get_api_key()
    if key:
        return key
    print("No Converse API key found. Get one from the converse.trelis.com dashboard.")
    key = getpass.getpass("Paste your API key (ck_…): ").strip()
    if key:
        config.save_api_key(key)
        print(f"Saved to {config.CONFIG_PATH}.")
    return key or None


async def _login(url: str) -> int:
    key = getpass.getpass("Paste your Converse API key (ck_…): ").strip()
    if not key:
        print("No key given.")
        return 1
    if await converse.validate_key(key, url=url):
        config.save_api_key(key)
        print(f"Key is valid. Saved to {config.CONFIG_PATH}.")
        return 0
    print("That key was not accepted by the Converse API.")
    return 1


def _pi_argv(command: str) -> list[str]:
    bridge = Path(__file__).with_name("pi_bridge.ts")
    return [*shlex.split(command), "-e", str(bridge)]


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
            f"Could not start the Converse session server on port {args.port}: {exc}\n"
            "Another instance may be running; stop it or pass --port 0.",
            file=sys.stderr,
        )
        return 1
    try:
        if not await converse.validate_key(api_key, url=args.broker_url):
            await server.stop()
            print("Converse rejected that API key. Run: converse-code login", file=sys.stderr)
            return 1
    except Exception as exc:  # noqa: BLE001 - normalize broker failures at the CLI boundary
        await server.stop()
        print(f"Could not reach Converse ({exc}). Pi was not started.", file=sys.stderr)
        return 1

    stopped = asyncio.Event()
    tool_tasks: set[asyncio.Task] = set()
    bridge = BrowserBridge(server.send_json_to_tab)
    pi = PiTUIBridge(server.send_json_to_pi)
    router = AgentToolRouter(pi, bridge, handle=_session_handle())
    pi.on_event = router.on_event
    async def end_session() -> None:
        stopped.set()

    router.on_end_session = end_session

    async def issue_credential(session_id: str) -> dict:
        credential = await converse.mint_session_credential(
            api_key, session_id, api_url=args.api_url,
        )
        return {**credential, "ws_url": args.broker_url, "tools": agent_tools.manifest()}

    server.on_session_credential = issue_credential
    server.on_tab_json = bridge.handle_browser_message
    server.on_tab_closed = bridge.on_browser_disconnected
    server.on_pi_json = pi.handle_message
    server.on_pi_connected = lambda: pi.set_connected(True)
    server.on_pi_closed = lambda: pi.set_connected(False)
    async def spawn_tool(call: dict) -> None:
        task = asyncio.create_task(router.handle_tool_call(call))
        tool_tasks.add(task)
        task.add_done_callback(tool_tasks.discard)

    bridge.on_tool_call = spawn_tool
    bridge.on_tool_cancel = router.handle_tool_cancel

    environment = {**os.environ, "CONVERSE_CODE_PI_BRIDGE_URL": server.pi_url}
    try:
        process = await asyncio.create_subprocess_exec(*_pi_argv(args.pi), env=environment)
    except FileNotFoundError:
        await server.stop()
        print("Could not launch Pi. Install it first, then run converse-code again.", file=sys.stderr)
        return 1

    print(f"Converse voice control: {server.url}")
    if not args.no_browser:
        webbrowser.open(server.url)
    process_wait = asyncio.create_task(process.wait())
    stop_wait = asyncio.create_task(stopped.wait())
    completed = set()
    try:
        completed, _ = await asyncio.wait(
            {process_wait, stop_wait}, return_when=asyncio.FIRST_COMPLETED,
        )
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        if process_wait in completed and router.active_call_id:
            await router.on_event({"type": "process_exit", "status": process.returncode})
        elif router.active_call_id:
            await router.handle_tool_cancel({"id": router.active_call_id})
        if tool_tasks:
            await asyncio.wait(tool_tasks, timeout=2)
        if process.returncode is None:
            try:
                await pi.command("shutdown")
                await asyncio.wait_for(process.wait(), timeout=5)
            except (PiTUIBridgeError, TimeoutError):
                process.terminate()
                await process.wait()
        for task in (process_wait, stop_wait):
            if not task.done():
                task.cancel()
        await asyncio.gather(process_wait, stop_wait, return_exceptions=True)
        await server.stop()
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="converse-code",
        description="Minimal Converse voice control for the visible Pi terminal.",
    )
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument(
        "--pi", default=os.environ.get("CONVERSE_CODE_PI_CMD", DEFAULT_PI_CMD),
        help="Visible Pi TUI command",
    )
    parser.add_argument("--broker-url", default=os.environ.get("CONVERSE_URL", converse.DEFAULT_WS_URL))
    parser.add_argument(
        "--api-url", default=os.environ.get("CONVERSE_API_URL", converse.DEFAULT_API_URL),
    )
    parser.add_argument("--no-browser", action="store_true")
    sub = parser.add_subparsers(dest="cmd")
    sub.add_parser("login", help="store and validate a Converse API key")
    args = parser.parse_args()
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr)
    if args.cmd == "login":
        raise SystemExit(asyncio.run(_login(args.broker_url)))
    raise SystemExit(asyncio.run(_run(args)))


if __name__ == "__main__":
    main()
