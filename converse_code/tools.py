"""The Converse tools and their idle/working/awaiting-input phase model.

Everything the voice brain can do to Claude Code goes through here; everything
it learns comes back as small {speak, data, handle} payloads: thin brain context,
structure from the screen, and prose from the transcript.
"""

import asyncio
import logging
from collections import deque
from dataclasses import dataclass
from typing import Literal
from pathlib import Path

from . import screen as screenmod
from . import transcript as tmod
from .ptyhost import sanitize
from .tracelog import trace

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ModelObservation:
    name: str
    source: Literal["visible_ui", "verified"]


@dataclass(frozen=True)
class ToolReply:
    content: dict
    outcome: str
    verified: bool = False

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
    "names, function names, flags, error text — compress filler, never editorialize. This "
    "includes contextual requests to open or close an app, file, browser, game, window, or "
    "other project object: preserve pronouns such as 'it' or 'that' and send them here. Work "
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
    "been interrupted. Returns idle/working/canceling/awaiting_input phase, the active task, open UI, "
    "selected option, and the last action with its status and evidence. Answer directly from this "
    "result; never call long_task merely to inspect or restate Claude Code's current state."
)

SET_MODEL_DESCRIPTION = (
    "Change Claude Code's model as one verified operation. Pass the requested model name. This "
    "uses Claude Code's documented session-only /model command and verifies the rendered result. "
    "Use this both to change a model and to ensure a requested model is already selected."
)

SELECT_DESCRIPTION = (
    "Answer a genuine blocking choice currently shown by Claude Code, such as a permission or "
    "clarification prompt. Pass the option's text or number. Use only after the user has chosen; "
    "never use it to browse Claude Code's interface or answer on the user's behalf."
)

END_SESSION_DESCRIPTION = (
    "End the current Converse session after a brief goodbye. Use only when the user "
    "explicitly asks to end or hang up this voice conversation or Converse session. Never use "
    "this to close a file, app, browser, game, program, terminal window, or a contextual 'it' or "
    "'that'; those are Claude Code actions and must use long_task. This stops the microphone and "
    "voice connection but leaves Claude Code running in the terminal."
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
        tool("select_option", SELECT_DESCRIPTION,
             {"option": {"type": "string", "description": "Option text or number."}},
             ["option"], timeout=15),
        tool("end_session", END_SESSION_DESCRIPTION, timeout=15),
    ]


