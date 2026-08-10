"""Integration smoke against the REAL claude CLI (no broker): launch it under
ClaudeHost with our Stop-hook settings, drive one long_task through ToolRouter,
verify the hook fires and the transcript-derived result comes back. Handles a
trust dialog via the menu path if one appears."""

import asyncio
import os
import sys
import tempfile
from pathlib import Path

from converse_code.hooks import write_settings
from converse_code.localserver import LocalServer
from converse_code.ptyhost import ClaudeHost
from converse_code.tools import ToolRouter


class Collector:
    def __init__(self):
        self.results, self.progress, self.context = [], [], []
        self.deferred, self.partials = [], []

    async def send_tool_result(self, cid, content):
        self.results.append(content)

    async def send_tool_progress(self, cid, note):
        self.progress.append(note)

    async def send_tool_deferred(self, cid, handle, status_label=None):
        self.deferred.append((cid, handle))

    async def send_tool_partial_result(self, cid, content, reply=False):
        self.partials.append((content, reply))

    async def send_context(self, text, role="context", reply=False):
        self.context.append((text, role, reply))


async def main() -> None:
    original_dir = Path.cwd()
    project_dir = Path(tempfile.mkdtemp(prefix="cc-smoke-project-"))
    os.chdir(project_dir)
    server = LocalServer()
    port = await server.start(port=0)
    settings = write_settings(
        tempfile.mkdtemp(prefix="cc-smoke-"),
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
    sender = Collector()
    router = ToolRouter(
        host,
        sender,
        handle="cc-smoke",
        project_dir=project_dir,
        verify_submissions=True,
    )
    router.POLL_S = 1.0
    async def on_hook(event, payload):
        print(f"HOOK {event}: keys={sorted(payload)} transcript_path={payload.get('transcript_path')}")
        await router.on_hook(event, payload)

    server.on_hook = on_hook

    try:
        await asyncio.sleep(6)  # let the TUI boot
        menu = router.menu()
        if menu:
            print(f"startup menu: {menu.title!r} options={menu.options}")
            await router.handle_tool_call({"id": "m", "name": "select_option", "args": {"option": "yes"}})
            print("answered menu:", sender.results[-1]["speak"])
            await asyncio.sleep(3)

        print("state before task:", router.state())
        await router.handle_tool_call({
            "id": "t1", "name": "long_task",
            "args": {"request": "Reply with exactly the word pong and do nothing else."},
        })
        result = sender.results[-1]
        print("speak:", result["speak"])
        print("data:", result["data"])
        print("progress notes:", sender.progress)
        ok = "pong" in result["speak"].lower() and result["data"]["state"] == "idle"
        print("prompt flow:", "PASS" if ok else "FAIL")
        if not ok:
            print("--- screen ---")
            print("\n".join(l for l in host.snapshot() if l.strip()))

        await router.handle_tool_call({
            "id": "model-menu", "name": "command", "args": {"command": "/model"},
        })
        menu_result = sender.results[-1]
        menu = router.menu()
        menu_ok = menu is not None and bool(menu.options)
        print("model menu:", menu_result["speak"])
        if menu_ok:
            before = menu.options[menu.selected]
            # A regression once made selection look successful while Claude was
            # still one interaction behind. Choose a genuinely different model,
            # then reopen /model and verify Claude itself reports that choice.
            selected = next(option for option in reversed(menu.options) if option != before)
            await router.handle_tool_call({
                "id": "model-select",
                "name": "select_option",
                "args": {"option": selected},
            })
            await router.handle_tool_call({
                "id": "model-confirm", "name": "command", "args": {"command": "/model"},
            })
            confirmed = router.menu()
            def model_name(option: str) -> str:
                lowered = option.lower()
                return next(
                    (name for name in ("default", "opus", "fable", "sonnet", "haiku")
                     if name in lowered),
                    lowered.replace("✔", "").strip(),
                )
            menu_ok = (
                confirmed is not None
                and bool(confirmed.options)
                and model_name(confirmed.options[confirmed.selected]) == model_name(selected)
            )
            if confirmed is not None:
                host.send_key("escape")
            if not menu_ok:
                print("--- screen after menu selection ---")
                print("\n".join(line for line in host.snapshot() if line.strip()))
        print("menu navigation:", "PASS" if menu_ok else "FAIL")
        print("REAL-CLAUDE SMOKE:", "PASS" if ok and menu_ok else "FAIL")
        if not (ok and menu_ok):
            raise RuntimeError("real Claude smoke assertions failed")
    finally:
        host.inject("/exit")
        try:
            await asyncio.wait_for(host.exited.wait(), 15)
        except asyncio.TimeoutError:
            await host.stop()
        await server.stop()
        os.chdir(original_dir)


sys.exit(asyncio.run(main()))
