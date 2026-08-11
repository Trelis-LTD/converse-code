# Design

Converse Code is a voice remote and reference implementation, not an agent framework. Pi owns the
visible coding session and terminal UI. Converse owns speech and the background-tool lifecycle.

## Public surface

- `coding_task(request)` is deferred and starts work only while the bridge is idle.
- `continue_task(request)` semantically steers active work.
- `end_session()` ends the voice session and gracefully shuts down Pi.
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
task, `deliverAs: "steer"` for active guidance, `abort()` for cancellation, and `shutdown()` for
graceful exit. It emits explicit message, tool, and settled events. It never reads terminal rows,
types keys, or infers menu state.

## Event mapping

| Visible Pi extension evidence | Converse control |
| --- | --- |
| acknowledged `sendUserMessage` command | `tool_deferred` |
| ordinary tool start | `tool_progress` |
| `edit` or `write` start | silent `tool_partial_result` |
| test command start | spoken `tool_partial_result`, `reply: true` |
| `message_end` | authoritative final-text candidate |
| `agent_settled` | one terminal `tool_result` |
| cancellation | Pi extension `abort()` |

Early events are buffered until `tool_deferred` has been delivered. One bridge-origin task is
active at a time because Pi's visible-TUI extension API does not attach a caller ID to lifecycle
events. The extension therefore emits an ownership event for each matched `sendUserMessage()`
input. Manual terminal input, another extension's input, session replacement, or bridge loss while
a voice task is active fails that Converse tool closed instead of attributing unrelated output.

## Approvals

The approval extension intercepts `bash`, `edit`, and `write` before execution and calls native
`ctx.ui.select()` with target-specific allow-once, allow-for-session, and block choices. Pi renders
and owns that menu. Converse never navigates it. `reply: true` is used for meaningful spoken
progress, not remote menu control.

## Evidence and outcomes

Prompt acknowledgement proves only that Pi accepted the user message. `agent_settled` proves Pi
will not continue automatically. It does not independently verify claims about files, tests,
browsers, or external systems, so ordinary completion remains `verified: false`.

## Browser boundary

The page contains microphone control and status only. It has no text input or duplicate chat
transcript. Python holds the persistent Converse key and mints a short-lived browser credential.
Controls are sequenced, acknowledged, retained across disconnects, and replayed after reconnect.
