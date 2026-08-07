# Converse Code

Talk to Claude Code by voice. `converse-code` wraps the real `claude` CLI in your own
terminal and connects it to [Converse](https://converse.trelis.com) over the public
WebSocket tool protocol — you speak into a browser tab, instructions appear in the
terminal as typed text, and the voice tells you what happened.

## Install

Not published to PyPI yet, so install from the repo:

```bash
git clone https://github.com/Trelis-LTD/converse-code && cd converse-code
uv tool install .                    # puts `converse-code` on your PATH
```

Or run it without installing — from anywhere, pointing at your clone:

```bash
uvx --from /path/to/converse-code converse-code
```

Once it's on PyPI, `uvx converse-code` will work directly (`uvx` fetches a package into a
throwaway environment and runs its CLI, so there's nothing to install or clean up).

To update an installation from this repository:

```bash
git pull
uv tool install --force .
```

## Use

```bash
cd my-project && converse-code
```

First run asks for a Converse API key (converse.trelis.com dashboard) and stores it in
`~/.config/converse-code/`. Every run: Claude Code opens in your terminal as normal, plus a
`http://127.0.0.1:<port>/?t=<token>` tab with a mic button and a live transcript — open the
URL it prints, since the token is what keeps other pages out. Close the terminal, everything
stops.

Claude starts in auto permission mode by default. Override the wrapped command when needed:

```bash
converse-code --claude "claude --permission-mode default"
```

The browser uses Converse's Classic voice. The mic button starts or stops only the voice
connection; Claude Code remains fully interactive in the terminal. Saying that you want to end
the session closes the voice connection after the final spoken reply and leaves Claude running.

See [docs/DESIGN.md](docs/DESIGN.md) for the full design spec.

## How it works

```
   Dev's terminal                        Dev's browser tab (localhost)
   `claude` TUI, fully interactive      mic + speaker + live ASR transcript
        ^                                       ^
        | pty                                   | audio + captions
        +------------------+-------------------+
                           |
                    converse-code   <- this repo
                           |
                           |  one Converse WebSocket: audio + tool protocol
                           v
              Converse broker (unchanged)
         brain, ASR, TTS, turn-taking, barge-in
```

- Nothing about voice reaches Claude Code directly; nothing about Claude Code's raw
  output reaches the user directly. `converse-code` translates in both directions.
- Prompt text and Enter are sent as separate PTY writes. A native `UserPromptSubmit` HTTP hook
  confirms that Claude accepted the prompt; swallowed submissions get two bounded Enter retries
  instead of an open tool call that silently waits for minutes.
- Claude Code's `Stop` hook resolves voice-started work and proactively wakes Converse when work
  typed directly in the terminal finishes. A `PermissionRequest` hook wakes voice when terminal
  work needs a menu decision, but never approves on the user's behalf.
- Trust dialogs, model pickers, and permission menus are detected from the rendered terminal
  structure. Prompt-history cursors are excluded so a closed menu cannot be mistaken for an open
  one. Converse's managed pending-job cancellation interrupts only the matching Claude turn.
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

MIT — see [LICENSE](LICENSE).
