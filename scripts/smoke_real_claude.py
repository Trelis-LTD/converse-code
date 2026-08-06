"""Integration smoke against the REAL claude CLI (no broker): launch it under
ClaudeHost with our Stop-hook settings, drive one long_task through ToolRouter,
verify the hook fires and the transcript-derived result comes back. Handles a
trust dialog via the menu path if one appears."""

import asyncio
import sys
import tempfile

from converse_code.hooks import write_settings
from converse_code.localserver import LocalServer
from converse_code.ptyhost import ClaudeHost
from converse_code.tools import ToolRouter


class Collector:
    def __init__(self):
        self.results, self.partials, self.progress = [], [], []

    async def send_tool_result(self, cid, content):
        self.results.append(content)

    async def send_tool_partial_result(self, cid, content, reply=False):
        self.partials.append(content)

    async def send_tool_progress(self, cid, note):
        self.progress.append(note)


async def main() -> None:
    server = LocalServer()
    port = await server.start(port=0)
    settings = write_settings(tempfile.mkdtemp(prefix="cc-smoke-"), server.hook_url("stop"))

    host = ClaudeHost(["claude", "--settings", str(settings)], attach_terminal=False)
    await host.start()
    sender = Collector()
    router = ToolRouter(host, sender, handle="cc-smoke")
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
        print("REAL-CLAUDE SMOKE:", "PASS" if ok else "FAIL")
        if not ok:
            print("--- screen ---")
            print("\n".join(l for l in host.snapshot() if l.strip()))
    finally:
        host.inject("/exit")
        try:
            await asyncio.wait_for(host.exited.wait(), 15)
        except asyncio.TimeoutError:
            await host.stop()
        await server.stop()


sys.exit(asyncio.run(main()))
