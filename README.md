# Converse Code

Talk or type to Claude Code. `converse-code` wraps the real `claude` CLI in your own
terminal and connects it to [Converse](https://converse.trelis.com) over the public
WebSocket tool protocol — speak or type in a browser tab, watch instructions arrive in
Claude's terminal, and hear or read what happened.

> **Alpha — invite only.** Trelis Converse is in alpha for individual developers and
> enterprise. Reach out to [voice@trelis.com](mailto:voice@trelis.com) to get on the waitlist.

This repository is also the **reference implementation** for building a client against the
[Converse WebSocket tool contract](https://converse.trelis.com/docs/api/websocket/#tools).
It exercises the full surface — audio streaming, tool calls and resolutions, proactive
wake-ups, and pending-job cancellation — against a real interactive CLI, so it doubles as
a working example for wiring Converse to any terminal program or agent of your own.

## Install

```bash
uvx converse-code
```

`uvx` fetches the package into a throwaway environment and runs its CLI, so there's nothing
to install or clean up. To put `converse-code` on your PATH permanently:

```bash
uv tool install converse-code
```

To run from a checkout instead (for development or to try unreleased changes):

```bash
git clone https://github.com/Trelis-LTD/converse-code && cd converse-code
uv tool install .                    # or: uvx --from /path/to/converse-code converse-code
```

## Use

```bash
cd my-project && converse-code
```

First run asks for a Converse API key from the Converse dashboard, validates it, and stores it in
`~/.config/converse-code/config.json` with mode `0600`. Run `converse-code login` to replace the
saved key, or set `CONVERSE_API_KEY` to supply one without writing it to disk. Every run: Claude
Code opens in your terminal as normal, plus a
`http://127.0.0.1:<port>/?t=<token>` tab with voice controls, typed input, and a live transcript — open the
URL it prints, since the token is what keeps other pages out. Close the terminal, everything
stops.

Claude starts in auto permission mode by default. Override the wrapped command when needed:

```bash
converse-code --claude "claude --permission-mode default"
```

The browser uses Converse's Classic voice. The mic button starts or stops voice input while
preserving the multimodal Converse session; the separate Mute button temporarily gates microphone
audio. Claude Code remains fully interactive in the terminal. Asking to end the Converse session
closes it after the final spoken reply and leaves Claude running.

Typed turns use Converse's canonical user-turn path: the SDK sends `inject_context` with
`role: "user"` and `reply: true`, and the broker echoes the turn as `asr` with the same stable
turn ID used by the response. Starting with text suppresses the voice greeting and does not turn
on the microphone.

## Headless control

`--headless` replaces the terminal and Converse connection with a JSONL control channel on
stdin/stdout. It does not require a Converse API key. Each request and event is one JSON object:

```bash
converse-code --headless
```

```json
{"type":"tool_call","id":"task-1","name":"long_task","args":{"request":"Fix the failing tests"}}
{"type":"screen_snapshot","id":"state-1"}
{"type":"tool_cancel","id":"task-1"}
{"type":"shutdown","id":"done"}
```

The first event is `ready` with protocol `converse-code-headless-v1`. Tool calls use the same
names as voice mode and emit `tool_deferred`, `tool_progress`, `tool_partial_result`, and
`tool_result` events. A screen snapshot returns Claude's rendered terminal rows, semantic state,
last full response, and transcript path. stdout is reserved for protocol events; diagnostics go
to stderr.

See [docs/DESIGN.md](docs/DESIGN.md) for the current architecture and security boundaries.

## How it works

```
   Dev's terminal                        Dev's browser tab (localhost)
   `claude` TUI, fully interactive      mic + typed turns + transcript
        ^                                       |
        | pty                           audio + tool protocol
        |                                       v
   converse-code <--- acknowledged ---> Browser SDK
        |              local controls           |
        +-- scoped credential + Claude hooks    v
                                      Converse broker
         brain, ASR, TTS, turn-taking, barge-in
```

- Nothing about voice reaches Claude Code directly; nothing about Claude Code's raw
  output reaches the user directly. `converse-code` translates in both directions.
- Voice and typed input reach the machine only as natural-language instructions to Claude Code —
  never as raw shell commands. The TUI's `!` bash-mode prefix is refused on the voice path,
  so Claude Code's permission system remains the single safety chokepoint.
- Prompt text and Enter are sent as separate PTY writes. A native `UserPromptSubmit` HTTP hook
  confirms that Claude accepted the prompt; swallowed submissions get two bounded Enter retries
  instead of an open tool call that silently waits for minutes.
- Claude Code's `Stop` hook resolves voice-started work and proactively wakes Converse when work
  typed directly in the terminal finishes. `StopFailure` resolves errors immediately rather than
  waiting for the long tool deadline. A `PermissionRequest` hook wakes voice when terminal work
  needs a menu decision, but never approves on the user's behalf.
- Trust dialogs, model pickers, and permission menus are detected from the rendered terminal
  structure. Prompt-history cursors are excluded so a closed menu cannot be mistaken for an open
  one. Model changes also consume Claude Code's specific follow-up model confirmation before they
  report success. Converse's managed pending-job cancellation interrupts only the matching turn.
- New work and steering are explicit: `long_task` starts an idle Claude turn, while `steer_task`
  adds requirements to work already in progress. A second task is never silently reinterpreted as
  guidance or claimed to be queued.
- Voice responses receive an authoritative semantic snapshot of Claude's UI. `observe_claude`
  reports idle/working/canceling/awaiting_input phase and structured UI and the last verified action; `set_model` selects and
  confirms the model, verifies Claude's explicit result, and falls back to reopening the picker
  when that result is unavailable.
- Claude hook `prompt_id` values bind each voice episode to its actual completion. Cancellation
  remains `canceling` until the matching Stop arrives or the terminal is stably idle. Late
  completion from an older prompt cannot be attributed to newer work.
- The browser SDK connects directly to Converse with a short-lived, session-bound credential.
  Its supported resume-state API preserves a live conversation across page reloads; only tool
  calls and Claude status cross the authenticated localhost link.
- Built entirely against the public Converse tool contract
  (`converse.trelis.com/docs/api/websocket/#tools`).

## Development

```bash
uv sync
uv run pytest -q
uv run playwright install chromium
uv run scripts/browser_e2e.py       # real Chromium + deterministic microphone
uv run scripts/audio_loopback_probe.py  # Xvfb + private PulseAudio output capture
uv run scripts/smoke_real_claude.py  # real Claude CLI, isolated temporary project
```

The real smoke test covers fresh-folder trust, auto mode, prompt acknowledgement and completion,
plus opening and selecting the `/model` menu. It consumes a small Claude request.

The browser suite drives the shipped page in Chromium against a deterministic fake SDK, including
real microphone permissions and WAV input; it does not replace live SDK/broker evaluation. Failed
scenarios retain screenshots and traces. See
[docs/BROWSER_TESTING.md](docs/BROWSER_TESTING.md) for setup and the opt-in Linux virtual-output
loopback used for deeper audio diagnostics.

## License

Converse Code is licensed under the [Apache License 2.0](LICENSE). The bundled browser SDK is
Apache-licensed Trelis source with its required third-party terms retained under
[`converse_code/web/vendor/converse`](converse_code/web/vendor/converse).

These licenses cover the open Converse Code client and bundled SDK code. They do **not** license
the hosted Converse service, its models, Trelis trademarks, or server-side software. See
[NOTICE](NOTICE) for the complete boundary and attributions.
