# Converse Code architecture

This document describes the current client. It is not a roadmap or design diary.

## Runtime shape

`converse-code` starts the real interactive `claude` CLI inside a pseudo-terminal and serves a
single localhost browser page. The terminal remains directly usable. The browser owns microphone
capture and assistant playback through the bundled `@trelis/converse` Browser SDK.

```text
browser SDK  <-- localhost /proxy -->  converse-code  <-- WebSocket --> hosted Converse
     |                                      |
 microphone + speaker                 PTY + Claude hooks
                                            |
                                      interactive claude CLI
```

There are four client-side boundaries:

| Component | Responsibility |
| --- | --- |
| Browser SDK | Microphone capture, AEC, audio playback, turn events, reconnect/resume |
| Local server | Serves the page and relays authenticated status, hook, and Converse traffic |
| Tool router | Converts explicit voice tool calls into Claude prompts, commands, keys, and menus |
| PTY host | Preserves the real Claude TUI while providing safe input injection and screen structure |

The API key stays in the Python process. The page uses a placeholder key; the relay replaces it
with the real key and attaches the tool manifest when forwarding the SDK start frame.

## Channels and state

The localhost server exposes:

- `/proxy`: the Browser SDK's Converse protocol, including JSON control frames and binary audio.
- `/ws`: Converse Code status only: Claude state, queue, and injected instruction labels.
- `/hook/{event}`: native Claude Code HTTP lifecycle hooks.
- `/vendor/converse/*.js`: the Apache-licensed browser SDK modules bundled with the wheel.

The tool router reports `idle`, `working`, or `menu`. A long task stays open until Claude emits
`Stop`, fails through `StopFailure`, needs a visible menu answer, is cancelled, or reaches the
bounded hold deadline. Completion after that deadline is pushed into the voice session rather
than requiring a polling call.

Prompt injection is acknowledged, not assumed. Text and Enter are separate PTY writes, concurrent
injections are serialized, and `UserPromptSubmit` confirms that Claude accepted the exact prompt.
A swallowed Enter is retried twice before the voice call resolves with a recovery instruction.

Claude menus are read from a rendered terminal emulator, never from raw ANSI output. The detector
uses structure only and excludes the idle composer and historical prompt cursors. Menu choices
remain user decisions; `PermissionRequest` can wake voice but never approves a tool automatically.

Claude response prose comes from its JSONL transcript or documented hook payloads, not screen
scraping. Voice transcript corrections are keyed by Converse turn and barge sequence so revised
events update an existing row instead of duplicating it.

## Security boundaries

- The HTTP server binds to `127.0.0.1` by default.
- Every page, WebSocket, proxy, and hook session is scoped by a random per-run token. WebSocket
  upgrades additionally reject foreign browser origins.
- The Converse API key is read from `CONVERSE_API_KEY` or a mode-`0600` config file and is never
  sent to browser JavaScript.
- Remote prompt text is flattened, length-limited, and stripped of terminal control bytes before
  it reaches the PTY.
- Only an explicitly matching managed tool cancellation may press Escape against an active turn.
- Claude starts in `--permission-mode auto`, not a bypass mode. Remaining trust and permission
  prompts stay visible and require the user's choice.
- The browser SDK files are public source and contain no runtime secrets, so their static module
  route is not token-gated. Control and audio routes are token-gated.

The localhost token protects against unrelated pages and local web content; it is not a machine
account boundary. A process running as the same OS user can generally inspect that user's terminal,
files, and process state.

## Open-source boundary

Converse Code and the Trelis-authored Browser SDK source bundled with it are licensed under Apache
2.0. The SDK's binary-derived AEC source retains the third-party licenses, disclaimers, and patent
notices under `converse_code/web/vendor/converse/THIRD_PARTY_LICENSES`.

Those licenses do not cover the hosted Converse service, its models, Trelis trademarks, or
server-side software. The client communicates with that service through the documented public
WebSocket protocol.

## Maintenance

Run the deterministic suite with `uv run pytest -q`. Run
`uv run scripts/smoke_real_claude.py` for an isolated end-to-end check against the installed Claude
CLI. Browser SDK updates must come from a source release that declares Apache 2.0 and includes its
NOTICE and complete third-party terms; regenerate the copy with `scripts/vendor_converse_sdk.py`
and commit its updated `UPSTREAM.json`.
