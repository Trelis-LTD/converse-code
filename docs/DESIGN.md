# Converse Code architecture

This document describes the current client. It is not a roadmap or design diary.

## Runtime shape

`converse-code` starts the real interactive `claude` CLI inside a pseudo-terminal and serves a
single localhost browser page. The terminal remains directly usable. The browser owns microphone
capture and assistant playback through the bundled `@trelis/converse` Browser SDK.

```text
browser SDK  <===============================>  hosted Converse
     |             audio + tool protocol
     |
     +-- acknowledged localhost controls --> converse-code
                                                   |
                                             PTY + Claude hooks
                                                   |
                                             interactive claude CLI
```

There are four client-side boundaries:

| Component | Responsibility |
| --- | --- |
| Browser SDK | Microphone capture, AEC, audio playback, turn events, reconnect/resume |
| Local server | Serves the page, mints scoped credentials, and carries tool/status controls |
| Tool router | Converts explicit voice tool calls into Claude prompts, commands, keys, and menus |
| PTY host | Preserves the real Claude TUI while providing safe input injection and screen structure |

The persistent API key stays in the Python process. For each browser session, the local server
exchanges it for a short-lived credential bound to that session ID. The browser SDK then connects
straight to Converse with the tool manifest. Supported SDK resume state is kept in
`sessionStorage`, so a reload can resume without exposing the persistent key.

## Channels and state

The localhost server exposes:

- `/session-credential`: exchanges a requested session ID for a browser-safe scoped credential.
- `/ws`: acknowledged tool controls plus Claude state and injected instruction labels.
- `/hook/{event}`: native Claude Code HTTP lifecycle hooks.
- `/vendor/converse/*.js`: the Apache-licensed browser SDK modules bundled with the wheel.

The tool router reports `idle`, `working`, or `menu`. A `long_task` call is acknowledged as
deferred once its prompt is confirmed accepted, so no voice turn is held open while Claude works.
The unit of work is a **working episode**: one run of Claude Code from accepting input until it
comes to rest (`Stop`, `StopFailure`, or cancellation). `long_task` starts an episode only while
idle; `steer_task` explicitly adds guidance to the episode already in progress. There is no
implicit queue and a second `long_task` is rejected with recovery guidance. A deferred job is the
broker's handle on the current episode, never on an individual prompt. While an episode runs,
milestones go out as
partial results under the cadence rule *milestones speak, telemetry stays silent*: a menu
(blocked on a decision) and a test run announce themselves; file edits stay silent but keep the
voice current; other activity is plain progress. The episode's closing message resolves the job
and is announced by the broker. Episodes typed directly at the terminal inject their outcome
silently — the user already read it there.

Prompt injection is acknowledged, not assumed. Text and Enter are separate PTY writes, concurrent
injections are serialized, and `UserPromptSubmit` confirms that Claude accepted the exact prompt.
A swallowed Enter is retried twice before the voice call resolves with a recovery instruction.

Claude menus are read from a rendered terminal emulator, never from raw ANSI output. The detector
uses structure only and excludes the idle composer and historical prompt cursors. Menu choices
remain user decisions; `PermissionRequest` can wake voice but never approves a tool automatically.
The one narrow exception is Claude's second-phase model-change confirmation: an explicit model
choice authorizes the matching “Yes, switch to that model” prompt, and no other confirmation is
automatically accepted.

Claude response prose comes from its JSONL transcript or documented hook payloads, not screen
scraping. Voice transcript corrections are keyed by Converse turn and barge sequence so revised
events update an existing row instead of duplicating it.

## Security boundaries

- The HTTP server binds to `127.0.0.1` by default.
- Every page, local WebSocket, credential request, and hook is scoped by a random per-run token. WebSocket
  upgrades additionally reject foreign browser origins.
- The Converse API key is read from `CONVERSE_API_KEY` or a mode-`0600` config file and is never
  sent to browser JavaScript.
- Remote prompt text is flattened, length-limited, and stripped of terminal control bytes before
  it reaches the PTY.
- Only an explicitly matching managed tool cancellation may press Escape against an active turn.
- Claude starts in `--permission-mode auto`, not a bypass mode. Remaining trust and permission
  prompts stay visible and require the user's choice.
- The browser SDK files are public source and contain no runtime secrets, so their static module
  route is not token-gated. Local control and credential routes are token-gated; audio travels
  directly between the Browser SDK and Converse under the scoped credential.

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
