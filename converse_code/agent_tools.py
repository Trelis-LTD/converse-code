"""Converse background tools controlling a visible Pi TUI semantically."""

from __future__ import annotations

import asyncio
import re
from typing import Literal

from .pi_tui import PiTUIBridgeError

TOOL_TIMEOUT_S = 30
DEFERRED_TIMEOUT_S = 7200

CODING_TASK_DESCRIPTION = (
    "Immediately pass any request or question requiring knowledge of the user's computer, current "
    "directory, repository, files, code, git state, commands, or coding work to Pi. Preserve the "
    "complete wording in request; never ask whether to pass it. The task runs in the background; "
    "progress, meaningful partial results, and completion arrive automatically. Do not call this "
    "for a task already in progress; use continue_task to refine it."
)
CONTINUE_TASK_DESCRIPTION = (
    "Send guidance or a requested answer to the coding task already in progress. Preserve the "
    "user's wording. Use this for refinements and corrections while Pi is still working."
)
END_SESSION_DESCRIPTION = (
    "End this Converse session after a brief goodbye. Use only when the user explicitly asks to "
    "end or hang up the conversation."
)
APPROVAL_DECISION_DESCRIPTION = (
    "Answer a pending Pi approval only after the user explicitly chooses. Use the exact "
    "approval_id from the approval request. Call immediately without a spoken preamble. Never "
    "infer approval from the coding task itself."
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
        tool(
            "approval_decision",
            APPROVAL_DECISION_DESCRIPTION,
            {
                "approval_id": {"type": "string", "description": "Pending approval ID."},
                "decision": {
                    "type": "string",
                    "enum": ["allow_once", "allow_session", "block"],
                },
            },
            ["approval_id", "decision"],
            timeout=15,
        ),
        tool("end_session", END_SESSION_DESCRIPTION, timeout=15),
    ]


