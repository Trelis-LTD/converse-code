"""Minimal reference implementation of Converse background tools over Pi RPC."""

from __future__ import annotations

import asyncio
import re
from typing import Literal

from .pi_rpc import PiRPCError


TOOL_TIMEOUT_S = 30
DEFERRED_TIMEOUT_S = 7200

CODING_TASK_DESCRIPTION = (
    "Start a coding task in the user's current project. Preserve technical wording and pass the "
    "complete instruction in request. The task runs in the background; progress, meaningful "
    "partial results, questions, and completion arrive automatically. Do not call this for a "
    "task already in progress; use continue_task to refine it or answer its question."
)
CONTINUE_TASK_DESCRIPTION = (
    "Send guidance or a requested answer to the coding task already in progress. Preserve the "
    "user's wording. Use this for refinements, corrections, and replies to a blocking question."
)
END_SESSION_DESCRIPTION = (
    "End this Converse session after a brief goodbye. Use only when the user explicitly asks to "
    "end or hang up the conversation."
)


def manifest() -> list[dict]:
    def tool(name, description, props=None, required=None, **flags):
        return {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object", "properties": props or {}, "required": required or [],
            },
            **flags,
        }

    request = {"request": {"type": "string", "description": "The user's instruction."}}
    return [
        tool(
            "coding_task", CODING_TASK_DESCRIPTION, request, ["request"],
            timeout=TOOL_TIMEOUT_S, deferred=True, deferred_timeout=DEFERRED_TIMEOUT_S,
            notify_on_complete=True, status_label="Coding task",
        ),
        tool("continue_task", CONTINUE_TASK_DESCRIPTION, request, ["request"], timeout=15),
        tool("end_session", END_SESSION_DESCRIPTION, timeout=15),
    ]


