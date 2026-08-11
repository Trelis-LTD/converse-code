# Design

Converse Code is a reference, not a general agent framework. Pi owns the coding loop; Converse
owns conversation, background-tool lifecycle, and speech.

## Public surface

- `coding_task(request)` is deferred. It starts work only while idle.
- `continue_task(request)` steers active work or answers its blocking structured request.
- `end_session()` ends the Converse session.
- Cancellation is part of the tool protocol and maps to Pi `abort`; it is not another tool.

## Event mapping

| Pi RPC event | Converse event |
| --- | --- |
| accepted `prompt` response | `tool_deferred` |
| ordinary tool start | `tool_progress` |
| `edit` or `write` start | silent `tool_partial_result` |
| test command start | `tool_partial_result`, `reply: true` |
| blocking extension UI request | structured `tool_partial_result`, `reply: true` |
| `message_end` | authoritative final-text candidate |
| `agent_settled` | one terminal `tool_result` |
| tool cancellation | Pi `abort` |

The router has one state: `idle`, `starting`, `running`, `awaiting_input`, or `canceling`. Early
Pi events are buffered until `tool_deferred` has been delivered, preserving the public lifecycle.

The bundled Pi extension intercepts `bash`, `edit`, and `write` before execution and calls
`ctx.ui.select` with allow-once, allow-for-session, and block choices. Pi RPC turns that into an
ID-correlated extension UI request. The router never guesses at a menu or types keys; it sends the
user's explicit answer back by request ID.

## Outcomes

Prompt acceptance proves only that Pi accepted work. `agent_settled` proves that Pi stopped
automatically processing it. It does not independently verify claimed file, test, browser, or
external-system effects, so ordinary completion is `outcome: succeeded, verified: false`.

Immediate protocol actions such as accepted steering are verifiable and may use `verified: true`.
Cancellation uses the Browser SDK spelling `outcome: cancelled`.

## Browser boundary

The local page uses the official Browser SDK for voice, text, tools, and partial replies. Python
holds the persistent Converse key and mints a short-lived credential for the page. Local controls
are sequenced, acknowledged, retained across disconnects, and replayed after SDK reconnection.
