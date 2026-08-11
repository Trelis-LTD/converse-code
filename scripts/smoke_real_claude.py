"""Integration smoke against the REAL claude CLI (no broker): launch it under
ClaudeHost with our Stop-hook settings, drive one long_task through ToolRouter,
verify the hook fires and the transcript-derived result comes back. Handles a
trust dialog via the menu path if one appears."""

import asyncio
import os
import sys
import subprocess
import tempfile
from pathlib import Path

from converse_code.hooks import write_settings
from converse_code.localserver import LocalServer
from converse_code.ptyhost import ClaudeHost
from converse_code import screen as screenmod
from converse_code.tools import ToolRouter


class Collector:
    def __init__(self):
        self.results, self.progress, self.context = [], [], []
        self.deferred, self.partials = [], []
        self.result_metadata = []

    async def send_tool_result(self, cid, content, **metadata):
        self.results.append(content)
        self.result_metadata.append(metadata)

    async def send_tool_progress(self, cid, note):
        self.progress.append(note)

    async def send_tool_deferred(self, cid, handle, status_label=None):
        self.deferred.append((cid, handle))

    async def send_tool_partial_result(self, cid, content, reply=False):
        self.partials.append((content, reply))

    async def send_context(self, text, role="context", reply=False):
        self.context.append((text, role, reply))


async def run_task(router, sender, call_id: str, request: str) -> dict:
    """Run one real Claude turn and require authoritative episode completion."""
    await router.handle_tool_call({
        "id": call_id, "name": "long_task", "args": {"request": request},
    })
    result = sender.results[-1]
    action = result.get("data", {}).get("last_action") or {}
    if action.get("status") == "completed":
        deadline = asyncio.get_running_loop().time() + 3
        while router.semantic_state()["phase"] != "idle" and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.1)
        # An HTTP permission allow can finish the tool before Ink repaints its
        # modal. Only after the matching verified Stop is it safe to dismiss
        # that stale overlay; never use this to approve pending work.
        if router.semantic_state()["phase"] != "idle" and router.menu():
            router.driver.send_key("escape")
            await asyncio.sleep(0.5)
    current = router.semantic_state()
    result["data"].update(current)
    if action.get("status") != "completed" or current.get("phase") != "idle":
        visible = "\n".join(line for line in router.driver.snapshot() if line.strip())
        raise RuntimeError(
            f"{call_id} did not complete cleanly: {result}\n--- screen ---\n{visible}"
        )
    return result


