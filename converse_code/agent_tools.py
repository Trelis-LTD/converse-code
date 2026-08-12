"""Converse background tools controlling a visible Pi TUI semantically."""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from typing import Literal

from .pi_tui import PiTUIBridgeError

TOOL_TIMEOUT_S = 30
DEFERRED_TIMEOUT_S = 7200

CODING_TASK_DESCRIPTION = (
    "Default handoff for every actionable user request or question to the visible Pi coding agent. "
    "Pi owns the coding environment and its runtime configuration, including its active model. "
    "Only pure social chat, an explicit session end, or a response to a pending approval is not a "
    "new coding_task. Preserve the complete wording; never ask whether to pass it. The task runs "
    "in the background, with progress, partial results, and completion delivered automatically. "
    "For an already-active task, use continue_task instead."
)
CONTINUE_TASK_DESCRIPTION = (
    "Send guidance or a requested answer to the coding task already in progress. Preserve the "
    "user's wording. Use this for refinements and corrections while Pi is still working."
)
PI_MODEL_DESCRIPTION = (
    "Authoritative interface to the visible Pi coding agent's model state. Always call for any "
    "question about Pi's current or available model and for any request to change or switch it. "
    "Pass the user's complete exact words unchanged. Never infer, choose, or insert a model the "
    "user did not name. With no uniquely named available model, this reads Pi's current and "
    "available models without changing state; with one, it changes Pi semantically and reports "
    "the acknowledged selection."
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
    model_request = {
        "request": {
            "type": "string",
            "description": "The user's requested Pi provider, model, or alias.",
        },
    }
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
        tool(
            "pi_model", PI_MODEL_DESCRIPTION, model_request, ["request"], timeout=30,
        ),
    ]


@dataclass(frozen=True)
class TaskResult:
    outcome: Literal["succeeded", "failed", "cancelled"]
    speak: str


@dataclass(frozen=True)
class OwnedInput:
    command_id: str


@dataclass(frozen=True)
class ForeignInput:
    pass


@dataclass(frozen=True)
class SessionLost:
    pass


@dataclass(frozen=True)
class ProcessExited:
    status: int


@dataclass(frozen=True)
class ToolStarted:
    name: str
    args: dict


@dataclass(frozen=True)
class ApprovalRequested:
    approval_id: str
    tool_name: str
    summary: str


@dataclass(frozen=True)
class AssistantMessage:
    text: str


@dataclass(frozen=True)
class MessageFailed:
    error: str


@dataclass(frozen=True)
class AgentSettled:
    pass


@dataclass(frozen=True)
class InvalidPiEvent:
    reason: str


PiEvent = (
    OwnedInput | ForeignInput | SessionLost | ProcessExited | ToolStarted | ApprovalRequested
    | AssistantMessage | MessageFailed | AgentSettled | InvalidPiEvent
)


@dataclass
class PendingTask:
    call_id: str
    result: asyncio.Future[TaskResult]
    events: list[PiEvent] = field(default_factory=list)


@dataclass
class RunningTask:
    call_id: str
    result: asyncio.Future[TaskResult]
    command_id: str
    ownership_confirmed: bool = False
    assistant_text: str = ""
    routine_partials: int = 0
    progress_updates: int = 0
    approvals: set[str] = field(default_factory=set)


@dataclass(frozen=True)
class EndingTask:
    call_id: str
    result: asyncio.Future[TaskResult]
    outcome: TaskResult


@dataclass(frozen=True)
class CompletedTask:
    call_id: str


ActiveTask = PendingTask | RunningTask | EndingTask | CompletedTask


