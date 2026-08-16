# Design

Converse Code is a voice remote and reference implementation, not an agent framework. Pi owns the
visible coding session and terminal UI. Converse owns speech and the background-tool lifecycle.

## Public surface

- `pi_request(user_request)` starts one deferred Pi turn while idle and semantically steers it while
  working.
- `pi_approval(approval_id, decision)` resolves only a matching pending approval.
- `pi_cancel()` calls Pi's documented extension `abort()` API without ending voice.

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
turn, `deliverAs: "steer"` for active guidance, `abort()` for cancellation, and `shutdown()` for
graceful exit. Model questions and changes go through the same natural-language Pi message as any
other request. A Pi-internal `pi_session_model` capability reads `ctx.model`/`modelRegistry` and
changes the session through `setModel()`; it is not a Converse tool or a menu driver. The extension
emits explicit message, tool, and settled events. It never reads terminal rows, types keys, or
infers menu state.

## Event mapping

| Visible Pi extension evidence | Converse control |
| --- | --- |
| acknowledged `sendUserMessage` command | `tool_deferred` |
| ordinary tool start | silent structured `tool_partial_result` |
| approval request | structured `tool_partial_result` with `interaction` prompt and choices |
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
approval ID, and waits without opening a terminal menu. Converse receives the ID, tool, target, and
valid decisions as a structured background-tool interaction. Converse queues its narration when
the voice floor is busy and exposes the narration lifecycle. Only an explicit
`pi_approval` carrying the pending ID can resume the Pi hook. A new `pi_request` first blocks any
pending approval and then steers the change of course. Stale IDs, malformed decisions, disconnects,
cancellation, and timeouts fail closed.

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
