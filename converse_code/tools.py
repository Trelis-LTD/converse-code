"""The Converse tools and the idle/working/menu state machine.

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
from .ptyhost import KEYMAP, sanitize
from .tracelog import trace

log = logging.getLogger(__name__)

# long_task is a deferred tool: `timeout` is only the acknowledgement deadline
# (we defer within seconds of a confirmed injection); once deferred, the job
# lives under deferred_timeout and the terminal result follows whenever Claude
# finishes.
TOOL_TIMEOUT_S = 600
DEFERRED_TIMEOUT_S = 7200

LONG_TASK_DESCRIPTION = (
    "Send an instruction to Claude Code, an AI coding agent working in the user's project. "
    "Claude Code can do anything a developer at this terminal could: read, write, and run "
    "code, use git, open files or apps, and answer questions about the project. Call this "
    "whenever the user asks for something to be done or answered from the project, passing "
    "their instruction in 'request' with their technical wording preserved exactly — file "
    "names, function names, flags, error text — compress filler, never editorialize. Work "
    "runs in the background: meaningful milestones and completion are announced "
    "automatically. Start only a new turn with this tool; if Claude Code is already working, "
    "use steer_task to add guidance to that turn. Not for questions about progress (status "
    "arrives automatically) and not "
    "for stopping work (Converse manages cancellation of the pending job)."
)

STEER_TASK_DESCRIPTION = (
    "Add a follow-up instruction to the Claude Code task that is already running. Use this only "
    "when the user wants to refine, redirect, or add requirements to current work. Preserve their "
    "technical wording exactly. This steers the active turn; it does not queue a separate task. "
    "If Claude Code is idle, use long_task instead."
)

OBSERVE_DESCRIPTION = (
    "Inspect Claude Code's authoritative current UI state without changing it. Use this when the "
    "user asks what is happening, challenges a claimed result, or when prior actions may have "
    "been interrupted. Returns idle/working/canceling/menu state, the active task, open menu, "
    "selected option, and the last verified action."
)

SET_MODEL_DESCRIPTION = (
    "Change Claude Code's model as one verified operation. Pass the requested model name. This "
    "opens the picker, selects and confirms the model, then reopens the picker to verify the "
    "actual selected model. Never use command('/model') to claim a model was changed."
)

COMMAND_DESCRIPTION = (
    "Run a Claude Code slash command, e.g. /clear (reset context), /compact (compress "
    "context), /model (safely inspect the selected model; use set_model to change models). Pass the "
    "full command string starting with '/'. Any command the user names can be passed through. "
    "If a menu opens, its options come back in the result; opening a menu is never completion."
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

END_SESSION_DESCRIPTION = (
    "End the current Converse voice session after a brief goodbye. Use only when the user "
    "explicitly asks to end, close, leave, or hang up the voice session. This stops the "
    "microphone and voice connection but leaves Claude Code running in the terminal."
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
             ["request"], timeout=TOOL_TIMEOUT_S,
             deferred=True, deferred_timeout=DEFERRED_TIMEOUT_S,
             notify_on_complete=True,
             status_label="Claude Code task"),
        tool("steer_task", STEER_TASK_DESCRIPTION,
             {"request": {"type": "string", "description": "Guidance for the active task."}},
             ["request"], timeout=15),
        tool("observe_claude", OBSERVE_DESCRIPTION, timeout=15),
        tool("set_model", SET_MODEL_DESCRIPTION,
             {"model": {"type": "string", "description": "Requested model name."}},
             ["model"], timeout=30),
        tool("command", COMMAND_DESCRIPTION,
             {"command": {"type": "string", "description": "Slash command, starting with '/'."}},
             ["command"], timeout=15),
        tool("select_option", SELECT_DESCRIPTION,
             {"option": {"type": "string", "description": "Option text or number."}},
             ["option"], timeout=15),
        tool("press_key", PRESS_KEY_DESCRIPTION,
             {"key": {"type": "string", "enum": sorted(KEYMAP)}},
             ["key"], timeout=15),
        tool("end_session", END_SESSION_DESCRIPTION, timeout=15),
    ]


class ToolRouter:
    HOLD_S = DEFERRED_TIMEOUT_S - 120   # resolve before the broker expires the deferred job
    POLL_S = 2.0            # transcript/menu poll cadence while monitoring
    SETTLE_S = 1.2          # wait after command/select before reading the screen
    MAX_PROGRESS = 10       # protocol cap is 12/call; keep headroom
    MAX_PARTIALS = 6        # protocol cap is 8/call; keep headroom
    MENU_RESERVE = 2        # partial budget only blocking menus may spend
    SUBMIT_ACK_S = 2.0      # UserPromptSubmit should arrive almost immediately
    SUBMIT_ATTEMPTS = 3     # initial submit plus two bounded Enter retries
    CANCEL_GRACE_S = 0.3    # let the submitted prompt leave the composer
    CANCEL_POLL_S = 0.1
    CANCEL_RETRY_S = 0.5    # an initial Escape can race prompt activation
    CANCEL_ESCAPE_RETRIES = 3
    CANCEL_IDLE_SAMPLES = 3 # avoid accepting a transient repaint as settled

    def __init__(self, driver, sender, handle: str, project_dir: str | Path | None = None,
                 verify_submissions: bool = False):
        """driver: ClaudeHost-like (inject/send_key/snapshot).
        sender: BrowserBridge-like (tool results/progress and context injection)."""
        self.driver = driver
        self.sender = sender
        self.handle = handle
        self.project_dir = Path(project_dir) if project_dir else Path.cwd()
        self.working = False
        self.queue: list[str] = []
        self.transcript_path: Path | None = None
        self.session_id: str | None = None
        self.last_assistant_text = ""
        self._offset = 0
        self._turn_done = asyncio.Event()
        self._interrupted = False
        self._active_call_id: str | None = None
        self._server_canceled: set[str] = set()
        self._suppress_next_stop_notification = False
        self._voice_owed = False  # a resolved voice call still owes its outcome out loud
        self._verify_submissions = verify_submissions
        self._submit_lock = asyncio.Lock()
        self._prompt_submitted = asyncio.Event()
        self._expected_prompt: str | None = None
        self._turn_failure = ""
        self._active_request: str | None = None
        self._last_action: dict | None = None
        self._episode_prompt_ids: set[str] = set()
        self._canceled_prompt_ids: set[str] = set()
        self._canceling_prompt_ids: set[str] = set()
        self._known_model: str | None = None
        self._known_model_source: str | None = None
        self._cancel_watch_task: asyncio.Task | None = None
        self.on_status = None  # async callback(dict) → browser tab

    # -- events from the Stop hook -------------------------------------------

    async def on_hook(self, event: str, payload: dict) -> None:
        if event == "user_prompt_submit":
            prompt = payload.get("prompt")
            if isinstance(prompt, str) and sanitize(prompt) == self._expected_prompt:
                prompt_id = payload.get("prompt_id")
                if isinstance(prompt_id, str) and prompt_id:
                    self._episode_prompt_ids.add(prompt_id)
                self._prompt_submitted.set()
            elif isinstance(prompt, str) and self._expected_prompt is None:
                prompt_id = payload.get("prompt_id")
                self.working = True
                self._active_request = sanitize(prompt)
                self._episode_prompt_ids.clear()
                if isinstance(prompt_id, str) and prompt_id:
                    self._episode_prompt_ids.add(prompt_id)
                self._last_action = {
                    "action": "terminal_task", "status": "pending",
                    "effect": "working", "completed": False,
                }
                await self._push_status()
            return
        if event == "permission_request":
            # HTTP hooks run before Claude paints the interactive prompt. Return
            # promptly, then inspect the settled screen and wake voice only for
            # terminal-originated work; an open long_task already polls menus.
            asyncio.create_task(self._announce_permission_request(payload))
            return
        if event == "stop_failure":
            await self._on_stop_failure(payload)
            return
        if event != "stop":
            return
        prompt_id = payload.get("prompt_id")
        if isinstance(prompt_id, str) and prompt_id in self._canceled_prompt_ids:
            was_canceling = prompt_id in self._canceling_prompt_ids
            self._canceling_prompt_ids.discard(prompt_id)
            if was_canceling:
                if not self._canceling_prompt_ids:
                    self._active_request = None
                self._last_action = {
                    "action": "cancel_task", "status": "verified",
                    "effect": "stopped", "completed": True,
                }
            trace("canceled_stop_ignored", prompt_id=prompt_id)
            if was_canceling:
                await self._push_status()
            return
        if (
            (self._active_call_id is not None or self.working)
            and self._episode_prompt_ids
            and isinstance(prompt_id, str)
            and prompt_id not in self._episode_prompt_ids
        ):
            trace("unrelated_stop_ignored", prompt_id=prompt_id)
            return
        voice_call_was_waiting = self._active_call_id is not None
        work_was_active = self.working
        suppress_notification = self._suppress_next_stop_notification
        self._suppress_next_stop_notification = False
        # The hook tells us authoritatively which session/file is ours; that
        # replaces the mtime guess _ensure_transcript had to make beforehand.
        session_id = payload.get("session_id")
        path = payload.get("transcript_path")
        if path:
            new_path = Path(path)
            if new_path != self.transcript_path:
                self._offset = 0
            self.transcript_path = new_path
        if session_id:
            self.session_id = session_id
        # The Stop hook can fire before the final assistant entry is flushed to
        # the transcript file — the payload carries the text directly.
        msg = payload.get("last_assistant_message")
        hook_text = msg.strip() if isinstance(msg, str) and msg.strip() else ""
        if hook_text:
            self.last_assistant_text = hook_text
        voice_owed = self._voice_owed
        self._voice_owed = False
        self.working = False
        self._active_request = None
        self._episode_prompt_ids.clear()
        if voice_call_was_waiting:
            self._last_action = {
                "action": "long_task", "status": "verified",
                "effect": "completed", "completed": True,
            }
        elif work_was_active:
            self._last_action = {
                "action": "terminal_task", "status": "verified",
                "effect": "completed", "completed": True,
            }
        self._turn_done.set()
        await self._push_status()
        if not voice_call_was_waiting and not suppress_notification:
            await self._wake_voice_for_terminal_turn(hook_text, announce=voice_owed)

    async def _on_stop_failure(self, payload: dict) -> None:
        waiting = self._active_call_id is not None
        detail = payload.get("error_details") or payload.get("error") or "unknown Claude error"
        self._turn_failure = str(detail)
        self.working = False
        self.queue.clear()
        self._voice_owed = False  # the error itself is the announcement
        self._turn_done.set()
        await self._push_status()
        if not waiting:
            trace("inject_context", reason="stop_failure", detail=self._turn_failure)
            await self.sender.send_context(
                f"Claude Code stopped because of an error: {self._turn_failure}. "
                "Tell the user briefly and suggest trying again after fixing the error.",
                role="context",
                reply=True,
            )

    async def _wake_voice_for_terminal_turn(self, hook_text: str = "",
                                            announce: bool = False) -> None:
        """Keep voice current on an episode no open tool call is watching.

        announce=True is for voice-initiated work whose call already resolved
        because the tracking window closed: the user asked out
        loud and never heard the outcome, so completion is a milestone. A turn
        the user typed at the terminal was read there — telemetry: inject
        silently so the brain is current the moment they ask, without narrating
        over their shoulder.
        """
        text = hook_text
        if not text:
            entries, self._offset = self._read_from(self._offset)
            text = tmod.summarize_entries(entries).text
        summary = tmod.speak_summary(text) if text else "Claude Code finished the terminal task."
        trace("inject_context", reason="episode_done", summary=summary, announce=announce)
        if announce:
            await self.sender.send_context(
                "Claude Code finished the voice-requested task. Briefly tell the "
                f"user it finished and summarize this update: {summary}",
                role="context",
                reply=True,
            )
        else:
            await self.sender.send_context(
                "Claude Code finished work entered directly in the terminal. "
                f"Do not announce this unless asked; the update was: {summary}",
                role="context",
                reply=False,
            )

    async def _announce_permission_request(self, payload: dict) -> None:
        await asyncio.sleep(min(self.SETTLE_S, 0.5))
        await self._push_status()
        if self._active_call_id is not None:
            return
        menu = self.menu()
        # Auto mode also emits PermissionRequest immediately before an automatic
        # denial, where no interactive menu is ever painted. Only wake voice for
        # a decision the user can actually make.
        if menu is None:
            return
        tool_name = payload.get("tool_name")
        detail = f" for {tool_name}" if isinstance(tool_name, str) and tool_name else ""
        options = f" Options: {', '.join(menu.options)}." if menu.options else ""
        trace("inject_context", reason="permission_request", tool=tool_name, options=menu.options)
        await self.sender.send_context(
            f"Claude Code is waiting for permission{detail}.{options} "
            "Tell the user briefly and ask which option they want; do not choose for them.",
            role="context",
            reply=True,
        )

    # -- dispatch --------------------------------------------------------------

    async def handle_tool_call(self, call: dict) -> None:
        name, call_id, args = call.get("name"), call.get("id"), call.get("args") or {}
        trace("tool_call", id=call_id, name=name, args=args)
        handlers = {
            "long_task": self._long_task,
            "steer_task": self._steer_task,
            "observe_claude": self._observe_claude,
            "set_model": self._set_model,
            "command": self._command,
            "select_option": self._select_option,
            "press_key": self._press_key,
            "end_session": self._end_session,
        }
        handler = handlers.get(name)
        try:
            if self._canceling_prompt_ids and name not in {"observe_claude", "end_session"}:
                content = self._result(
                    "Claude Code is still stopping the interrupted task. Wait for it to reach "
                    "idle before starting another action."
                )
            elif handler is None:
                content = self._result(f"Unknown tool {name}.")
            else:
                content = await handler(call_id, args)
        except Exception:
            log.exception("tool %s failed", name)
            content = self._result("Something went wrong driving Claude Code; the session itself is still alive.")
        # The host work is complete before its result crosses the network. Stop
        # treating it as cancellable now, so a late cancel cannot press Escape
        # against a following Claude turn while this send is in flight.
        if self._active_call_id == call_id:
            self._active_call_id = None
        if call_id in self._server_canceled:
            self._server_canceled.discard(call_id)
            self._interrupted = False
            trace("tool_result_dropped_after_cancel", id=call_id, name=name)
        else:
            trace("tool_result", id=call_id, name=name, content=content)
            await self.sender.send_tool_result(call_id, content)
        await self._push_status()

    async def handle_tool_cancel(self, call: dict) -> None:
        """Honor Converse's managed cancellation for the matching pending job."""
        call_id = call.get("id")
        trace("tool_cancel", id=call_id, active=self._active_call_id)
        if not call_id or call_id != self._active_call_id:
            return
        self._server_canceled.add(call_id)
        self._interrupted = True
        if self._episode_prompt_ids:
            self._canceled_prompt_ids.update(self._episode_prompt_ids)
            self._canceling_prompt_ids.update(self._episode_prompt_ids)
            if self._cancel_watch_task is None or self._cancel_watch_task.done():
                self._cancel_watch_task = asyncio.create_task(
                    self._watch_cancellation(set(self._episode_prompt_ids))
                )
        else:
            self._suppress_next_stop_notification = True
        self.driver.send_key("escape")
        self.working = False
        self.queue.clear()
        self._voice_owed = False  # canceled work owes no completion
        self._last_action = {
            "action": "cancel_task", "status": "pending",
            "effect": "cancel_requested", "completed": False,
        }
        self._turn_done.set()
        await self._push_status()

    async def _watch_cancellation(self, prompt_ids: set[str]) -> None:
        """Settle an interruption from visible idle state when Claude emits no Stop hook."""
        await asyncio.sleep(self.CANCEL_GRACE_S)
        idle_samples = 0
        escape_retries = 0
        last_escape = 0.0
        while prompt_ids & self._canceling_prompt_ids:
            if screenmod.is_idle(self.driver.snapshot()):
                idle_samples += 1
                if idle_samples >= self.CANCEL_IDLE_SAMPLES:
                    self._canceling_prompt_ids.difference_update(prompt_ids)
                    if not self._canceling_prompt_ids:
                        self._active_request = None
                    self._last_action = {
                        "action": "cancel_task", "status": "verified",
                        "effect": "idle_ui_observed", "completed": True,
                    }
                    trace("canceled_idle_observed", prompt_ids=sorted(prompt_ids))
                    await self._push_status()
                    return
            else:
                idle_samples = 0
                now = asyncio.get_running_loop().time()
                if (
                    escape_retries < self.CANCEL_ESCAPE_RETRIES
                    and now - last_escape >= self.CANCEL_RETRY_S
                ):
                    self.driver.send_key("escape")
                    escape_retries += 1
                    last_escape = now
                    trace(
                        "cancel_escape_retried", prompt_ids=sorted(prompt_ids),
                        attempt=escape_retries,
                    )
            await asyncio.sleep(self.CANCEL_POLL_S)

    # -- state ------------------------------------------------------------------

    def menu(self) -> screenmod.Menu | None:
        return screenmod.detect_menu(self.driver.snapshot())

    def state(self) -> str:
        if self._canceling_prompt_ids:
            return "canceling"
        if self.menu():
            return "menu"
        return "working" if self.working else "idle"

    def semantic_state(self) -> dict:
        menu = self.menu()
        state = self.state()
        phase = "awaiting_input" if state == "menu" else state
        ui = {"kind": "none"}
        if menu:
            ui = {
                "kind": "model_picker" if self._is_model_menu(menu) else "menu",
                "title": menu.title,
                "options": list(menu.options),
                "selected": menu.options[menu.selected] if menu.options else "",
            }
        visible_model = None
        if menu and self._is_model_menu(menu) and menu.options:
            visible_model = self._model_name(menu.options[menu.selected])
        else:
            visible_model = screenmod.detect_model(self.driver.snapshot())
        if visible_model:
            if visible_model != self._known_model or self._known_model_source != "verified":
                self._known_model_source = "visible_ui"
            self._known_model = visible_model
        model = None
        if self._known_model:
            model = {
                "name": self._known_model,
                "source": self._known_model_source or "remembered",
            }
        return {
            "phase": phase,
            "active_task": self._active_request,
            "ui": ui,
            "model": model,
            "last_action": dict(self._last_action) if self._last_action else None,
        }

    def _status_data(self, **extra) -> dict:
        data = {
            "state": self.state(), "queue": list(self.queue),
            **self.semantic_state(), **extra,
        }
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
        # Guard the exact text that will be typed: sanitize() strips control
        # characters, so testing the raw string would let "\x01!ls" reach the
        # PTY as "!ls".
        request = sanitize((args.get("request") or "").strip())
        if not request:
            return self._result("No instruction was given.")
        # Voice input reaches the machine only as natural-language instructions
        # to Claude Code. A leading '!' is the TUI's raw-shell mode: it would
        # bypass Claude Code's permission system entirely (and never fires the
        # UserPromptSubmit hook, so submission could not be confirmed anyway).
        if request.startswith("!"):
            return self._result(
                "Raw shell commands are not allowed over voice. Phrase it as a plain "
                "instruction instead — Claude Code will run the command itself."
            )
        if request.startswith("/"):
            return self._result(
                "That looks like a slash command — use the command tool for it."
            )
        menu = self.menu()
        if menu:
            return self._result(
                f"Claude Code is showing a menu and needs an answer first: "
                f"{menu.title or 'a menu'} — options: {', '.join(menu.options)}."
            )

        if self.working:
            return self._result(
                "Claude Code is already working. Use steer_task to add guidance to the current "
                "task, or wait for it to finish before starting another task."
            )

        # Arm completion before touching the PTY. A trivial Claude turn can
        # emit UserPromptSubmit and Stop back-to-back; clearing _turn_done
        # after injection would lose that Stop and wait until the deadline.
        self.working = True
        self._interrupted = False
        self._turn_done.clear()
        self.last_assistant_text = ""
        self._turn_failure = ""
        self._active_call_id = call_id
        self._active_request = request
        self._episode_prompt_ids.clear()
        self._last_action = {
            "action": "long_task", "status": "pending",
            "effect": "submitting", "completed": False,
        }
        self._ensure_transcript()
        start_offset, start_path = self._offset, self.transcript_path

        if not await self._inject_and_confirm(request):
            self.working = False
            self._active_call_id = None
            self._active_request = None
            self._last_action = {
                "action": "long_task", "status": "failed",
                "effect": "submission_unverified", "completed": False,
            }
            return self._result(
                "I put the instruction into Claude Code, but couldn't confirm it was submitted. "
                "The text may still be visible in the terminal input; press Enter there or try again."
            )
        if self.on_status:
            await self.on_status({"type": "local", "event": "injected", "text": request})
        self._last_action = {
            "action": "long_task", "status": "pending",
            "effect": "working", "completed": False,
        }

        # Detach from the voice turn: the brain closes its reply naturally now,
        # and notify_on_complete announces the terminal result when it lands.
        trace("tool_deferred", id=call_id)
        await self.sender.send_tool_deferred(
            call_id, f"{self.handle}-{call_id}", status_label="Claude Code task"
        )
        return await self._await_turn(call_id, start_offset, start_path)

    async def _steer_task(self, _call_id: str, args: dict) -> dict:
        request = sanitize((args.get("request") or "").strip())
        if not request:
            return self._result("No steering instruction was given.")
        if request.startswith("!"):
            return self._result(
                "Raw shell commands are not allowed over voice. Phrase the guidance as a plain "
                "instruction instead."
            )
        if request.startswith("/"):
            return self._result("That looks like a slash command — use the command tool for it.")
        menu = self.menu()
        if menu:
            return self._result(
                f"Claude Code needs the open menu answered before it can be steered: "
                f"{menu.title or 'a menu'} — options: {', '.join(menu.options)}."
            )
        if not self.working:
            return self._result("Claude Code is idle. Use long_task to start new work.")
        if not await self._inject_and_confirm(request):
            return self._result(
                "I added the guidance to Claude Code, but couldn't confirm it was submitted. "
                "It may still be visible in the terminal input."
            )
        if self.on_status:
            await self.on_status({"type": "local", "event": "injected", "text": request})
        return self._result("Added that guidance to the current Claude Code task.")

    async def _observe_claude(self, _call_id: str, _args: dict) -> dict:
        snapshot = self.semantic_state()
        phase, ui = snapshot["phase"], snapshot["ui"]
        if ui["kind"] == "model_picker":
            speak = (
                f"Claude Code is showing the model picker. Currently selected: "
                f"{ui['selected'] or 'unknown'}. No model changes should be claimed from "
                "this observation alone."
            )
        elif ui["kind"] == "menu":
            speak = (
                f"Claude Code is awaiting input in {ui['title'] or 'a menu'}. "
                f"Currently selected: {ui['selected'] or 'unknown'}."
            )
        elif phase == "working":
            speak = f"Claude Code is working on: {snapshot['active_task'] or 'the active task'}."
        elif phase == "canceling":
            speak = "Claude Code is still stopping the interrupted task; it is not idle yet."
        else:
            model = snapshot["model"]
            if model and model["source"] == "verified":
                suffix = f" The last verified model is {model['name']}."
            elif model:
                suffix = f" The visible Claude UI reports model {model['name']}."
            else:
                suffix = ""
            speak = f"Claude Code is idle with no menu open.{suffix}"
        return self._result(speak)

    async def _set_model(self, _call_id: str, args: dict) -> dict:
        wanted = (args.get("model") or "").strip()
        if not wanted:
            return self._result("No model was requested.")
        if self.working:
            return self._result("Claude Code is working. Wait until it is idle before changing model.")

        menu = self.menu()
        if menu and not self._is_model_menu(menu):
            return self._result(
                f"Claude Code needs the current menu answered first: {menu.title or 'options'}."
            )
        if menu is None:
            self.driver.inject("/model")
            if self.on_status:
                await self.on_status({"type": "local", "event": "injected", "text": "/model"})
            await asyncio.sleep(self.SETTLE_S)
            menu = self.menu()
        if not self._is_model_menu(menu):
            self._last_action = {
                "action": "set_model", "status": "failed",
                "effect": "picker_not_found", "completed": False,
            }
            return self._result("Couldn't open and verify Claude Code's model picker.")

        before = self._model_name(menu.options[menu.selected])
        idx = screenmod.match_option(menu, wanted)
        if idx is None:
            self._last_action = {
                "action": "set_model", "status": "failed",
                "effect": "model_not_found", "completed": False,
            }
            return self._result(
                f"Couldn't find a model matching '{wanted}'. Options: {', '.join(menu.options)}."
            )
        target = self._model_name(menu.options[idx])
        if before == target:
            self.driver.send_key("escape")
            await asyncio.sleep(self.SETTLE_S)
            self._known_model = target
            self._known_model_source = "verified"
            self._last_action = {
                "action": "set_model", "status": "verified", "effect": "already_selected",
                "completed": True, "from": before, "to": target,
            }
            return self._result(f"Verified that {menu.options[idx]} is already selected.")

        await self._choose_menu_index(menu, idx)
        after = self.menu()
        if self._is_matching_model_confirmation(after, menu.options[idx]):
            yes_idx = next(
                i for i, option in enumerate(after.options)
                if option.lower().lstrip("0123456789. ").startswith("yes")
            )
            await self._choose_menu_index(after, yes_idx)
        elif after:
            self._last_action = {
                "action": "set_model", "status": "failed",
                "effect": "unexpected_confirmation", "completed": False,
            }
            return self._result(
                f"Couldn't verify the model change because another menu is open: "
                f"{after.title or 'options'}."
            )

        self.driver.inject("/model")
        if self.on_status:
            await self.on_status({"type": "local", "event": "injected", "text": "/model"})
        await asyncio.sleep(self.SETTLE_S)
        verified_menu = self.menu()
        actual = (
            self._model_name(verified_menu.options[verified_menu.selected])
            if self._is_model_menu(verified_menu) and verified_menu.options else None
        )
        if verified_menu:
            self.driver.send_key("escape")
            await asyncio.sleep(self.SETTLE_S)
        if actual != target:
            self._last_action = {
                "action": "set_model", "status": "failed",
                "effect": "selection_unverified", "completed": False,
                "from": before, "requested": target, "observed": actual,
            }
            return self._result(
                f"Couldn't verify the model change. Requested {target}, but the picker shows "
                f"{actual or 'an unknown selection'}."
            )
        self._last_action = {
            "action": "set_model", "status": "verified", "effect": "model_changed",
            "completed": True, "from": before, "to": actual,
        }
        self._known_model = actual
        self._known_model_source = "verified"
        return self._result(f"Verified that Claude Code changed from {before} to {actual}.")

    async def _inject_and_confirm(self, text: str) -> bool:
        """Inject one prompt and, in production, wait for Claude to acknowledge it.

        Splitting text from Enter prevents paste-mode from swallowing submission;
        UserPromptSubmit then distinguishes an accepted prompt from text merely
        sitting in the composer. Retries are intentionally bounded so a changed
        or unrecognized TUI state cannot strand the voice tool until its deadline.
        """
        async with self._submit_lock:
            self._expected_prompt = sanitize(text)
            self._prompt_submitted.clear()
            try:
                self.driver.inject(text)
                if not self._verify_submissions:
                    return True
                for attempt in range(self.SUBMIT_ATTEMPTS):
                    try:
                        await asyncio.wait_for(
                            self._prompt_submitted.wait(), timeout=self.SUBMIT_ACK_S
                        )
                        return True
                    except asyncio.TimeoutError:
                        if attempt + 1 < self.SUBMIT_ATTEMPTS:
                            self.driver.send_key("enter")
                return False
            finally:
                self._expected_prompt = None

    async def _await_turn(self, call_id: str, start_offset: int, start_path) -> dict:
        """Monitor the deferred turn until it resolves.

        The call is already acknowledged with tool_deferred, so no voice turn is
        held open. While the work runs, milestones go out as partial results —
        spoken (reply=true) only for moments worth interrupting silence, per the
        cadence rule "milestones speak, telemetry stays silent": blocked-on-a-
        decision and test runs speak, file edits stay silent but current,
        everything else is a plain progress note. The one terminal result lands
        on the Stop hook, interruption, or failure."""
        deadline = asyncio.get_running_loop().time() + self.HOLD_S
        sent_progress = sent_partials = 0
        tests_announced = False
        announced_menu = None
        while True:
            try:
                await asyncio.wait_for(self._turn_done.wait(), timeout=self.POLL_S)
            except asyncio.TimeoutError:
                pass
            if self._turn_done.is_set():
                if self._interrupted:
                    return self._result("Stopped — the task was interrupted before finishing.")
                if self._turn_failure:
                    detail, self._turn_failure = self._turn_failure, ""
                    return self._result(f"Claude Code stopped because of an error: {detail}")
                return self._turn_result(start_offset, start_path)

            menu = self.menu()
            if menu:
                # A blocking menu is the one interjection that must never be
                # starved: it draws on the full budget while edits/tests keep
                # MENU_RESERVE partials free for it below.
                key = (menu.title, tuple(menu.options))
                if key != announced_menu and sent_partials < self.MAX_PARTIALS:
                    announced_menu = key
                    sent_partials += 1
                    await self._send_partial(
                        call_id,
                        f"Claude Code needs input: {menu.title or 'a menu is open'} — "
                        f"options: {', '.join(menu.options)}. Ask the user, then use select_option.",
                        reply=True,
                    )
            else:
                announced_menu = None

            for m in self._new_milestones():
                if m["kind"] == "note":
                    if sent_progress < self.MAX_PROGRESS:
                        trace("tool_progress", id=call_id, note=m["note"])
                        await self.sender.send_tool_progress(call_id, m["note"])
                        sent_progress += 1
                elif m["kind"] == "tests":
                    if not tests_announced and sent_partials < self.MAX_PARTIALS - self.MENU_RESERVE:
                        tests_announced = True
                        sent_partials += 1
                        await self._send_partial(call_id, m["speak"], reply=True)
                elif sent_partials < self.MAX_PARTIALS - self.MENU_RESERVE:
                    sent_partials += 1
                    await self._send_partial(call_id, m["speak"], files=m.get("files"))

            if asyncio.get_running_loop().time() >= deadline:
                self._voice_owed = True  # completion still gets announced
                return self._result(
                    "Claude Code is still working as the tracking window closed — "
                    "completion will be announced when it lands."
                )

    async def _send_partial(self, call_id: str, speak: str, reply: bool = False,
                            files: list | None = None) -> None:
        content = {"speak": speak, "data": {"files": files} if files else {},
                   "handle": self.handle}
        trace("tool_partial_result", id=call_id, speak=speak, reply=reply)
        await self.sender.send_tool_partial_result(call_id, content, reply=reply)

    def _turn_result(self, start_offset: int, start_path) -> dict:
        # The Stop hook may have pointed us at a different session file than the
        # one we started tailing (fresh session, stale glob) — offsets don't
        # carry across files.
        if self.transcript_path != start_path:
            start_offset = 0
        entries, self._offset = self._read_from(start_offset)
        summary = tmod.summarize_entries(entries)
        # The hook's last_assistant_message is authoritative for the turn that
        # just stopped. The transcript is only a fallback: it can lag the hook,
        # and a lagged entry flushed during idle would otherwise be read as the
        # *next* turn's newest text, repeating the previous turn's summary.
        text = self.last_assistant_text or summary.text
        speak = tmod.speak_summary(text) if text else "Done."
        extra = {}
        if summary.files:
            extra["files"] = summary.files[:10]
        return self._result(speak, **extra)

    async def _command(self, _call_id: str, args: dict) -> dict:
        cmd = (args.get("command") or "").strip()
        if not cmd.startswith("/"):
            return self._result("Commands must start with a slash, like /clear.")
        if self.menu():
            return self._result("A menu is open — answer it first with select_option.")
        # Built-in UI commands such as /model are handled entirely inside the
        # TUI and intentionally fire neither UserPromptSubmit nor
        # UserPromptExpansion. The PTY's split text/Enter write is therefore the
        # reliable submission path; menu/screen state below confirms the effect.
        self.driver.inject(cmd)
        if self.on_status:
            await self.on_status({"type": "local", "event": "injected", "text": cmd})
        await asyncio.sleep(self.SETTLE_S)
        menu = self.menu()
        if cmd == "/model" and self._is_model_menu(menu):
            selected = menu.options[menu.selected] if menu.options else "unknown"
            model = self._model_name(selected)
            self.driver.send_key("escape")
            await asyncio.sleep(self.SETTLE_S)
            if self.menu():
                self._last_action = {
                    "action": "command", "status": "failed",
                    "effect": "picker_close_unverified", "completed": False,
                }
                return self._result(
                    f"The selected model appears to be {model}, but the model picker did not "
                    "close cleanly. No model change was attempted."
                )
            self._known_model = model
            self._known_model_source = "visible_ui"
            self._last_action = {
                "action": "command", "status": "verified",
                "effect": "model_observed", "completed": True,
            }
            return self._result(f"Current model: {model.title()}; no model was changed.")
        if menu:
            self._last_action = {
                "action": "command", "status": "awaiting_input",
                "effect": "menu_opened", "completed": False,
            }
            return self._result(
                f"{cmd} opened a menu: {menu.title or 'options'} — {', '.join(menu.options)}. "
                f"Currently selected: {menu.options[menu.selected] if menu.options else 'unknown'}."
            )
        self._last_action = {
            "action": "command", "status": "unverified",
            "effect": "sent", "completed": False,
        }
        return self._result(f"Sent {cmd}, but Claude Code exposed no state that verifies an effect.")

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
        await self._choose_menu_index(menu, idx)
        after = self.menu()
        if self._is_model_menu(menu) and self._is_matching_model_confirmation(
            after, menu.options[idx]
        ):
            yes_idx = next(
                i for i, option in enumerate(after.options)
                if option.lower().lstrip("0123456789. ").startswith("yes")
            )
            delta = yes_idx - after.selected
            key = "down" if delta > 0 else "up"
            for _ in range(abs(delta)):
                self.driver.send_key(key)
                await asyncio.sleep(0.05)
            self.driver.send_key("enter")
            await asyncio.sleep(self.SETTLE_S)
            final_menu = self.menu()
            if final_menu:
                return self._result(
                    f"Confirmed {menu.options[idx]}, but Claude Code opened another menu: "
                    f"{final_menu.title or 'options'} — {', '.join(final_menu.options)}."
                )
            return self._result(f"Chose and confirmed {menu.options[idx]}.")
        if after:
            return self._result(
                f"Chose {menu.options[idx]}; another menu opened: {after.title or 'options'} — "
                f"{', '.join(after.options)}."
            )
        return self._result(f"Chose {menu.options[idx]}.")

    async def _choose_menu_index(self, menu: screenmod.Menu, idx: int) -> None:
        delta = idx - menu.selected
        key = "down" if delta > 0 else "up"
        for _ in range(abs(delta)):
            self.driver.send_key(key)
            await asyncio.sleep(0.05)
        self.driver.send_key("enter")
        await asyncio.sleep(self.SETTLE_S)

    @staticmethod
    def _is_model_menu(menu: screenmod.Menu | None) -> bool:
        if not menu:
            return False
        if "model" in (menu.title or "").lower():
            return True
        known = {ToolRouter._model_name(option) for option in menu.options}
        return len(known & {"default", "opus", "fable", "sonnet", "haiku"}) >= 2

    @staticmethod
    def _model_name(option: str) -> str:
        lowered = option.lower()
        return next(
            (name for name in ("default", "opus", "fable", "sonnet", "haiku")
             if name in lowered),
            lowered.replace("✔", "").strip(),
        )

    @staticmethod
    def _is_matching_model_confirmation(
        menu: screenmod.Menu | None, selected_option: str
    ) -> bool:
        if not menu:
            return False
        yes_options = [
            option.lower() for option in menu.options
            if option.lower().lstrip("0123456789. ").startswith("yes")
        ]
        has_go_back = any("go back" in option.lower() for option in menu.options)
        if not yes_options or not has_go_back:
            return False
        selected = selected_option.lower()
        model = next(
            (name for name in ("default", "opus", "fable", "sonnet", "haiku") if name in selected),
            None,
        )
        return model is not None and any(model in option for option in yes_options)

    async def _press_key(self, _call_id: str, args: dict) -> dict:
        key = (args.get("key") or "").strip().lower()
        if key not in KEYMAP:
            return self._result(f"Unknown key '{key}'. Keys: {', '.join(sorted(KEYMAP))}.")
        self.driver.send_key(key)
        await asyncio.sleep(0.3)
        return self._result(f"Pressed {key}.")

    async def _end_session(self, _call_id: str, _args: dict) -> dict:
        if self.on_status:
            await self.on_status({"type": "local", "event": "end_session"})
        return self._result("Ending the voice session now. Claude Code will remain open in the terminal.")

    # -- transcript tailing ----------------------------------------------------

    def _ensure_transcript(self) -> None:
        """Locate our transcript before the first Stop hook has told us where it
        is. Once `session_id` is known the file is named after it; until then we
        fall back to the newest transcript in this project directory, which can
        pick another concurrent session's file — progress notes may be off for
        the first turn, but the turn's own result never depends on this (the hook
        supplies both the real path and `last_assistant_message`)."""
        if self.session_id:
            by_session = self._project_transcript_dir() / f"{self.session_id}.jsonl"
            if by_session.exists():
                if by_session != self.transcript_path:
                    self.transcript_path, self._offset = by_session, 0
                return
        if self.transcript_path and self.transcript_path.exists():
            return
        candidates = sorted(
            self._project_transcript_dir().glob("*.jsonl"), key=lambda p: p.stat().st_mtime
        )
        if candidates:
            self.transcript_path = candidates[-1]
            self._offset = 0

    def _project_transcript_dir(self) -> Path:
        munged = str(self.project_dir.resolve()).replace("/", "-").replace(".", "-").replace("_", "-")
        return Path.home() / ".claude" / "projects" / munged

    def _read_from(self, offset: int) -> tuple[list[dict], int]:
        if not self.transcript_path:
            self._ensure_transcript()
        if not self.transcript_path:
            return [], offset
        return tmod.read_new(self.transcript_path, offset)

    def _new_milestones(self) -> list[dict]:
        entries, self._offset = self._read_from(self._offset)
        out: list[dict] = []
        for entry in entries:
            m = tmod.milestone(entry)
            if m and m not in out:
                out.append(m)
        return out
