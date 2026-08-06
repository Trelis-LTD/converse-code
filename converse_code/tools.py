"""The five Converse tools and the idle/working/menu state machine.

Everything the voice brain can do to Claude Code goes through here; everything
it learns comes back as small {speak, data, handle} payloads (see DESIGN.md
Sections 9–10 — thin brain context, structure from the screen, prose from the
transcript).
"""

import asyncio
import logging
from pathlib import Path

from . import screen as screenmod
from . import transcript as tmod
from .ptyhost import KEYMAP

log = logging.getLogger(__name__)

LONG_TASK_DESCRIPTION = (
    "Send a coding instruction to Claude Code, an AI coding agent working in the user's "
    "project, and wait for it to finish (or report back if it is still working). Use for "
    "any request to write, edit, investigate, run, or explain code. Pass the user's "
    "instruction in 'request', preserving their technical wording exactly — file names, "
    "function names, flags, error text — compress filler, never editorialize. If Claude "
    "Code is already working, the instruction is queued behind the current task. Not for "
    "questions about progress (the latest status arrives with each result) and not for "
    "stopping work (use stop_long_task)."
)

STOP_DESCRIPTION = (
    "Interrupt Claude Code's current task immediately. This loses unfinished work — use it "
    "only when the user clearly wants the work stopped or redirected, never for progress "
    "questions or for adding instructions (long_task queues those safely). If it is "
    "ambiguous whether the user wants to stop, ask them first."
)

COMMAND_DESCRIPTION = (
    "Run a Claude Code slash command, e.g. /clear (reset context), /compact (compress "
    "context), /model (switch model — opens a menu). Pass the full command string starting "
    "with '/'. Any command the user names can be passed through. If a menu opens, its "
    "options come back in the result — then use select_option."
)

SELECT_DESCRIPTION = (
    "Choose an option in the menu currently shown by Claude Code (model picker, permission "
    "prompt, trust dialog...). Pass the option's text, or its number. Only valid while the "
    "state is 'menu'."
)

PRESS_KEY_DESCRIPTION = (
    "Low-level fallback: press a single key in Claude Code. One of: escape, enter, up, "
    "down, left, right, tab, shift-tab, ctrl-c. Prefer long_task / command / select_option; "
    "use this only when they don't fit."
)


def manifest() -> list[dict]:
    def tool(name, description, props=None, required=None, **flags):
        return {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": props or {},
                "required": required or [],
            },
            **flags,
        }

    return [
        tool("long_task", LONG_TASK_DESCRIPTION,
             {"request": {"type": "string", "description": "The coding instruction."}},
             ["request"], requires_permission=True, timeout=120),
        tool("stop_long_task", STOP_DESCRIPTION, timeout=15),
        tool("command", COMMAND_DESCRIPTION,
             {"command": {"type": "string", "description": "Slash command, starting with '/'."}},
             ["command"], timeout=15),
        tool("select_option", SELECT_DESCRIPTION,
             {"option": {"type": "string", "description": "Option text or number."}},
             ["option"], timeout=15),
        tool("press_key", PRESS_KEY_DESCRIPTION,
             {"key": {"type": "string", "enum": sorted(KEYMAP)}},
             ["key"], timeout=15),
    ]