class AgentToolRouter:
    """Translate one parsed Pi episode into one Converse background-tool lifecycle."""

    MAX_ROUTINE_PARTIALS = 3
    MAX_PROGRESS = 12

    def __init__(self, pi, sender, *, handle: str) -> None:
        self.pi = pi
        self.sender = sender
        self.handle = handle
        self._task: ActiveTask | None = None

    @property
    def active_call_id(self) -> str | None:
        return self._task.call_id if self._task is not None else None

    async def handle_tool_call(self, call: dict) -> None:
        call_id = call.get("id")
        if not isinstance(call_id, str) or not call_id:
            return
        name = call.get("name")
        args = call.get("args")
        if not isinstance(name, str) or not isinstance(args, dict):
            await self._result(call_id, "The tool request was malformed.", outcome="failed")
        elif name == "coding_task":
            await self._coding_task(call_id, self._request(args))
        elif name == "continue_task":
            await self._continue_task(call_id, self._request(args))
        elif name == "approval_decision":
            await self._approval_decision(call_id, args)
        elif name == "pi_model":
            await self._pi_model(call_id, self._request(args))
        else:
            await self._result(call_id, f"Unknown tool: {name}", outcome="failed")

    async def handle_tool_cancel(self, call: dict) -> None:
        task = self._task
        if not isinstance(call.get("id"), str) or call["id"] != self.active_call_id:
            return
        if not isinstance(task, (PendingTask, RunningTask)):
            return
        cancelled = TaskResult("cancelled", "The coding task was cancelled.")
        self._task = EndingTask(task.call_id, task.result, cancelled)
        try:
            await self.pi.command("abort")
        except PiTUIBridgeError:
            self._complete(cancelled)

    async def on_event(self, event: dict) -> None:
        parsed = self._parse_event(event)
        if parsed is None:
            return
        if isinstance(self._task, PendingTask):
            self._task.events.append(parsed)
            return
        await self._handle_event(parsed)

    async def _handle_event(self, event: PiEvent) -> None:
        task = self._task
        if task is None or isinstance(task, CompletedTask):
            return
        if isinstance(task, EndingTask):
            if isinstance(event, (AgentSettled, SessionLost, ProcessExited)):
                self._complete(task.outcome)
            return
        if isinstance(event, InvalidPiEvent):
            self._complete(TaskResult("failed", event.reason))
        elif isinstance(event, SessionLost):
            self._complete(TaskResult(
                "failed",
                "The visible Pi session or semantic bridge ended before the voice task "
                "produced attributable completion evidence.",
            ))
        elif isinstance(event, ProcessExited):
            self._complete(TaskResult(
                "failed", f"Pi exited unexpectedly with status {event.status}.",
            ))
        elif not isinstance(task, RunningTask):
            return
        elif isinstance(event, OwnedInput):
            if task.ownership_confirmed or event.command_id == task.command_id:
                task.ownership_confirmed = True
            else:
                self._complete(TaskResult(
                    "failed", "Pi reported bridge input that did not match the active voice task.",
                ))
        elif isinstance(event, ForeignInput):
            self._complete(TaskResult(
                "failed",
                "Pi received unrelated terminal or extension input while the voice task was "
                "active, so its outcome cannot be attributed safely.",
            ))
        elif isinstance(event, ToolStarted):
            await self._tool_started(task, event.name, event.args)
        elif isinstance(event, ApprovalRequested):
            await self._approval_requested(
                task, event.approval_id, event.tool_name, event.summary,
            )
        elif isinstance(event, MessageFailed):
            self._task = EndingTask(
                task.call_id, task.result, TaskResult("failed", event.error),
            )
        elif isinstance(event, AssistantMessage):
            if task.ownership_confirmed and event.text:
                task.assistant_text = event.text
        elif isinstance(event, AgentSettled):
            if task.ownership_confirmed:
                self._complete(TaskResult(
                    "succeeded",
                    task.assistant_text or "The coding task finished without a text summary.",
                ))
            else:
                self._complete(TaskResult(
                    "failed",
                    "Pi settled without confirming that the active turn belonged to the voice "
                    "task, so no outcome can be attributed safely.",
                ))

    async def _coding_task(self, call_id: str, request: str | None) -> None:
        if request is None:
            await self._result(call_id, "A coding instruction is required.", outcome="failed")
            return
        if self._task is not None:
            await self._result(
                call_id, "A coding task is already active. Use continue_task.", outcome="failed",
            )
            return
        result = asyncio.get_running_loop().create_future()
        pending = PendingTask(call_id, result)
        self._task = pending
        try:
            receipt = await self.pi.command("prompt", message=request)
        except PiTUIBridgeError as exc:
            if isinstance(self._task, EndingTask):
                self._complete(self._task.outcome)
                outcome = await result
                await self._result(call_id, outcome.speak, outcome=outcome.outcome)
            else:
                await self._result(call_id, f"Pi rejected the task: {exc}", outcome="failed")
            self._task = None
            return
        await self.sender.send_tool_deferred(call_id, self.handle, status_label="Coding task")
        if isinstance(self._task, PendingTask):
            early_events = tuple(self._task.events)
            self._task = RunningTask(call_id, result, receipt.command_id)
            for event in early_events:
                await self._handle_event(event)
        outcome = await result
        await self._result(call_id, outcome.speak, outcome=outcome.outcome)
        self._task = None

    async def _continue_task(self, call_id: str, request: str | None) -> None:
        if request is None:
            await self._result(call_id, "A reply or instruction is required.", outcome="failed")
            return
        if not isinstance(self._task, (PendingTask, RunningTask)):
            await self._result(call_id, "There is no active coding task.", outcome="failed")
            return
        try:
            await self.pi.command("steer", message=request)
        except PiTUIBridgeError as exc:
            await self._result(call_id, f"Pi rejected the guidance: {exc}", outcome="failed")
            return
        await self._result(call_id, "Passed that to the active coding task.", verified=True)

    async def _pi_model(self, call_id: str, request: str | None) -> None:
        if request is None:
            await self._result(call_id, "A model is required.", outcome="failed")
            return
        if self._task is not None:
            await self._result(
                call_id, "Wait for the active coding task to finish before changing model.",
                outcome="failed",
            )
            return
        try:
            receipt = await self.pi.command("model_state", request=request)
        except PiTUIBridgeError as exc:
            await self._result(call_id, f"Pi could not change model: {exc}", outcome="failed")
            return
        provider, model = receipt.data.get("provider"), receipt.data.get("model")
        changed, available = receipt.data.get("changed"), receipt.data.get("available")
        if (
            not isinstance(provider, str) or not provider
            or not isinstance(model, str) or not model
            or type(changed) is not bool
            or not isinstance(available, list)
            or not all(isinstance(item, str) and item for item in available)
        ):
            await self._result(
                call_id, "Pi acknowledged the request without valid model state.",
                outcome="failed",
            )
            return
        data = {
            "provider": provider, "model": model,
            "changed": changed, "available": available,
        }
        if not changed:
            choices = ", ".join(available[:-1]) + (
                f" and {available[-1]}" if len(available) > 1 else available[0]
            )
            await self._result(
                call_id,
                f"Pi is using {provider}/{model}. Available models are {choices}. "
                "Which one would you like?",
                data=data,
                verified=True,
            )
            return
        await self._result(
            call_id, f"Pi is now using {provider}/{model}.",
            data=data, verified=True,
        )

    async def _approval_requested(
        self, task: RunningTask, approval_id: str, tool_name: str, summary: str,
    ) -> None:
        if approval_id in task.approvals:
            return
        task.approvals.add(approval_id)
        await self.sender.send_tool_partial_result(
            task.call_id,
            {
                "speak": (
                    f"Pi wants to run {tool_name}: {summary}. Ask the user to allow once, "
                    "allow for this session, or block it."
                ),
                "data": {
                    "event": "approval_required", "approval_id": approval_id,
                    "tool": tool_name, "summary": summary,
                },
                "handle": self.handle,
            },
            reply=True,
        )

    async def _approval_decision(self, call_id: str, args: dict) -> None:
        task = self._task
        approval_id = args.get("approval_id")
        decision = args.get("decision")
        choices = {"allow_once", "allow_session", "block"}
        if (
            not isinstance(task, RunningTask)
            or not isinstance(approval_id, str) or approval_id not in task.approvals
            or not isinstance(decision, str) or decision not in choices
        ):
            await self._result(
                call_id, "There is no matching pending approval; nothing was changed.",
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
            task.approvals.clear()
        else:
            task.approvals.remove(approval_id)
        labels = {
            "allow_once": "Allowed that action once.",
            "allow_session": "Allowed protected actions for this Pi session.",
            "block": "Blocked that action.",
        }
        await self._result(call_id, labels[decision], verified=True)

    async def _tool_started(self, task: RunningTask, name: str, args: dict) -> None:
        if name in {"edit", "write"}:
            if task.routine_partials >= self.MAX_ROUTINE_PARTIALS:
                return
            path = self._text(args.get("path")) or self._text(args.get("file_path")) or "a file"
            task.routine_partials += 1
            await self.sender.send_tool_partial_result(
                task.call_id,
                {
                    "speak": f"Pi is preparing to update {path}.",
                    "data": {"tool": name, "path": path}, "handle": self.handle,
                },
                reply=False,
            )
            return
        command = self._text(args.get("command")) or ""
        if name == "bash" and self._is_test_command(command):
            if task.routine_partials >= self.MAX_ROUTINE_PARTIALS:
                return
            task.routine_partials += 1
            await self.sender.send_tool_partial_result(
                task.call_id,
                {
                    "speak": "The coding agent is preparing to run tests.",
                    "data": {"tool": name, "command": command[:500]}, "handle": self.handle,
                },
                reply=True,
            )
            return
        if task.progress_updates >= self.MAX_PROGRESS:
            return
        task.progress_updates += 1
        detail = ""
        if name == "bash" and command:
            detail = f": {' '.join(command.split())[:180]}"
        elif path := self._text(args.get("path")) or self._text(args.get("file_path")):
            detail = f": {path[:180]}"
        await self.sender.send_tool_progress(
            task.call_id, f"Pi is preparing to use {name}{detail}.",
        )

    def _complete(self, outcome: TaskResult) -> None:
        task = self._task
        if task is None or isinstance(task, CompletedTask):
            return
        task.result.set_result(outcome)
        self._task = CompletedTask(task.call_id)

    @staticmethod
    def _request(args: dict) -> str | None:
        value = args.get("request")
        if not isinstance(value, str) or not (value := value.strip()):
            return None
        return value

    @staticmethod
    def _text(value) -> str | None:
        return value if isinstance(value, str) and value else None

    @staticmethod
    def _is_test_command(command: str) -> bool:
        return bool(re.search(r"(?:^|\s)(?:pytest|test|tests|cargo test|go test|npm test)", command))

    @classmethod
    def _parse_event(cls, event) -> PiEvent | None:
        if not isinstance(event, dict) or not isinstance(event.get("type"), str):
            return None
        kind = event["type"]
        if kind == "input_seen":
            owner = event.get("owner")
            command_id = event.get("commandId")
            if owner == "bridge" and (not isinstance(command_id, str) or not command_id):
                return InvalidPiEvent("Pi sent malformed input ownership evidence.")
            if owner not in {"bridge", "interactive", "other"}:
                return InvalidPiEvent("Pi sent malformed input ownership evidence.")
            return OwnedInput(command_id) if owner == "bridge" else ForeignInput()
        if kind in {"session_shutdown", "bridge_disconnect", "bridge_replaced"}:
            return SessionLost()
        if kind == "process_exit":
            status = event.get("status")
            return (
                ProcessExited(status)
                if type(status) is int
                else InvalidPiEvent("Pi sent a malformed process exit event.")
            )
        if kind == "tool_execution_start":
            name = cls._text(event.get("toolName"))
            args = event.get("args")
            return (
                ToolStarted(name, args)
                if name is not None and isinstance(args, dict)
                else InvalidPiEvent("Pi sent a malformed tool event.")
            )
        if kind == "approval_request":
            values = tuple(
                cls._text(event.get(key))
                for key in ("approvalId", "toolName", "summary")
            )
            if any(value is None or not value.strip() for value in values):
                return InvalidPiEvent("Pi sent a malformed approval request.")
            return ApprovalRequested(*(value.strip() for value in values))
        if kind == "message_end":
            message = event.get("message")
            if not isinstance(message, dict):
                return InvalidPiEvent("Pi sent a malformed terminal message.")
            if message.get("stopReason") in {"error", "aborted"}:
                supplied = message.get("errorMessage")
                return MessageFailed(
                    supplied if isinstance(supplied, str) and supplied
                    else "Pi stopped with an error."
                )
            return AssistantMessage(cls._message_text(message))
        if kind == "agent_settled":
            return AgentSettled()
        if kind == "extension_error":
            error = cls._text(event.get("error")) or "unknown extension error"
            return InvalidPiEvent(f"The Pi extension failed: {error}")
        return None

    @staticmethod
    def _message_text(message: dict) -> str:
        if message.get("role") != "assistant":
            return ""
        content = message.get("content")
        if isinstance(content, str):
            return content.strip()
        if not isinstance(content, list):
            return ""
        return "\n".join(
            item["text"].strip()
            for item in content
            if isinstance(item, dict) and item.get("type") == "text"
            and isinstance(item.get("text"), str) and item["text"].strip()
        )

    async def _result(
        self, call_id: str, speak: str, *, data: dict | None = None,
        outcome: str = "succeeded", verified: bool = False,
    ) -> None:
        await self.sender.send_tool_result(
            call_id, {"speak": speak, "data": data or {}, "handle": self.handle},
            outcome=outcome, verified=verified,
        )
