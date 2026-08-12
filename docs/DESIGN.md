# Design

Converse Code is a voice remote and reference implementation, not an agent framework. Pi owns the
visible coding session and terminal UI. Converse owns speech and the background-tool lifecycle.

## Public surface

- `coding_task(request)` is deferred and starts work only while the bridge is idle.
- `continue_task(request)` semantically steers active work.
- `approval_decision(approval_id, decision)` resolves only a matching pending approval.
- `pi_model(request)` reads authoritative model state and changes it only when the request uniquely
  names an available model, using Pi's semantic `setModel()` API.
- Tool cancellation calls Pi's documented extension `abort()` API.

## Boundary

```text
Converse voice model
        │ background tool call / cancellation
        ▼
voice-only Browser SDK page
        │ acknowledged localhost controls
        ▼
Python lifecycle router
        │ ID-correlated local WebSocket
        ▼
Pi extension ── sendUserMessage / events ── visible Pi TUI ── Codex
```

Pi is launched in its default interactive mode. The extension uses `sendUserMessage()` for a new
task, `deliverAs: "steer"` for active guidance, `setModel()` for model changes, `abort()` for
cancellation, and `shutdown()` for graceful exit. It emits explicit message, tool, and settled
events. It never reads terminal rows, types keys, or infers menu state.

## Event mapping

| Visible Pi extension evidence | Converse control |
| --- | --- |
| acknowledged `sendUserMessage` command | `tool_deferred` |
| ordinary tool start | `tool_progress` |
| `edit` or `write` start | silent `tool_partial_result` |
| test command start | spoken `tool_partial_result`, `reply: true` |
| approval request | spoken structured `tool_partial_result`, `reply: true` |
| `message_end` | authoritative final-text candidate |
| `agent_settled` | one terminal `tool_result` |
| cancellation | Pi extension `abort()` |

Early events are buffered until `tool_deferred` has been delivered. One bridge-origin task is
active at a time because Pi's visible-TUI extension API does not attach a caller ID to lifecycle
events. The extension therefore emits an ownership event for each matched `sendUserMessage()`
input. Manual terminal input, another extension's input, session replacement, or bridge loss while
a voice task is active fails that Converse tool closed instead of attributing unrelated output.

## Approvals

The bridge extension intercepts `bash`, `edit`, and `write` before execution, creates a unique
approval ID, and waits without opening a terminal menu. Converse receives the target as a
structured background-tool partial with `reply: true`, which asks the user aloud to allow once,
allow for the session, or block. Only an explicit `approval_decision` carrying the pending ID can
resume the Pi hook. Stale
IDs, malformed decisions, disconnects, cancellation, and timeouts fail closed.

## Evidence and outcomes

Prompt acknowledgement proves only that Pi accepted the user message. `agent_settled` proves Pi
will not continue automatically. It does not independently verify claims about files, tests,
browsers, or external systems, so ordinary completion remains `verified: false`.

## Browser boundary

Converse owns conversational ending. Its intentional transport close is exposed by the Browser SDK
as `session_end`; the page forwards that structured lifecycle event and the host gracefully shuts
down Pi. Converse Code does not duplicate end intent with a phrase matcher or tool.

The page has microphone control but no text input. It mirrors Browser SDK speech, reply, and tool
lifecycle events; Pi remains the canonical coding transcript. Python holds the persistent Converse
key and mints a short-lived browser credential. Controls are sequenced, acknowledged, retained
across disconnects, and replayed after reconnect.
