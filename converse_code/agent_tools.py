"""Minimal Converse controls for one visible Pi session."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Literal

from .pi_tui import PiTUIBridgeError

TOOL_TIMEOUT_S = 30
DEFERRED_TIMEOUT_S = 7200
DECISIONS = {
    "allow_once": "Allow once",
    "allow_session": "Allow for this session",
    "block": "Block",
}


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

    request = {
        "user_request": {
            "type": "string",
            "description": (
                "The user's request or question for Pi, preserving their intent. This must be "
                "an instruction or question for Pi—not assistant narration, an inferred answer, "
                "or a claim that the requested work already happened."
            ),
        },
    }
    return [
        tool(
            "pi_request",
            (
                "Hand an actionable coding, repository, environment, or Pi question to Pi. "
                "Forward the user's request faithfully and let Pi inspect or act; never substitute "
                "your own progress narration or unsupported answer."
            ),
            request,
            ["user_request"],
            timeout=TOOL_TIMEOUT_S,
            deferred=True,
            deferred_timeout=DEFERRED_TIMEOUT_S,
            notify_on_complete=True,
            status_label="Pi",
        ),
        tool(
            "pi_approval",
            "Submit the user's explicit decision for a pending Pi approval using its supplied ID.",
            {
                "approval_id": {"type": "string", "description": "The pending approval ID."},
                "decision": {"type": "string", "enum": list(DECISIONS)},
            },
            ["approval_id", "decision"],
            timeout=15,
        ),
        tool(
            "pi_cancel",
            "Cancel Pi's current turn without ending the voice session.",
            timeout=15,
        ),
    ]


@dataclass(frozen=True)
class TaskResult:
    outcome: Literal["succeeded", "failed", "cancelled"]
    event: str
    message: str = ""


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
    arguments: dict


@dataclass(frozen=True)
class ApprovalRequested:
    approval_id: str
    tool: str
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
class PendingTurn:
    call_id: str
    result: asyncio.Future[TaskResult]
    events: list[PiEvent] = field(default_factory=list)


@dataclass
class RunningTurn:
    call_id: str
    result: asyncio.Future[TaskResult]
    command_id: str
    owned: bool = False
    assistant_text: str = ""
    approvals: set[str] = field(default_factory=set)


@dataclass(frozen=True)
class CancellingTurn:
    call_id: str
    result: asyncio.Future[TaskResult]


@dataclass(frozen=True)
class CompletedTurn:
    call_id: str


ActiveTurn = PendingTurn | RunningTurn | CancellingTurn | CompletedTurn


class PiControlRouter:
    """Map Converse's human-like controls onto one attributable Pi turn."""

    def __init__(self, pi, sender, *, handle: str) -> None:
        self.pi = pi
        self.sender = sender
        self.handle = handle
        self._turn: ActiveTurn | None = None

    @property
    def active_call_id(self) -> str | None:
        return self._turn.call_id if self._turn is not None else None

    async def handle_tool_call(self, call: dict) -> None:
        call_id = call.get("id")
        if not isinstance(call_id, str) or not call_id:
            return
        name, args = call.get("name"), call.get("args")
        if not isinstance(name, str) or not isinstance(args, dict):
            await self._error(call_id, "malformed_tool_call")
        elif name == "pi_request":
            await self._pi_request(call_id, self._text(args.get("user_request")))
        elif name == "pi_approval":
            await self._pi_approval(call_id, args)
        elif name == "pi_cancel":
            await self._pi_cancel(call_id)
        else:
            await self._error(call_id, "unknown_tool")

    async def handle_tool_cancel(self, call: dict) -> None:
        if call.get("id") != self.active_call_id:
            return
        await self._request_cancel()

    async def on_event(self, event: dict) -> None:
        parsed = self._parse_event(event)
        if parsed is None:
            return
        if isinstance(self._turn, PendingTurn):
            self._turn.events.append(parsed)
            return
        await self._handle_event(parsed)

    async def _pi_request(self, call_id: str, request: str | None) -> None:
        if request is None:
            await self._error(call_id, "user_request_required")
            return
        if isinstance(self._turn, (PendingTurn, RunningTurn)):
            await self._steer(call_id, request)
            return
        if self._turn is not None:
            await self._error(call_id, "pi_turn_settling")
            return

        result = asyncio.get_running_loop().create_future()
        self._turn = PendingTurn(call_id, result)
        try:
            command_id = await self.pi.command("prompt", message=request)
        except PiTUIBridgeError as exc:
            await self._error(call_id, "pi_rejected_message", reason=str(exc))
            self._turn = None
            return

        await self.sender.send_tool_deferred(call_id, self.handle, status_label="Pi")
        if isinstance(self._turn, PendingTurn):
            early_events = tuple(self._turn.events)
            self._turn = RunningTurn(call_id, result, command_id)
            for event in early_events:
                await self._handle_event(event)
        outcome = await result
        content = {
            "event": outcome.event, "pi_response": outcome.message, "handle": self.handle,
        }
        await self.sender.send_tool_result(
            call_id, content, outcome=outcome.outcome, verified=False,
        )
        self._turn = None

    async def _steer(self, call_id: str, message: str) -> None:
        turn = self._turn
        if isinstance(turn, RunningTurn):
            for approval_id in tuple(turn.approvals):
                try:
                    await self.pi.command(
                        "approval_response", approvalId=approval_id, decision="block",
                    )
                except PiTUIBridgeError as exc:
                    await self._error(
                        call_id, "pi_rejected_approval", reason=str(exc),
                    )
                    return
                turn.approvals.remove(approval_id)
        try:
            await self.pi.command("steer", message=message)
        except PiTUIBridgeError as exc:
            await self._error(call_id, "pi_rejected_message", reason=str(exc))
            return
        await self._result(
            call_id,
            {"event": "pi_message_delivered", "mode": "steer", "task_status": "running"},
            verified=True,
        )

    async def _pi_approval(self, call_id: str, args: dict) -> None:
        turn = self._turn
        approval_id, decision = args.get("approval_id"), args.get("decision")
        if (
            not isinstance(turn, RunningTurn)
            or not isinstance(approval_id, str) or approval_id not in turn.approvals
            or decision not in DECISIONS
        ):
            await self._error(call_id, "approval_not_pending")
            return
        try:
            await self.pi.command(
                "approval_response", approvalId=approval_id, decision=decision,
            )
        except PiTUIBridgeError as exc:
            await self._error(call_id, "pi_rejected_approval", reason=str(exc))
            return
        if decision == "allow_session":
            turn.approvals.clear()
        else:
            turn.approvals.remove(approval_id)
        await self._result(
            call_id,
            {"event": "pi_approval_delivered", "decision": decision, "task_status": "running"},
            verified=True,
        )

    async def _pi_cancel(self, call_id: str) -> None:
        if not isinstance(self._turn, (PendingTurn, RunningTurn)):
            await self._error(call_id, "no_active_pi_turn")
            return
        await self._request_cancel()
        await self._result(
            call_id,
            {"event": "pi_cancel_requested", "task_status": "cancelling"},
            verified=True,
        )

    async def _request_cancel(self) -> None:
        turn = self._turn
        if not isinstance(turn, (PendingTurn, RunningTurn)):
            return
        self._turn = CancellingTurn(turn.call_id, turn.result)
        try:
            await self.pi.command("abort")
        except PiTUIBridgeError:
            self._complete(TaskResult("cancelled", "pi_turn_cancelled"))

    async def _handle_event(self, event: PiEvent) -> None:
        turn = self._turn
        if turn is None or isinstance(turn, CompletedTurn):
            return
        if isinstance(turn, CancellingTurn):
            if isinstance(event, (AgentSettled, SessionLost, ProcessExited)):
                self._complete(TaskResult("cancelled", "pi_turn_cancelled"))
            return
        if isinstance(event, InvalidPiEvent):
            self._complete(TaskResult("failed", "invalid_pi_event", event.reason))
        elif isinstance(event, SessionLost):
            self._complete(TaskResult("failed", "pi_session_lost"))
        elif isinstance(event, ProcessExited):
            self._complete(TaskResult("failed", "pi_process_exited", str(event.status)))
        elif not isinstance(turn, RunningTurn):
            return
        elif isinstance(event, OwnedInput):
            if turn.owned or event.command_id == turn.command_id:
                turn.owned = True
            else:
                self._complete(TaskResult("failed", "pi_input_mismatch"))
        elif isinstance(event, ForeignInput):
            self._complete(TaskResult("failed", "unrelated_pi_input"))
        elif isinstance(event, ToolStarted):
            await self.sender.send_tool_partial_result(
                turn.call_id,
                {
                    "event": "pi_tool_started", "tool": event.name,
                    "arguments": event.arguments, "handle": self.handle,
                },
            )
        elif isinstance(event, ApprovalRequested):
            if event.approval_id in turn.approvals:
                return
            turn.approvals.add(event.approval_id)
            await self.sender.send_tool_partial_result(
                turn.call_id,
                {
                    "event": "pi_approval_required", "approval_id": event.approval_id,
                    "tool": event.tool, "summary": event.summary,
                    "decisions": list(DECISIONS), "handle": self.handle,
                },
                interaction={
                    "prompt": f"Allow Pi to run {event.tool}: {event.summary}?",
                    "options": list(DECISIONS.values()),
                },
            )
        elif isinstance(event, MessageFailed):
            self._complete(TaskResult("failed", "pi_message_failed", event.error))
        elif isinstance(event, AssistantMessage):
            if turn.owned and event.text:
                turn.assistant_text = event.text
        elif isinstance(event, AgentSettled):
            if turn.owned:
                self._complete(TaskResult("succeeded", "pi_settled", turn.assistant_text))
            else:
                self._complete(TaskResult("failed", "pi_ownership_unconfirmed"))

    def _complete(self, outcome: TaskResult) -> None:
        turn = self._turn
        if turn is None or isinstance(turn, CompletedTurn):
            return
        if not turn.result.done():
            turn.result.set_result(outcome)
        self._turn = CompletedTurn(turn.call_id)

    async def _result(
        self, call_id: str, content: dict, *, outcome: str = "succeeded",
        verified: bool = False,
    ) -> None:
        await self.sender.send_tool_result(
            call_id, content, outcome=outcome, verified=verified,
        )

    async def _error(self, call_id: str, event: str, **data) -> None:
        await self._result(call_id, {"event": event, **data}, outcome="failed")

    @staticmethod
    def _text(value) -> str | None:
        return value.strip() if isinstance(value, str) and value.strip() else None

    @classmethod
    def _parse_event(cls, event) -> PiEvent | None:
        if not isinstance(event, dict) or not isinstance(event.get("type"), str):
            return None
        kind = event["type"]
        if kind == "input_seen":
            owner, command_id = event.get("owner"), event.get("commandId")
            if owner == "bridge" and not cls._text(command_id):
                return InvalidPiEvent("malformed input ownership")
            if owner not in {"bridge", "interactive", "other"}:
                return InvalidPiEvent("malformed input ownership")
            return OwnedInput(command_id) if owner == "bridge" else ForeignInput()
        if kind in {"session_shutdown", "bridge_disconnect", "bridge_replaced"}:
            return SessionLost()
        if kind == "process_exit":
            status = event.get("status")
            return ProcessExited(status) if type(status) is int else InvalidPiEvent("malformed exit")
        if kind == "tool_execution_start":
            name, arguments = cls._text(event.get("toolName")), event.get("args")
            return (
                ToolStarted(name, arguments)
                if name is not None and isinstance(arguments, dict)
                else InvalidPiEvent("malformed tool event")
            )
        if kind == "approval_request":
            values = tuple(cls._text(event.get(key)) for key in ("approvalId", "toolName", "summary"))
            return (
                ApprovalRequested(*values)
                if all(value is not None for value in values)
                else InvalidPiEvent("malformed approval request")
            )
        if kind == "message_end":
            message = event.get("message")
            if not isinstance(message, dict):
                return InvalidPiEvent("malformed terminal message")
            if message.get("stopReason") in {"error", "aborted"}:
                error = cls._text(message.get("errorMessage")) or "Pi stopped with an error."
                return MessageFailed(error)
            return AssistantMessage(cls._message_text(message))
        if kind == "agent_settled":
            return AgentSettled()
        if kind == "extension_error":
            return InvalidPiEvent(cls._text(event.get("error")) or "unknown extension error")
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
