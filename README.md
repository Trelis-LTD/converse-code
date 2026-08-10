# Converse Code

Talk to Claude Code by voice. `converse-code` wraps the real `claude` CLI in your own
terminal and connects it to [Converse](https://converse.trelis.com) over the public
WebSocket tool protocol — you speak into a browser tab, instructions appear in the
terminal as typed text, and the voice tells you what happened.

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
`http://127.0.0.1:<port>/?t=<token>` tab with a mic button and a live transcript — open the
URL it prints, since the token is what keeps other pages out. Close the terminal, everything
stops.

Claude starts in auto permission mode by default. Override the wrapped command when needed:

```bash
converse-code --claude "claude --permission-mode default"
```

The browser uses Converse's Classic voice. The mic button starts or cleanly ends the voice
connection; the separate Mute button gates microphone audio without disconnecting. Claude Code
remains fully interactive in the terminal. Saying that you want to end the session closes the
voice connection after the final spoken reply and leaves Claude running.

See [docs/DESIGN.md](docs/DESIGN.md) for the current architecture and security boundaries.

## How it works

```
   Dev's terminal                        Dev's browser tab (localhost)
   `claude` TUI, fully interactive      mic + speaker + live ASR transcript
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
- Voice input reaches the machine only as natural-language instructions to Claude Code —
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
  reports idle/working/canceling/menu state and the last verified action; `set_model` selects and
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
uv run scripts/smoke_real_claude.py  # real Claude CLI, isolated temporary project
```

The real smoke test covers fresh-folder trust, auto mode, prompt acknowledgement and completion,
plus opening and selecting the `/model` menu. It consumes a small Claude request.

## License

Converse Code is licensed under the [Apache License 2.0](LICENSE). The bundled browser SDK is
Apache-licensed Trelis source with its required third-party terms retained under
[`converse_code/web/vendor/converse`](converse_code/web/vendor/converse).

These licenses cover the open Converse Code client and bundled SDK code. They do **not** license
the hosted Converse service, its models, Trelis trademarks, or server-side software. See
[NOTICE](NOTICE) for the complete boundary and attributions.
