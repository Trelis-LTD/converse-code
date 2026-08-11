"""The Converse tools and their idle/working/awaiting-input phase model.

Everything the voice brain can do to Claude Code goes through here; everything
it learns comes back as small {speak, data, handle} payloads: thin brain context,
structure from the screen, and prose from the transcript.
"""

import asyncio
import hashlib
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
class DecisionState:
    kind: str
    title: str
    options: tuple[str, ...]
    selected: str
    revision: str


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

CHANGE_MODEL_DESCRIPTION = (
    "Change Claude Code's current-session model with its documented /model command and verify "
    "the result from the current rendered UI. Use only for an explicit model-change request. "
    "Choose one documented alias exactly; never pass a provider model ID."
)

RESOLVE_DECISION_DESCRIPTION = (
    "Resolve the exact blocking decision currently shown by Claude Code. Copy the revision and "
    "option label exactly from observe_claude or a decision notification. Use only when the user "
    "has chosen that option, or when it is a deterministic confirmation of the active instruction "
    "the user already authorized. Never approve permissions, destructive actions, persistent "
    "changes, or genuine preferences without the user's explicit choice. If the option was not "
    "already highlighted, the first call only focuses it safely; immediately call this tool again "
    "with the new revision returned by that result and the same option to submit it."
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
        tool("change_model", CHANGE_MODEL_DESCRIPTION, {
            "model": {
                "type": "string",
                "enum": ["default", "opus", "fable", "sonnet", "haiku"],
                "description": "Documented Claude Code model alias.",
            },
        }, ["model"], timeout=30),
        tool("resolve_decision", RESOLVE_DECISION_DESCRIPTION, {
            "revision": {"type": "string", "description": "Exact current decision revision."},
            "option": {"type": "string", "description": "Exact visible option label."},
        }, ["revision", "option"], timeout=15),
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
        self._decision_signature: tuple | None = None
        self._decision_generation = 0
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
        decision = self._decision_state()
        if decision is None:
            return
        tool_name = payload.get("tool_name")
        detail = f" for {tool_name}" if isinstance(tool_name, str) and tool_name else ""
        options = f" Options: {', '.join(menu.options)}." if menu.options else ""
        trace("inject_context", reason="permission_request", tool=tool_name, options=menu.options)
        await self.sender.send_context(
            f"Claude Code is waiting for permission{detail}.{options} "
            f"Decision revision: {decision.revision}. Tell the user briefly and "
            "ask which option they want; do not choose for them. Then use resolve_decision with "
            "that exact revision and option label.",
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
            "change_model": self._change_model,
            "resolve_decision": self._resolve_decision,
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

    def _decision_revision(
        self, kind: str, title: str, options: tuple[str, ...],
    ) -> str:
        # Highlight movement does not create a new semantic decision, but any new host paint
        # invalidates an approval token. The latter catches an identical modal that was closed
        # and reopened without an intervening observe_claude call.
        signature = (kind, title, options)
        if signature != self._decision_signature:
            self._decision_generation += 1
            self._decision_signature = signature
        screen_revision = getattr(self.driver, "screen_revision", 0)
        payload = "\x1f".join(
            (str(self._decision_generation), str(screen_revision), kind, title, *options)
        ).encode()
        return hashlib.sha256(payload).hexdigest()[:16]

    @staticmethod
    def _same_decision(left: DecisionState, right: DecisionState) -> bool:
        return (
            left.kind == right.kind
            and left.title == right.title
            and left.options == right.options
        )

    def _decision_state(self, lines: list[str] | None = None) -> DecisionState | None:
        lines = self.driver.snapshot() if lines is None else lines
        menu = screenmod.detect_menu(lines)
        if menu:
            selected = menu.options[menu.selected]
            kind = "model_picker" if self._is_model_menu(menu) else "menu"
            return DecisionState(
                kind=kind,
                title=menu.title,
                options=tuple(menu.options),
                selected=selected,
                revision=self._decision_revision(kind, menu.title, tuple(menu.options)),
            )
        if self._model_scope_visible(lines):
            kind = "model_scope_prompt"
            title = "Apply selected model"
            options = ("Set as default", "Use this session only", "Cancel")
            return DecisionState(
                kind=kind,
                title=title,
                options=options,
                selected="",
                revision=self._decision_revision(kind, title, options),
            )
        self._decision_signature = None
        return None

    def _model_scope_visible(self, lines: list[str] | None = None) -> bool:
        lines = self.driver.snapshot() if lines is None else lines
        visible = screenmod.is_model_scope_prompt(lines)
        if not visible:
            self._model_scope_dismissed = False
        return visible and not self._model_scope_dismissed

    def semantic_state(self) -> dict:
        lines = self.driver.snapshot()
        menu = screenmod.detect_menu(lines)
        decision = self._decision_state(lines)
        if self._canceling_prompt_ids is not None:
            phase = "canceling"
        elif decision:
            phase = "awaiting_input"
        else:
            phase = "working" if self.working else "idle"
        ui = {"kind": "none"}
        if decision:
            ui = {
                "kind": decision.kind,
                "title": decision.title,
                "options": list(decision.options),
                "selected": decision.selected,
                "revision": decision.revision,
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
                "Direct slash commands are not exposed to Converse. Phrase the intended result "
                "as a normal instruction to Claude Code."
            )
        decision = self._decision_state()
        if decision:
            return self._result(
                f"Claude Code needs the current decision answered first: "
                f"{decision.title or 'a decision'} — options: "
                f"{', '.join(decision.options)}; revision: {decision.revision}."
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
        decision = self._decision_state()
        if decision:
            return self._result(
                f"Claude Code needs the open decision answered before it can be steered: "
                f"{decision.title or 'a decision'} — options: "
                f"{', '.join(decision.options)}; revision: {decision.revision}."
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
        if ui["kind"] != "none":
            speak = (
                f"Claude Code is awaiting a blocking decision in "
                f"{ui['title'] or 'the current menu'}. Options: {', '.join(ui['options'])}. "
                f"Currently selected: {ui['selected'] or 'none'}. Decision revision: "
                f"{ui['revision']}."
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

    async def _change_model(self, _call_id: str, args: dict) -> dict | ToolReply:
        target = str(args.get("model") or "").strip().lower()
        allowed = ("default", "opus", "fable", "sonnet", "haiku")
        if target not in allowed:
            self._last_action = {
                "action": "change_model", "status": "failed",
                "effect": "model_not_supported", "completed": False,
                "requested": target,
            }
            return self._reply(
                f"Unsupported model alias. Choose one of: {', '.join(allowed)}.",
                outcome="failed", verified=False,
            )
        if self.working:
            self._last_action = {
                "action": "change_model", "status": "failed",
                "effect": "claude_working", "completed": False,
            }
            return self._reply(
                "Claude Code is working. Wait until it is idle before changing model.",
                outcome="failed", verified=False,
            )
        existing = self._decision_state()
        if existing:
            self._last_action = {
                "action": "change_model", "status": "failed",
                "effect": "decision_already_open", "completed": False,
            }
            return self._reply(
                "Claude Code already has a blocking decision open; resolve it first.",
                outcome="failed", verified=False,
            )
        if not await self._wait_until_ready():
            self._last_action = {
                "action": "change_model", "status": "failed",
                "effect": "input_not_ready", "completed": False,
            }
            return self._reply(
                "Claude Code input is not stably ready for a model change.",
                outcome="failed", verified=False,
            )

        before = screenmod.detect_header_model(self.driver.snapshot())
        if before == target:
            self._known_model = ModelObservation(target, "verified")
            self._last_action = {
                "action": "change_model", "status": "verified",
                "effect": "already_selected", "completed": True,
                "from": before, "to": target, "postcondition_verified": True,
                "evidence": {"kind": "current_model_header", "model": target},
            }
            return self._reply(
                f"Verified that Claude Code is already using {target}.",
                outcome="succeeded", verified=True,
            )

        self.driver.inject_command(
            f"/model {target}", submit_delay_s=self.COMMAND_SUBMIT_DELAY_S,
        )
        await asyncio.sleep(self.SETTLE_S)
        self._needs_ready_gate = True

        decision = self._decision_state()
        menu = self.menu()
        if decision and menu and self._is_matching_model_confirmation(
            menu, target, screenmod.menu_context(self.driver.snapshot(), menu),
        ):
            yes_index = next(
                index for index, option in enumerate(menu.options)
                if option.casefold().lstrip("0123456789. ").startswith("yes")
            )
            if menu.selected == yes_index and self._submit_menu_index(
                menu, yes_index, decision,
            ):
                await asyncio.sleep(self.SETTLE_S)
            else:
                self._last_action = {
                    "action": "change_model", "status": "pending",
                    "effect": "confirmation_required", "completed": False,
                    "requested": target,
                }
                return self._reply(
                    "Claude Code needs the visible model-change confirmation resolved.",
                    outcome="succeeded", verified=False,
                )
        elif decision:
            self._last_action = {
                "action": "change_model", "status": "pending",
                "effect": "decision_required", "completed": False,
                "requested": target,
            }
            return self._reply(
                "Claude Code opened a decision for this model change. Resolve the exact visible "
                "option before continuing.",
                outcome="succeeded", verified=False,
            )

        for _ in range(8):
            current = screenmod.detect_header_model(self.driver.snapshot())
            if current == target:
                self._known_model = ModelObservation(target, "verified")
                self._last_action = {
                    "action": "change_model", "status": "verified",
                    "effect": "model_changed", "completed": True,
                    "from": before, "to": target, "postcondition_verified": True,
                    "evidence": {"kind": "current_model_header", "model": target},
                }
                return self._reply(
                    f"Verified that Claude Code changed from {before or 'unknown'} to {target}.",
                    outcome="succeeded", verified=True,
                )
            await asyncio.sleep(min(self.SETTLE_S, 0.35))

        self._last_action = {
            "action": "change_model", "status": "failed",
            "effect": "selection_unverified", "completed": False,
            "from": before, "requested": target,
        }
        return self._reply(
            f"Sent /model {target}, but the current UI did not verify that model.",
            outcome="failed", verified=False,
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

            decision = self._decision_state()
            if decision:
                # A blocking menu is the one interjection that must never be
                # starved: it draws on the full budget while edits/tests keep
                # MENU_RESERVE partials free for it below.
                key = decision.revision
                if key != announced_menu and sent_partials < self.MAX_PARTIALS:
                    announced_menu = key
                    sent_partials += 1
                    await self._send_partial(
                        call_id,
                        f"Claude Code needs input: {decision.title or 'a decision is open'} — "
                        f"options: {', '.join(decision.options)}. Decision revision: "
                        f"{decision.revision}. Ask the user when approval or preference is needed, "
                        "then use resolve_decision with that exact revision and option label.",
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

    async def _resolve_decision(self, _call_id: str, args: dict) -> dict:
        revision = (args.get("revision") or "").strip()
        wanted = (args.get("option") or "").strip()
        decision = self._decision_state()
        if not decision:
            self._last_action = {
                "action": "resolve_decision", "status": "failed",
                "effect": "no_decision", "completed": False,
            }
            return self._result("There is no blocking decision open right now.")
        if not revision or revision != decision.revision:
            self._last_action = {
                "action": "resolve_decision", "status": "failed",
                "effect": "stale_decision", "completed": False,
                "requested_revision": revision, "observed_revision": decision.revision,
            }
            return self._result(
                "That decision is stale or changed. Observe Claude Code again before choosing."
            )
        matches = [
            index for index, option in enumerate(decision.options)
            if option.casefold() == wanted.casefold()
        ]
        if len(matches) != 1:
            self._last_action = {
                "action": "resolve_decision", "status": "failed",
                "effect": "option_not_found", "completed": False,
                "requested_option": wanted,
            }
            return self._result(
                f"Choose one exact visible option: {', '.join(decision.options)}."
            )

        if decision.kind == "model_scope_prompt":
            keys = {
                "Set as default": "enter",
                "Use this session only": "s",
                "Cancel": "escape",
            }
            # This is an adapter from Claude Code's rendered single-key control into the same
            # revisioned decision protocol used by ordinary menus; it is not exposed as raw TUI.
            self.driver.send_key(keys[decision.options[matches[0]]])
            await asyncio.sleep(self.SETTLE_S)
        else:
            menu = self.menu()
            target_index = matches[0]
            if menu is not None and menu.selected != target_index:
                if not await self._move_menu_index(menu, target_index):
                    self._last_action = {
                        "action": "resolve_decision", "status": "failed",
                        "effect": "decision_changed", "completed": False,
                    }
                    return self._result(
                        "The decision changed while I was focusing that option."
                    )
                focused = self._decision_state()
                focused_menu = self.menu()
                if (
                    focused is None
                    or focused_menu is None
                    or not self._same_decision(focused, decision)
                    or focused_menu.selected != target_index
                ):
                    self._last_action = {
                        "action": "resolve_decision", "status": "failed",
                        "effect": "decision_changed", "completed": False,
                    }
                    return self._result(
                        "The decision changed while I was focusing that option."
                    )
                self._last_action = {
                    "action": "resolve_decision", "status": "pending",
                    "effect": "option_focused", "completed": False,
                    "option": decision.options[target_index],
                    "revision": focused.revision,
                }
                return self._reply(
                    f"Focused {decision.options[target_index]} without submitting it. "
                    f"Resolve the decision again with revision {focused.revision} and the same "
                    "exact option to confirm it.",
                    outcome="succeeded", verified=False,
                )
            if menu is None or not self._submit_menu_index(
                menu, target_index, decision,
            ):
                self._last_action = {
                    "action": "resolve_decision", "status": "failed",
                    "effect": "decision_changed", "completed": False,
                }
                return self._result(
                    "The decision changed before I could submit that option."
                )
            await asyncio.sleep(self.SETTLE_S)

        after = self._decision_state()
        if after and self._same_decision(after, decision):
            self._last_action = {
                "action": "resolve_decision", "status": "failed",
                "effect": "selection_unverified", "completed": False,
            }
            return self._result(
                f"Sent {decision.options[matches[0]]}, but the same decision is still visible."
            )
        destination = f"decision:{after.revision}" if after else "closed"
        self._last_action = {
            "action": "resolve_decision", "status": "verified",
            "effect": "option_selected", "completed": True,
            "option": decision.options[matches[0]], "postcondition_verified": True,
            "evidence": {
                "kind": "ui_transition", "from_revision": decision.revision,
                "to": destination,
            },
        }
        if after:
            return self._result(
                f"Chose {decision.options[matches[0]]}; another decision is now visible: "
                f"{after.title or 'options'} — {', '.join(after.options)}."
            )
        return self._result(f"Chose {decision.options[matches[0]]}.")

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

    def _submit_menu_index(
        self, menu: screenmod.Menu, idx: int, authorized: DecisionState,
    ) -> bool:
        # Submission is only allowed when the authorized row is already highlighted. Navigation
        # is a separate revision-producing call because waiting for its repaint would otherwise
        # let an identical replacement modal inherit stale authorization.
        current = self.menu()
        current_decision = self._decision_state()
        if (
            current is None
            or self._menu_signature(current) != self._menu_signature(menu)
            or current.selected != idx
            or current_decision is None
            or not self._same_decision(current_decision, authorized)
            or current_decision.revision != authorized.revision
        ):
            return False
        self.driver.send_key("enter")
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
        menu: screenmod.Menu | None, target: str, lines: list[str],
    ) -> bool:
        if not menu:
            return False
        context = " ".join(line.casefold() for line in lines)
        yes_options = [
            option.casefold() for option in menu.options
            if option.casefold().lstrip("0123456789. ").startswith("yes")
        ]
        return (
            "switch model" in context
            and "conversation is cached" in context
            and "full history" in context
            and any("go back" in option.casefold() for option in menu.options)
            and any(target in option for option in yes_options)
        )

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