async def main() -> None:
    model_only = "--model-only" in sys.argv
    original_dir = Path.cwd()
    temp_root = Path(__file__).resolve().parents[1] / "tmp"
    temp_root.mkdir(exist_ok=True)
    project_temp = tempfile.TemporaryDirectory(prefix="cc-smoke-project-", dir=temp_root)
    settings_temp = tempfile.TemporaryDirectory(prefix="cc-smoke-settings-", dir=temp_root)
    project_dir = Path(project_temp.name)
    server = LocalServer()
    host = None
    try:
        subprocess.run(["git", "init", "-q", str(project_dir)], check=True)
        os.chdir(project_dir)
        await server.start(port=0)
        settings = write_settings(
            settings_temp.name,
            server.hook_url("stop"),
            server.hook_url("user_prompt_submit"),
            server.hook_url("permission_request"),
            server.hook_url("stop_failure"),
        )
        host = ClaudeHost(
            [
                "claude", "--model", "haiku", "--permission-mode", "auto",
                "--settings", str(settings), "--setting-sources", "",
                "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
                "--no-chrome", "--tools", "",
            ],
            attach_terminal=False,
        )
        await host.start()
    except BaseException:
        try:
            if host is not None:
                await host.stop()
        finally:
            await server.stop()
            os.chdir(original_dir)
            settings_temp.cleanup()
            project_temp.cleanup()
        raise
    original_inject = host.inject
    original_inject_command = host.inject_command
    original_send_key = host.send_key

    def logged_inject(text, submit_delay_s=None):
        print(f"PTY inject: {text!r} delay={submit_delay_s}")
        original_inject(text, **({"submit_delay_s": submit_delay_s} if submit_delay_s else {}))

    def logged_inject_command(text, submit_delay_s=None):
        print(f"PTY command: {text!r} delay={submit_delay_s}")
        original_inject_command(text, submit_delay_s=submit_delay_s or 0.4)

    def logged_send_key(key):
        print(f"PTY key: {key} keyboard_flags={host._screen_filter.keyboard_flags}")
        original_send_key(key)

    host.inject, host.inject_command, host.send_key = logged_inject, logged_inject_command, logged_send_key
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
        print(f"HOOK {event}: prompt={payload.get('prompt')!r} keys={sorted(payload)} transcript_path={payload.get('transcript_path')}")
        hook_cwd = payload.get("cwd")
        if hook_cwd and Path(hook_cwd).resolve() != project_dir.resolve():
            raise RuntimeError(f"Claude escaped disposable project: {hook_cwd}")
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

        print("state before task:", router.semantic_state()["phase"])
        initial_request = "Reply with exactly the word pong and do nothing else."
        result = await run_task(router, sender, "t1", initial_request)
        print("speak:", result["speak"])
        print("data:", result["data"])
        print("progress notes:", sender.progress)
        ok = result["data"]["phase"] == "idle" and "pong" in result["speak"].lower()
        print("prompt flow:", "PASS" if ok else "FAIL")
        if not ok:
            print("--- screen ---")
            print("\n".join(l for l in host.snapshot() if l.strip()))

        if model_only:
            # Verify the documented session-only direct model path.
            selected = "Sonnet"
            await router.handle_tool_call({
                "id": "model-set", "name": "set_model", "args": {"model": selected},
            })
            changed = sender.results[-1]
            changed_metadata = sender.result_metadata[-1]
            print("initial model result:", changed["speak"], changed["data"]["last_action"])

            await router.handle_tool_call({
                "id": "model-observe", "name": "observe_claude", "args": {},
            })
            menu_ok = (
                changed["data"]["last_action"]["status"] == "verified"
                and changed["data"]["last_action"]["completed"] is True
                and changed["data"]["last_action"]["effect"] in {"already_selected", "model_changed"}
                and changed["data"]["last_action"].get("postcondition_verified") is True
                and changed["data"]["last_action"].get("evidence", {}).get("model")
                    == selected.lower()
                and changed_metadata == {"outcome": "succeeded", "verified": True}
                and screenmod.detect_current_model(host.snapshot()) == selected.lower()
                and changed["data"]["phase"] == "idle"
                and changed["data"]["ui"]["kind"] == "none"
                and sender.results[-1]["data"]["last_action"] == changed["data"]["last_action"]
            )
            print("model selection:", changed["speak"])
            if not menu_ok:
                print("--- screen after menu selection ---")
                print("\n".join(line for line in host.snapshot() if line.strip()))
            print("session-only model switch:", "PASS" if menu_ok else "FAIL")
            if not menu_ok:
                raise RuntimeError("session-only model switch failed; refusing to continue past its modal")
            print("REAL-CLAUDE MODEL SWITCH: PASS")
            return

        # Exercise real interruption rather than only synthesizing hook order in
        # a unit test. Cancel as soon as Claude acknowledges the prompt, wait for
        # its matching Stop, then prove a following prompt completes cleanly.
        old_request = (
            "Work on a detailed internal answer for at least several seconds, then reply with exactly "
            "old-finished. Do not use tools."
        )
        canceled = asyncio.create_task(router.handle_tool_call({
            "id": "cancel-real", "name": "long_task", "args": {"request": old_request},
        }))
        deadline = asyncio.get_running_loop().time() + 10
        while not router._episode_prompt_ids and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.05)
        cancel_ids = set(router._episode_prompt_ids)
        if not cancel_ids:
            print("--- cancellation prompt admission diagnostics ---")
            print("needs_ready_gate:", router._needs_ready_gate)
            print("semantic_state:", router.semantic_state())
            print("screen:")
            print("\n".join(repr(line) for line in host.snapshot()))
            raise RuntimeError("real cancellation prompt was never acknowledged")
        await router.handle_tool_cancel({"type": "tool_cancel", "id": "cancel-real"})
        await canceled
        cancel_requested = router.semantic_state()["phase"] == "canceling"
        deadline = asyncio.get_running_loop().time() + 15
        while router.semantic_state()["phase"] == "canceling" and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.1)
        cancel_state = router.semantic_state()["last_action"] or {}
        settled_by_hook = cancel_ids.issubset(set(stopped_prompt_ids))
        settled_by_ui = cancel_state.get("effect") == "idle_ui_observed"
        cancel_ok = (
            cancel_requested and router.semantic_state()["phase"] == "idle"
            and cancel_state.get("status") == "verified"
            and (settled_by_hook or settled_by_ui)
        )
        settlement = "Stop hook" if settled_by_hook else "idle UI"
        print(
            "real cancellation:", "PASS" if cancel_ok else "FAIL",
            f"(settled by {settlement})",
        )
        if not cancel_ok:
            print("--- cancellation settlement diagnostics ---")
            print("state:", router.semantic_state()["phase"], "last_action:", cancel_state)
            print("screen:")
            print("\n".join(repr(line) for line in host.snapshot()))
            raise RuntimeError("real cancellation did not settle safely")

        await router.handle_tool_call({
            "id": "after-cancel", "name": "long_task",
            "args": {"request": "Reply with exactly new-ok and do nothing else."},
        })
        followup = sender.results[-1]
        followup_ok = (
            "new-ok" in followup["speak"].lower()
            and "old-finished" not in followup["speak"].lower()
            and followup["data"]["phase"] == "idle"
            and followup["data"]["last_action"]["status"] == "completed"
        )
        print("post-cancel episode:", "PASS" if followup_ok else "FAIL")

        passed = ok and cancel_ok and followup_ok
        print("REAL-CLAUDE SMOKE:", "PASS" if passed else "FAIL")
        if not passed:
            raise RuntimeError("real Claude smoke assertions failed")
    finally:
        try:
            # Never type /exit into an unexpected modal: Enter can accept its
            # destructive/default action. Terminate the disposable child.
            await host.stop()
        finally:
            await server.stop()
            os.chdir(original_dir)
            settings_temp.cleanup()
            project_temp.cleanup()


sys.exit(asyncio.run(asyncio.wait_for(main(), timeout=600)))