class AgentToolRouter:
    """Translate one Pi session into the smallest useful Converse tool lifecycle."""

    MAX_ROUTINE_PARTIALS = 3
    MAX_PROGRESS = 12

    def __init__(self, pi, sender, *, handle: str) -> None:
        self.pi = pi
        self.sender = sender
        self.handle = handle
        self.pi.on_event = self.on_event
        self.phase: Literal["idle", "starting", "running", "canceling"] = "idle"
        self.active_call_id: str | None = None
        self.active_request: str | None = None
        self.last_assistant_text = ""
        self._settled = asyncio.Event()
        self._cancelled = False
        self._failure = ""
        self._routine_partial_count = 0
        self._progress_count = 0
        self._deferred_sent = False
        self._early_events: list[dict] = []
        self._ownership_confirmed = False
        self._prompt_command_id: str | None = None
        self._terminal_observed = False
        self._pending_approvals: dict[str, dict] = {}
        self.on_end_session = None

    async def handle_tool_call(self, call: dict) -> None:
        call_id = str(call.get("id") or "")
        name = call.get("name")
        args = call.get("args") if isinstance(call.get("args"), dict) else {}
        if name == "coding_task":
            await self._coding_task(call_id, args)
        elif name == "continue_task":
            await self._continue_task(call_id, args)
        elif name == "approval_decision":
            await self._approval_decision(call_id, args)
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
        except PiTUIBridgeError:
            self._settled.set()

    async def on_event(self, event: dict) -> None:
        if self.active_call_id and not self._deferred_sent:
            self._early_events.append(event)
            return
        await self._handle_event(event)

    async def _handle_event(self, event: dict) -> None:
        kind = event.get("type")
        if kind == "input_seen" and self.active_call_id and not self._terminal_observed:
            if event.get("owner") == "bridge":
                if self._ownership_confirmed or event.get("commandId") == self._prompt_command_id:
                    self._ownership_confirmed = True
                else:
                    self._failure = (
                        "Pi reported bridge input that did not match the active voice task."
                    )
                    self._settled.set()
            else:
                self._failure = (
                    "Pi received unrelated terminal or extension input while the voice task was "
                    "active, so its outcome cannot be attributed safely."
                )
                self._settled.set()
        elif kind in {"session_shutdown", "bridge_disconnect", "bridge_replaced"}:
            if self.active_call_id and not self._terminal_observed and not self._failure:
                self._failure = (
                    "The visible Pi session or semantic bridge ended before the voice task "
                    "produced attributable completion evidence."
                )
                self._settled.set()
        elif self._failure and self._settled.is_set():
            return
        elif kind == "agent_start" and self.phase == "starting":
            self.phase = "running"
        elif kind == "tool_execution_start" and self.active_call_id:
            await self._tool_started(event)
        elif kind == "approval_request" and self.active_call_id:
            await self._approval_requested(event)
        elif kind == "message_end":
            message = event.get("message")
            text = self._message_text(message)
            if text and self._ownership_confirmed:
                self.last_assistant_text = text
            if isinstance(message, dict) and message.get("stopReason") in {"error", "aborted"}:
                self._failure = str(message.get("errorMessage") or "Pi stopped with an error.")
        elif kind == "agent_settled":
            self._terminal_observed = True
            if not self._ownership_confirmed:
                self._failure = (
                    "Pi settled without confirming that the active turn belonged to the voice "
                    "task, so no outcome can be attributed safely."
                )
            self._settled.set()
        elif kind == "process_exit" and self.active_call_id and not self._terminal_observed:
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
        self._routine_partial_count = 0
        self._progress_count = 0
        self._deferred_sent = False
        self._early_events.clear()
        self._ownership_confirmed = False
        self._prompt_command_id = None
        self._terminal_observed = False
        self._settled.clear()
        try:
            response = await self.pi.command("prompt", message=request)
        except PiTUIBridgeError as exc:
            await self._result(call_id, f"Pi rejected the task: {exc}", outcome="failed")
            self._reset()
            return
        self._prompt_command_id = response.get("id")
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
        if self.phase in {"starting", "running"}:
            try:
                await self.pi.command("steer", message=request)
            except PiTUIBridgeError as exc:
                await self._result(call_id, f"Pi rejected the guidance: {exc}", outcome="failed")
                return
        else:
            await self._result(call_id, "There is no active coding task.", outcome="failed")
            return
        await self._result(call_id, "Passed that to the active coding task.", verified=True)

    async def _approval_requested(self, event: dict) -> None:
        approval_id = str(event.get("approvalId") or "")
        tool_name = str(event.get("toolName") or "tool")
        summary = str(event.get("summary") or "").strip()
        if not approval_id or approval_id in self._pending_approvals:
            return
        self._pending_approvals[approval_id] = {
            "tool": tool_name,
            "summary": summary,
        }
        await self.sender.send_tool_partial_result(
            self.active_call_id,
            {
                "speak": (
                    f"Pi wants to run {tool_name}: {summary}. Ask the user to allow once, "
                    "allow for this session, or block it."
                ),
                "data": {
                    "event": "approval_required",
                    "approval_id": approval_id,
                    "tool": tool_name,
                    "summary": summary,
                },
                "handle": self.handle,
            },
            reply=False,
        )
        await self.sender.send_voice_prompt(
            approval_id,
            (
                "A protected Pi action is waiting for explicit approval. "
                f"Approval ID: {approval_id}. Tool: {tool_name}. Target: {summary}. "
                "Ask the user now whether to allow once, allow for this session, or block. "
                "Do not approve it until the user answers."
            ),
        )

    async def _approval_decision(self, call_id: str, args: dict) -> None:
        approval_id = str(args.get("approval_id") or "")
        decision = str(args.get("decision") or "")
        pending = self._pending_approvals.get(approval_id)
        if pending is None or decision not in {"allow_once", "allow_session", "block"}:
            await self._result(
                call_id,
                "There is no matching pending approval; nothing was changed.",
                outcome="failed",
            )
            return
        try:
            await self.pi.command(
                "approval_response", approvalId=approval_id, decision=decision,
            )
        except PiTUIBridgeError as exc:
            await self._result(call_id, f"Pi rejected the approval response: {exc}", outcome="failed")
            return
        if decision == "allow_session":
            self._pending_approvals.clear()
        else:
            self._pending_approvals.pop(approval_id, None)
        labels = {
            "allow_once": "Allowed that action once.",
            "allow_session": "Allowed protected actions for this Pi session.",
            "block": "Blocked that action.",
        }
        await self._result(call_id, labels[decision], verified=True)

    async def _tool_started(self, event: dict) -> None:
        name = str(event.get("toolName") or "tool")
        args = event.get("args") if isinstance(event.get("args"), dict) else {}
        if name in {"edit", "write"}:
            if self._routine_partial_count >= self.MAX_ROUTINE_PARTIALS:
                return
            path = str(args.get("path") or args.get("file_path") or "a file")
            self._routine_partial_count += 1
            await self.sender.send_tool_partial_result(
                self.active_call_id,
                {
                    "speak": f"Pi is preparing to update {path}.",
                    "data": {"tool": name, "path": path},
                    "handle": self.handle,
                },
                reply=False,
            )
            return
        if name == "bash" and self._is_test_command(str(args.get("command") or "")):
            if self._routine_partial_count >= self.MAX_ROUTINE_PARTIALS:
                return
            self._routine_partial_count += 1
            await self.sender.send_tool_partial_result(
                self.active_call_id,
                {
                    "speak": "The coding agent is preparing to run tests.",
                    "data": {"tool": name, "command": str(args.get("command") or "")[:500]},
                    "handle": self.handle,
                },
                reply=True,
            )
            return
        if self._progress_count < self.MAX_PROGRESS:
            self._progress_count += 1
            detail = ""
            if name == "bash":
                command = " ".join(str(args.get("command") or "").split())[:180]
                detail = f": {command}" if command else ""
            elif args.get("path") or args.get("file_path"):
                path = str(args.get("path") or args.get("file_path"))[:180]
                detail = f": {path}"
            await self.sender.send_tool_progress(
                self.active_call_id, f"Pi is preparing to use {name}{detail}.",
            )

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
        self._deferred_sent = False
        self._early_events.clear()
        self._ownership_confirmed = False
        self._prompt_command_id = None
        self._terminal_observed = False
        self._pending_approvals.clear()
        self._settled.clear()
