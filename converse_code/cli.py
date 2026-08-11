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

from . import config, converse, hooks, selftest, tools
from .bridge import BrowserBridge
from .headless import HeadlessController, JsonLineBridge, stdin_reader
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
    if await converse.validate_key(key, url=url):
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
            f"Could not start the Converse session server on port {args.port}: {exc}\n"
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
        if not await converse.validate_key(api_key, url=args.broker_url):
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
    host = ClaudeHost(claude_argv, attach_terminal=True)

    handle = _session_handle()
    bridge = BrowserBridge(server.send_json_to_tab)
    router = tools.ToolRouter(host, bridge, handle=handle)
    router.on_status = server.send_json_to_tab
    server.on_hook = router.on_hook

    # The browser connects the official SDK straight to Converse. Python keeps
    # the persistent key and gives the page only a short-lived credential bound
    # to the browser's requested session ID.
    async def issue_credential(session_id: str) -> dict:
        credential = await converse.mint_session_credential(
            api_key, session_id, api_url=args.api_url,
        )
        return {
            **credential,
            "ws_url": args.broker_url,
            "tools": tools.manifest(),
        }

    async def tab_closed() -> None:
        await bridge.on_browser_disconnected()

    server.on_session_credential = issue_credential
    server.on_tab_json = bridge.handle_browser_message
    server.on_tab_closed = tab_closed
    bridge.on_tool_call = lambda call: _spawn_tool(router, call)
    bridge.on_tool_cancel = router.handle_tool_cancel

    print(f"Converse Code — session page: {url}   (session: {handle})")
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
        await server.stop()
        shutil.rmtree(scratch, ignore_errors=True)
    return host.returncode or 0


async def _run_headless(args) -> int:
    """Run Claude behind a JSONL stdin/stdout control protocol."""
    server = LocalServer()
    try:
        await server.start(port=args.port)
    except OSError as exc:
        print(f"Could not start the headless hook server on port {args.port}: {exc}", file=sys.stderr)
        return 1

    scratch = tempfile.mkdtemp(prefix="converse-code-")
    settings_path = hooks.write_settings(
        scratch,
        server.hook_url("stop"),
        server.hook_url("user_prompt_submit"),
        server.hook_url("permission_request"),
        server.hook_url("stop_failure"),
    )
    claude_argv = shlex.split(args.claude) + ["--settings", str(settings_path)]
    host = ClaudeHost(claude_argv, attach_terminal=False)
    bridge = JsonLineBridge(sys.stdout)
    router = tools.ToolRouter(host, bridge, handle=_session_handle())
    controller = HeadlessController(router, host, bridge)
    router.on_status = controller.status_event
    server.on_hook = router.on_hook

    started_at = asyncio.get_running_loop().time()
    requested_stop = False
    reader = None
    try:
        await host.start()
        reader = await stdin_reader()
        await bridge.emit({
            "type": "ready",
            "protocol": "converse-code-headless-v1",
            "handle": router.handle,
            "tools": [item["name"] for item in tools.manifest() if item["name"] != "end_session"],
        })
        control_task = asyncio.create_task(controller.read(reader))
        exit_task = asyncio.create_task(host.exited.wait())
        done, pending = await asyncio.wait(
            {control_task, exit_task}, return_when=asyncio.FIRST_COMPLETED,
        )
        requested_stop = control_task in done
        if requested_stop and not host.exited.is_set():
            await controller.cancel_tasks()
            await host.stop()
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
    finally:
        try:
            if reader is not None:
                reader.close()
            await controller.cancel_tasks()
            if not host.exited.is_set():
                await host.stop()
            if not requested_stop:
                _report_early_exit(host, asyncio.get_running_loop().time() - started_at)
        finally:
            try:
                await server.stop()
            finally:
                shutil.rmtree(scratch, ignore_errors=True)
    return 0 if requested_stop else (host.returncode or 0)


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
    parser = argparse.ArgumentParser(prog="converse-code", description="Talk or type to Claude Code via Converse, or use headless JSONL control.")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="local port for the session page")
    parser.add_argument("--claude", default=os.environ.get("CONVERSE_CODE_CLAUDE_CMD", DEFAULT_CLAUDE_CMD),
                        help="command used to launch Claude Code")
    parser.add_argument(
        "--broker-url", default=os.environ.get("CONVERSE_URL", converse.DEFAULT_WS_URL),
    )
    parser.add_argument(
        "--api-url", default=os.environ.get("CONVERSE_API_URL", converse.DEFAULT_API_URL),
        help="Converse HTTP API base used to mint browser session credentials",
    )
    parser.add_argument("--no-browser", action="store_true", help="don't auto-open the session page")
    parser.add_argument(
        "--headless", action="store_true",
        help="run Claude through the headless JSONL control protocol",
    )
    sub = parser.add_subparsers(dest="cmd")
    sub.add_parser("login", help="store and validate your Converse API key")
    sub.add_parser("selftest", help="check the audio path end to end, without a browser")
    args = parser.parse_args()

    _configure_logging(owns_terminal=args.cmd not in ("login", "selftest"))
    if args.cmd == "login":
        raise SystemExit(asyncio.run(_login(args.broker_url)))
    if args.cmd == "selftest":
        raise SystemExit(asyncio.run(selftest.run(args.broker_url)))
    runner = _run_headless if args.headless else _run
    raise SystemExit(asyncio.run(runner(args)))


if __name__ == "__main__":
    main()