class ToolRouter:
    HOLD_S = 110.0          # hold long_task open this long before resolving "still working"
    POLL_S = 2.0            # transcript/menu poll cadence while holding
    SETTLE_S = 1.2          # wait after command/select before reading the screen
    MAX_PROGRESS = 10       # protocol cap is 12/call; keep headroom

    def __init__(self, driver, sender, handle: str, project_dir: str | Path | None = None):
        """driver: ClaudeHost-like (inject/send_key/snapshot).
        sender: BrokerClient-like (send_tool_result/_partial_result/_progress)."""
        self.driver = driver
        self.sender = sender
        self.handle = handle
        self.project_dir = Path(project_dir) if project_dir else Path.cwd()
        self.working = False
        self.queue: list[str] = []
        self.transcript_path: Path | None = None
        self.last_assistant_text = ""
        self._offset = 0
        self._turn_done = asyncio.Event()
        self._interrupted = False
        self.on_status = None  # async callback(dict) → browser tab

    # -- events from the Stop hook -------------------------------------------

    async def on_hook(self, event: str, payload: dict) -> None:
        if event != "stop":
            return
        path = payload.get("transcript_path")
        if path:
            self.transcript_path = Path(path)
        # The Stop hook can fire before the final assistant entry is flushed to
        # the transcript file — the payload carries the text directly.
        msg = payload.get("last_assistant_message")
        if isinstance(msg, str) and msg.strip():
            self.last_assistant_text = msg.strip()
        if self.queue:
            self.queue.pop(0)  # next queued instruction starts automatically
            self.working = True
        else:
            self.working = False
        self._turn_done.set()
        await self._push_status()

    # -- dispatch --------------------------------------------------------------

    async def handle_tool_call(self, call: dict) -> None:
        name, call_id, args = call.get("name"), call.get("id"), call.get("args") or {}
        handlers = {
            "long_task": self._long_task,
            "stop_long_task": self._stop_long_task,
            "command": self._command,
            "select_option": self._select_option,
            "press_key": self._press_key,
        }
        handler = handlers.get(name)
        try:
            if handler is None:
                content = self._result(f"Unknown tool {name}.")
            else:
                content = await handler(call_id, args)
        except Exception:
            log.exception("tool %s failed", name)
            content = self._result("Something went wrong driving Claude Code; the session itself is still alive.")
        await self.sender.send_tool_result(call_id, content)
        await self._push_status()

    # -- state ------------------------------------------------------------------

    def menu(self) -> screenmod.Menu | None:
        return screenmod.detect_menu(self.driver.snapshot())

    def state(self) -> str:
        if self.menu():
            return "menu"
        return "working" if self.working else "idle"

    def _status_data(self, **extra) -> dict:
        data = {"state": self.state(), "queue": list(self.queue), **extra}
        menu = self.menu()
        if menu:
            data["menu_title"] = menu.title
            data["options"] = menu.options
            data["selected"] = menu.options[menu.selected] if menu.options else ""
        return data

    def _result(self, speak: str, **extra) -> dict:
        return {"speak": speak, "data": self._status_data(**extra), "handle": self.handle}

    async def _push_status(self) -> None:
        if self.on_status:
            await self.on_status({"type": "local", "event": "status", **self._status_data()})

    # -- tools -------------------------------------------------------------------

    async def _long_task(self, call_id: str, args: dict) -> dict:
        request = (args.get("request") or "").strip()
        if not request:
            return self._result("No instruction was given.")
        menu = self.menu()
        if menu:
            return self._result(
                f"Claude Code is showing a menu and needs an answer first: "
                f"{menu.title or 'a menu'} — options: {', '.join(menu.options)}."
            )

        self.driver.inject(request)
        if self.on_status:
            await self.on_status({"type": "local", "event": "injected", "text": request})

        if self.working:
            self.queue.append(request)
            return self._result(
                "Queued behind the current task; it will run next. The current task is still going."
            )

        self.working = True
        self._interrupted = False
        self._turn_done.clear()
        self.last_assistant_text = ""
        self._ensure_transcript()
        return await self._await_turn(call_id, self._offset, self.transcript_path)

    async def _await_turn(self, call_id: str, start_offset: int, start_path) -> dict:
        """Hold the call open: progress from the transcript tail, resolve on the
        Stop hook, a menu appearing, interruption, or the hold deadline."""
        deadline = asyncio.get_running_loop().time() + self.HOLD_S
        sent_progress = 0
        sent_midway_partial = False
        while True:
            try:
                await asyncio.wait_for(self._turn_done.wait(), timeout=self.POLL_S)
            except asyncio.TimeoutError:
                pass
            if self._turn_done.is_set():
                if self._interrupted:
                    return self._result("Stopped — the task was interrupted before finishing.")
                return self._turn_result(start_offset, start_path)

            menu = self.menu()
            if menu:
                return self._result(
                    f"Claude Code needs input: {menu.title or 'a menu is open'} — "
                    f"options: {', '.join(menu.options)}. Ask the user, then use select_option."
                )

            if sent_progress < self.MAX_PROGRESS:
                for note in self._new_progress_notes():
                    await self.sender.send_tool_progress(call_id, note)
                    sent_progress += 1
                    if sent_progress >= self.MAX_PROGRESS:
                        break

            now = asyncio.get_running_loop().time()
            if not sent_midway_partial and now > deadline - self.HOLD_S / 2:
                await self.sender.send_tool_partial_result(
                    call_id, self._result("Still working."), reply=False
                )
                sent_midway_partial = True
            if now >= deadline:
                return self._result(
                    "Still working on it — this is taking a while. Call long_task again with "
                    "a follow-up (or just to keep waiting) using the same handle."
                )

    def _turn_result(self, start_offset: int, start_path) -> dict:
        # The Stop hook may have pointed us at a different session file than the
        # one we started tailing (fresh session, stale glob) — offsets don't
        # carry across files.
        if self.transcript_path != start_path:
            start_offset = 0
        entries, self._offset = self._read_from(start_offset)
        summary = tmod.summarize_entries(entries)
        text = summary.text or self.last_assistant_text  # transcript can lag the hook
        speak = tmod.speak_summary(text) if text else "Done."
        extra = {}
        if summary.files:
            extra["files"] = summary.files[:10]
        return self._result(speak, **extra)

    async def _stop_long_task(self, _call_id: str, _args: dict) -> dict:
        if not self.working and not self.queue:
            return self._result("Nothing is running.")
        self._interrupted = True
        self.driver.send_key("escape")
        self.working = False
        self.queue.clear()
        self._turn_done.set()
        return self._result("Stopped. Unfinished work from that task is discarded.")

    async def _command(self, _call_id: str, args: dict) -> dict:
        cmd = (args.get("command") or "").strip()
        if not cmd.startswith("/"):
            return self._result("Commands must start with a slash, like /clear.")
        if self.menu():
            return self._result("A menu is open — answer it first with select_option.")
        self.driver.inject(cmd)
        if self.on_status:
            await self.on_status({"type": "local", "event": "injected", "text": cmd})
        await asyncio.sleep(self.SETTLE_S)
        menu = self.menu()
        if menu:
            return self._result(
                f"{cmd} opened a menu: {menu.title or 'options'} — {', '.join(menu.options)}. "
                f"Currently selected: {menu.options[menu.selected] if menu.options else 'unknown'}."
            )
        return self._result(f"Sent {cmd}.")

    async def _select_option(self, _call_id: str, args: dict) -> dict:
        wanted = (args.get("option") or "").strip()
        menu = self.menu()
        if not menu:
            return self._result("There is no menu open right now.")
        idx = screenmod.match_option(menu, wanted)
        if idx is None:
            return self._result(
                f"Couldn't find an option matching '{wanted}'. Options: {', '.join(menu.options)}."
            )
        delta = idx - menu.selected
        key = "down" if delta > 0 else "up"
        for _ in range(abs(delta)):
            self.driver.send_key(key)
            await asyncio.sleep(0.05)
        self.driver.send_key("enter")
        await asyncio.sleep(self.SETTLE_S)
        after = self.menu()
        if after:
            return self._result(
                f"Chose {menu.options[idx]}; another menu opened: {after.title or 'options'} — "
                f"{', '.join(after.options)}."
            )
        return self._result(f"Chose {menu.options[idx]}.")

    async def _press_key(self, _call_id: str, args: dict) -> dict:
        key = (args.get("key") or "").strip().lower()
        if key not in KEYMAP:
            return self._result(f"Unknown key '{key}'. Keys: {', '.join(sorted(KEYMAP))}.")
        self.driver.send_key(key)
        await asyncio.sleep(0.3)
        return self._result(f"Pressed {key}.")

    # -- transcript tailing ----------------------------------------------------

    def _ensure_transcript(self) -> None:
        """Before the first Stop hook we don't have transcript_path yet — find the
        newest transcript for this project under ~/.claude/projects/."""
        if self.transcript_path and self.transcript_path.exists():
            return
        munged = str(self.project_dir.resolve()).replace("/", "-").replace(".", "-").replace("_", "-")
        project_dir = Path.home() / ".claude" / "projects" / munged
        candidates = sorted(project_dir.glob("*.jsonl"), key=lambda p: p.stat().st_mtime)
        if candidates:
            self.transcript_path = candidates[-1]
            self._offset = 0

    def _read_from(self, offset: int) -> tuple[list[dict], int]:
        if not self.transcript_path:
            self._ensure_transcript()
        if not self.transcript_path:
            return [], offset
        return tmod.read_new(self.transcript_path, offset)

    def _new_progress_notes(self) -> list[str]:
        entries, self._offset = self._read_from(self._offset)
        notes = []
        for entry in entries:
            note = tmod.progress_note(entry)
            if note and note not in notes:
                notes.append(note)
        return notes[:3]
