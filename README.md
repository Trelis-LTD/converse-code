# Converse Code

Talk to Claude Code by voice. `converse-code` wraps the real `claude` CLI in your own
terminal and connects it to [Converse](https://converse.trelis.com) over the public
WebSocket tool protocol — you speak into a browser tab, instructions appear in the
terminal as typed text, and the voice tells you what happened.

```
uvx converse-code          # in your project directory
```

First run asks for a Converse API key (converse.trelis.com dashboard) and stores it.
Every run: Claude Code opens in your terminal as normal, plus a `http://localhost:<port>`
tab with a mic button and a live transcript. Close the terminal, everything stops.

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
- Built entirely against the public Converse tool contract
  (`converse.trelis.com/docs/api/websocket/#tools`).