class ToolRouter:
    HOLD_S = DEFERRED_TIMEOUT_S - 120   # resolve before the broker expires the deferred job
    POLL_S = 2.0            # transcript/menu poll cadence while monitoring
    SETTLE_S = 1.2          # wait after command/select before reading the screen
    MAX_PROGRESS = 10       # protocol cap is 12/call; keep headroom
    MAX_PARTIALS = 6        # protocol cap is 8/call; keep headroom
    MENU_RESERVE = 2        # partial budget only blocking menus may spend
    COMMAND_SUBMIT_DELAY_S = 0.4
    SUBMIT_ACK_S = 2.0      # UserPromptSubmit should arrive almost immediately
    SUBMIT_ATTEMPTS = 3     # initial submit plus two bounded Enter retries
    CANCEL_GRACE_S = 0.3    # let the submitted prompt leave the composer
    CANCEL_POLL_S = 0.1
    CANCEL_RETRY_S = 0.5    # an initial Escape can race prompt activation
    CANCEL_ESCAPE_RETRIES = 3
    CANCEL_IDLE_SAMPLES = 3 # avoid accepting a transient repaint as settled
    READY_POLL_S = 0.1
    READY_SAMPLES = 3
    READY_TIMEOUT_S = 5.0

    def __init__(self, driver, sender, handle: str, project_dir: str | Path | None = None):
        """driver: ClaudeHost-like (inject/send_key/snapshot).
        sender: BrowserBridge-like (tool results/progress and context injection)."""
        self.driver = driver
        self.sender = sender
        self.handle = handle
        self.project_dir = Path(project_dir) if project_dir else Path.cwd()
        self.working = False
        self.transcript_path: Path | None = None
        self.session_id: str | None = None
        self.last_assistant_text = ""
        self._offset = 0
        self._turn_done = asyncio.Event()
        self._interrupted = False
        self._active_call_id: str | None = None
        self._server_canceled: set[str] = set()
        self._voice_owed = False  # a resolved voice call still owes its outcome out loud
        self._submit_lock = asyncio.Lock()
        self._prompt_submitted = asyncio.Event()
        self._expected_prompt: str | None = None
        self._turn_failure = ""
        self._active_request: str | None = None
        self._last_action: dict | None = None
        self._episode_prompt_ids: set[str] = set()
        self._episode_session_id: str | None = None
        self._canceled_prompt_ids: deque[str] = deque(maxlen=128)
        self._canceling_prompt_ids: set[str] | None = None
        self._known_model: ModelObservation | None = None
        self._model_scope_dismissed = False
        self._needs_ready_gate = False
        self._cancel_watch_task: asyncio.Task | None = None
        self.on_status = None  # async callback(dict) → browser tab

    # -- events from the Stop hook -------------------------------------------

    async def on_hook(self, event: str, payload: dict) -> None:
        if event == "user_prompt_submit":
            prompt = payload.get("prompt")
            if isinstance(prompt, str) and sanitize(prompt) == self._expected_prompt:
                prompt_id = payload.get("prompt_id")
                session_id = payload.get("session_id")
                if isinstance(session_id, str) and session_id:
                    self.session_id = session_id
                    self._episode_session_id = session_id
                if isinstance(prompt_id, str) and prompt_id:
                    self._episode_prompt_ids.add(prompt_id)
                    self._prompt_submitted.set()
                else:
                    trace("prompt_submit_missing_id_ignored", prompt=sanitize(prompt))
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
        if self._canceling_prompt_ids is not None and not self._canceling_prompt_ids:
            self._canceling_prompt_ids = None
            self._active_request = None
            self._last_action = {
                "action": "cancel_task", "status": "verified",
                "effect": "stopped", "completed": True,
                "postcondition_verified": True,
                "evidence": {"kind": "stop_hook", "prompt_id": prompt_id},
            }
            trace("unidentified_canceled_stop_ignored")
            await self._push_status()
            return
        if self._canceling_prompt_ids is not None and (
            not isinstance(prompt_id, str)
            or not prompt_id
            or prompt_id not in self._canceling_prompt_ids
        ):
            trace("unrelated_canceling_stop_ignored", prompt_id=prompt_id)
            return
        if isinstance(prompt_id, str) and prompt_id in self._canceled_prompt_ids:
            was_canceling = (
                self._canceling_prompt_ids is not None
                and prompt_id in self._canceling_prompt_ids
            )
            if was_canceling:
                self._canceling_prompt_ids.discard(prompt_id)
            if was_canceling:
                if not self._canceling_prompt_ids:
                    self._canceling_prompt_ids = None
                    self._active_request = None
                self._last_action = {
                    "action": "cancel_task", "status": "verified",
                    "effect": "stopped", "completed": True,
                    "postcondition_verified": True,
                    "evidence": {"kind": "stop_hook", "prompt_id": prompt_id},
                }
            trace("canceled_stop_ignored", prompt_id=prompt_id)
            if was_canceling:
                await self._push_status()
            return
        if self._active_call_id is not None and (
            not isinstance(prompt_id, str)
            or not prompt_id
            or prompt_id not in self._episode_prompt_ids
        ):
            trace("unrelated_stop_ignored", prompt_id=prompt_id)
            return
        if self.working and self._episode_prompt_ids and (
            not isinstance(prompt_id, str)
            or not prompt_id
            or prompt_id not in self._episode_prompt_ids
        ):
            trace("unrelated_stop_ignored", prompt_id=prompt_id)
            return
        voice_call_was_waiting = self._active_call_id is not None
        work_was_active = self.working
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
        self._episode_session_id = None
        if voice_call_was_waiting:
            self._last_action = {
                "action": "long_task", "status": "completed",
                "effect": "claude_episode_completed", "completed": True,
                "postcondition_verified": False,
                "evidence": {"kind": "stop_hook", "prompt_id": prompt_id},
            }
        elif work_was_active:
            self._last_action = {
                "action": "terminal_task", "status": "completed",
                "effect": "claude_episode_completed", "completed": True,
                "postcondition_verified": False,
                "evidence": {"kind": "stop_hook", "prompt_id": prompt_id},
            }
        self._needs_ready_gate = True
        self._turn_done.set()
        await self._push_status()
        if not voice_call_was_waiting:
            await self._wake_voice_for_terminal_turn(hook_text, announce=voice_owed)

    async def _on_stop_failure(self, payload: dict) -> None:
        waiting = self._active_call_id is not None
        work_was_active = self.working
        hook_session_id = payload.get("session_id")
        expected_session_id = self._episode_session_id
        if (
            expected_session_id
            and (
                not isinstance(hook_session_id, str)
                or hook_session_id != expected_session_id
            )
        ):
            trace(
                "unrelated_stop_failure_ignored",
                session_id=hook_session_id,
                expected_session_id=expected_session_id,
            )
            return
        detail = payload.get("error_details") or payload.get("error") or "unknown Claude error"
        self._turn_failure = str(detail)
        self.working = False
        self._active_request = None
        self._episode_prompt_ids.clear()
        self._episode_session_id = None
        if waiting or work_was_active:
            self._last_action = {
                "action": "long_task" if waiting else "terminal_task",
                "status": "failed", "effect": "stop_failure", "completed": True,
                "postcondition_verified": False,
                "evidence": {
                    "kind": "stop_failure_hook", "error": self._turn_failure,
                    "session_id": hook_session_id,
                    "correlated": bool(
                        expected_session_id and hook_session_id == expected_session_id
                    ),
                },
            }
        self._voice_owed = False  # the error itself is the announcement
        self._needs_ready_gate = True
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
                "Claude Code finished the voice-requested task. Attribute the following as "
                "Claude Code's report; do not present external effects as independently verified. "
                f"Briefly summarize its report: {summary}",
                role="context",
                reply=True,
            )
        else:
            await self.sender.send_context(
                "Claude Code finished work entered directly in the terminal. "
                "Do not announce this unless asked. Attribute it as Claude Code's report and do "
                f"not imply independent verification. Its report was: {summary}",
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
        call_id = call.get("id")
        if not isinstance(call_id, str) or not call_id:
            log.warning("ignored tool call without a string id")
            return
        name = call.get("name")
        args = call.get("args", {})
        if not isinstance(name, str) or not isinstance(args, dict):
            log.warning("rejected malformed tool call %s", call_id)
            await self.sender.send_tool_result(
                call_id, self._result("The tool call was malformed and was not run."),
                outcome="failed", verified=False,
            )
            return
        trace("tool_call", id=call_id, name=name, args=args)
        handlers = {
            "long_task": self._long_task,
            "steer_task": self._steer_task,
            "observe_claude": self._observe_claude,
            "set_model": self._set_model,
            "select_option": self._select_option,
            "end_session": self._end_session,
        }
        handler = handlers.get(name)
        action_before = self._last_action
        outcome, verified = "unknown", False
        try:
            if self._canceling_prompt_ids is not None and name not in {"observe_claude", "end_session"}:
                content = self._result(
                    "Claude Code is still stopping the interrupted task. Wait for it to reach "
                    "idle before starting another action."
                )
            elif handler is None:
                content = self._result(f"Unknown tool {name}.")
            else:
                reply = await handler(call_id, args)
                if isinstance(reply, ToolReply):
                    content = reply.content
                    outcome, verified = reply.outcome, reply.verified
                else:
                    content = reply
                    if name in {"observe_claude", "end_session"}:
                        outcome, verified = "succeeded", True
                    elif (
                        self._last_action is not action_before
                        and self._last_action
                        and self._last_action.get("action") == name
                    ):
                        status = self._last_action.get("status")
                        if status in {"completed", "verified"}:
                            outcome = "succeeded"
                            # Completion proves that Claude's episode ended. It does not prove that
                            # the user's requested external postcondition happened.
                            verified = bool(
                                self._last_action.get("postcondition_verified")
                                and self._last_action.get("evidence")
                            )
                        elif status == "failed":
                            outcome = "failed"
        except Exception:
            log.exception("tool %s failed", name)
            if self._active_call_id == call_id:
                self.working = False
                self._active_request = None
                self._episode_prompt_ids.clear()
                self._last_action = {
                    "action": str(name or "unknown"), "status": "failed",
                    "effect": "driver_error", "completed": False,
                }
            outcome, verified = "failed", False
            content = self._result(
                "Something went wrong driving Claude Code; the session itself is still alive."
            )
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
            await self.sender.send_tool_result(
                call_id, content, outcome=outcome, verified=verified,
            )
        await self._push_status()

    async def handle_tool_cancel(self, call: dict) -> None:
        """Honor Converse's managed cancellation for the matching pending job."""
        call_id = call.get("id")
        trace("tool_cancel", id=call_id, active=self._active_call_id)
        if not isinstance(call_id, str) or not call_id or call_id != self._active_call_id:
            return
        self._server_canceled.add(call_id)
        self._interrupted = True
        if self._episode_prompt_ids:
            self._canceled_prompt_ids.extend(self._episode_prompt_ids)
            self._canceling_prompt_ids = set(self._episode_prompt_ids)
            if self._cancel_watch_task is None or self._cancel_watch_task.done():
                self._cancel_watch_task = asyncio.create_task(
                    self._watch_cancellation()
                )
        else:
            self._canceling_prompt_ids = set()
            if self._cancel_watch_task is None or self._cancel_watch_task.done():
                self._cancel_watch_task = asyncio.create_task(self._watch_cancellation())
        self.driver.send_key("escape")
        self.working = False
        self._voice_owed = False  # canceled work owes no completion
        self._last_action = {
            "action": "cancel_task", "status": "pending",
            "effect": "cancel_requested", "completed": False,
        }
        self._turn_done.set()
        await self._push_status()

    async def _watch_cancellation(self) -> None:
        """Settle an interruption from visible idle state when Claude emits no Stop hook."""
        await asyncio.sleep(self.CANCEL_GRACE_S)
        idle_samples = 0
        escape_retries = 0
        last_escape = 0.0
        while self._canceling_prompt_ids is not None:
            snapshot = self.driver.snapshot()
            idle = screenmod.is_idle(snapshot) or (
                self._model_scope_dismissed and screenmod.has_empty_composer(
                    snapshot, allow_stale_scope=True,
                )
            )
            if idle:
                idle_samples += 1
                if idle_samples >= self.CANCEL_IDLE_SAMPLES:
                    settled_prompt_ids = self._canceling_prompt_ids
                    self._canceling_prompt_ids = None
                    self._active_request = None
                    self._last_action = {
                        "action": "cancel_task", "status": "verified",
                        "effect": "idle_ui_observed", "completed": True,
                        "postcondition_verified": True,
                        "evidence": {
                            "kind": "stable_idle_ui",
                            "samples": self.CANCEL_IDLE_SAMPLES,
                        },
                    }
                    self._needs_ready_gate = True
                    trace("canceled_idle_observed", prompt_ids=sorted(settled_prompt_ids))
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
                        "cancel_escape_retried", prompt_ids=sorted(self._canceling_prompt_ids),
                        attempt=escape_retries,
                    )
            await asyncio.sleep(self.CANCEL_POLL_S)

    # -- state ------------------------------------------------------------------

    def menu(self) -> screenmod.Menu | None:
        return screenmod.detect_menu(self.driver.snapshot())

    def _model_scope_visible(self, lines: list[str] | None = None) -> bool:
        lines = self.driver.snapshot() if lines is None else lines
        visible = screenmod.is_model_scope_prompt(lines)
        if not visible:
            self._model_scope_dismissed = False
        return visible and not self._model_scope_dismissed

    def semantic_state(self) -> dict:
        lines = self.driver.snapshot()
        menu = screenmod.detect_menu(lines)
        model_scope = self._model_scope_visible(lines)
        if self._canceling_prompt_ids is not None:
            phase = "canceling"
        elif menu or model_scope:
            phase = "awaiting_input"
        else:
            phase = "working" if self.working else "idle"
        ui = {"kind": "none"}
        if menu:
            ui = {
                "kind": "model_picker" if self._is_model_menu(menu) else "menu",
                "title": menu.title,
                "options": list(menu.options),
                "selected": menu.options[menu.selected] if menu.options else "",
            }
        elif model_scope:
            ui = {
                "kind": "model_scope_prompt",
                "title": "Apply selected model",
                "options": ["Set as default", "Use this session only", "Cancel"],
                "selected": "",
            }
        visible_model = None
        if menu and self._is_model_menu(menu) and menu.options:
            visible_model = self._model_name(menu.options[menu.selected])
        else:
            visible_model = screenmod.detect_current_model(lines)
        if visible_model:
            if (
                self._known_model is None
                or visible_model != self._known_model.name
                or self._known_model.source != "verified"
            ):
                self._known_model = ModelObservation(visible_model, "visible_ui")
        model = None
        if self._known_model:
            model = {
                "name": self._known_model.name,
                "source": self._known_model.source,
            }
        return {
            "phase": phase,
            "active_task": self._active_request,
            "ui": ui,
            "model": model,
            "last_action": dict(self._last_action) if self._last_action else None,
        }

    def _status_data(self, **extra) -> dict:
        return {**self.semantic_state(), **extra}

    def _result(self, speak: str, **extra) -> dict:
        return {"speak": speak, "data": self._status_data(**extra), "handle": self.handle}

    def _reply(
        self, speak: str, *, outcome: str, verified: bool = False, **extra,
    ) -> ToolReply:
        return ToolReply(self._result(speak, **extra), outcome, verified)

    async def _push_status(self) -> None:
        if self.on_status:
            await self.on_status({"type": "local", "event": "status", **self._status_data()})

    # -- tools -------------------------------------------------------------------

    async def _long_task(self, call_id: str, args: dict) -> dict | ToolReply:
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
                "Direct slash commands are not exposed to Converse. Use set_model for a model "
                "change, or phrase the intended result as a normal instruction to Claude Code."
            )
        if self._model_scope_visible():
            return self._result(
                "Claude Code needs its model scope answered before starting another task."
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

        if not await self._wait_until_ready():
            self._last_action = {
                "action": "long_task", "status": "failed",
                "effect": "input_not_ready", "completed": False,
            }
            return self._result(
                "Claude Code finished its last turn, but its input is not stably ready yet. "
                "Wait a moment and try again."
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
            await self.on_status({
                "type": "local", "event": "prompt_accepted", "text": request,
            })
        if not self._turn_done.is_set():
            self._last_action = {
                "action": "long_task", "status": "pending",
                "effect": "working", "completed": False,
                "accepted": True,
                "evidence": {
                    "kind": "user_prompt_submit",
                    "prompt_ids": sorted(self._episode_prompt_ids),
                },
            }

        # Detach from the voice turn: the brain closes its reply naturally now,
        # and notify_on_complete announces the terminal result when it lands.
        trace("tool_deferred", id=call_id)
        await self.sender.send_tool_deferred(
            call_id, f"{self.handle}-{call_id}", status_label="Claude Code task"
        )
        return await self._await_turn(call_id, start_offset, start_path)

    async def _steer_task(self, _call_id: str, args: dict) -> dict | ToolReply:
        request = sanitize((args.get("request") or "").strip())
        if not request:
            return self._result("No steering instruction was given.")
        if request.startswith("!"):
            return self._result(
                "Raw shell commands are not allowed over voice. Phrase the guidance as a plain "
                "instruction instead."
            )
        if request.startswith("/"):
            return self._result(
                "Direct slash commands are not exposed to Converse. Phrase the intended result "
                "as normal guidance to Claude Code."
            )
        if self._model_scope_visible():
            return self._result(
                "Claude Code needs its model scope answered before it can be steered."
            )
        menu = self.menu()
        if menu:
            return self._result(
                f"Claude Code needs the open menu answered before it can be steered: "
                f"{menu.title or 'a menu'} — options: {', '.join(menu.options)}."
            )
        if not self.working:
            return self._result("Claude Code is idle. Use long_task to start new work.")
        prompt_ids_before = set(self._episode_prompt_ids)
        if not await self._inject_and_confirm(request):
            return self._result(
                "I added the guidance to Claude Code, but couldn't confirm it was submitted. "
                "It may still be visible in the terminal input."
            )
        accepted_prompt_ids = sorted(self._episode_prompt_ids - prompt_ids_before)
        if not accepted_prompt_ids:
            return self._result(
                "Claude Code acknowledged the guidance without a correlatable prompt ID, so I "
                "cannot claim it was accepted into the active task."
            )
        self._last_action = {
            "action": "steer_task", "status": "verified",
            "effect": "guidance_accepted", "completed": True,
            "postcondition_verified": True,
            "evidence": {
                "kind": "user_prompt_submit", "prompt_ids": accepted_prompt_ids,
            },
        }
        if self.on_status:
            await self.on_status({
                "type": "local", "event": "prompt_accepted", "text": request,
            })
        return self._reply(
            "Added that guidance to the current Claude Code task.",
            outcome="succeeded", verified=True,
        )

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
        elif ui["kind"] == "model_scope_prompt":
            speak = (
                "Claude Code is asking whether the selected model should be the default or apply "
                "only to this session."
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

        if not await self._wait_until_ready():
            return self._result("Claude Code input is not ready for a model change.")

        menu = self.menu()
        if (
            self._model_scope_visible()
            and not self._is_model_menu(menu)
        ):
            return self._result(
                "Claude Code needs the existing model scope answered before changing model again."
            )
        if menu and not self._is_model_menu(menu):
            return self._result(
                f"Claude Code needs the current menu answered first: {menu.title or 'options'}."
            )
        if menu is None:
            return await self._set_model_direct(wanted)
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
        await asyncio.sleep(min(self.SETTLE_S, 0.8))
        refreshed_menu = self.menu()
        if self._is_model_menu(refreshed_menu):
            refreshed_idx = screenmod.match_option(refreshed_menu, wanted)
            if refreshed_idx is not None:
                menu, idx = refreshed_menu, refreshed_idx
                before = self._model_name(menu.options[menu.selected])
        target = self._model_name(menu.options[idx])
        if target not in {"default", "opus", "fable", "sonnet", "haiku"}:
            requested_name = self._model_name(wanted)
            if requested_name in {"default", "opus", "fable", "sonnet", "haiku"}:
                target = requested_name
        baseline_model_acks = screenmod.session_model_ack_count(
            self.driver.snapshot(), target,
        )
        scope_actions = screenmod.is_model_scope_prompt(self.driver.snapshot())
        if before == target:
            if scope_actions:
                # Claude 2.1.227 exposes scope as picker actions: `s` selects
                # the current row for this session; Enter changes the default.
                scope_closed = await self._choose_session_model_scope(target)
            else:
                self.driver.send_key("escape")
                await asyncio.sleep(self.SETTLE_S)
                scope_closed = True
            if not scope_closed:
                self._last_action = {
                    "action": "set_model", "status": "failed",
                    "effect": "scope_prompt_not_closed", "completed": False,
                    "from": before, "requested": target,
                }
                return self._result("The model scope prompt stayed open, so I canceled it.")
            self._known_model = ModelObservation(target, "verified")
            self._last_action = {
                "action": "set_model", "status": "verified", "effect": "already_selected",
                "completed": True, "from": before, "to": target,
                "postcondition_verified": True,
                "evidence": {"kind": "visible_model", "model": target},
            }
            return self._result(f"Verified that {menu.options[idx]} is already selected.")

        if scope_actions:
            if not await self._move_menu_index(menu, idx):
                self._last_action = {
                    "action": "set_model", "status": "failed",
                    "effect": "menu_changed", "completed": False,
                }
                return self._result("The model picker changed while selecting; nothing was submitted.")
            if not await self._choose_session_model_scope(target):
                self._last_action = {
                    "action": "set_model", "status": "failed",
                    "effect": "scope_prompt_not_closed", "completed": False,
                    "from": before, "requested": target,
                }
                return self._result(
                    "Claude Code's model scope picker did not close; I canceled it."
                )
        else:
            if not await self._choose_menu_index(menu, idx):
                self._last_action = {
                    "action": "set_model", "status": "failed",
                    "effect": "menu_changed", "completed": False,
                }
                return self._result("The model picker changed while selecting; nothing was submitted.")
        after = None if scope_actions else self.menu()
        if self._is_matching_model_confirmation(after, menu.options[idx]):
            yes_idx = next(
                i for i, option in enumerate(after.options)
                if option.lower().lstrip("0123456789. ").startswith("yes")
            )
            if not await self._choose_menu_index(after, yes_idx):
                self._last_action = {
                    "action": "set_model", "status": "failed",
                    "effect": "confirmation_changed", "completed": False,
                }
                return self._result("The model confirmation changed; I did not submit an answer.")
        elif after:
            self._last_action = {
                "action": "set_model", "status": "failed",
                "effect": "unexpected_confirmation", "completed": False,
            }
            return self._result(
                f"Couldn't verify the model change because another menu is open: "
                f"{after.title or 'options'}."
            )

        if not scope_actions and not await self._choose_session_model_scope(target):
            self._last_action = {
                "action": "set_model", "status": "failed",
                "effect": "scope_prompt_not_closed", "completed": False,
                "from": before, "requested": target,
            }
            return self._result(
                "Claude Code's model scope prompt did not close; I canceled it and did not "
                "send another command."
            )

        # Claude prints an explicit result after the picker closes. This is a
        # stronger and less disruptive postcondition than opening the picker a
        # second time; retain the picker round-trip below as a compatibility
        # fallback when that acknowledgement is absent or changes format.
        confirmation_snapshot = self.driver.snapshot()
        current_model = screenmod.detect_current_model(confirmation_snapshot)
        fresh_ack = (
            screenmod.session_model_ack_count(confirmation_snapshot, target)
            > baseline_model_acks
        )
        if current_model == target or fresh_ack:
            confirmed = target
            self._last_action = {
                "action": "set_model", "status": "verified", "effect": "model_changed",
                "completed": True, "from": before, "to": confirmed,
                "postcondition_verified": True,
                "evidence": {
                    "kind": "current_model_header" if current_model == target
                    else "fresh_session_model_ack",
                    "model": confirmed,
                },
            }
            self._known_model = ModelObservation(confirmed, "verified")
            return self._result(f"Verified that Claude Code changed from {before} to {confirmed}.")

        # A scope prompt can render late after the picker closes. Check again
        # immediately before the compatibility fallback so a command can never
        # be typed into that modal.
        if not await self._choose_session_model_scope(target):
            self._last_action = {
                "action": "set_model", "status": "failed",
                "effect": "scope_prompt_not_closed", "completed": False,
                "from": before, "requested": target,
            }
            return self._result(
                "Claude Code's model scope prompt did not close; I canceled it and did not "
                "send another command."
            )

        self._model_scope_dismissed = False
        self.driver.inject_command("/model", submit_delay_s=self.COMMAND_SUBMIT_DELAY_S)
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
            "postcondition_verified": True,
            "evidence": {"kind": "visible_model", "model": actual},
        }
        self._known_model = ModelObservation(actual, "verified")
        return self._result(f"Verified that Claude Code changed from {before} to {actual}.")

    async def _set_model_direct(self, wanted: str) -> dict:
        """Use Claude's documented session-only `/model <alias>` command.

        The interactive picker changes across Claude releases and now exposes a separate
        default-setting shortcut. The argument form is the stable automation contract and, per
        Claude's documentation, applies only to this session. We still verify the rendered model
        after any cached-context confirmation before reporting success.
        """
        target = self._model_name(wanted)
        allowed = {"default", "opus", "fable", "sonnet", "haiku"}
        if target not in allowed:
            self._last_action = {
                "action": "set_model", "status": "failed",
                "effect": "model_not_found", "completed": False,
            }
            return self._result(
                f"Unsupported model '{wanted}'. Choose one of: {', '.join(sorted(allowed))}."
            )

        snapshot = self.driver.snapshot()
        before = screenmod.detect_current_model(snapshot)
        baseline_acks = screenmod.session_model_ack_count(snapshot, target)
        if before == target:
            self._known_model = ModelObservation(target, "verified")
            self._last_action = {
                "action": "set_model", "status": "verified", "effect": "already_selected",
                "completed": True, "from": before, "to": target,
                "postcondition_verified": True,
                "evidence": {"kind": "current_model_header", "model": target},
            }
            return self._result(f"Verified that {target} is already selected.")

        command = f"/model {target}"
        self._model_scope_dismissed = False
        # The PTY driver types the command name, dismisses its autocomplete, then appends the
        # argument before Enter. This avoids both selecting a suggestion and clearing full input.
        self.driver.inject_command(command, submit_delay_s=self.COMMAND_SUBMIT_DELAY_S)
        await asyncio.sleep(self.SETTLE_S)

        confirmation = self.menu()
        if self._is_matching_model_confirmation(confirmation, target):
            yes_idx = next(
                i for i, option in enumerate(confirmation.options)
                if option.lower().lstrip("0123456789. ").startswith("yes")
            )
            if not await self._choose_menu_index(confirmation, yes_idx):
                self._last_action = {
                    "action": "set_model", "status": "failed",
                    "effect": "confirmation_changed", "completed": False,
                }
                return self._result("The model confirmation changed; I did not submit an answer.")
        elif confirmation:
            self.driver.send_key("escape")
            self._last_action = {
                "action": "set_model", "status": "failed",
                "effect": "unexpected_confirmation", "completed": False,
                "from": before, "requested": target,
            }
            return self._result(
                f"Claude Code opened an unexpected menu after {command}; I canceled it."
            )

        for _ in range(8):
            snapshot = self.driver.snapshot()
            if self.menu() is None and not self._model_scope_visible(snapshot):
                current = screenmod.detect_current_model(snapshot)
                fresh_ack = screenmod.session_model_ack_count(snapshot, target) > baseline_acks
                if current == target or fresh_ack:
                    confirmed = target
                    self._known_model = ModelObservation(confirmed, "verified")
                    self._last_action = {
                        "action": "set_model", "status": "verified",
                        "effect": "model_changed", "completed": True,
                        "from": before, "to": confirmed,
                        "postcondition_verified": True,
                        "evidence": {
                            "kind": "current_model_header" if current == target
                            else "fresh_session_model_ack",
                            "model": confirmed,
                        },
                    }
                    return self._result(
                        f"Verified that Claude Code changed from {before or 'unknown'} "
                        f"to {confirmed} for this session."
                    )
            await asyncio.sleep(min(self.SETTLE_S, 0.35))

        self._last_action = {
            "action": "set_model", "status": "failed",
            "effect": "selection_unverified", "completed": False,
            "from": before, "requested": target,
        }
        return self._result(
            f"Sent {command}, but Claude Code did not expose a verified {target} selection."
        )

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

    async def _await_turn(
        self, call_id: str, start_offset: int, start_path,
    ) -> dict | ToolReply:
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
                    return self._reply(
                        f"Claude Code stopped because of an error: {detail}",
                        outcome="failed",
                    )
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

    def _turn_result(self, start_offset: int, start_path) -> ToolReply:
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
        report = tmod.speak_summary(text) if text else "Claude Code provided no final summary."
        speak = f"Claude Code reports: {report}"
        extra = {}
        if summary.files:
            extra["files"] = summary.files[:10]
        return self._reply(speak, outcome="succeeded", verified=False, **extra)

    async def _select_option(self, _call_id: str, args: dict) -> dict:
        wanted = (args.get("option") or "").strip()
        if self._model_scope_visible():
            lowered = wanted.lower()
            if "session" in lowered:
                key, label = "s", "Use this session only"
            elif "default" in lowered:
                key, label = "enter", "Set as default"
            elif "cancel" in lowered:
                key, label = "escape", "Cancel"
            else:
                return self._result(
                    "Choose Set as default, Use this session only, or Cancel."
                )
            self.driver.send_key(key)
            await asyncio.sleep(self.SETTLE_S)
            if self._model_scope_visible():
                self._last_action = {
                    "action": "select_option", "status": "failed",
                    "effect": "selection_unverified", "completed": False,
                }
                return self._result(
                    f"Sent the {label} choice, but the same prompt is still visible."
                )
            self._record_option_selection(label, "model_scope_prompt", "closed")
            return self._result(f"Chose {label}.")
        menu = self.menu()
        if not menu:
            return self._result("There is no menu open right now.")
        idx = screenmod.match_option(menu, wanted)
        if idx is None:
            return self._result(
                f"Couldn't find an option matching '{wanted}'. Options: {', '.join(menu.options)}."
            )
        if not await self._choose_menu_index(menu, idx):
            self._last_action = {
                "action": "select_option", "status": "failed",
                "effect": "menu_changed", "completed": False,
            }
            return self._result("The menu changed before I could submit that choice.")
        after = self.menu()
        if self._is_model_menu(menu) and self._is_matching_model_confirmation(
            after, menu.options[idx]
        ):
            yes_idx = next(
                i for i, option in enumerate(after.options)
                if option.lower().lstrip("0123456789. ").startswith("yes")
            )
            if not await self._choose_menu_index(after, yes_idx):
                self._last_action = {
                    "action": "select_option", "status": "failed",
                    "effect": "confirmation_changed", "completed": False,
                }
                return self._result("The confirmation changed before I could submit that choice.")
            if not await self._choose_session_model_scope(self._model_name(menu.options[idx])):
                return self._result(
                    "The model scope prompt stayed open, so I canceled it."
                )
            final_menu = self.menu()
            if final_menu:
                return self._result(
                    f"Confirmed {menu.options[idx]}, but Claude Code opened another menu: "
                    f"{final_menu.title or 'options'} — {', '.join(final_menu.options)}."
                )
            self._record_option_selection(menu.options[idx], menu.title or "menu", "closed")
            return self._result(f"Chose and confirmed {menu.options[idx]}.")
        if self._is_model_menu(menu) and screenmod.is_model_scope_prompt(
            self.driver.snapshot()
        ):
            if not await self._choose_session_model_scope(self._model_name(menu.options[idx])):
                return self._result("The model scope prompt stayed open, so I canceled it.")
            self._record_option_selection(menu.options[idx], menu.title or "menu", "closed")
            return self._result(f"Chose {menu.options[idx]} for this session.")
        if after:
            # A changed highlight is not proof that Enter was accepted. The original menu must
            # close or be replaced by a structurally different blocking step.
            before_signature = (menu.title, tuple(menu.options))
            after_signature = (after.title, tuple(after.options))
            if after_signature == before_signature:
                self._last_action = {
                    "action": "select_option", "status": "failed",
                    "effect": "selection_unverified", "completed": False,
                }
                return self._result(
                    f"Sent the {menu.options[idx]} choice, but the same menu is still visible."
                )
            self._record_option_selection(
                menu.options[idx], menu.title or "menu", after.title or "another_menu",
            )
            return self._result(
                f"Chose {menu.options[idx]}; another menu opened: {after.title or 'options'} — "
                f"{', '.join(after.options)}."
            )
        self._record_option_selection(menu.options[idx], menu.title or "menu", "closed")
        return self._result(f"Chose {menu.options[idx]}.")

    def _record_option_selection(self, option: str, source: str, destination: str) -> None:
        """Record only an observed transition caused by the current selection."""
        self._last_action = {
            "action": "select_option", "status": "verified",
            "effect": "option_selected", "completed": True,
            "option": option, "postcondition_verified": True,
            "evidence": {
                "kind": "ui_transition", "from": source, "to": destination,
            },
        }

    async def _choose_session_model_scope(self, target: str) -> bool:
        for attempt in range(10):
            if self._model_scope_visible():
                break
            if attempt < 9:
                await asyncio.sleep(min(self.SETTLE_S, 0.35))
        else:
            return True
        baseline_acks = screenmod.session_model_ack_count(
            self.driver.snapshot(), target
        )
        self.driver.send_key("s")
        await asyncio.sleep(self.SETTLE_S)
        # With cached history, current Claude builds can ask for a second,
        # target-specific confirmation after the session-only key. Accept only
        # that exact confirmation; never send a blind Enter into an unknown UI.
        confirmation = self.menu()
        if self._is_matching_model_confirmation(confirmation, target):
            yes_idx = next(
                i for i, option in enumerate(confirmation.options)
                if option.lower().lstrip("0123456789. ").startswith("yes")
            )
            if not await self._choose_menu_index(confirmation, yes_idx):
                self.driver.send_key("escape")
                return False
        elif confirmation and not self._is_model_menu(confirmation):
            self.driver.send_key("escape")
            return False

        # Require the fresh session-only acknowledgement before cleanup. Ink
        # may retain the picker footer afterward; Escape is safe only once the
        # requested scope is already authoritative.
        for _ in range(6):
            if screenmod.session_model_ack_count(
                self.driver.snapshot(), target
            ) > baseline_acks:
                if self._model_scope_visible():
                    self.driver.send_key("escape")
                    await asyncio.sleep(min(self.SETTLE_S, 0.35))
                self._model_scope_dismissed = True
                return True
            await asyncio.sleep(min(self.SETTLE_S, 0.35))
        self.driver.send_key("escape")
        for _ in range(5):
            await asyncio.sleep(min(self.SETTLE_S, 0.35))
            if screenmod.session_model_ack_count(
                self.driver.snapshot(), target
            ) > baseline_acks:
                self._model_scope_dismissed = True
                return True
        return False

    async def _wait_until_ready(self) -> bool:
        """Require several consecutive empty-composer frames before typing."""
        if not self._needs_ready_gate:
            return True
        deadline = asyncio.get_running_loop().time() + self.READY_TIMEOUT_S
        samples = 0
        while asyncio.get_running_loop().time() < deadline:
            snapshot = self.driver.snapshot()
            ready = (
                screenmod.has_empty_composer(snapshot)
                and screenmod.detect_menu(snapshot) is None
                and not self._model_scope_visible(snapshot)
            )
            samples = samples + 1 if ready else 0
            if samples >= self.READY_SAMPLES:
                self.driver.send_key("ctrl-u")
                await asyncio.sleep(self.READY_POLL_S)
                self._needs_ready_gate = False
                return True
            await asyncio.sleep(self.READY_POLL_S)
        return False

    @staticmethod
    def _menu_signature(menu: screenmod.Menu) -> tuple[str, tuple[str, ...]]:
        return menu.title, tuple(menu.options)

    async def _move_menu_index(self, menu: screenmod.Menu, idx: int) -> bool:
        delta = idx - menu.selected
        key = "down" if delta > 0 else "up"
        expected_selected = menu.selected
        signature = self._menu_signature(menu)
        for _ in range(abs(delta)):
            current = self.menu()
            if (
                current is None
                or self._menu_signature(current) != signature
                or current.selected != expected_selected
            ):
                return False
            self.driver.send_key(key)
            expected_selected += 1 if delta > 0 else -1
            await asyncio.sleep(0.2)
        current = self.menu()
        return bool(
            current is not None
            and self._menu_signature(current) == signature
            and current.selected == idx
        )

    async def _choose_menu_index(self, menu: screenmod.Menu, idx: int) -> bool:
        if not await self._move_menu_index(menu, idx):
            return False
        # Re-read immediately before Enter. Never submit into a composer or a replacement modal.
        current = self.menu()
        if (
            current is None
            or self._menu_signature(current) != self._menu_signature(menu)
            or current.selected != idx
        ):
            return False
        self.driver.send_key("enter")
        await asyncio.sleep(self.SETTLE_S)
        return True

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

    async def _end_session(self, _call_id: str, _args: dict) -> dict:
        if self.on_status:
            await self.on_status({"type": "local", "event": "end_session"})
        return self._result("Ending the Converse session now. Claude Code will remain open in the terminal.")

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