class AgentToolRouter:
    """Translate one Pi session into the smallest useful Converse tool lifecycle."""

    MAX_PARTIALS = 8
    MAX_ROUTINE_PARTIALS = 3
    MAX_PROGRESS = 12

    def __init__(self, pi, sender, *, handle: str) -> None:
        self.pi = pi
        self.sender = sender
        self.handle = handle
        self.pi.on_event = self.on_event
        self.phase: Literal["idle", "starting", "running", "awaiting_input", "canceling"] = "idle"
        self.active_call_id: str | None = None
        self.active_request: str | None = None
        self.last_assistant_text = ""
        self.pending_ui: dict | None = None
        self._settled = asyncio.Event()
        self._cancelled = False
        self._failure = ""
        self._partial_count = 0
        self._routine_partial_count = 0
        self._progress_count = 0
        self._deferred_sent = False
        self._early_events: list[dict] = []
        self.on_end_session = None

    def semantic_state(self) -> dict:
        return {
            "phase": self.phase,
            "active_task": self.active_request,
            "waiting_for": self._public_ui(self.pending_ui) if self.pending_ui else None,
        }

    async def handle_tool_call(self, call: dict) -> None:
        call_id = str(call.get("id") or "")
        name = call.get("name")
        args = call.get("args") if isinstance(call.get("args"), dict) else {}
        if name == "coding_task":
            await self._coding_task(call_id, args)
        elif name == "continue_task":
            await self._continue_task(call_id, args)
        elif name == "end_session":
            await self._result(call_id, "Ending the Converse session.", verified=True)
            if self.on_end_session is not None:
                await self.on_end_session()
        else:
            await self._result(call_id, f"Unknown tool: {name}", outcome="failed")

    async def handle_tool_cancel(self, call: dict) -> None:
        if call.get("id") != self.active_call_id or self.phase == "idle":
            return
        self.phase = "canceling"
        self._cancelled = True
        try:
            await self.pi.command("abort")
        except PiRPCError:
            self._settled.set()

    async def on_event(self, event: dict) -> None:
        if self.active_call_id and not self._deferred_sent:
            self._early_events.append(event)
            return
        await self._handle_event(event)

    async def _handle_event(self, event: dict) -> None:
        kind = event.get("type")
        if kind == "agent_start" and self.phase == "starting":
            self.phase = "running"
        elif kind == "tool_execution_start" and self.active_call_id:
            await self._tool_started(event)
        elif kind == "message_end":
            message = event.get("message")
            text = self._message_text(message)
            if text:
                self.last_assistant_text = text
            if isinstance(message, dict) and message.get("stopReason") in {"error", "aborted"}:
                self._failure = str(message.get("errorMessage") or "Pi stopped with an error.")
        elif kind == "extension_ui_request" and event.get("method") in {
            "select", "confirm", "input", "editor",
        }:
            if self._partial_count >= self.MAX_PARTIALS:
                self._failure = "The task exceeded the supported number of user interactions."
                await self.pi.send_extension_response(event["id"], cancelled=True)
                return
            self.pending_ui = event
            self.phase = "awaiting_input"
            if self.active_call_id:
                self._partial_count += 1
                await self.sender.send_tool_partial_result(
                    self.active_call_id,
                    {
                        "speak": self._ui_prompt(event),
                        "data": self._public_ui(event),
                        "handle": self.handle,
                    },
                    reply=True,
                )
        elif kind == "agent_settled":
            self._settled.set()
        elif kind == "process_exit" and self.active_call_id:
            self._failure = f"Pi exited unexpectedly with status {event.get('status')}."
            self._settled.set()

    async def _coding_task(self, call_id: str, args: dict) -> None:
        request = str(args.get("request") or "").strip()
        if not request:
            await self._result(call_id, "A coding instruction is required.", outcome="failed")
            return
        if self.phase != "idle":
            await self._result(
                call_id, "A coding task is already active. Use continue_task.", outcome="failed",
            )
            return
        self.phase = "starting"
        self.active_call_id = call_id
        self.active_request = request
        self.last_assistant_text = ""
        self._cancelled = False
        self._failure = ""
        self._partial_count = 0
        self._routine_partial_count = 0
        self._progress_count = 0
        self._deferred_sent = False
        self._early_events.clear()
        self._settled.clear()
        try:
            await self.pi.command("prompt", message=request)
        except PiRPCError as exc:
            await self._result(call_id, f"Pi rejected the task: {exc}", outcome="failed")
            self._reset()
            return
        await self.sender.send_tool_deferred(call_id, self.handle, status_label="Coding task")
        self._deferred_sent = True
        for event in self._early_events:
            await self._handle_event(event)
        self._early_events.clear()
        await self._settled.wait()
        if self._cancelled:
            await self._result(call_id, "The coding task was cancelled.", outcome="cancelled")
        elif self._failure:
            await self._result(call_id, self._failure, outcome="failed")
        else:
            await self._result(
                call_id,
                self.last_assistant_text or "The coding task finished without a text summary.",
                outcome="succeeded",
            )
        self._reset()

    async def _continue_task(self, call_id: str, args: dict) -> None:
        request = str(args.get("request") or "").strip()
        if not request:
            await self._result(call_id, "A reply or instruction is required.", outcome="failed")
            return
        if self.phase == "awaiting_input" and self.pending_ui:
            try:
                answered = await self._answer_ui(request)
            except PiRPCError as exc:
                await self._result(
                    call_id, f"Pi did not accept that answer: {exc}", outcome="failed",
                )
                return
            if not answered:
                await self._result(
                    call_id, "That reply does not match the pending question.", outcome="failed",
                )
                return
            self.pending_ui = None
            self.phase = "running"
        elif self.phase in {"starting", "running"}:
            try:
                await self.pi.command("steer", message=request)
            except PiRPCError as exc:
                await self._result(call_id, f"Pi rejected the guidance: {exc}", outcome="failed")
                return
        else:
            await self._result(call_id, "There is no active coding task.", outcome="failed")
            return
        await self._result(call_id, "Passed that to the active coding task.", verified=True)

    async def _answer_ui(self, request: str) -> bool:
        assert self.pending_ui is not None
        event = self.pending_ui
        method = event["method"]
        if method == "confirm":
            normalized = request.casefold().strip(" .!?")
            yes = {"yes", "y", "confirm", "allow", "approve", "continue", "do it"}
            no = {"no", "n", "deny", "block", "cancel", "stop", "don't", "do not"}
            if normalized not in yes | no:
                return False
            await self.pi.send_extension_response(event["id"], confirmed=normalized in yes)
            return True
        if method == "select":
            options = event.get("options") or []
            match = next((option for option in options if option.casefold() == request.casefold()), None)
            if match is None:
                return False
            await self.pi.send_extension_response(event["id"], value=match)
            return True
        await self.pi.send_extension_response(event["id"], value=request)
        return True

    async def _tool_started(self, event: dict) -> None:
        name = str(event.get("toolName") or "tool")
        args = event.get("args") if isinstance(event.get("args"), dict) else {}
        if name in {"edit", "write"}:
            if self._routine_partial_count >= self.MAX_ROUTINE_PARTIALS:
                return
            path = str(args.get("path") or args.get("file_path") or "a file")
            self._routine_partial_count += 1
            self._partial_count += 1
            await self.sender.send_tool_partial_result(
                self.active_call_id,
                {"speak": f"Updating {path}.", "data": {"tool": name}, "handle": self.handle},
                reply=False,
            )
            return
        if name == "bash" and self._is_test_command(str(args.get("command") or "")):
            if self._routine_partial_count >= self.MAX_ROUTINE_PARTIALS:
                return
            self._routine_partial_count += 1
            self._partial_count += 1
            await self.sender.send_tool_partial_result(
                self.active_call_id,
                {"speak": "The coding agent is preparing to run tests.", "data": {"tool": name},
                 "handle": self.handle},
                reply=True,
            )
            return
        if self._progress_count < self.MAX_PROGRESS:
            self._progress_count += 1
            await self.sender.send_tool_progress(self.active_call_id, f"Using {name}.")

    @staticmethod
    def _is_test_command(command: str) -> bool:
        return bool(re.search(r"(?:^|\s)(?:pytest|test|tests|cargo test|go test|npm test)", command))

    @staticmethod
    def _message_text(message) -> str:
        if not isinstance(message, dict) or message.get("role") != "assistant":
            return ""
        content = message.get("content")
        if isinstance(content, str):
            return content.strip()
        if not isinstance(content, list):
            return ""
        return "\n".join(
            str(item.get("text") or "").strip()
            for item in content if isinstance(item, dict) and item.get("type") == "text"
            if item.get("text")
        ).strip()

    @staticmethod
    def _public_ui(event: dict | None) -> dict | None:
        if event is None:
            return None
        return {
            "request_id": event.get("id"),
            "method": event.get("method"),
            "title": event.get("title"),
            "message": event.get("message"),
            "options": event.get("options") or [],
        }

    @staticmethod
    def _ui_prompt(event: dict) -> str:
        parts = [str(event.get("title") or "The coding agent needs input")]
        if event.get("message"):
            parts.append(str(event["message"]))
        if event.get("options"):
            parts.append("Options: " + ", ".join(map(str, event["options"])))
        return " — ".join(parts)

    async def _result(
        self, call_id: str, speak: str, *, outcome: str = "succeeded", verified: bool = False,
    ) -> None:
        await self.sender.send_tool_result(
            call_id,
            {"speak": speak, "data": {}, "handle": self.handle},
            outcome=outcome,
            verified=verified,
        )

    def _reset(self) -> None:
        self.phase = "idle"
        self.active_call_id = None
        self.active_request = None
        self.pending_ui = None
        self._deferred_sent = False
        self._early_events.clear()
        self._settled.clear()
