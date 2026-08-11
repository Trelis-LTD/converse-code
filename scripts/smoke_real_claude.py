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
    )
    router.POLL_S = 1.0
    submitted_prompt_ids = []
    stopped_prompt_ids = []

    async def on_hook(event, payload):
        print(f"HOOK {event}: keys={sorted(payload)} transcript_path={payload.get('transcript_path')}")
        if event == "user_prompt_submit" and payload.get("prompt_id"):
            submitted_prompt_ids.append(payload["prompt_id"])
        if event == "stop" and payload.get("prompt_id"):
            stopped_prompt_ids.append(payload["prompt_id"])
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

        # set_model owns inspection, selection, confirmation, and postcondition
        # verification. If Haiku is already selected, Sonnet guarantees that
        # this smoke test still exercises a real transition.
        selected = "Haiku"
        await router.handle_tool_call({
            "id": "model-set", "name": "set_model", "args": {"model": selected},
        })
        changed = sender.results[-1]
        if changed["data"]["last_action"].get("effect") == "already_selected":
            selected = "Sonnet"
            await router.handle_tool_call({
                "id": "model-set", "name": "set_model", "args": {"model": selected},
            })
            changed = sender.results[-1]
        await router.handle_tool_call({
            "id": "model-observe", "name": "observe_claude", "args": {},
        })
        menu_ok = (
            changed["data"]["last_action"]["status"] == "verified"
            and changed["data"]["last_action"]["completed"] is True
            and changed["data"]["last_action"]["effect"] == "model_changed"
            and sender.results[-1]["data"]["last_action"] == changed["data"]["last_action"]
        )
        print("model transition:", changed["speak"])
        if not menu_ok:
            print("--- screen after menu selection ---")
            print("\n".join(line for line in host.snapshot() if line.strip()))
        print("menu navigation:", "PASS" if menu_ok else "FAIL")

        # Exercise real interruption rather than only synthesizing hook order in
        # a unit test. Cancel as soon as Claude acknowledges the prompt, wait for
        # its matching Stop, then prove a following prompt completes cleanly.
        old_request = (
            "Run the command sleep 15, then reply with exactly old-finished. "
            "Do not do anything else."
        )
        canceled = asyncio.create_task(router.handle_tool_call({
            "id": "cancel-real", "name": "long_task", "args": {"request": old_request},
        }))
        deadline = asyncio.get_running_loop().time() + 10
        while not router._episode_prompt_ids and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.05)
        cancel_ids = set(router._episode_prompt_ids)
        if not cancel_ids:
            raise RuntimeError("real cancellation prompt was never acknowledged")
        await router.handle_tool_cancel({"type": "tool_cancel", "id": "cancel-real"})
        await canceled
        cancel_requested = router.state() == "canceling"
        deadline = asyncio.get_running_loop().time() + 15
        while router.state() == "canceling" and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.1)
        cancel_state = router.semantic_state()["last_action"] or {}
        settled_by_hook = cancel_ids.issubset(set(stopped_prompt_ids))
        settled_by_ui = cancel_state.get("effect") == "idle_ui_observed"
        cancel_ok = (
            cancel_requested and router.state() == "idle"
            and cancel_state.get("status") == "verified"
            and (settled_by_hook or settled_by_ui)
        )
        settlement = "Stop hook" if settled_by_hook else "idle UI"
        print(
            "real cancellation:", "PASS" if cancel_ok else "FAIL",
            f"(settled by {settlement})",
        )

        await router.handle_tool_call({
            "id": "after-cancel", "name": "long_task",
            "args": {"request": "Reply with exactly new-ok and do nothing else."},
        })
        followup = sender.results[-1]
        followup_ok = (
            "new-ok" in followup["speak"].lower()
            and "old-finished" not in followup["speak"].lower()
            and followup["data"]["phase"] == "idle"
            and followup["data"]["last_action"]["status"] == "verified"
        )
        print("post-cancel episode:", "PASS" if followup_ok else "FAIL")

        passed = ok and menu_ok and cancel_ok and followup_ok
        print("REAL-CLAUDE SMOKE:", "PASS" if passed else "FAIL")
        if not passed:
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
