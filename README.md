# Converse Code

A bridge that exposes a running Claude Code CLI session as a callable tool over the
[Converse](https://converse.trelis.com) WebSocket tool protocol — so you can drive
Claude Code by voice, while watching (and typing into) the same tmux session yourself.

See [docs/DESIGN.md](docs/DESIGN.md) for the full design spec.

## How it works

```
User (voice/text)
     |
     v
Converse broker (unchanged) -- brain, ASR, TTS, turn-taking, barge-in
     |  WebSocket tool protocol (start/tool_call/tool_result/tool_progress/tool_cancel)
     v
Converse Code bridge   <- this repo
     |  tmux send-keys (input) / Stop-hook + JSONL transcript tail (output)
     v
tmux session running the real `claude` CLI
```

- Nothing about voice reaches Claude Code directly; nothing about Claude Code's raw
  output reaches the user directly. The bridge translates in both directions.
- Built entirely against the public Converse tool contract
  (`converse.trelis.com/docs/api/websocket/#tools`).
