# Converse Code

A deliberately small reference implementation for [Converse](https://converse.trelis.com)
background tools and the Converse Browser SDK. It runs one Pi coding-agent session with the
user's ChatGPT Plus/Pro Codex subscription.

The example demonstrates the complete deferred-tool lifecycle:

1. `coding_task` accepts work and immediately backgrounds it.
2. Pi tool events become progress and partial results.
3. Routine edits use silent partials; meaningful milestones and questions use `reply: true`.
4. `continue_task` steers the running task or answers a structured question.
5. Pi's `agent_settled` event resolves the deferred tool exactly once.
6. Converse cancellation maps directly to Pi's `abort` command.

There is no terminal emulation, screen scraping, model picker, shell bypass, or compatibility
layer for a proprietary terminal harness. A bundled Pi extension asks for structured approval of
`bash`, `edit`, and `write`, offering allow-once, allow-for-session, and block choices. Those
questions travel through the same `reply: true` path.

## Install

Install Pi, sign into the Codex provider, and install Converse Code:

```bash
npm install -g @earendil-works/pi-coding-agent
pi
# In Pi: /login → ChatGPT Plus/Pro (Codex)

uvx converse-code
```

Converse Code starts Pi as:

```bash
pi --mode rpc --provider openai-codex
```

Override that command when testing another Pi configuration:

```bash
converse-code --pi "pi --mode rpc --provider openai-codex --model gpt-5.6-codex"
```

Run `converse-code login` to store a Converse API key. The persistent key remains in Python; the
browser receives only a short-lived session credential.

## Reference architecture

```text
Converse voice/text model
        │ tool call / cancellation
        ▼
Browser SDK reference page
        │ acknowledged localhost controls
        ▼
AgentToolRouter ── JSONL RPC ── Pi ── Codex
        │
        └─ deferred / progress / partial(reply) / terminal result
```

The model-facing surface is intentionally limited to `coding_task`, `continue_task`, and
`end_session`. See [docs/DESIGN.md](docs/DESIGN.md) for the event mapping and evidence rules.

## Test

```bash
uv sync
uv run pytest -q
uv run playwright install chromium
uv run scripts/browser_e2e.py
```

The deterministic suite uses a fake Pi JSONL process. The browser suite drives the shipped page
in real Chromium and checks backgrounding, silent and spoken partials, completion, cancellation,
and reconnect replay.

## License

Apache-2.0. The vendored Converse Browser SDK retains its own notices and third-party licenses.
