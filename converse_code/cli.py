"""Run Pi/Codex behind a minimal Converse Browser SDK reference client."""

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
from .pi_rpc import PiRPC, PiRPCError

DEFAULT_PORT = 8737
DEFAULT_PI_CMD = "pi --mode rpc --provider openai-codex"
_tool_tasks: set[asyncio.Task] = set()


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


async def _spawn_tool(router: AgentToolRouter, call: dict) -> None:
    task = asyncio.create_task(router.handle_tool_call(call))
    _tool_tasks.add(task)
    task.add_done_callback(_tool_tasks.discard)


def _pi_argv(command: str) -> list[str]:
    approval = Path(__file__).with_name("pi_approval.ts")
    return [*shlex.split(command), "-e", str(approval)]


def _require_pi_model(response: dict) -> None:
    model = (response.get("data") or {}).get("model") or {}
    if model.get("id") in {None, "", "unknown"}:
        raise PiRPCError(
            "the Codex provider is not authenticated; run `pi`, then `/login`, and choose "
            "ChatGPT Plus/Pro (Codex)"
        )


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
    except Exception as exc:
        await server.stop()
        print(f"Could not reach Converse ({exc}). Pi was not started.", file=sys.stderr)
        return 1

    pi = PiRPC(_pi_argv(args.pi), cwd=Path.cwd())
    try:
        await pi.start()
        _require_pi_model(await pi.command("get_state"))
    except PiRPCError as exc:
        await pi.stop()
        await server.stop()
        print(f"Could not start Pi: {exc}", file=sys.stderr)
        return 1

    stopped = asyncio.Event()
    bridge = BrowserBridge(server.send_json_to_tab)
    router = AgentToolRouter(pi, bridge, handle=_session_handle())
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
    bridge.on_tool_call = lambda call: _spawn_tool(router, call)
    bridge.on_tool_cancel = router.handle_tool_cancel

    print(f"Converse Code reference session: {server.url}")
    if not args.no_browser:
        webbrowser.open(server.url)
    try:
        await stopped.wait()
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        if router.active_call_id:
            await router.handle_tool_cancel({"id": router.active_call_id})
        await pi.stop()
        await server.stop()
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="converse-code",
        description="A minimal Converse background-tool reference using Pi and Codex.",
    )
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument(
        "--pi", default=os.environ.get("CONVERSE_CODE_PI_CMD", DEFAULT_PI_CMD),
        help="Pi RPC command",
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
